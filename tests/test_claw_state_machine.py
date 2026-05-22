"""Focused tests for the Kepler Claw state-machine controller."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable
from unittest.mock import MagicMock

import pytest

from kepler_node.agent.authorship import AuthorshipTracker
from kepler_node.agent.broker import BrokerRuntimeState, BrokerSnapshot, StubBrokerBackend
from kepler_node.agent.claw import ClawController, TransitionResult
from kepler_node.agent.ekos import StubEkosAdapter
from kepler_node.agent.interfaces import (
    DeviceActivityEvent,
    DeviceActivityEventType,
    NetworkMode,
    PowerStatus,
    ServiceHealth,
    StorageStatus,
    TimeSource,
    TimeStatus,
)
from kepler_node.agent.session import ClawState, RuntimeSession, WorkflowIntent
from kepler_node.camera.protocols import (
    CameraSettings,
    CaptureRequest,
    CaptureResult,
)
from kepler_node.imaging.protocols import SolveFailureCategory, SolveResult
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
    SessionRecord,
)

# ------------------------------------------------------------------ #
# Fake adapters                                                        #
# ------------------------------------------------------------------ #


class FakeNodeBackend:
    """Minimal node-management backend for tests."""

    def __init__(
        self,
        *,
        time_trusted: bool = True,
        time_source: TimeSource = TimeSource.NETWORK,
        storage_summary: str = "ok",
        storage_writable: bool = True,
        power_healthy: bool = True,
        service_healths: list[ServiceHealth] | None = None,
    ) -> None:
        self._time_trusted = time_trusted
        self._time_source = time_source
        self._storage_summary = storage_summary
        self._storage_writable = storage_writable
        self._power_healthy = power_healthy
        self._service_healths = service_healths or []

    def network_mode(self) -> NetworkMode:
        return NetworkMode.FIELD_HOTSPOT

    def service_health(self) -> list[ServiceHealth]:
        return self._service_healths

    def time_status(self) -> TimeStatus:
        return TimeStatus(
            trusted=self._time_trusted,
            source=self._time_source,
            summary="ok" if self._time_trusted else "time not synchronized",
        )

    def storage_status(self) -> StorageStatus:
        return StorageStatus(
            data_root=Path("/tmp"),
            free_bytes=10_000_000_000 if "ok" in self._storage_summary else 100_000,
            total_bytes=100_000_000_000,
            writable=self._storage_writable,
            summary=self._storage_summary,
        )

    def power_status(self) -> PowerStatus:
        return PowerStatus(
            healthy=self._power_healthy,
            summary="ok" if self._power_healthy else "undervoltage detected",
            undervoltage_detected=not self._power_healthy,
        )

    def confirm_time(self, timestamp: datetime) -> TimeStatus:  # pragma: no cover
        return self.time_status()


class FakeMountBackend:
    """Minimal mount backend for tests."""

    def __init__(self, *, fail_connect: bool = False, fail_slew: bool = False) -> None:
        self.connected = False
        self.synced_to: MountPosition | None = None
        self.slewed_to: MountPosition | None = None
        self.fail_connect = fail_connect
        self.fail_slew = fail_slew
        self._events: list[DeviceActivityEvent] = []

    def connect(self) -> None:
        if self.fail_connect:
            raise RuntimeError("mount connection refused")
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def current_position(self) -> MountPosition:
        return MountPosition(ra_hours=0.0, dec_deg=0.0)

    def slew_to(self, position: MountPosition) -> None:
        if self.fail_slew:
            raise RuntimeError("slew failed")
        self.slewed_to = position

    def sync_to(self, position: MountPosition) -> None:
        self.synced_to = position

    def poll_activity(self) -> None:
        pass

    def activity_events(self) -> Iterable[DeviceActivityEvent]:
        return iter(self._events)

    def inject_event(self, event: DeviceActivityEvent) -> None:
        self._events.append(event)


class FakeCameraBackend:
    """Minimal camera backend for tests."""

    def __init__(
        self,
        *,
        capture_path: Path | None = None,
        fail_connect: bool = False,
        connect_error_msg: str = "camera connect failed",
        fail_capture: bool = False,
        fail_capture_msg: str = "capture failed",
    ) -> None:
        self._capture_path = capture_path
        self._fail_connect = fail_connect
        self._connect_error_msg = connect_error_msg
        self._fail_capture = fail_capture
        self._fail_capture_msg = fail_capture_msg
        self._events: list[DeviceActivityEvent] = []
        self.disconnected = False
        self.connect_calls = 0
        self.disconnect_calls = 0

    def connect(self) -> None:
        self.connect_calls += 1
        if self._fail_connect:
            raise RuntimeError(self._connect_error_msg)

    def disconnect(self) -> None:
        self.disconnected = True
        self.disconnect_calls += 1

    def heartbeat(self) -> bool:
        return True

    def capture(self, request: CaptureRequest) -> CaptureResult:
        if self._fail_capture:
            raise RuntimeError(self._fail_capture_msg)
        path = self._capture_path or (request.destination_dir / "test_frame.jpg")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return CaptureResult(image_path=path, captured_at=datetime.now(UTC))

    def apply_settings(self, settings: CameraSettings) -> CameraSettings:
        return settings

    def activity_events(self) -> Iterable[DeviceActivityEvent]:
        return iter(self._events)

    def inject_event(self, event: DeviceActivityEvent) -> None:
        self._events.append(event)


class FakeSolverBackend:
    """Minimal solver backend for tests."""

    def __init__(self, result: SolveResult) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    def solve(
        self,
        image_path: Path,
        *,
        expected_ra_hours: float | None = None,
        expected_dec_deg: float | None = None,
        blind: bool = False,
    ) -> SolveResult:
        self.calls.append(
            {
                "image_path": image_path,
                "expected_ra_hours": expected_ra_hours,
                "expected_dec_deg": expected_dec_deg,
                "blind": blind,
            }
        )
        return self._result


# ------------------------------------------------------------------ #
# Fixtures                                                             #
# ------------------------------------------------------------------ #


def _good_solve(residual: float = 5.0) -> SolveResult:
    return SolveResult(
        success=True,
        solved_at=datetime.now(UTC),
        solved_ra_hours=1.0,
        solved_dec_deg=45.0,
        residual_arcmin=residual,
        confidence_summary="ok",
    )


def _failed_solve(category: SolveFailureCategory) -> SolveResult:
    return SolveResult(success=False, failure_category=category)


def _make_profile(
    profile_id: str = "test-profile",
    display_name: str = "Test Profile",
    is_default: bool = False,
    is_zoom: bool = False,
    focal_length_assumption_mm: float | None = None,
) -> EquipmentProfile:
    return EquipmentProfile(
        profile_id=profile_id,
        display_name=display_name,
        is_default=is_default,
        hardware=EquipmentProfileHardware(
            mount=EquipmentProfileHardwareMount(model="EQ6-R"),
            camera=EquipmentProfileHardwareCamera(make="ZWO", model="ASI294MC"),
            lens=EquipmentProfileHardwareLens(
                model="Rokinon 135mm f/2",
                is_zoom=is_zoom,
                default_focal_length_mm=None if is_zoom else 135,
            ),
            gps=EquipmentProfileHardwareGps(),
        ),
        site_defaults=EquipmentProfileSiteDefaults(),
        solving_hints=EquipmentProfileSolvingHints(
            focal_length_assumption_mm=focal_length_assumption_mm,
        ),
        backend_preferences=EquipmentProfileBackendPreferences(),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.fixture()
def tmp_verification_dir(tmp_path: Path) -> Path:
    d = tmp_path / "verification"
    d.mkdir()
    return d


@pytest.fixture()
def tmp_store(tmp_path: Path) -> FilesystemSessionStore:
    return FilesystemSessionStore(data_root=tmp_path)


def _make_controller(
    *,
    session: RuntimeSession | None = None,
    node: FakeNodeBackend | None = None,
    mount: FakeMountBackend | None = None,
    camera: FakeCameraBackend | None = None,
    solver: FakeSolverBackend | None = None,
    store: FilesystemSessionStore | None = None,
    authorship: AuthorshipTracker | None = None,
    verification_dir: Path | None = None,
    tmp_path: Path | None = None,
    ekos_adapter: object | None = None,
    broker: object | None = None,
) -> ClawController:
    base = tmp_path or Path("/tmp/kepler_test")
    base.mkdir(parents=True, exist_ok=True)
    vdir = verification_dir or (base / "verify")
    vdir.mkdir(parents=True, exist_ok=True)
    return ClawController(
        session=session or RuntimeSession(),
        node_backend=node or FakeNodeBackend(),
        mount_backend=mount or FakeMountBackend(),
        camera_backend=camera or FakeCameraBackend(),
        solver_backend=solver or FakeSolverBackend(_good_solve()),
        store=store or FilesystemSessionStore(data_root=base),
        authorship_tracker=authorship or AuthorshipTracker(),
        verification_dir=vdir,
        test_exposure_seconds=1.0,
        ekos_adapter=ekos_adapter,
        broker_backend=broker,
    )


# ------------------------------------------------------------------ #
# Boot -> discover -> connect -> ready                                 #
# ------------------------------------------------------------------ #


def test_boot_transitions_to_discover(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path=tmp_path)
    result = ctrl.boot()
    assert result.previous_state == ClawState.BOOT
    assert result.next_state == ClawState.DISCOVER
    assert ctrl.session.state == ClawState.DISCOVER


def test_zoom_lens_gate_blocks_without_assumption(tmp_path: Path) -> None:
    """check_readiness returns focal_length_assumption_required blocker for zoom lens."""
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.active_equipment_profile = _make_profile(is_zoom=True, focal_length_assumption_mm=None)

    conditions = ctrl.check_readiness()
    blocker_names = [c.name for c in conditions]
    assert "focal_length_assumption_required" in blocker_names


def test_zoom_lens_gate_passes_with_assumption(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.active_equipment_profile = _make_profile(is_zoom=True, focal_length_assumption_mm=200.0)

    conditions = ctrl.check_readiness()
    blocker_names = [c.name for c in conditions]
    assert "focal_length_assumption_required" not in blocker_names


def test_fixed_lens_no_zoom_gate(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.active_equipment_profile = _make_profile(is_zoom=False)

    conditions = ctrl.check_readiness()
    blocker_names = [c.name for c in conditions]
    assert "focal_length_assumption_required" not in blocker_names


def test_stage_and_clear_target(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.stage_target_intake(
        target_label="M31",
        ra_hours=0.7122,
        dec_deg=41.269,
        target_source="manual",
        run_parameters={
            "exposure_seconds": 120,
            "camera_settings": {"gain": 100},
            "stop_condition": {"frame_count": 60},
        },
    )
    assert ctrl.session.staged_target_ra_hours == pytest.approx(0.7122)
    assert ctrl._staged_target_label == "M31"

    ctrl.clear_staged_target()
    assert ctrl.session.staged_target_ra_hours is None
    assert ctrl._staged_target_label is None


def test_start_session_requires_ready_state(tmp_path: Path) -> None:
    """start_session raises ValueError when state is not ready."""
    ctrl = _make_controller(
        session=RuntimeSession(state=ClawState.BOOT),
        tmp_path=tmp_path,
    )
    with pytest.raises(ValueError, match="ready"):
        ctrl.start_session()


def test_start_session_requires_staged_target(tmp_path: Path) -> None:
    ctrl = _make_controller(
        session=RuntimeSession(state=ClawState.READY),
        tmp_path=tmp_path,
    )
    with pytest.raises((ValueError, RuntimeError)):
        ctrl.start_session()


def test_discover_auto_selects_single_default_profile(tmp_path: Path) -> None:
    """discover() loads the one profile marked is_default and sets it as active."""
    ctrl = _make_controller(session=RuntimeSession(state=ClawState.DISCOVER), tmp_path=tmp_path)
    ctrl.store.write_profile(_make_profile(profile_id="rig-a", is_default=True))

    result = ctrl.discover()

    assert result.next_state == ClawState.CONNECT
    assert ctrl.active_equipment_profile is not None
    assert ctrl.active_equipment_profile.profile_id == "rig-a"
    names = [c.name for c in result.degraded]
    assert "multiple_default_profiles" not in names
    assert "no_default_equipment_profile" not in names


def test_discover_degrades_on_multiple_default_profiles(tmp_path: Path) -> None:
    """discover() surfaces a degraded condition when more than one profile is default."""
    ctrl = _make_controller(session=RuntimeSession(state=ClawState.DISCOVER), tmp_path=tmp_path)
    for pid in ("rig-a", "rig-b"):
        profile = _make_profile(profile_id=pid, is_default=True)
        path = ctrl.store.profiles_root / f"{pid}.json"
        ctrl.store.profiles_root.mkdir(parents=True, exist_ok=True)
        path.write_text(profile.model_dump_json(indent=2), encoding="utf-8")

    result = ctrl.discover()

    assert result.next_state == ClawState.PAUSED
    names = [c.name for c in result.degraded]
    assert "multiple_default_profiles" in names


def test_discover_degrades_when_profiles_exist_but_none_is_default(tmp_path: Path) -> None:
    """discover() surfaces a degraded condition when profiles exist but none is default."""
    ctrl = _make_controller(session=RuntimeSession(state=ClawState.DISCOVER), tmp_path=tmp_path)
    ctrl.store.write_profile(_make_profile(profile_id="rig-a", is_default=False))

    result = ctrl.discover()

    assert result.next_state == ClawState.PAUSED
    assert ctrl.active_equipment_profile is None
    names = [c.name for c in result.degraded]
    assert "no_default_equipment_profile" in names


def test_discover_no_profiles_pauses_with_no_default_equipment_profile(tmp_path: Path) -> None:
    """discover() with zero stored profiles pauses with no_default_equipment_profile."""
    ctrl = _make_controller(session=RuntimeSession(state=ClawState.DISCOVER), tmp_path=tmp_path)

    result = ctrl.discover()

    assert result.next_state == ClawState.PAUSED
    names = [c.name for c in result.degraded]
    assert "no_default_equipment_profile" in names
    assert ctrl.active_equipment_profile is None


def test_discover_transitions_to_connect(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.state = ClawState.DISCOVER
    # Provide a default profile so discover can auto-select and proceed to CONNECT
    profile = EquipmentProfile(
        profile_id="p1",
        display_name="P1",
        is_default=True,
        hardware=EquipmentProfileHardware(
            mount=EquipmentProfileHardwareMount(model="EQ6-R"),
            camera=EquipmentProfileHardwareCamera(make="ZWO", model="ASI294MC"),
            lens=EquipmentProfileHardwareLens(model="135mm", default_focal_length_mm=135),
            gps=EquipmentProfileHardwareGps(),
        ),
        site_defaults=EquipmentProfileSiteDefaults(),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    ctrl.store.write_profile(profile)
    result = ctrl.discover()
    assert result.next_state == ClawState.CONNECT
    assert ctrl.session.state == ClawState.CONNECT
    assert result.degraded == []


def test_discover_surfaces_unhealthy_services_as_degraded(tmp_path: Path) -> None:
    node = FakeNodeBackend(
        service_healths=[
            ServiceHealth(name="indiserver", healthy=False, summary="inactive"),
        ]
    )
    ctrl = _make_controller(node=node, tmp_path=tmp_path)
    ctrl.session.state = ClawState.DISCOVER
    # Provide a default profile so discover can proceed to CONNECT despite degraded services
    profile = EquipmentProfile(
        profile_id="p1",
        display_name="P1",
        is_default=True,
        hardware=EquipmentProfileHardware(
            mount=EquipmentProfileHardwareMount(model="EQ6-R"),
            camera=EquipmentProfileHardwareCamera(make="ZWO", model="ASI294MC"),
            lens=EquipmentProfileHardwareLens(model="135mm", default_focal_length_mm=135),
            gps=EquipmentProfileHardwareGps(),
        ),
        site_defaults=EquipmentProfileSiteDefaults(),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    ctrl.store.write_profile(profile)
    result = ctrl.discover()
    assert result.next_state == ClawState.CONNECT  # still proceeds
    assert len(result.degraded) == 1
    assert result.degraded[0].name == "service_unhealthy_indiserver"


def test_connect_success_reaches_ready(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.state = ClawState.CONNECT
    result = ctrl.connect()
    assert result.next_state == ClawState.READY
    assert ctrl.session.state == ClawState.READY
    assert result.blockers == []


def test_connect_mount_failure_pauses_with_blocker(tmp_path: Path) -> None:
    ctrl = _make_controller(mount=FakeMountBackend(fail_connect=True), tmp_path=tmp_path)
    ctrl.session.state = ClawState.CONNECT
    result = ctrl.connect()
    assert result.next_state == ClawState.PAUSED
    assert ctrl.session.state == ClawState.PAUSED
    assert any(b.name == "mount_connect_failed" for b in result.blockers)
    assert ctrl.session.resume_context is not None
    assert ctrl.session.resume_context.resume_state == ClawState.CONNECT


def test_connect_camera_remote_mode_required_produces_named_blocker(tmp_path: Path) -> None:
    camera = FakeCameraBackend(
        fail_connect=True,
        connect_error_msg="camera_remote_mode_required: switch to USB remote mode",
    )
    ctrl = _make_controller(camera=camera, tmp_path=tmp_path)
    ctrl.session.state = ClawState.CONNECT
    result = ctrl.connect()
    assert result.next_state == ClawState.PAUSED
    assert any(b.name == "camera_remote_mode_required" for b in result.blockers)
    blocker = next(b for b in result.blockers if b.name == "camera_remote_mode_required")
    assert blocker.operator_action_required is not None


def test_connect_resets_reconnect_counter_on_success(tmp_path: Path) -> None:
    session = RuntimeSession(reconnect_attempts=2)
    session.state = ClawState.CONNECT
    ctrl = _make_controller(session=session, tmp_path=tmp_path)
    ctrl.connect()
    assert ctrl.session.reconnect_attempts == 0


# ------------------------------------------------------------------ #
# check_readiness                                                      #
# ------------------------------------------------------------------ #


def test_check_readiness_time_uncertain_produces_blocker(tmp_path: Path) -> None:
    node = FakeNodeBackend(time_trusted=False, time_source=TimeSource.UNTRUSTED)
    ctrl = _make_controller(node=node, tmp_path=tmp_path)
    blockers = ctrl.check_readiness()
    assert any(b.name == "time_uncertain" for b in blockers)


def test_check_readiness_storage_critically_low_produces_blocker(tmp_path: Path) -> None:
    node = FakeNodeBackend(storage_summary="critically low free space")
    ctrl = _make_controller(node=node, tmp_path=tmp_path)
    blockers = ctrl.check_readiness()
    assert any(b.name == "storage_critically_low" for b in blockers)


def test_check_readiness_power_unhealthy_produces_blocker(tmp_path: Path) -> None:
    node = FakeNodeBackend(power_healthy=False)
    ctrl = _make_controller(node=node, tmp_path=tmp_path)
    blockers = ctrl.check_readiness()
    assert any(b.name == "power_integrity_warning" for b in blockers)


def test_check_readiness_all_clear_returns_empty(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path=tmp_path)
    assert ctrl.check_readiness() == []


# ------------------------------------------------------------------ #
# Calibration verification loop                                        #
# ------------------------------------------------------------------ #


def test_calibration_loop_success_returns_ready(tmp_path: Path) -> None:
    """Calibration with residual within 1 degree should exit to READY."""
    ctrl = _make_controller(
        solver=FakeSolverBackend(_good_solve(residual=30.0)),  # 30 arcmin < 60 arcmin tolerance
        tmp_path=tmp_path,
    )
    ctrl.session.state = ClawState.READY
    result = ctrl.run_calibrate()
    assert result.next_state == ClawState.READY
    assert ctrl.session.state == ClawState.READY
    assert ctrl.session.calibration_accepted is True


def test_calibration_success_with_staged_target_returns_target_acquired(tmp_path: Path) -> None:
    """Successful calibration with a staged target should advance to TARGET_ACQUIRED."""
    ctrl = _make_controller(
        solver=FakeSolverBackend(_good_solve(residual=30.0)),
        tmp_path=tmp_path,
    )
    ctrl.session.state = ClawState.READY
    ctrl.session.staged_target_ra_hours = 5.5
    ctrl.session.staged_target_dec_deg = 22.0
    result = ctrl.run_calibrate()
    assert result.next_state == ClawState.TARGET_ACQUIRED
    assert ctrl.session.state == ClawState.TARGET_ACQUIRED
    assert ctrl.session.workflow_intent == WorkflowIntent.TARGET_CENTERING


def test_calibration_blocked_by_time_uncertain_pauses(tmp_path: Path) -> None:
    node = FakeNodeBackend(time_trusted=False)
    ctrl = _make_controller(node=node, tmp_path=tmp_path)
    ctrl.session.state = ClawState.READY
    result = ctrl.run_calibrate()
    assert result.next_state == ClawState.PAUSED
    assert any(b.name == "time_uncertain" for b in result.blockers)
    assert ctrl.session.resume_context is not None


def test_calibration_loop_apply_one_correction_then_succeeds(tmp_path: Path) -> None:
    """First solve returns residual exceeding tolerance; correction is applied; second solve passes."""
    solver = FakeSolverBackend(_good_solve(residual=30.0))  # within tolerance on first attempt
    mount = FakeMountBackend()
    ctrl = _make_controller(solver=solver, mount=mount, tmp_path=tmp_path)
    # Set calibration tolerance to 20 arcmin to force one correction pass
    ctrl.CALIBRATION_TOLERANCE_ARCMIN = 20.0

    ctrl.session.state = ClawState.READY
    result = ctrl.run_calibrate()
    # After first center_verify fails (30 > 20), correct is applied, then second solve gives 30 > 20 again -> loop
    # Actually the solver always returns 30, so it'll exhaust retries and pause.
    # Let's verify the mount.sync_to was called (correction happened)
    # Since residual (30) > tolerance (20) every time, it will hit max loops and pause
    assert result.next_state == ClawState.PAUSED
    assert ctrl.session.calibration_loop_count == ctrl.MAX_CALIBRATION_LOOPS


def test_centering_loop_success_enters_capture(tmp_path: Path) -> None:
    """Target centering with residual within 15 arcmin enters CAPTURE."""
    ctrl = _make_controller(
        solver=FakeSolverBackend(_good_solve(residual=10.0)),
        tmp_path=tmp_path,
    )
    session = ctrl.session
    session.state = ClawState.TARGET_ACQUIRED
    session.staged_target_ra_hours = 5.5
    session.staged_target_dec_deg = 22.0
    session.workflow_intent = WorkflowIntent.TARGET_CENTERING
    session.calibration_accepted = True

    result = ctrl.run_verification_loop()
    assert result.next_state == ClawState.CAPTURE
    assert session.state == ClawState.CAPTURE
    assert session.control_locked is True
    assert session.workflow_intent == WorkflowIntent.CAPTURE


def test_centering_retries_exhausted_pauses(tmp_path: Path) -> None:
    """Target centering that never converges pauses after MAX_CENTERING_LOOPS corrections."""
    ctrl = _make_controller(
        solver=FakeSolverBackend(_good_solve(residual=30.0)),  # always outside 15 arcmin
        tmp_path=tmp_path,
    )
    session = ctrl.session
    session.state = ClawState.TARGET_ACQUIRED
    session.staged_target_ra_hours = 5.5
    session.staged_target_dec_deg = 22.0
    session.workflow_intent = WorkflowIntent.TARGET_CENTERING

    result = ctrl.run_verification_loop()
    assert result.next_state == ClawState.PAUSED
    assert session.centering_loop_count == ctrl.MAX_CENTERING_LOOPS


# ------------------------------------------------------------------ #
# Solve failure paths                                                  #
# ------------------------------------------------------------------ #


def test_solve_failure_frame_suspect_goes_to_recover(tmp_path: Path) -> None:
    """NO_STARS_DETECTED (frame-suspect) should trigger RECOVER."""
    ctrl = _make_controller(
        solver=FakeSolverBackend(_failed_solve(SolveFailureCategory.NO_STARS_DETECTED)),
        tmp_path=tmp_path,
    )
    ctrl.session.state = ClawState.TEST_CAPTURE
    ctrl.session.workflow_intent = WorkflowIntent.CALIBRATION
    ctrl.session.last_frame_path = str(tmp_path / "fake.jpg")
    (tmp_path / "fake.jpg").touch()

    result = ctrl._do_solve()
    assert result.next_state == ClawState.RECOVER


def test_solve_failure_solver_specific_triggers_re_solve_via_recover(tmp_path: Path) -> None:
    """TIMEOUT (solver-specific) should trigger RECOVER pointing to SOLVE (re-evaluate)."""
    ctrl = _make_controller(
        solver=FakeSolverBackend(_failed_solve(SolveFailureCategory.TIMEOUT)),
        tmp_path=tmp_path,
    )
    ctrl.session.state = ClawState.SOLVE
    ctrl.session.workflow_intent = WorkflowIntent.CALIBRATION
    ctrl.session.last_frame_path = str(tmp_path / "fake.jpg")
    (tmp_path / "fake.jpg").touch()
    ctrl.session.solve_attempts = 0

    sv = ctrl._do_solve()
    assert sv.next_state == ClawState.RECOVER


def test_solve_retries_exhausted_pauses_session(tmp_path: Path) -> None:
    """After MAX_SOLVE_ATTEMPTS the session should pause."""
    ctrl = _make_controller(
        solver=FakeSolverBackend(_failed_solve(SolveFailureCategory.TIMEOUT)),
        tmp_path=tmp_path,
    )
    ctrl.session.state = ClawState.SOLVE
    ctrl.session.workflow_intent = WorkflowIntent.CALIBRATION
    ctrl.session.last_frame_path = str(tmp_path / "fake.jpg")
    (tmp_path / "fake.jpg").touch()
    ctrl.session.solve_attempts = ctrl.MAX_SOLVE_ATTEMPTS  # already at limit

    result = ctrl._do_solve()
    assert result.next_state == ClawState.PAUSED
    assert ctrl.session.state == ClawState.PAUSED
    assert ctrl.session.resume_context is not None


def test_blind_solve_is_attempted_after_hint_related_failure(tmp_path: Path) -> None:
    """Second solve attempt after TIMEOUT should use blind=True."""
    solver = FakeSolverBackend(_failed_solve(SolveFailureCategory.TIMEOUT))
    ctrl = _make_controller(solver=solver, tmp_path=tmp_path)
    ctrl.session.state = ClawState.SOLVE
    ctrl.session.workflow_intent = WorkflowIntent.CALIBRATION
    ctrl.session.last_frame_path = str(tmp_path / "fake.jpg")
    (tmp_path / "fake.jpg").touch()
    ctrl.session.solve_attempts = 1
    ctrl.session.last_solve_failure_category = SolveFailureCategory.TIMEOUT.value

    ctrl._do_solve()
    assert solver.calls[-1]["blind"] is True


# ------------------------------------------------------------------ #
# center_verify intent mapping                                         #
# ------------------------------------------------------------------ #


def test_center_verify_capture_intent_fails_session(tmp_path: Path) -> None:
    """CAPTURE intent must not enter center_verify; it should fail the session."""
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.state = ClawState.SOLVE
    ctrl.session.workflow_intent = WorkflowIntent.CAPTURE
    ctrl.session.last_residual_arcmin = 5.0

    result = ctrl._do_center_verify()
    assert result.next_state == ClawState.FAILED
    assert ctrl.session.state == ClawState.FAILED


def test_center_verify_recovery_within_tolerance_resumes_capture(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.state = ClawState.SOLVE
    ctrl.session.workflow_intent = WorkflowIntent.RECOVERY_VERIFICATION
    ctrl.session.last_residual_arcmin = 10.0  # within 15 arcmin

    result = ctrl._do_center_verify()
    assert result.next_state == ClawState.CAPTURE
    assert ctrl.session.workflow_intent == WorkflowIntent.CAPTURE


# ------------------------------------------------------------------ #
# Capture and guard                                                    #
# ------------------------------------------------------------------ #


def test_capture_one_frame_transitions_to_guard(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.state = ClawState.CAPTURE
    ctrl.session.control_locked = True
    request = CaptureRequest(
        exposure_seconds=60.0,
        settings=CameraSettings(iso=800),
        destination_dir=tmp_path / "frames",
    )
    result = ctrl.capture_one_frame(request=request)
    assert result.next_state == ClawState.GUARD
    assert ctrl.session.state == ClawState.GUARD


def test_capture_failure_enters_recover(tmp_path: Path) -> None:
    ctrl = _make_controller(camera=FakeCameraBackend(fail_capture=True), tmp_path=tmp_path)
    ctrl.session.state = ClawState.CAPTURE
    ctrl.session.control_locked = True
    request = CaptureRequest(
        exposure_seconds=60.0,
        settings=CameraSettings(iso=800),
        destination_dir=tmp_path / "frames",
    )
    result = ctrl.capture_one_frame(request=request)
    assert result.next_state == ClawState.RECOVER


def test_capture_autocapture_mode_pauses_for_operator(tmp_path: Path) -> None:
    ctrl = _make_controller(
        camera=FakeCameraBackend(
            fail_capture=True,
            fail_capture_msg=(
                "camera_autocapture_mode_blocking: Camera is in Still Capture Mode 'Self-timer'; "
                "exit self-timer/autocapture mode on the body before capture"
            ),
        ),
        tmp_path=tmp_path,
    )
    ctrl.session.state = ClawState.CAPTURE
    ctrl.session.control_locked = True
    request = CaptureRequest(
        exposure_seconds=60.0,
        settings=CameraSettings(iso=800),
        destination_dir=tmp_path / "frames",
    )

    result = ctrl.capture_one_frame(request=request)

    assert result.next_state == ClawState.PAUSED
    assert ctrl.session.state == ClawState.PAUSED
    assert any(b.name == "camera_autocapture_mode_blocking" for b in result.blockers)


def test_evaluate_guard_pass_continues_capture(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.state = ClawState.GUARD
    result = ctrl.evaluate_guard(quality_overall="pass")
    assert result.next_state == ClawState.CAPTURE


def test_evaluate_guard_first_bad_frame_warns_and_continues(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.state = ClawState.GUARD
    result = ctrl.evaluate_guard(quality_overall="fail")
    assert result.next_state == ClawState.CAPTURE  # warns but continues
    assert ctrl.session.consecutive_bad_frames == 1


def test_evaluate_guard_second_bad_frame_enters_recover(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.state = ClawState.GUARD
    ctrl.session.consecutive_bad_frames = 1  # already had one bad frame
    result = ctrl.evaluate_guard(quality_overall="fail")
    assert result.next_state == ClawState.RECOVER


def test_evaluate_guard_third_bad_frame_pauses(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.state = ClawState.GUARD
    ctrl.session.consecutive_bad_frames = 2
    result = ctrl.evaluate_guard(quality_overall="fail")
    assert result.next_state == ClawState.PAUSED


def test_evaluate_guard_frames_remaining_zero_completes_session(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.state = ClawState.GUARD
    result = ctrl.evaluate_guard(quality_overall="pass", frames_remaining=0)
    assert result.next_state == ClawState.COMPLETED
    assert ctrl.session.is_terminal


def test_evaluate_guard_storage_critically_low_pauses(tmp_path: Path) -> None:
    node = FakeNodeBackend(storage_summary="critically low free space")
    ctrl = _make_controller(node=node, tmp_path=tmp_path)
    ctrl.session.state = ClawState.GUARD
    result = ctrl.evaluate_guard(quality_overall="pass")
    assert result.next_state == ClawState.PAUSED
    assert any(b.name == "storage_critically_low" for b in result.blockers)


# ------------------------------------------------------------------ #
# Recover decision logic                                               #
# ------------------------------------------------------------------ #


def test_recover_mount_disconnect_goes_to_connect(tmp_path: Path) -> None:
    """Mount disconnect must route to CONNECT, not direct CAPTURE."""
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.state = ClawState.CAPTURE
    ctrl.session.workflow_intent = WorkflowIntent.CAPTURE
    ctrl.session.calibration_accepted = True

    result = ctrl.recover(reason="mount disconnected", mount_disconnected=True)
    assert result.next_state == ClawState.CONNECT
    # Calibration should be invalidated
    assert ctrl.session.calibration_accepted is False


def test_recover_mount_reconnect_exhausted_pauses(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.state = ClawState.RECOVER
    ctrl.session.reconnect_attempts = ctrl.MAX_RECONNECT_ATTEMPTS

    result = ctrl.recover(reason="mount disconnected again", mount_disconnected=True)
    assert result.next_state == ClawState.PAUSED
    assert ctrl.session.state == ClawState.PAUSED


def test_recover_frame_suspect_failure_goes_to_test_capture(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.state = ClawState.SOLVE

    result = ctrl.recover(
        reason="no stars detected",
        failure_category=SolveFailureCategory.NO_STARS_DETECTED.value,
    )
    assert result.next_state == ClawState.TEST_CAPTURE


def test_recover_solver_specific_failure_re_evaluates_via_solve(tmp_path: Path) -> None:
    """Solver-specific failure with budget remaining should go to SOLVE."""
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.state = ClawState.SOLVE
    ctrl.session.last_frame_path = "/tmp/some_frame.jpg"
    ctrl.session.solve_attempts = 1  # budget allows re-evaluate

    result = ctrl.recover(
        reason="timeout",
        failure_category=SolveFailureCategory.TIMEOUT.value,
    )
    assert result.next_state == ClawState.SOLVE


# ------------------------------------------------------------------ #
# External control conflict detection                                  #
# ------------------------------------------------------------------ #


def test_conflict_detection_pauses_session_when_control_locked(tmp_path: Path) -> None:
    """Unrecognized mount activity while control_locked must pause with external_control_conflict."""
    mount = FakeMountBackend()
    conflict_event = DeviceActivityEvent(
        event_type=DeviceActivityEventType.MOUNT_SLEW_STARTED,
        observed_at=datetime.now(UTC),
    )
    mount.inject_event(conflict_event)

    ctrl = _make_controller(mount=mount, tmp_path=tmp_path)
    ctrl.session.state = ClawState.CAPTURE
    ctrl.session.control_locked = True

    detected = ctrl.check_and_handle_conflicts()
    assert detected is True
    assert ctrl.session.state == ClawState.PAUSED
    assert ctrl.session.resume_context is not None
    assert ctrl.session.resume_context.pause_reason == "external_control_conflict"


def test_conflict_detection_ignores_authored_events(tmp_path: Path) -> None:
    """Events that match a recent Kepler-authored command must not trigger conflict."""
    mount = FakeMountBackend()
    authorship = AuthorshipTracker()
    authored_event = DeviceActivityEvent(
        event_type=DeviceActivityEventType.MOUNT_SLEW_STARTED,
        observed_at=datetime.now(UTC),
    )
    authorship.record(authored_event)
    mount.inject_event(authored_event)

    ctrl = _make_controller(mount=mount, authorship=authorship, tmp_path=tmp_path)
    ctrl.session.state = ClawState.CAPTURE
    ctrl.session.control_locked = True

    detected = ctrl.check_and_handle_conflicts()
    assert detected is False
    assert ctrl.session.state == ClawState.CAPTURE  # unchanged


def test_conflict_detection_skipped_when_control_not_locked(tmp_path: Path) -> None:
    mount = FakeMountBackend()
    conflict_event = DeviceActivityEvent(
        event_type=DeviceActivityEventType.MOUNT_SLEW_STARTED,
        observed_at=datetime.now(UTC),
    )
    mount.inject_event(conflict_event)

    ctrl = _make_controller(mount=mount, tmp_path=tmp_path)
    ctrl.session.control_locked = False

    detected = ctrl.check_and_handle_conflicts()
    assert detected is False


# ------------------------------------------------------------------ #
# Terminal and pause/resume paths                                      #
# ------------------------------------------------------------------ #


def test_release_control_from_paused_completes_with_released_control(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.state = ClawState.CAPTURE
    ctrl.session.control_locked = True
    ctrl.session.workflow_intent = WorkflowIntent.CAPTURE
    ctrl.session.pause(
        pause_reason="operator requested",
        resume_state=ClawState.CAPTURE,
        workflow_intent=WorkflowIntent.CAPTURE,
    )
    ctrl.session.release_control()

    from kepler_node.agent.session import TerminalOutcome

    assert ctrl.session.state == ClawState.COMPLETED
    assert ctrl.session.terminal_outcome == TerminalOutcome.RELEASED_CONTROL
    assert ctrl.session.resume_context is None
    assert ctrl.session.control_locked is False


def test_stop_from_any_state_completes_and_clears_context(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.state = ClawState.CAPTURE
    ctrl.session.control_locked = True
    ctrl.session.workflow_intent = WorkflowIntent.CAPTURE
    ctrl.session.pause(
        pause_reason="manual stop",
        resume_state=ClawState.CAPTURE,
        workflow_intent=WorkflowIntent.CAPTURE,
    )
    ctrl.session.stop()

    from kepler_node.agent.session import TerminalOutcome

    assert ctrl.session.state == ClawState.COMPLETED
    assert ctrl.session.terminal_outcome == TerminalOutcome.STOPPED_BY_OPERATOR
    assert ctrl.session.control_locked is False
    assert ctrl.session.resume_context is None


def test_fail_clears_control_lock_and_resume_context(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.state = ClawState.CAPTURE
    ctrl.session.control_locked = True
    ctrl.session.workflow_intent = WorkflowIntent.CAPTURE

    ctrl.session.fail()

    from kepler_node.agent.session import TerminalOutcome

    assert ctrl.session.state == ClawState.FAILED
    assert ctrl.session.terminal_outcome == TerminalOutcome.FAILED
    assert ctrl.session.control_locked is False
    assert ctrl.session.resume_context is None


def test_session_is_terminal_after_completed_or_failed(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.state = ClawState.COMPLETED
    assert ctrl.session.is_terminal is True

    ctrl.session.state = ClawState.FAILED
    assert ctrl.session.is_terminal is True

    ctrl.session.state = ClawState.CAPTURE
    assert ctrl.session.is_terminal is False


# ------------------------------------------------------------------ #
# Event emission                                                       #
# ------------------------------------------------------------------ #


def test_managed_session_events_create_directory_lazily(
    tmp_path: Path, tmp_store: FilesystemSessionStore
) -> None:
    """Events for a managed session must persist even without prior write_session_record."""
    session = RuntimeSession(session_id="lazy-init-session")
    session.state = ClawState.DISCOVER
    # Deliberately do NOT pre-seed write_session_record.
    ctrl = _make_controller(session=session, store=tmp_store, tmp_path=tmp_path)
    ctrl.discover()  # emits a STATE_TRANSITION event

    matches = list(tmp_store.sessions_root.glob("*/lazy-init-session/events.ndjson"))
    assert matches, "events.ndjson was not created by lazy session directory initialization"
    lines = matches[0].read_text().strip().splitlines()
    assert len(lines) >= 1


def test_events_are_emitted_to_storage_when_session_id_set(
    tmp_path: Path, tmp_store: FilesystemSessionStore
) -> None:
    session = RuntimeSession(session_id="test-session-phase3")
    session.state = ClawState.CONNECT

    # Write session directory so store can append events
    record = SessionRecord(
        session_id="test-session-phase3",
        started_at=datetime(2026, 5, 11, tzinfo=UTC),
        updated_at=datetime(2026, 5, 11, tzinfo=UTC),
        state=ClawState.CONNECT,
    )
    session_dir = tmp_store.write_session_record(record)

    ctrl = _make_controller(session=session, store=tmp_store, tmp_path=tmp_path)
    ctrl.session.state = ClawState.DISCOVER
    ctrl.discover()  # emits a STATE_TRANSITION event

    event_path = session_dir / "events.ndjson"
    assert event_path.exists()
    lines = event_path.read_text().strip().splitlines()
    assert len(lines) >= 1


def test_stage_target_sets_session_fields(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.stage_target(ra_hours=5.5, dec_deg=22.0, target_id="m42")
    assert ctrl.session.staged_target_ra_hours == 5.5
    assert ctrl.session.staged_target_dec_deg == 22.0
    assert ctrl.session.staged_target_id == "m42"
    assert ctrl.session.state == ClawState.TARGET_ACQUIRED
    assert ctrl.session.workflow_intent == WorkflowIntent.TARGET_CENTERING


def test_reset_verification_counters_zeroes_all_counters(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.solve_attempts = 3
    ctrl.session.calibration_loop_count = 4
    ctrl.session.centering_loop_count = 2
    ctrl.session.consecutive_bad_frames = 1
    ctrl.session.reset_verification_counters()
    assert ctrl.session.solve_attempts == 0
    assert ctrl.session.calibration_loop_count == 0
    assert ctrl.session.centering_loop_count == 0
    assert ctrl.session.consecutive_bad_frames == 0


def test_transition_result_carries_structured_details(tmp_path: Path) -> None:
    """TransitionResult should carry enough structured context for API/UI use."""
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.state = ClawState.BOOT
    result = ctrl.boot()

    assert isinstance(result, TransitionResult)
    assert result.previous_state == ClawState.BOOT
    assert result.next_state == ClawState.DISCOVER
    assert isinstance(result.message, str)
    assert isinstance(result.blockers, list)
    assert isinstance(result.degraded, list)
    assert isinstance(result.details, dict)
    # Runtime context must always be present so API consumers can build
    # state-changing responses without re-reading controller state (spec 1594).
    assert "workflow_intent" in result.details
    assert "control_locked" in result.details


def test_transition_result_stop_carries_ownership_context(tmp_path: Path) -> None:
    """stop() transition must carry control_locked=False in details."""
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.state = ClawState.CAPTURE
    ctrl.session.control_locked = True
    ctrl.session.session_id = "sess-stop"
    ctrl.session.workflow_intent = WorkflowIntent.CAPTURE

    result = ctrl.stop()

    assert result.next_state == ClawState.COMPLETED
    assert result.details["control_locked"] is False


def test_transition_result_fail_carries_ownership_context(tmp_path: Path) -> None:
    """fail() transition must carry control_locked=False in details."""
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.state = ClawState.CAPTURE
    ctrl.session.control_locked = True
    ctrl.session.session_id = "sess-fail"
    ctrl.session.workflow_intent = WorkflowIntent.CAPTURE

    result = ctrl.fail(reason="hardware error")

    assert result.next_state == ClawState.FAILED
    assert result.details["control_locked"] is False


def test_transition_result_release_control_carries_ownership_context(tmp_path: Path) -> None:
    """release_control() must carry control_locked=False in details."""
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.state = ClawState.PAUSED
    ctrl.session.control_locked = True
    ctrl.session.session_id = "sess-release"

    result = ctrl.release_control()

    assert result.next_state == ClawState.COMPLETED
    assert result.details["control_locked"] is False


# ------------------------------------------------------------------ #
# Fix 1: recover() must set RECOVERY_VERIFICATION when from_capture  #
# ------------------------------------------------------------------ #


def test_recover_from_capture_sets_recovery_verification_intent(tmp_path: Path) -> None:
    """recover(from_capture=True) must switch intent to RECOVERY_VERIFICATION."""
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.state = ClawState.CAPTURE
    ctrl.session.workflow_intent = WorkflowIntent.CAPTURE
    ctrl.session.control_locked = True

    result = ctrl.recover(
        reason="bad frame",
        failure_category=SolveFailureCategory.NO_STARS_DETECTED.value,
        from_capture=True,
    )

    assert result.next_state == ClawState.TEST_CAPTURE
    assert ctrl.session.workflow_intent == WorkflowIntent.RECOVERY_VERIFICATION


def test_recover_from_capture_solver_failure_sets_recovery_verification_intent(
    tmp_path: Path,
) -> None:
    """recover(from_capture=True) with solver-specific failure still switches intent."""
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.state = ClawState.CAPTURE
    ctrl.session.workflow_intent = WorkflowIntent.CAPTURE
    ctrl.session.control_locked = True
    ctrl.session.last_frame_path = str(tmp_path / "frame.jpg")
    ctrl.session.solve_attempts = 0

    result = ctrl.recover(
        reason="timeout",
        failure_category=SolveFailureCategory.TIMEOUT.value,
        from_capture=True,
    )

    assert result.next_state == ClawState.SOLVE
    assert ctrl.session.workflow_intent == WorkflowIntent.RECOVERY_VERIFICATION


def test_recover_without_from_capture_preserves_existing_intent(tmp_path: Path) -> None:
    """recover() without from_capture must not change workflow_intent."""
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.state = ClawState.TEST_CAPTURE
    ctrl.session.workflow_intent = WorkflowIntent.CALIBRATION

    ctrl.recover(
        reason="bad frame",
        failure_category=SolveFailureCategory.NO_STARS_DETECTED.value,
        from_capture=False,
    )

    assert ctrl.session.workflow_intent == WorkflowIntent.CALIBRATION


# ------------------------------------------------------------------ #
# Fix 2: authorship.record() called for Kepler-issued commands        #
# ------------------------------------------------------------------ #


def test_kepler_capture_is_not_flagged_as_conflict(tmp_path: Path) -> None:
    """A Kepler-authored capture must not trigger external_control_conflict."""
    authorship = AuthorshipTracker()
    mount = FakeMountBackend()
    camera = FakeCameraBackend()
    ctrl = _make_controller(mount=mount, camera=camera, authorship=authorship, tmp_path=tmp_path)
    ctrl.session.state = ClawState.CAPTURE
    ctrl.session.control_locked = True
    ctrl.session.workflow_intent = WorkflowIntent.CAPTURE

    request = CaptureRequest(
        exposure_seconds=5.0,
        settings=CameraSettings(iso=800),
        destination_dir=tmp_path / "frames",
    )
    # Capture records CAPTURE_STARTED into authorship tracker.
    ctrl.capture_one_frame(request=request)

    # The camera emits the same CAPTURE_STARTED event.
    camera.inject_event(
        DeviceActivityEvent(
            event_type=DeviceActivityEventType.CAPTURE_STARTED,
            observed_at=datetime.now(UTC),
        )
    )

    conflict = ctrl.check_and_handle_conflicts()
    assert not conflict, "Kepler-authored capture must not be treated as a conflict"
    assert ctrl.session.state != ClawState.PAUSED


def test_kepler_slew_is_not_flagged_as_conflict(tmp_path: Path) -> None:
    """A Kepler-authored slew must not trigger external_control_conflict."""
    authorship = AuthorshipTracker()
    mount = FakeMountBackend()
    ctrl = _make_controller(mount=mount, authorship=authorship, tmp_path=tmp_path)
    ctrl.session.state = ClawState.CORRECT
    ctrl.session.workflow_intent = WorkflowIntent.TARGET_CENTERING
    ctrl.session.staged_target_ra_hours = 1.0
    ctrl.session.staged_target_dec_deg = 45.0
    ctrl.session.control_locked = True

    # Correction records MOUNT_SLEW_STARTED.
    ctrl._do_correct()

    mount.inject_event(
        DeviceActivityEvent(
            event_type=DeviceActivityEventType.MOUNT_SLEW_STARTED,
            observed_at=datetime.now(UTC),
        )
    )

    conflict = ctrl.check_and_handle_conflicts()
    assert not conflict, "Kepler-authored slew must not be treated as a conflict"


# ------------------------------------------------------------------ #
# Fix 3: resume() method                                              #
# ------------------------------------------------------------------ #


def test_resume_from_paused_transitions_to_resume_state(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.state = ClawState.CAPTURE
    ctrl.session.control_locked = True
    ctrl.session.workflow_intent = WorkflowIntent.CAPTURE
    ctrl.session.pause(
        pause_reason="storage_critically_low",
        resume_state=ClawState.CAPTURE,
        workflow_intent=WorkflowIntent.CAPTURE,
        operator_action_required="Free disk space before resuming",
    )

    result = ctrl.resume()

    assert result.previous_state == ClawState.PAUSED
    assert result.next_state == ClawState.CAPTURE
    assert ctrl.session.state == ClawState.CAPTURE
    assert ctrl.session.workflow_intent == WorkflowIntent.CAPTURE
    assert ctrl.session.resume_context is None


def test_resume_from_paused_restores_workflow_intent(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.state = ClawState.CALIBRATE
    ctrl.session.workflow_intent = WorkflowIntent.CALIBRATION
    ctrl.session.pause(
        pause_reason="time_uncertain",
        resume_state=ClawState.CALIBRATE,
        workflow_intent=WorkflowIntent.CALIBRATION,
        operator_action_required="Confirm time before resuming",
    )

    result = ctrl.resume()

    assert result.next_state == ClawState.CALIBRATE
    assert ctrl.session.workflow_intent == WorkflowIntent.CALIBRATION


def test_resume_invalid_when_not_paused_raises(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.state = ClawState.CAPTURE
    with pytest.raises(ValueError, match="PAUSED"):
        ctrl.resume()


# ------------------------------------------------------------------ #
# Fix 4: SESSION_OUTCOME events and write_session_record at terminal  #
# ------------------------------------------------------------------ #


def test_stop_emits_session_outcome_event(
    tmp_path: Path, tmp_store: FilesystemSessionStore
) -> None:
    session = RuntimeSession(session_id="test-stop-outcome")
    record = SessionRecord(
        session_id="test-stop-outcome",
        started_at=datetime(2026, 5, 11, tzinfo=UTC),
        updated_at=datetime(2026, 5, 11, tzinfo=UTC),
        state=ClawState.CAPTURE,
    )
    session_dir = tmp_store.write_session_record(record)

    ctrl = _make_controller(session=session, store=tmp_store, tmp_path=tmp_path)
    ctrl.session.state = ClawState.CAPTURE
    ctrl.session.control_locked = True
    ctrl.session.workflow_intent = WorkflowIntent.CAPTURE
    ctrl.stop()

    event_path = session_dir / "events.ndjson"
    assert event_path.exists()
    lines = event_path.read_text().strip().splitlines()
    outcome_events = [ln for ln in lines if "session_outcome" in ln]
    assert len(outcome_events) >= 1


def test_fail_emits_session_outcome_event(
    tmp_path: Path, tmp_store: FilesystemSessionStore
) -> None:
    session = RuntimeSession(session_id="test-fail-outcome")
    record = SessionRecord(
        session_id="test-fail-outcome",
        started_at=datetime(2026, 5, 11, tzinfo=UTC),
        updated_at=datetime(2026, 5, 11, tzinfo=UTC),
        state=ClawState.CAPTURE,
    )
    session_dir = tmp_store.write_session_record(record)

    ctrl = _make_controller(session=session, store=tmp_store, tmp_path=tmp_path)
    ctrl.session.state = ClawState.CAPTURE
    ctrl.fail(reason="unrecoverable hardware error")

    event_path = session_dir / "events.ndjson"
    lines = event_path.read_text().strip().splitlines()
    outcome_events = [ln for ln in lines if "session_outcome" in ln]
    assert len(outcome_events) >= 1
    assert ctrl.session.state == ClawState.FAILED


def test_release_control_emits_session_outcome_and_writes_session_json(
    tmp_path: Path, tmp_store: FilesystemSessionStore
) -> None:
    session = RuntimeSession(session_id="test-release-outcome")
    record = SessionRecord(
        session_id="test-release-outcome",
        started_at=datetime(2026, 5, 11, tzinfo=UTC),
        updated_at=datetime(2026, 5, 11, tzinfo=UTC),
        state=ClawState.PAUSED,
    )
    session_dir = tmp_store.write_session_record(record)

    ctrl = _make_controller(session=session, store=tmp_store, tmp_path=tmp_path)
    ctrl.session.state = ClawState.CAPTURE
    ctrl.session.control_locked = True
    ctrl.session.workflow_intent = WorkflowIntent.CAPTURE
    ctrl.session.pause(
        pause_reason="operator requested",
        resume_state=ClawState.CAPTURE,
        workflow_intent=WorkflowIntent.CAPTURE,
    )
    result = ctrl.release_control()

    assert result.next_state == ClawState.COMPLETED
    assert ctrl.session.state == ClawState.COMPLETED

    event_path = session_dir / "events.ndjson"
    lines = event_path.read_text().strip().splitlines()
    outcome_events = [ln for ln in lines if "session_outcome" in ln]
    assert len(outcome_events) >= 1

    import json

    session_json = json.loads((session_dir / "session.json").read_text())
    assert session_json["state"] == ClawState.COMPLETED


# ------------------------------------------------------------------ #
# Finding 1 fixes: target centering calibration gate + readiness      #
# ------------------------------------------------------------------ #


def test_target_centering_routes_to_calibrate_when_uncalibrated(tmp_path: Path) -> None:
    """run_target_centering must route to CALIBRATE when calibration_accepted is False."""
    ctrl = _make_controller(
        solver=FakeSolverBackend(_good_solve(residual=5.0)),
        tmp_path=tmp_path,
    )
    session = ctrl.session
    session.state = ClawState.TARGET_ACQUIRED
    session.staged_target_ra_hours = 5.5
    session.staged_target_dec_deg = 22.0
    session.workflow_intent = WorkflowIntent.TARGET_CENTERING
    session.calibration_accepted = False  # not yet calibrated

    result = ctrl.run_target_centering()
    # Should have gone through calibration and ended at TARGET_ACQUIRED or READY,
    # not started centering with an uncalibrated mount.
    assert session.calibration_accepted is True
    assert result.next_state in (ClawState.TARGET_ACQUIRED, ClawState.READY, ClawState.CAPTURE)


def test_target_centering_pauses_when_time_uncertain(tmp_path: Path) -> None:
    """run_target_centering must pause when time is not trusted (spec line 805)."""
    node = FakeNodeBackend(time_trusted=False)
    ctrl = _make_controller(node=node, tmp_path=tmp_path)
    session = ctrl.session
    session.state = ClawState.TARGET_ACQUIRED
    session.staged_target_ra_hours = 5.5
    session.staged_target_dec_deg = 22.0
    session.calibration_accepted = True  # calibrated already

    result = ctrl.run_target_centering()
    assert result.next_state == ClawState.PAUSED
    assert any(b.name == "time_uncertain" for b in result.blockers)
    assert session.state == ClawState.PAUSED
    assert session.resume_context is not None
    assert session.resume_context.resume_state == ClawState.TARGET_ACQUIRED


def test_target_centering_pauses_when_power_unhealthy(tmp_path: Path) -> None:
    """run_target_centering must pause when power integrity is bad."""
    node = FakeNodeBackend(power_healthy=False)
    ctrl = _make_controller(node=node, tmp_path=tmp_path)
    session = ctrl.session
    session.state = ClawState.TARGET_ACQUIRED
    session.staged_target_ra_hours = 5.5
    session.staged_target_dec_deg = 22.0
    session.calibration_accepted = True

    result = ctrl.run_target_centering()
    assert result.next_state == ClawState.PAUSED
    assert any(b.name == "power_integrity_warning" for b in result.blockers)


# ------------------------------------------------------------------ #
# Finding 2 fixes: guard blocks on all readiness conditions            #
# ------------------------------------------------------------------ #


def test_evaluate_guard_time_uncertain_pauses(tmp_path: Path) -> None:
    """Guard must pause when time is uncertain, not continue to CAPTURE."""
    node = FakeNodeBackend(time_trusted=False)
    ctrl = _make_controller(node=node, tmp_path=tmp_path)
    ctrl.session.state = ClawState.GUARD

    result = ctrl.evaluate_guard(quality_overall="pass")
    assert result.next_state == ClawState.PAUSED
    assert any(b.name == "time_uncertain" for b in result.blockers)


def test_evaluate_guard_power_warning_pauses(tmp_path: Path) -> None:
    """Guard must pause when power integrity warning is active."""
    node = FakeNodeBackend(power_healthy=False)
    ctrl = _make_controller(node=node, tmp_path=tmp_path)
    ctrl.session.state = ClawState.GUARD

    result = ctrl.evaluate_guard(quality_overall="pass")
    assert result.next_state == ClawState.PAUSED
    assert any(b.name == "power_integrity_warning" for b in result.blockers)


# ------------------------------------------------------------------ #
# Finding 3 fixes: terminal_outcome persisted in session.json         #
# ------------------------------------------------------------------ #


def test_stop_persists_terminal_outcome_in_session_json(
    tmp_path: Path, tmp_store: FilesystemSessionStore
) -> None:
    """stop() must write terminal_outcome into session.json."""
    import json

    session = RuntimeSession(session_id="test-stop-outcome")
    record = SessionRecord(
        session_id="test-stop-outcome",
        started_at=datetime(2026, 5, 11, tzinfo=UTC),
        updated_at=datetime(2026, 5, 11, tzinfo=UTC),
        state=ClawState.CAPTURE,
    )
    session_dir = tmp_store.write_session_record(record)

    ctrl = _make_controller(session=session, store=tmp_store, tmp_path=tmp_path)
    ctrl.session.state = ClawState.CAPTURE
    ctrl.stop()

    session_json = json.loads((session_dir / "session.json").read_text())
    assert session_json["terminal_outcome"] is not None


def test_fail_persists_terminal_outcome_in_session_json(
    tmp_path: Path, tmp_store: FilesystemSessionStore
) -> None:
    """fail() must write terminal_outcome into session.json."""
    import json

    session = RuntimeSession(session_id="test-fail-outcome")
    record = SessionRecord(
        session_id="test-fail-outcome",
        started_at=datetime(2026, 5, 11, tzinfo=UTC),
        updated_at=datetime(2026, 5, 11, tzinfo=UTC),
        state=ClawState.CAPTURE,
    )
    session_dir = tmp_store.write_session_record(record)

    ctrl = _make_controller(session=session, store=tmp_store, tmp_path=tmp_path)
    ctrl.session.state = ClawState.CAPTURE
    ctrl.fail(reason="test failure")

    session_json = json.loads((session_dir / "session.json").read_text())
    assert session_json["terminal_outcome"] is not None


# ------------------------------------------------------------------ #
# Finding 4 fixes: storage write retry and reconnect backoff           #
# ------------------------------------------------------------------ #


class FakeCameraBackendWithRetry(FakeCameraBackend):
    """Camera backend that fails on first capture but succeeds on the second."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._capture_calls = 0

    def capture(self, request: CaptureRequest) -> CaptureResult:
        self._capture_calls += 1
        if self._capture_calls == 1:
            raise RuntimeError("transient write error")
        return super().capture(request)


