"""Tests for the v1.1 supervisory state machine and monitoring API."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

import pytest

from kepler_node.agent.absolute_state import (
    EkosRuntimeState,
    InterventionWindowState,
    NormalizedEkosSnapshot,
)
from kepler_node.agent.authorship import AuthorshipTracker
from kepler_node.agent.broker import BrokerRuntimeState, BrokerSnapshot, StubBrokerBackend
from kepler_node.agent.claw import ClawController
from kepler_node.agent.ekos import StubEkosAdapter
from kepler_node.agent.interfaces import (
    DeviceActivityEvent,
    NetworkMode,
    PowerStatus,
    ServiceHealth,
    StorageStatus,
    TimeSource,
    TimeStatus,
)
from kepler_node.agent.session import (
    ClawState,
    InterventionKind,
    InterventionLedger,
    RuntimeSession,
    WorkflowIntent,
)
from kepler_node.camera.protocols import CameraSettings, CaptureRequest, CaptureResult
from kepler_node.imaging.protocols import SolveResult
from kepler_node.mount.protocols import MountPosition
from kepler_node.storage.filesystem import FilesystemSessionStore
from kepler_node.storage.models import (
    EquipmentProfile,
    EquipmentProfileBackendPreferences,
    EquipmentProfileHardware,
    EquipmentProfileHardwareCamera,
    EquipmentProfileHardwareGps,
    EquipmentProfileHardwareLens,
    EquipmentProfileHardwareMount,
    EquipmentProfileSiteDefaults,
    EquipmentProfileSolvingHints,
)

# ------------------------------------------------------------------ #
# Fake adapters                                                        #
# ------------------------------------------------------------------ #


class _FakeNodeBackend:
    def __init__(
        self,
        *,
        time_trusted: bool = True,
        storage_writable: bool = True,
        power_healthy: bool = True,
    ) -> None:
        self._time_trusted = time_trusted
        self._storage_writable = storage_writable
        self._power_healthy = power_healthy

    def network_mode(self) -> NetworkMode:
        return NetworkMode.FIELD_HOTSPOT

    def service_health(self) -> list[ServiceHealth]:
        return []

    def time_status(self) -> TimeStatus:
        return TimeStatus(
            trusted=self._time_trusted,
            source=TimeSource.NETWORK,
            summary="ok" if self._time_trusted else "untrusted",
        )

    def storage_status(self) -> StorageStatus:
        return StorageStatus(
            data_root=Path("/tmp"),
            free_bytes=10_000_000_000,
            total_bytes=100_000_000_000,
            writable=self._storage_writable,
            summary="ok",
        )

    def power_status(self) -> PowerStatus:
        return PowerStatus(
            healthy=self._power_healthy,
            summary="ok" if self._power_healthy else "undervoltage",
            undervoltage_detected=not self._power_healthy,
        )

    def confirm_time(self, timestamp: datetime) -> TimeStatus:
        return self.time_status()

    def install_manifest(self):  # noqa: ANN201
        return None


class _FakeMountBackend:
    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def current_position(self) -> MountPosition:
        return MountPosition(ra_hours=0.0, dec_deg=0.0)

    def slew_to(self, position: MountPosition) -> None:
        pass

    def sync_to(self, position: MountPosition) -> None:
        pass

    def poll_activity(self) -> None:
        pass

    def activity_events(self) -> Iterable[DeviceActivityEvent]:
        return iter([])


class _FakeCameraBackend:
    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def heartbeat(self) -> bool:
        return True

    def capture(self, request: CaptureRequest) -> CaptureResult:
        path = request.destination_dir / "test_frame.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return CaptureResult(image_path=path, captured_at=datetime.now(UTC))

    def apply_settings(self, settings: CameraSettings) -> CameraSettings:
        return settings

    def activity_events(self) -> Iterable[DeviceActivityEvent]:
        return iter([])


class _FakeSolverBackend:
    def solve(self, image_path: Path, **kwargs: object) -> SolveResult:
        return SolveResult(success=True, solved_at=datetime.now(UTC))


class _EkosWithSequence(StubEkosAdapter):
    """Ekos adapter that reports an active sequence in a given state."""

    def __init__(
        self,
        *,
        ekos_state: EkosRuntimeState = EkosRuntimeState.RUNNING,
        sequence_exists: bool = True,
        pause_ok: bool = True,
        resume_ok: bool = True,
    ) -> None:
        self._ekos_state = ekos_state
        self._sequence_exists = sequence_exists
        self._pause_ok = pause_ok
        self._resume_ok = resume_ok
        self._pause_call_count = 0

    def status(self) -> NormalizedEkosSnapshot:
        return NormalizedEkosSnapshot(
            ekos_state=self._ekos_state,
            sequence_exists=self._sequence_exists,
            snapshot_at=datetime.now(UTC),
        )

    def pause(self) -> bool:
        self._pause_call_count += 1
        if self._pause_ok:
            self._ekos_state = EkosRuntimeState.PAUSED
        return self._pause_ok

    def resume(self) -> bool:
        if self._resume_ok:
            self._ekos_state = EkosRuntimeState.RUNNING
        return self._resume_ok


class _BrokerUnknown(StubBrokerBackend):
    def snapshot(self) -> BrokerSnapshot:
        return BrokerSnapshot(broker_state=BrokerRuntimeState.UNKNOWN)


class _BrokerUnavailable(StubBrokerBackend):
    def snapshot(self) -> BrokerSnapshot:
        return BrokerSnapshot(broker_state=BrokerRuntimeState.UNAVAILABLE)


class _BrokerDegradedNoDevice(StubBrokerBackend):
    def snapshot(self) -> BrokerSnapshot:
        return BrokerSnapshot(
            broker_state=BrokerRuntimeState.DEGRADED,
            device_path_available=False,
        )


class _EkosUnavailable(StubEkosAdapter):
    def status(self) -> NormalizedEkosSnapshot:
        return NormalizedEkosSnapshot(
            ekos_state=EkosRuntimeState.UNAVAILABLE,
            snapshot_at=datetime.now(UTC),
        )


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #


def _make_profile() -> EquipmentProfile:
    return EquipmentProfile(
        profile_id="test-profile",
        display_name="Test Profile",
        is_default=True,
        hardware=EquipmentProfileHardware(
            mount=EquipmentProfileHardwareMount(model="EQ6-R"),
            camera=EquipmentProfileHardwareCamera(make="ZWO", model="ASI294MC"),
            lens=EquipmentProfileHardwareLens(
                model="Rokinon 135mm f/2",
                is_zoom=False,
                default_focal_length_mm=135,
            ),
            gps=EquipmentProfileHardwareGps(),
        ),
        site_defaults=EquipmentProfileSiteDefaults(),
        solving_hints=EquipmentProfileSolvingHints(),
        backend_preferences=EquipmentProfileBackendPreferences(),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _make_ready_controller(
    tmp_path: Path,
    *,
    ekos_adapter: object | None = None,
    broker: object | None = None,
    node: _FakeNodeBackend | None = None,
) -> ClawController:
    base = tmp_path / "kepler"
    base.mkdir(parents=True, exist_ok=True)
    ctrl = ClawController(
        session=RuntimeSession(state=ClawState.READY),
        node_backend=node or _FakeNodeBackend(),
        mount_backend=_FakeMountBackend(),
        camera_backend=_FakeCameraBackend(),
        solver_backend=_FakeSolverBackend(),
        store=FilesystemSessionStore(data_root=base),
        authorship_tracker=AuthorshipTracker(),
        verification_dir=base / "verify",
        test_exposure_seconds=1.0,
        ekos_adapter=ekos_adapter or _EkosWithSequence(),
        broker_backend=broker or StubBrokerBackend(),
    )
    ctrl.active_equipment_profile = _make_profile()
    return ctrl


# ------------------------------------------------------------------ #
# attach_session() tests                                               #
# ------------------------------------------------------------------ #


def test_attach_session_transitions_to_ekos_wait(tmp_path: Path) -> None:
    ctrl = _make_ready_controller(tmp_path)
    result = ctrl.attach_session()

    assert result.previous_state == ClawState.READY
    assert result.next_state == ClawState.EKOS_WAIT
    assert ctrl.session.state == ClawState.EKOS_WAIT
    assert ctrl.session.session_id is not None
    assert ctrl.session.control_locked is True
    assert ctrl.session.intervention_ledger is not None
    assert ctrl.session.workflow_intent == WorkflowIntent.SUPERVISION


def test_attach_session_requires_ready_state(tmp_path: Path) -> None:
    ctrl = _make_ready_controller(tmp_path)
    ctrl.session.state = ClawState.BOOT
    with pytest.raises(ValueError, match="only valid from ready"):
        ctrl.attach_session()


def test_attach_session_blocked_when_time_untrusted(tmp_path: Path) -> None:
    ctrl = _make_ready_controller(tmp_path, node=_FakeNodeBackend(time_trusted=False))
    with pytest.raises(RuntimeError, match="blocked"):
        ctrl.attach_session()


def test_attach_session_blocked_when_ekos_unavailable(tmp_path: Path) -> None:
    ctrl = _make_ready_controller(tmp_path, ekos_adapter=_EkosUnavailable())
    with pytest.raises(RuntimeError, match="unavailable"):
        ctrl.attach_session()


def test_attach_session_blocked_when_broker_unknown(tmp_path: Path) -> None:
    ctrl = _make_ready_controller(tmp_path, broker=_BrokerUnknown())
    with pytest.raises(RuntimeError, match="unknown"):
        ctrl.attach_session()


def test_attach_session_blocked_when_broker_unavailable(tmp_path: Path) -> None:
    ctrl = _make_ready_controller(tmp_path, broker=_BrokerUnavailable())
    with pytest.raises(RuntimeError, match="unavailable"):
        ctrl.attach_session()


def test_attach_session_blocked_when_broker_degraded_no_device(tmp_path: Path) -> None:
    ctrl = _make_ready_controller(tmp_path, broker=_BrokerDegradedNoDevice())
    with pytest.raises(RuntimeError, match="degraded"):
        ctrl.attach_session()


def test_attach_session_requires_equipment_profile(tmp_path: Path) -> None:
    ctrl = _make_ready_controller(tmp_path)
    ctrl.active_equipment_profile = None
    with pytest.raises(RuntimeError, match="equipment profile"):
        ctrl.attach_session()


# ------------------------------------------------------------------ #
# advance_ekos_wait() tests                                            #
# ------------------------------------------------------------------ #


def test_advance_ekos_wait_transitions_to_monitoring_when_ready(tmp_path: Path) -> None:
    ctrl = _make_ready_controller(
        tmp_path, ekos_adapter=_EkosWithSequence(ekos_state=EkosRuntimeState.RUNNING)
    )
    ctrl.attach_session()
    assert ctrl.session.state == ClawState.EKOS_WAIT

    result = ctrl.advance_ekos_wait()
    assert result.next_state == ClawState.MONITORING
    assert ctrl.session.state == ClawState.MONITORING
    assert ctrl.session.supervisory_next_action == "monitor_ekos_session"


def test_advance_ekos_wait_stays_when_no_sequence(tmp_path: Path) -> None:
    ctrl = _make_ready_controller(
        tmp_path,
        ekos_adapter=_EkosWithSequence(
            ekos_state=EkosRuntimeState.IDLE, sequence_exists=False
        ),
    )
    ctrl.attach_session()

    result = ctrl.advance_ekos_wait()
    assert result.next_state == ClawState.EKOS_WAIT
    assert ctrl.session.state == ClawState.EKOS_WAIT
    blocker_names = [b.name for b in result.blockers]
    assert "ekos_no_sequence" in blocker_names


def test_advance_ekos_wait_stays_when_ekos_unavailable(tmp_path: Path) -> None:
    ekos = _EkosWithSequence()
    ctrl = _make_ready_controller(tmp_path, ekos_adapter=ekos)
    ctrl.attach_session()

    ekos._ekos_state = EkosRuntimeState.UNAVAILABLE
    result = ctrl.advance_ekos_wait()
    assert result.next_state == ClawState.EKOS_WAIT
    blocker_names = [b.name for b in result.blockers]
    assert "ekos_unavailable" in blocker_names


def test_advance_ekos_wait_noop_when_not_in_ekos_wait(tmp_path: Path) -> None:
    ctrl = _make_ready_controller(tmp_path)
    ctrl.session.state = ClawState.READY
    result = ctrl.advance_ekos_wait()
    assert result.next_state == ClawState.READY
    assert "skipped" in result.message


def test_advance_ekos_wait_stays_when_ekos_idle(tmp_path: Path) -> None:
    """IDLE Ekos must not advance to MONITORING.

    Spec: `monitoring` means "Ekos is executing normally".  An idle Ekos
    session is not executing normally, so advancement must be blocked.
    """
    ctrl = _make_ready_controller(
        tmp_path,
        ekos_adapter=_EkosWithSequence(ekos_state=EkosRuntimeState.IDLE, sequence_exists=True),
    )
    ctrl.attach_session()

    result = ctrl.advance_ekos_wait()
    assert result.next_state == ClawState.EKOS_WAIT
    assert ctrl.session.state == ClawState.EKOS_WAIT
    blocker_names = [b.name for b in result.blockers]
    assert "ekos_not_running" in blocker_names


def test_advance_ekos_wait_stays_when_ekos_paused(tmp_path: Path) -> None:
    """A paused Ekos session must not advance to MONITORING.

    Spec: `monitoring` requires Ekos executing normally; a paused Ekos is
    suspended, not executing.
    """
    ctrl = _make_ready_controller(
        tmp_path,
        ekos_adapter=_EkosWithSequence(
            ekos_state=EkosRuntimeState.PAUSED, sequence_exists=True
        ),
    )
    ctrl.attach_session()

    result = ctrl.advance_ekos_wait()
    assert result.next_state == ClawState.EKOS_WAIT
    assert ctrl.session.state == ClawState.EKOS_WAIT
    blocker_names = [b.name for b in result.blockers]
    assert "ekos_not_running" in blocker_names


# ------------------------------------------------------------------ #
# begin_intervention() tests                                           #
# ------------------------------------------------------------------ #


def test_begin_intervention_requires_monitoring_state(tmp_path: Path) -> None:
    ctrl = _make_ready_controller(tmp_path)
    ctrl.session.state = ClawState.EKOS_WAIT
    with pytest.raises(ValueError, match="only valid from monitoring"):
        ctrl.begin_intervention(kind=InterventionKind.AUTOFOCUS, reason="focus drift")


def test_begin_intervention_transitions_to_intervening_when_paused(tmp_path: Path) -> None:
    ekos = _EkosWithSequence(ekos_state=EkosRuntimeState.RUNNING)
    ctrl = _make_ready_controller(tmp_path, ekos_adapter=ekos)
    ctrl.attach_session()
    ctrl.advance_ekos_wait()
    assert ctrl.session.state == ClawState.MONITORING

    # begin_intervention pauses Ekos; the fake adapter flips to PAUSED
    result = ctrl.begin_intervention(kind=InterventionKind.AUTOFOCUS, reason="focus drift")

    assert result.next_state == ClawState.INTERVENING
    assert ctrl.session.state == ClawState.INTERVENING
    assert ctrl._intervention_window == InterventionWindowState.OPEN
    active = ctrl.session.intervention_ledger.active_record
    assert active is not None
    assert active.kind == InterventionKind.AUTOFOCUS
    assert active.reason == "focus drift"


def test_begin_intervention_moves_to_paused_when_pause_unconfirmed(tmp_path: Path) -> None:
    """When Ekos pause cannot be confirmed, Kepler must move toward PAUSED (ownership unknown).

    Spec §Control Handoff Protocol: "If pause confirmation is missing, stale,
    contradictory, or timed out, Kepler must treat ownership as unknown and move
    toward `paused` rather than continue."
    """

    class _RunningEkos(_EkosWithSequence):
        def pause(self) -> bool:
            # Claim success but don't flip state — confirm_ekos_paused() will fail
            return True

        def status(self) -> NormalizedEkosSnapshot:
            return NormalizedEkosSnapshot(
                ekos_state=EkosRuntimeState.RUNNING,
                sequence_exists=True,
                snapshot_at=datetime.now(UTC),
            )

    ctrl = _make_ready_controller(tmp_path, ekos_adapter=_RunningEkos())
    ctrl.attach_session()
    ctrl.session.state = ClawState.MONITORING
    ctrl.session.intervention_ledger = InterventionLedger()

    result = ctrl.begin_intervention(kind=InterventionKind.REVERIFY, reason="drift")
    # Must move toward PAUSED — not stay in MONITORING — when ownership is unclear.
    assert result.next_state == ClawState.PAUSED
    assert ctrl.session.state == ClawState.PAUSED
    assert ctrl.session.resume_context is not None
    assert ctrl.session.resume_context.resume_state == ClawState.MONITORING
    blocker_names = [b.name for b in result.blockers]
    assert "ekos_pause_not_confirmed" in blocker_names


def test_begin_intervention_pauses_session_when_retries_exhausted(tmp_path: Path) -> None:
    """Retry exhaustion must pause Ekos before pausing the Kepler session.

    Spec §Control Handoff Protocol: Kepler must not claim `paused` while Ekos
    continues running underneath.  The intervention window must be REQUESTED
    to reflect that an Ekos pause has been asked for.
    """
    ekos = _EkosWithSequence(ekos_state=EkosRuntimeState.RUNNING)
    ctrl = _make_ready_controller(tmp_path, ekos_adapter=ekos)
    ctrl.attach_session()
    ctrl.session.state = ClawState.MONITORING

    ledger = InterventionLedger(max_retries_per_kind=2)
    # Pre-fill two autofocus records (budget exhausted)
    from kepler_node.agent.session import InterventionRecord

    for i in range(2):
        ledger.records.append(
            InterventionRecord(kind=InterventionKind.AUTOFOCUS, reason="past", outcome="done")
        )
    ctrl.session.intervention_ledger = ledger

    result = ctrl.begin_intervention(kind=InterventionKind.AUTOFOCUS, reason="another")
    assert result.next_state == ClawState.PAUSED
    assert ctrl.session.state == ClawState.PAUSED
    assert ctrl.session.resume_context is not None
    assert ctrl.session.resume_context.resume_state == ClawState.MONITORING
    # Ekos must have been asked to pause so it does not keep running underneath.
    assert ekos._pause_call_count >= 1
    # Intervention window must reflect the outstanding pause request.
    assert ctrl._intervention_window == InterventionWindowState.REQUESTED


# ------------------------------------------------------------------ #
# complete_intervention() tests                                        #
# ------------------------------------------------------------------ #


def test_complete_intervention_returns_to_monitoring(tmp_path: Path) -> None:
    ekos = _EkosWithSequence(ekos_state=EkosRuntimeState.RUNNING)
    ctrl = _make_ready_controller(tmp_path, ekos_adapter=ekos)
    ctrl.attach_session()
    ctrl.advance_ekos_wait()
    ctrl.begin_intervention(kind=InterventionKind.AUTOFOCUS, reason="drift")
    assert ctrl.session.state == ClawState.INTERVENING

    result = ctrl.complete_intervention(outcome="success")
    assert result.next_state == ClawState.MONITORING
    assert ctrl.session.state == ClawState.MONITORING
    assert ctrl._intervention_window == InterventionWindowState.CLOSED
    assert ctrl.session.intervention_ledger.active_kind is None
    closed = ctrl.session.intervention_ledger.records[-1]
    assert closed.outcome == "success"


def test_complete_intervention_requires_intervening_state(tmp_path: Path) -> None:
    ctrl = _make_ready_controller(tmp_path)
    ctrl.session.state = ClawState.MONITORING
    with pytest.raises(ValueError, match="only valid from intervening"):
        ctrl.complete_intervention()


def test_complete_intervention_stays_when_resume_fails(tmp_path: Path) -> None:
    ekos = _EkosWithSequence(ekos_state=EkosRuntimeState.RUNNING, resume_ok=False)
    ctrl = _make_ready_controller(tmp_path, ekos_adapter=ekos)
    ctrl.attach_session()
    ctrl.advance_ekos_wait()
    ctrl.begin_intervention(kind=InterventionKind.AUTOFOCUS, reason="drift")

    result = ctrl.complete_intervention()
    assert result.next_state == ClawState.INTERVENING
    assert ctrl.session.state == ClawState.INTERVENING
    blocker_names = [b.name for b in result.blockers]
    assert "ekos_resume_failed" in blocker_names


# ------------------------------------------------------------------ #
# resume posture confirmation tests (spec lines 209-210)               #
# ------------------------------------------------------------------ #


class _EkosResumeDecoupled(StubEkosAdapter):
    """Adapter where resume() returns True (request accepted) but Ekos posture stays PAUSED.

    Models the spec lines 209-210 gap: the transport accepted the request but
    the physical posture has not yet been confirmed by the adapter.
    """

    def __init__(self) -> None:
        self._ekos_state: EkosRuntimeState = EkosRuntimeState.PAUSED

    def status(self) -> NormalizedEkosSnapshot:
        return NormalizedEkosSnapshot(
            ekos_state=self._ekos_state,
            sequence_exists=True,
            snapshot_at=datetime.now(UTC),
        )

    def pause(self) -> bool:
        self._ekos_state = EkosRuntimeState.PAUSED
        return True

    def resume(self) -> bool:
        # Returns True (request accepted) but state intentionally does not change.
        return True


class _EkosResumeToIdle(StubEkosAdapter):
    """Adapter where resume() returns True but Ekos settles to IDLE, not RUNNING."""

    def __init__(self) -> None:
        self._ekos_state: EkosRuntimeState = EkosRuntimeState.PAUSED

    def status(self) -> NormalizedEkosSnapshot:
        return NormalizedEkosSnapshot(
            ekos_state=self._ekos_state,
            sequence_exists=True,
            snapshot_at=datetime.now(UTC),
        )

    def pause(self) -> bool:
        self._ekos_state = EkosRuntimeState.PAUSED
        return True

    def resume(self) -> bool:
        self._ekos_state = EkosRuntimeState.IDLE
        return True


class _EkosResumeToAborted(StubEkosAdapter):
    """Adapter where resume() returns True but Ekos reports ABORTED."""

    def __init__(self) -> None:
        self._ekos_state: EkosRuntimeState = EkosRuntimeState.PAUSED

    def status(self) -> NormalizedEkosSnapshot:
        return NormalizedEkosSnapshot(
            ekos_state=self._ekos_state,
            sequence_exists=True,
            snapshot_at=datetime.now(UTC),
        )

    def pause(self) -> bool:
        self._ekos_state = EkosRuntimeState.PAUSED
        return True

    def resume(self) -> bool:
        self._ekos_state = EkosRuntimeState.ABORTED
        return True


def test_resume_stays_paused_when_ekos_posture_not_confirmed(tmp_path: Path) -> None:
    """resume() must stay PAUSED when Ekos accepted the request but posture is still PAUSED.

    Covers spec lines 209-210: the adapter must confirm the resumed posture.
    """
    ekos = _EkosWithSequence(ekos_state=EkosRuntimeState.RUNNING)
    ctrl = _make_ready_controller(tmp_path, ekos_adapter=ekos)
    ctrl.attach_session()
    ctrl.advance_ekos_wait()
    ctrl.pause()
    assert ctrl.session.state == ClawState.PAUSED

    # Swap to a decoupled adapter: resume() returns True but Ekos stays PAUSED.
    ctrl.ekos = _EkosResumeDecoupled()  # type: ignore[assignment]
    result = ctrl.resume()

    assert result.next_state == ClawState.PAUSED
    assert ctrl.session.state == ClawState.PAUSED
    # Canonical Ekos state is still PAUSED after the unconfirmed resume
    state = ctrl.canonical_state()
    assert state.ekos_state == EkosRuntimeState.PAUSED


def test_complete_intervention_stays_intervening_when_ekos_posture_not_confirmed(
    tmp_path: Path,
) -> None:
    """complete_intervention() must stay INTERVENING when resume accepted but Ekos still PAUSED.

    Covers spec lines 209-210 for the complete_intervention path.
    """
    ekos = _EkosWithSequence(ekos_state=EkosRuntimeState.RUNNING)
    ctrl = _make_ready_controller(tmp_path, ekos_adapter=ekos)
    ctrl.attach_session()
    ctrl.advance_ekos_wait()
    ctrl.begin_intervention(kind=InterventionKind.AUTOFOCUS, reason="drift")
    assert ctrl.session.state == ClawState.INTERVENING

    # Swap to a decoupled adapter after begin_intervention sets up the ledger.
    ctrl.ekos = _EkosResumeDecoupled()  # type: ignore[assignment]

    result = ctrl.complete_intervention(outcome="autofocus_complete")
    assert result.next_state == ClawState.INTERVENING
    assert ctrl.session.state == ClawState.INTERVENING
    blocker_names = [b.name for b in result.blockers]
    assert "ekos_resume_unconfirmed" in blocker_names


def test_resume_stays_paused_when_ekos_settles_idle(tmp_path: Path) -> None:
    """resume() must not treat IDLE as a confirmed running posture."""
    ekos = _EkosWithSequence(ekos_state=EkosRuntimeState.RUNNING)
    ctrl = _make_ready_controller(tmp_path, ekos_adapter=ekos)
    ctrl.attach_session()
    ctrl.advance_ekos_wait()
    ctrl.pause()
    assert ctrl.session.state == ClawState.PAUSED

    ctrl.ekos = _EkosResumeToIdle()  # type: ignore[assignment]
    result = ctrl.resume()

    assert result.next_state == ClawState.PAUSED
    assert ctrl.session.state == ClawState.PAUSED
    assert ctrl.canonical_state().ekos_state == EkosRuntimeState.IDLE


def test_complete_intervention_stays_intervening_when_ekos_aborted(tmp_path: Path) -> None:
    """complete_intervention() must not treat ABORTED as a confirmed running posture."""
    ekos = _EkosWithSequence(ekos_state=EkosRuntimeState.RUNNING)
    ctrl = _make_ready_controller(tmp_path, ekos_adapter=ekos)
    ctrl.attach_session()
    ctrl.advance_ekos_wait()
    ctrl.begin_intervention(kind=InterventionKind.AUTOFOCUS, reason="drift")
    assert ctrl.session.state == ClawState.INTERVENING

    ctrl.ekos = _EkosResumeToAborted()  # type: ignore[assignment]
    result = ctrl.complete_intervention(outcome="autofocus_complete")

    assert result.next_state == ClawState.INTERVENING
    assert ctrl.session.state == ClawState.INTERVENING
    blocker_names = [b.name for b in result.blockers]
    assert "ekos_resume_unconfirmed" in blocker_names
    assert ctrl.canonical_state().ekos_state == EkosRuntimeState.ABORTED


# ------------------------------------------------------------------ #
# complete_supervised_session() tests                                  #
# ------------------------------------------------------------------ #


def test_complete_supervised_session_from_monitoring(tmp_path: Path) -> None:
    ekos = _EkosWithSequence(ekos_state=EkosRuntimeState.RUNNING)
    ctrl = _make_ready_controller(tmp_path, ekos_adapter=ekos)
    ctrl.attach_session()
    ctrl.advance_ekos_wait()
    assert ctrl.session.state == ClawState.MONITORING

    result = ctrl.complete_supervised_session()
    assert result.next_state == ClawState.COMPLETED
    assert ctrl.session.state == ClawState.COMPLETED
    assert ctrl.session.terminal_outcome is not None
    assert ctrl.session.terminal_outcome.value == "completed"


def test_complete_supervised_session_from_ekos_wait(tmp_path: Path) -> None:
    ctrl = _make_ready_controller(tmp_path)
    ctrl.attach_session()
    assert ctrl.session.state == ClawState.EKOS_WAIT

    result = ctrl.complete_supervised_session()
    assert result.next_state == ClawState.COMPLETED


def test_complete_supervised_session_invalid_state(tmp_path: Path) -> None:
    ctrl = _make_ready_controller(tmp_path)
    ctrl.session.state = ClawState.INTERVENING
    with pytest.raises(ValueError, match="only valid from monitoring or ekos_wait"):
        ctrl.complete_supervised_session()


# ------------------------------------------------------------------ #
# Pause / resume in supervisory context                                #
# ------------------------------------------------------------------ #


def test_pause_from_monitoring_sets_monitoring_resume_state(tmp_path: Path) -> None:
    ekos = _EkosWithSequence(ekos_state=EkosRuntimeState.RUNNING)
    ctrl = _make_ready_controller(tmp_path, ekos_adapter=ekos)
    ctrl.attach_session()
    ctrl.advance_ekos_wait()
    assert ctrl.session.state == ClawState.MONITORING

    result = ctrl.pause()
    assert result.next_state == ClawState.PAUSED
    assert ctrl.session.resume_context is not None
    assert ctrl.session.resume_context.resume_state == ClawState.MONITORING


def test_pause_from_intervening_sets_monitoring_resume_state(tmp_path: Path) -> None:
    ekos = _EkosWithSequence(ekos_state=EkosRuntimeState.RUNNING)
    ctrl = _make_ready_controller(tmp_path, ekos_adapter=ekos)
    ctrl.attach_session()
    ctrl.advance_ekos_wait()
    ctrl.begin_intervention(kind=InterventionKind.AUTOFOCUS, reason="drift")
    assert ctrl.session.state == ClawState.INTERVENING

    result = ctrl.pause()
    assert result.next_state == ClawState.PAUSED
    assert ctrl.session.resume_context.resume_state == ClawState.MONITORING


def test_resume_returns_to_monitoring_after_supervisory_pause(tmp_path: Path) -> None:
    ekos = _EkosWithSequence(ekos_state=EkosRuntimeState.RUNNING)
    ctrl = _make_ready_controller(tmp_path, ekos_adapter=ekos)
    ctrl.attach_session()
    ctrl.advance_ekos_wait()
    ctrl.pause()
    assert ctrl.session.state == ClawState.PAUSED

    # Ekos needs to be paused for resume() to succeed
    ekos._ekos_state = EkosRuntimeState.PAUSED
    result = ctrl.resume()
    assert result.next_state == ClawState.MONITORING
    assert ctrl.session.state == ClawState.MONITORING


# ------------------------------------------------------------------ #
# Conflict detection from supervisory states                           #
# ------------------------------------------------------------------ #


def test_conflict_from_monitoring_pauses_with_monitoring_resume(tmp_path: Path) -> None:
    """External conflict from MONITORING should resume to MONITORING."""

    ekos = _EkosWithSequence(ekos_state=EkosRuntimeState.RUNNING)
    ctrl = _make_ready_controller(tmp_path, ekos_adapter=ekos)
    ctrl.attach_session()
    ctrl.advance_ekos_wait()
    assert ctrl.session.state == ClawState.MONITORING

    ctrl._pause_on_conflict("ekos", "capture_started_externally")
    assert ctrl.session.state == ClawState.PAUSED
    assert ctrl.session.resume_context is not None
    assert ctrl.session.resume_context.resume_state == ClawState.MONITORING


# ------------------------------------------------------------------ #
# InterventionLedger unit tests                                        #
# ------------------------------------------------------------------ #


def test_intervention_ledger_tracks_retries() -> None:
    ledger = InterventionLedger(max_retries_per_kind=2)
    assert ledger.retries_for(InterventionKind.AUTOFOCUS) == 0
    assert not ledger.is_retry_exhausted(InterventionKind.AUTOFOCUS)

    ledger.open_intervention(InterventionKind.AUTOFOCUS, "test 1")
    ledger.close_intervention("done")
    assert ledger.retries_for(InterventionKind.AUTOFOCUS) == 1
    assert not ledger.is_retry_exhausted(InterventionKind.AUTOFOCUS)

    ledger.open_intervention(InterventionKind.AUTOFOCUS, "test 2")
    ledger.close_intervention("done")
    assert ledger.retries_for(InterventionKind.AUTOFOCUS) == 2
    assert ledger.is_retry_exhausted(InterventionKind.AUTOFOCUS)


def test_intervention_ledger_active_record() -> None:
    ledger = InterventionLedger()
    assert ledger.active_record is None

    ledger.open_intervention(InterventionKind.REVERIFY, "test")
    assert ledger.active_record is not None
    assert ledger.active_record.kind == InterventionKind.REVERIFY

    ledger.close_intervention("success")
    assert ledger.active_record is None


def test_intervention_ledger_independent_per_kind() -> None:
    ledger = InterventionLedger(max_retries_per_kind=2)
    ledger.open_intervention(InterventionKind.AUTOFOCUS, "focus")
    ledger.close_intervention("done")
    ledger.open_intervention(InterventionKind.AUTOFOCUS, "focus2")
    ledger.close_intervention("done")
    # AUTOFOCUS exhausted, REVERIFY still available
    assert ledger.is_retry_exhausted(InterventionKind.AUTOFOCUS)
    assert not ledger.is_retry_exhausted(InterventionKind.REVERIFY)


# ------------------------------------------------------------------ #
# Supervisory session fields on RuntimeSession                         #
# ------------------------------------------------------------------ #


def test_runtime_session_supervisory_fields_default_to_none() -> None:
    session = RuntimeSession()
    assert session.intervention_ledger is None
    assert session.supervisory_next_action is None
    assert session.ekos_session_id is None


def test_acknowledge_complete_clears_supervisory_fields(tmp_path: Path) -> None:
    """acknowledge_complete() must clear intervention_ledger and supervisory fields.

    After a supervised session completes and the operator acknowledges it,
    the node returns to READY.  The /api/v1/session/current/intervention
    endpoint checks intervention_ledger is None to return null; stale ledger
    data after acknowledge would incorrectly report a phantom intervention.
    """
    ekos = _EkosWithSequence(ekos_state=EkosRuntimeState.RUNNING)
    ctrl = _make_ready_controller(tmp_path, ekos_adapter=ekos)
    ctrl.attach_session()
    ctrl.advance_ekos_wait()
    ctrl.complete_supervised_session()
    assert ctrl.session.state == ClawState.COMPLETED
    assert ctrl.session.intervention_ledger is None

    ctrl.acknowledge_complete()

    assert ctrl.session.state == ClawState.READY
    assert ctrl.session.intervention_ledger is None, (
        "intervention_ledger must be cleared after acknowledge_complete"
    )
    assert ctrl.session.supervisory_next_action is None
    assert ctrl.session.ekos_session_id is None


def test_clear_failure_clears_supervisory_fields(tmp_path: Path) -> None:
    """clear_failure() must clear intervention_ledger and supervisory fields.

    After a supervised session fails and the operator clears it, the node
    returns to READY.  Stale ledger data after clear_failure would cause
    /api/v1/session/current/intervention to report a phantom intervention.
    """
    ekos = _EkosWithSequence(ekos_state=EkosRuntimeState.RUNNING)
    ctrl = _make_ready_controller(tmp_path, ekos_adapter=ekos)
    ctrl.attach_session()
    ctrl.advance_ekos_wait()
    assert ctrl.session.intervention_ledger is not None

    ctrl.fail(reason="test failure")
    assert ctrl.session.state == ClawState.FAILED

    ctrl.clear_failure()

    assert ctrl.session.state == ClawState.READY
    assert ctrl.session.intervention_ledger is None, (
        "intervention_ledger must be cleared after clear_failure"
    )
    assert ctrl.session.supervisory_next_action is None
    assert ctrl.session.ekos_session_id is None


def test_release_control_clears_supervisory_fields(tmp_path: Path) -> None:
    """release_control() must clear intervention_ledger and supervisory fields.

    After a supervised session is released by the operator, the in-memory
    session moves to COMPLETED.  The /api/v1/session/current/intervention
    endpoint returns null only when intervention_ledger is None; leaving it
    populated would expose stale data between release_control() and the
    subsequent acknowledge_complete() call.
    """
    ekos = _EkosWithSequence(ekos_state=EkosRuntimeState.RUNNING)
    ctrl = _make_ready_controller(tmp_path, ekos_adapter=ekos)
    ctrl.attach_session()
    ctrl.advance_ekos_wait()
    assert ctrl.session.intervention_ledger is not None

    ctrl.pause()
    assert ctrl.session.state == ClawState.PAUSED

    ctrl.release_control()

    assert ctrl.session.state == ClawState.COMPLETED
    assert ctrl.session.intervention_ledger is None, (
        "intervention_ledger must be cleared after release_control"
    )
    assert ctrl.session.supervisory_next_action is None
    assert ctrl.session.ekos_session_id is None


def test_new_claw_states_are_not_terminal() -> None:
    for state in (ClawState.EKOS_WAIT, ClawState.MONITORING, ClawState.INTERVENING):
        session = RuntimeSession(state=state)
        assert not session.is_terminal, f"Expected {state} to be non-terminal"


def test_new_claw_states_are_in_enum() -> None:
    states = {s.value for s in ClawState}
    assert "ekos_wait" in states
    assert "monitoring" in states
    assert "intervening" in states


def test_supervision_workflow_intent_is_in_enum() -> None:
    intents = {i.value for i in WorkflowIntent}
    assert "supervision" in intents


def test_complete_supervised_session_clears_supervisory_fields(tmp_path: Path) -> None:
    """complete_supervised_session() must clear intervention_ledger and supervisory fields.

    Stale ledger data after a normal completion would cause
    GET /api/v1/session/current/intervention to return a non-null body and
    GET /api/v1/session/current/state to report a stale supervisory_next_action
    in the COMPLETED window before acknowledge.
    """
    ekos = _EkosWithSequence(ekos_state=EkosRuntimeState.RUNNING)
    ctrl = _make_ready_controller(tmp_path, ekos_adapter=ekos)
    ctrl.attach_session()
    ctrl.advance_ekos_wait()
    assert ctrl.session.state == ClawState.MONITORING
    assert ctrl.session.intervention_ledger is not None

    ctrl.complete_supervised_session()

    assert ctrl.session.state == ClawState.COMPLETED
    assert ctrl.session.intervention_ledger is None, (
        "intervention_ledger must be cleared by complete_supervised_session"
    )
    assert ctrl.session.supervisory_next_action is None, (
        "supervisory_next_action must be cleared by complete_supervised_session"
    )
    assert ctrl.session.ekos_session_id is None, (
        "ekos_session_id must be cleared by complete_supervised_session"
    )


def test_fail_clears_supervisory_fields(tmp_path: Path) -> None:
    """fail() must clear intervention_ledger and supervisory fields.

    Stale ledger data after fail() would cause GET /api/v1/session/current/intervention
    to return a non-null body and GET /api/v1/session/current/state to report a
    stale supervisory_next_action in the FAILED window before clear_failure.
    """
    ekos = _EkosWithSequence(ekos_state=EkosRuntimeState.RUNNING)
    ctrl = _make_ready_controller(tmp_path, ekos_adapter=ekos)
    ctrl.attach_session()
    ctrl.advance_ekos_wait()
    assert ctrl.session.state == ClawState.MONITORING
    assert ctrl.session.intervention_ledger is not None

    ctrl.fail(reason="test failure")

    assert ctrl.session.state == ClawState.FAILED
    assert ctrl.session.intervention_ledger is None, (
        "intervention_ledger must be cleared by fail()"
    )
    assert ctrl.session.supervisory_next_action is None, (
        "supervisory_next_action must be cleared by fail()"
    )
    assert ctrl.session.ekos_session_id is None, (
        "ekos_session_id must be cleared by fail()"
    )