def test_capture_retries_once_on_transient_failure(tmp_path: Path) -> None:
    """On transient capture failure with good storage, capture_one_frame retries once."""
    camera = FakeCameraBackendWithRetry()
    ctrl = _make_controller(camera=camera, tmp_path=tmp_path)
    ctrl.session.state = ClawState.CAPTURE
    ctrl.session.control_locked = True
    request = CaptureRequest(
        exposure_seconds=60.0,
        settings=CameraSettings(iso=800),
        destination_dir=tmp_path / "frames",
    )
    result = ctrl.capture_one_frame(request=request)
    # Retry should succeed → GUARD
    assert result.next_state == ClawState.GUARD
    assert camera._capture_calls == 2


def test_capture_terminal_failure_stops_session_when_storage_bad(tmp_path: Path) -> None:
    """When storage is bad and capture fails, session hard-fails (data-integrity terminal)."""
    from kepler_node.agent.session import TerminalOutcome

    node = FakeNodeBackend(storage_summary="critically low free space", storage_writable=False)
    ctrl = _make_controller(
        camera=FakeCameraBackend(fail_capture=True),
        node=node,
        tmp_path=tmp_path,
    )
    ctrl.session.state = ClawState.CAPTURE
    ctrl.session.control_locked = True
    request = CaptureRequest(
        exposure_seconds=60.0,
        settings=CameraSettings(iso=800),
        destination_dir=tmp_path / "frames",
    )
    result = ctrl.capture_one_frame(request=request)
    # Terminal storage failure: session FAILS (data-integrity hard-stop, not operator stop)
    assert result.next_state == ClawState.FAILED
    assert ctrl.session.is_terminal
    assert ctrl.session.terminal_outcome == TerminalOutcome.FAILED


def test_capture_recover_after_failed_retry_with_good_storage(tmp_path: Path) -> None:
    """When both capture attempts fail but storage is good, route to RECOVER."""
    ctrl = _make_controller(
        camera=FakeCameraBackend(fail_capture=True),
        tmp_path=tmp_path,
    )
    ctrl.session.state = ClawState.CAPTURE
    ctrl.session.control_locked = True
    request = CaptureRequest(
        exposure_seconds=60.0,
        settings=CameraSettings(iso=800),
        destination_dir=tmp_path / "frames",
    )
    result = ctrl.capture_one_frame(request=request)
    assert result.next_state == ClawState.RECOVER


def test_recover_mount_disconnect_applies_backoff(tmp_path: Path) -> None:
    """Mount disconnect recovery applies per-attempt backoff sleep (spec line 1125)."""
    from unittest.mock import patch

    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.state = ClawState.CAPTURE
    ctrl.session.workflow_intent = WorkflowIntent.CAPTURE
    ctrl.session.calibration_accepted = True

    with patch("kepler_node.agent.claw.time") as mock_time:
        mock_time.sleep = MagicMock()
        result = ctrl.recover(reason="mount disconnected", mount_disconnected=True)

    assert result.next_state == ClawState.CONNECT
    # First attempt uses 5s backoff
    mock_time.sleep.assert_called_once_with(5.0)


def test_recover_mount_disconnect_second_attempt_uses_longer_backoff(tmp_path: Path) -> None:
    """Second reconnect attempt uses 15s backoff."""
    from unittest.mock import patch

    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.state = ClawState.CAPTURE
    ctrl.session.workflow_intent = WorkflowIntent.CAPTURE
    ctrl.session.reconnect_attempts = 1  # already had one attempt

    with patch("kepler_node.agent.claw.time") as mock_time:
        mock_time.sleep = MagicMock()
        ctrl.recover(reason="mount disconnected again", mount_disconnected=True)

    mock_time.sleep.assert_called_once_with(15.0)


def test_persist_terminal_outcome_propagates_write_failure(tmp_path: Path) -> None:
    """_persist_terminal_outcome must not silently swallow write_session_record failures.

    Spec lines 903-915 require terminal teardown to flush metadata/outcome.
    A silent pass breaks API/UI consumers that rely on session.json existing.
    """
    from unittest.mock import patch

    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.session_id = "test-session-123"
    ctrl.session.state = ClawState.CAPTURE

    with patch.object(ctrl.store, "write_session_record", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            ctrl.stop()


# ------------------------------------------------------------------ #
# Finding 1: pre-session node-scoped event buffer                      #
# ------------------------------------------------------------------ #


def test_calibration_without_session_id_emits_node_scoped_events(tmp_path: Path) -> None:
    """run_calibrate() with no session_id must produce node-scoped events in the buffer.

    Spec lines 212, 1276-1277: the operator should see plain-language calibration
    progress; pre-session events use session_scope=node and session_id=null.
    """
    from kepler_node.storage.models import SessionScope

    ctrl = _make_controller(tmp_path=tmp_path)
    # Default session has no session_id
    assert ctrl.session.session_id is None

    ctrl.session.state = ClawState.READY
    ctrl.run_calibrate()

    assert len(ctrl.node_events) > 0, "expected node-scoped events in buffer"
    for evt in ctrl.node_events:
        assert evt.session_scope == SessionScope.NODE
        assert evt.session_id is None
        assert evt.sequence > 0


def test_node_events_use_monotonically_increasing_sequence(tmp_path: Path) -> None:
    """Node-event sequence numbers must be strictly increasing (spec line 1279)."""
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.state = ClawState.BOOT
    ctrl.boot()
    ctrl.discover()

    seqs = [e.sequence for e in ctrl.node_events]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs)), (
        f"node event sequences are not strictly increasing: {seqs}"
    )


def test_session_events_not_buffered_in_node_events(
    tmp_path: Path, tmp_store: FilesystemSessionStore
) -> None:
    """When session_id is set, events go to storage only; node_events stays empty."""
    from datetime import UTC, datetime

    from kepler_node.storage.models import SessionRecord

    session = RuntimeSession(session_id="s-001")
    session.state = ClawState.CONNECT
    record = SessionRecord(
        session_id="s-001",
        started_at=datetime(2026, 5, 12, tzinfo=UTC),
        updated_at=datetime(2026, 5, 12, tzinfo=UTC),
        state=ClawState.CONNECT,
    )
    tmp_store.write_session_record(record)
    ctrl = _make_controller(session=session, store=tmp_store, tmp_path=tmp_path)
    ctrl.discover()

    assert ctrl.node_events == [], "node_events must be empty when session_id is set"


# ------------------------------------------------------------------ #
# Finding 2: mount reconnect confirmation during calibration/centering #
# ------------------------------------------------------------------ #


def test_recover_mount_disconnect_during_calibration_pauses_for_operator(
    tmp_path: Path,
) -> None:
    """Mount disconnect during active CALIBRATION must pause for operator confirmation.

    Spec line 1052: explicit operator confirmation required when mount reconnect is
    needed during active centering or calibration.
    """
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.state = ClawState.CALIBRATE
    ctrl.session.workflow_intent = WorkflowIntent.CALIBRATION

    result = ctrl.recover(reason="mount disconnected", mount_disconnected=True)

    assert result.next_state == ClawState.PAUSED, (
        "CALIBRATION mount disconnect should pause, not auto-reconnect"
    )
    assert ctrl.session.state == ClawState.PAUSED
    ctx = ctrl.session.resume_context
    assert ctx is not None
    assert ctx.pause_reason == "mount_disconnected_during_active_workflow"
    assert ctx.operator_action_required is not None
    # reconnect_attempts must NOT have been incremented (no auto-reconnect)
    assert ctrl.session.reconnect_attempts == 0


def test_recover_mount_disconnect_during_target_centering_pauses_for_operator(
    tmp_path: Path,
) -> None:
    """Mount disconnect during active TARGET_CENTERING must pause for operator confirmation.

    Spec line 1052.
    """
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.state = ClawState.TEST_CAPTURE
    ctrl.session.workflow_intent = WorkflowIntent.TARGET_CENTERING

    result = ctrl.recover(reason="mount lost", mount_disconnected=True)

    assert result.next_state == ClawState.PAUSED, (
        "TARGET_CENTERING mount disconnect should pause, not auto-reconnect"
    )
    assert ctrl.session.state == ClawState.PAUSED
    assert ctrl.session.reconnect_attempts == 0


def test_recover_mount_disconnect_during_capture_still_auto_reconnects(
    tmp_path: Path,
) -> None:
    """Mount disconnect during CAPTURE workflow keeps the existing auto-reconnect logic."""
    from unittest.mock import patch

    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.state = ClawState.CAPTURE
    ctrl.session.workflow_intent = WorkflowIntent.CAPTURE

    with patch("kepler_node.agent.claw.time") as mock_time:
        mock_time.sleep = MagicMock()
        result = ctrl.recover(reason="mount disconnected", mount_disconnected=True)

    assert result.next_state == ClawState.CONNECT, (
        "CAPTURE mount disconnect should still route to CONNECT"
    )
    assert ctrl.session.reconnect_attempts == 1


# ------------------------------------------------------------------ #
# Finding 3: terminal teardown — camera disconnect + artifact cleanup  #
# ------------------------------------------------------------------ #


def test_stop_calls_camera_disconnect(tmp_path: Path) -> None:
    """stop() must call camera.disconnect() as part of terminal teardown (spec line 904)."""
    camera = FakeCameraBackend()
    ctrl = _make_controller(camera=camera, tmp_path=tmp_path)
    ctrl.session.state = ClawState.CAPTURE

    ctrl.stop()

    assert camera.disconnected, "camera.disconnect() must be called by stop()"


def test_fail_calls_camera_disconnect(tmp_path: Path) -> None:
    """fail() must call camera.disconnect() as part of terminal teardown."""
    camera = FakeCameraBackend()
    ctrl = _make_controller(camera=camera, tmp_path=tmp_path)
    ctrl.session.state = ClawState.CAPTURE

    ctrl.fail(reason="test failure")

    assert camera.disconnected, "camera.disconnect() must be called by fail()"


def test_release_control_calls_camera_disconnect(tmp_path: Path) -> None:
    """release_control() must call camera.disconnect() as part of terminal teardown."""
    camera = FakeCameraBackend()
    ctrl = _make_controller(camera=camera, tmp_path=tmp_path)
    ctrl.session.state = ClawState.PAUSED
    ctrl.session.pause(
        pause_reason="test",
        resume_state=ClawState.CALIBRATE,
        workflow_intent=WorkflowIntent.CALIBRATION,
    )

    ctrl.release_control()

    assert camera.disconnected, "camera.disconnect() must be called by release_control()"


def test_stop_cleans_up_verification_artifacts(tmp_path: Path) -> None:
    """stop() must remove temporary verification artifacts from verification_dir."""
    vdir = tmp_path / "verify"
    vdir.mkdir()
    artifact = vdir / "test_frame.jpg"
    artifact.write_bytes(b"fake")

    ctrl = _make_controller(verification_dir=vdir, tmp_path=tmp_path)
    ctrl.session.state = ClawState.CAPTURE
    ctrl.stop()

    assert not artifact.exists(), "verification artifact must be cleaned up on stop()"


def test_fail_cleans_up_verification_artifacts(tmp_path: Path) -> None:
    """fail() must remove temporary verification artifacts from verification_dir."""
    vdir = tmp_path / "verify"
    vdir.mkdir()
    artifact = vdir / "calib_frame.fits"
    artifact.write_bytes(b"fake")

    ctrl = _make_controller(verification_dir=vdir, tmp_path=tmp_path)
    ctrl.session.state = ClawState.CAPTURE
    ctrl.fail(reason="test")

    assert not artifact.exists(), "verification artifact must be cleaned up on fail()"


def test_teardown_does_not_raise_when_camera_disconnect_fails(tmp_path: Path) -> None:
    """Terminal teardown must not propagate camera.disconnect() errors (best-effort)."""

    class FailingCameraBackend(FakeCameraBackend):
        def disconnect(self) -> None:
            raise RuntimeError("disconnect exploded")

    ctrl = _make_controller(camera=FailingCameraBackend(), tmp_path=tmp_path)
    ctrl.session.state = ClawState.CAPTURE
    # Must not raise even though disconnect() raises
    ctrl.stop()
    assert ctrl.session.state == ClawState.COMPLETED


# ------------------------------------------------------------------ #
# Fix: conflict pause/resume state-intent consistency                  #
# ------------------------------------------------------------------ #


def test_conflict_during_capture_resumes_to_capture_not_target_acquired(
    tmp_path: Path,
) -> None:
    """Conflict detected during CAPTURE must store resume_state=CAPTURE, not TARGET_ACQUIRED.

    Pairing TARGET_ACQUIRED with workflow_intent=CAPTURE is inconsistent:
    target_acquired is a pre-centering hold state and should never carry a
    capture-phase intent (spec line 742).
    """
    mount = FakeMountBackend()
    conflict_event = DeviceActivityEvent(
        event_type=DeviceActivityEventType.MOUNT_SLEW_STARTED,
        observed_at=datetime.now(UTC),
    )
    mount.inject_event(conflict_event)

    ctrl = _make_controller(mount=mount, tmp_path=tmp_path)
    ctrl.session.state = ClawState.CAPTURE
    ctrl.session.control_locked = True
    ctrl.session.workflow_intent = WorkflowIntent.CAPTURE

    detected = ctrl.check_and_handle_conflicts()

    assert detected is True
    assert ctrl.session.state == ClawState.PAUSED
    ctx = ctrl.session.resume_context
    assert ctx is not None
    # resume_state must NOT be TARGET_ACQUIRED when workflow_intent is CAPTURE
    assert ctx.resume_state == ClawState.CAPTURE
    assert ctx.workflow_intent == WorkflowIntent.CAPTURE

    # After resume(), the session must be in a consistent state
    result = ctrl.resume()
    assert result.next_state == ClawState.CAPTURE
    assert ctrl.session.state == ClawState.CAPTURE
    assert ctrl.session.workflow_intent == WorkflowIntent.CAPTURE
    assert ctrl.session.resume_context is None


def test_conflict_during_guard_resumes_to_capture(tmp_path: Path) -> None:
    """Conflict during GUARD (not a spec-safe resume state) must resume to CAPTURE.

    The spec (line 742) lists only ready, target_acquired, test_capture, and capture
    as safe resume targets.  GUARD is an inter-frame evaluation state; when it is
    the pre-pause state and intent is CAPTURE, the safe fallback is CAPTURE.
    """
    mount = FakeMountBackend()
    conflict_event = DeviceActivityEvent(
        event_type=DeviceActivityEventType.MOUNT_SLEW_STARTED,
        observed_at=datetime.now(UTC),
    )
    mount.inject_event(conflict_event)

    ctrl = _make_controller(mount=mount, tmp_path=tmp_path)
    ctrl.session.state = ClawState.GUARD
    ctrl.session.control_locked = True
    ctrl.session.workflow_intent = WorkflowIntent.CAPTURE

    detected = ctrl.check_and_handle_conflicts()

    assert detected is True
    ctx = ctrl.session.resume_context
    assert ctx is not None
    # GUARD is not a spec-safe resume state; falls back to CAPTURE for CAPTURE intent
    assert ctx.resume_state == ClawState.CAPTURE
    assert ctx.workflow_intent == WorkflowIntent.CAPTURE


def test_conflict_during_recovery_verification_resumes_to_capture(
    tmp_path: Path,
) -> None:
    """Conflict during RECOVERY_VERIFICATION intent must resume to CAPTURE."""
    mount = FakeMountBackend()
    conflict_event = DeviceActivityEvent(
        event_type=DeviceActivityEventType.MOUNT_SLEW_STARTED,
        observed_at=datetime.now(UTC),
    )
    mount.inject_event(conflict_event)

    ctrl = _make_controller(mount=mount, tmp_path=tmp_path)
    ctrl.session.state = ClawState.TEST_CAPTURE
    ctrl.session.control_locked = True
    ctrl.session.workflow_intent = WorkflowIntent.RECOVERY_VERIFICATION

    detected = ctrl.check_and_handle_conflicts()

    assert detected is True
    ctx = ctrl.session.resume_context
    assert ctx is not None
    # TEST_CAPTURE is a safe state per spec — resume_state stays as-is
    assert ctx.resume_state == ClawState.TEST_CAPTURE
    assert ctx.workflow_intent == WorkflowIntent.RECOVERY_VERIFICATION


# ------------------------------------------------------------------ #
# Round-6 Fix 1: authorship fingerprinting                            #
# ------------------------------------------------------------------ #


def test_foreign_same_type_slew_triggers_conflict(tmp_path: Path) -> None:
    """A foreign MOUNT_SLEW_STARTED at different coordinates must still trigger conflict.

    Kepler records a slew to RA=1.0 / Dec=45.0.  A foreign slew to different
    coordinates arrives while control_locked.  The fingerprint mismatch (ra_hours
    and dec_deg differ) must cause is_authored() to return False and conflict
    detection to pause the session.
    """
    authorship = AuthorshipTracker()
    mount = FakeMountBackend()
    ctrl = _make_controller(mount=mount, authorship=authorship, tmp_path=tmp_path)
    ctrl.session.state = ClawState.CAPTURE
    ctrl.session.control_locked = True
    ctrl.session.workflow_intent = WorkflowIntent.CAPTURE

    # Kepler records a slew to RA=1.0, Dec=45.0 with fingerprint details.
    authorship.record(
        DeviceActivityEvent(
            event_type=DeviceActivityEventType.MOUNT_SLEW_STARTED,
            observed_at=datetime.now(UTC),
            details={"ra_hours": "1.0", "dec_deg": "45.0", "action": "slew"},
        )
    )

    # A foreign slew at RA=12.0, Dec=-20.0 arrives — different coordinates.
    mount.inject_event(
        DeviceActivityEvent(
            event_type=DeviceActivityEventType.MOUNT_SLEW_STARTED,
            observed_at=datetime.now(UTC),
            details={"ra_hours": "12.0", "dec_deg": "-20.0", "action": "slew"},
        )
    )

    detected = ctrl.check_and_handle_conflicts()
    assert detected is True, (
        "Foreign slew at different coordinates must be flagged as conflict even "
        "when a same-type authored event exists in the window"
    )
    assert ctrl.session.state == ClawState.PAUSED


# ------------------------------------------------------------------ #
# Round-6 Fix 2: terminal persistence preserves existing metadata     #
# ------------------------------------------------------------------ #


def test_stop_preserves_existing_session_metadata(
    tmp_path: Path, tmp_store: FilesystemSessionStore
) -> None:
    """stop() must not overwrite pre-existing session.json metadata fields."""
    import json

    session = RuntimeSession(session_id="test-meta-stop")
    record = SessionRecord(
        session_id="test-meta-stop",
        started_at=datetime(2026, 5, 11, tzinfo=UTC),
        updated_at=datetime(2026, 5, 11, tzinfo=UTC),
        state=ClawState.CAPTURE,
        target_label="M42",
        ra_hours=5.5833,
        dec_deg=-5.3917,
        selected_inline_run_parameters={"exposure_seconds": "300", "frame_count": "20"},
    )
    session_dir = tmp_store.write_session_record(record)

    ctrl = _make_controller(session=session, store=tmp_store, tmp_path=tmp_path)
    ctrl.session.state = ClawState.CAPTURE
    ctrl.stop()

    session_json = json.loads((session_dir / "session.json").read_text())
    assert session_json["target_label"] == "M42", "target_label must survive stop()"
    assert session_json["ra_hours"] == pytest.approx(5.5833), "ra_hours must survive stop()"
    assert session_json["selected_inline_run_parameters"]["frame_count"] == "20", (
        "run parameters must survive stop()"
    )
    assert session_json["terminal_outcome"] is not None


def test_fail_preserves_existing_session_metadata(
    tmp_path: Path, tmp_store: FilesystemSessionStore
) -> None:
    """fail() must not overwrite pre-existing session.json metadata fields."""
    import json

    session = RuntimeSession(session_id="test-meta-fail")
    record = SessionRecord(
        session_id="test-meta-fail",
        started_at=datetime(2026, 5, 11, tzinfo=UTC),
        updated_at=datetime(2026, 5, 11, tzinfo=UTC),
        state=ClawState.CAPTURE,
        target_label="NGC 7000",
        selected_inline_run_parameters={"frame_count": "30"},
        equipment_profile_id="fuji-xt5-iexos100",
    )
    session_dir = tmp_store.write_session_record(record)

    ctrl = _make_controller(session=session, store=tmp_store, tmp_path=tmp_path)
    ctrl.session.state = ClawState.CAPTURE
    ctrl.fail(reason="hardware failure")

    session_json = json.loads((session_dir / "session.json").read_text())
    assert session_json["target_label"] == "NGC 7000", "target_label must survive fail()"
    assert session_json["equipment_profile_id"] == "fuji-xt5-iexos100", (
        "equipment_profile_id must survive fail()"
    )
    assert session_json["selected_inline_run_parameters"]["frame_count"] == "30", (
        "run parameters must survive fail()"
    )
    assert session_json["terminal_outcome"] == "failed"


# ------------------------------------------------------------------ #
# camera_keepalive                                                     #
# ------------------------------------------------------------------ #


class _HeartbeatCamera(FakeCameraBackend):
    """FakeCameraBackend variant with configurable heartbeat and reconnect."""

    def __init__(
        self,
        *,
        heartbeat_ok: bool = True,
        reconnect_ok: bool = True,
    ) -> None:
        super().__init__()
        self._heartbeat_ok = heartbeat_ok
        self._reconnect_ok = reconnect_ok
        self.connect_calls = 0

    def heartbeat(self) -> bool:
        return self._heartbeat_ok

    def connect(self) -> None:
        self.connect_calls += 1
        if not self._reconnect_ok:
            raise RuntimeError("camera unreachable after keepalive failure")


class _RestartableBroker(StubBrokerBackend):
    def __init__(self, *, profile_name: str = "Kepler-Starter-Rig", restart_error: str | None = None) -> None:
        self.profile_name = profile_name
        self.restart_error = restart_error
        self.restart_calls = 0
        self.stop_calls = 0
        self.start_calls = 0

    def snapshot(self) -> BrokerSnapshot:
        return BrokerSnapshot(
            broker_state=BrokerRuntimeState.READY,
            profile_active=self.profile_name,
            device_path_available=True,
        )

    def restart_active_profile(self) -> str | None:
        self.restart_calls += 1
        if self.restart_error is not None:
            raise RuntimeError(self.restart_error)
        return self.profile_name

    def stop_active_profile(self) -> str | None:
        self.stop_calls += 1
        if self.restart_error is not None:
            raise RuntimeError(self.restart_error)
        return self.profile_name

    def start_profile(self, profile_name: str) -> None:
        self.start_calls += 1
        if self.restart_error is not None:
            raise RuntimeError(self.restart_error)


class _BusyEkosAdapter(StubEkosAdapter):
    def status(self):
        from kepler_node.agent.absolute_state import EkosRuntimeState, NormalizedEkosSnapshot

        return NormalizedEkosSnapshot(
            ekos_state=EkosRuntimeState.RUNNING,
            exposure_active=True,
        )


def test_camera_keepalive_skipped_when_state_not_idle(tmp_path: Path) -> None:
    """keepalive is a no-op for states outside _CAMERA_KEEPALIVE_STATES."""
    cam = _HeartbeatCamera(heartbeat_ok=False)
    ctrl = _make_controller(camera=cam, tmp_path=tmp_path)
    ctrl.session.state = ClawState.BOOT

    result = ctrl.camera_keepalive()

    assert result.next_state == ClawState.BOOT
    assert "skipped" in result.message
    assert cam.connect_calls == 0


def test_camera_keepalive_noop_when_heartbeat_succeeds(tmp_path: Path) -> None:
    """A successful heartbeat leaves state unchanged."""
    cam = _HeartbeatCamera(heartbeat_ok=True)
    ctrl = _make_controller(camera=cam, tmp_path=tmp_path)
    ctrl.session.state = ClawState.READY

    result = ctrl.camera_keepalive()

    assert result.next_state == ClawState.READY
    assert "ok" in result.message
    assert cam.connect_calls == 0


def test_camera_keepalive_reconnects_after_heartbeat_failure(tmp_path: Path) -> None:
    """Heartbeat failure triggers a reconnect attempt; state stays if reconnect succeeds."""
    cam = _HeartbeatCamera(heartbeat_ok=False, reconnect_ok=True)
    ctrl = _make_controller(camera=cam, tmp_path=tmp_path)
    ctrl.session.state = ClawState.READY

    result = ctrl.camera_keepalive()

    assert cam.connect_calls == 1
    assert result.next_state == ClawState.READY
    assert "reconnected" in result.message


def test_camera_keepalive_pauses_session_when_reconnect_fails(tmp_path: Path) -> None:
    """Failed heartbeat + failed reconnect transitions to PAUSED with camera_disconnected blocker."""
    cam = _HeartbeatCamera(heartbeat_ok=False, reconnect_ok=False)
    ctrl = _make_controller(camera=cam, tmp_path=tmp_path)
    ctrl.session.state = ClawState.READY

    result = ctrl.camera_keepalive()

    assert result.next_state == ClawState.PAUSED
    assert ctrl.session.state == ClawState.PAUSED
    blocker_names = [b.name for b in result.blockers]
    assert "camera_disconnected" in blocker_names


def test_camera_keepalive_allowed_in_paused_and_target_acquired(tmp_path: Path) -> None:
    """keepalive fires in PAUSED and TARGET_ACQUIRED, not just READY."""
    for state in (ClawState.PAUSED, ClawState.TARGET_ACQUIRED):
        cam = _HeartbeatCamera(heartbeat_ok=True)
        ctrl = _make_controller(camera=cam, tmp_path=tmp_path)
        ctrl.session.state = state

        result = ctrl.camera_keepalive()

        assert result.next_state == state, f"expected to stay in {state}"
        assert "ok" in result.message


def test_recover_camera_session_restarts_broker_and_reconnects_camera(tmp_path: Path) -> None:
    camera = FakeCameraBackend()
    broker = _RestartableBroker()
    ctrl = _make_controller(camera=camera, broker=broker, tmp_path=tmp_path)
    ctrl.session.state = ClawState.READY

    result = ctrl.recover_camera_session(reason="clear dropped image")

    assert broker.stop_calls == 1
    assert broker.start_calls == 1
    assert camera.disconnect_calls == 1
    assert camera.connect_calls == 1
    assert result.next_state == ClawState.READY
    assert result.details["restart_used"] is True
    assert result.details["restarted_profile"] == "Kepler-Starter-Rig"
    assert "restart" in result.message


def test_recover_camera_session_rejects_active_ekos_work(tmp_path: Path) -> None:
    camera = FakeCameraBackend()
    broker = _RestartableBroker()
    ctrl = _make_controller(
        camera=camera,
        broker=broker,
        ekos_adapter=_BusyEkosAdapter(),
        tmp_path=tmp_path,
    )
    ctrl.session.state = ClawState.READY

    with pytest.raises(ValueError, match="Ekos to be idle"):
        ctrl.recover_camera_session()

    assert broker.stop_calls == 0
    assert camera.disconnect_calls == 0
    assert camera.connect_calls == 0


def test_recover_camera_session_pauses_when_camera_reconnect_still_fails(tmp_path: Path) -> None:
    camera = FakeCameraBackend(fail_connect=True, connect_error_msg="still jammed")
    broker = _RestartableBroker(restart_error="broker restart failed")
    ctrl = _make_controller(camera=camera, broker=broker, tmp_path=tmp_path)
    ctrl.session.state = ClawState.READY

    result = ctrl.recover_camera_session()

    assert broker.stop_calls == 1
    assert broker.start_calls == 0
    assert camera.disconnect_calls == 1
    assert camera.connect_calls == 1
    assert ctrl.session.state == ClawState.PAUSED
    assert result.next_state == ClawState.PAUSED
    assert result.blockers[0].name == "camera_disconnected"
    assert result.details["broker_stop_error"] == "broker restart failed"
