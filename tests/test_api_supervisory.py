"""Tests for supervisory API endpoints: planner-mode, session/attach, intervention."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

from fastapi.testclient import TestClient

from kepler_node.agent.absolute_state import EkosRuntimeState, NormalizedEkosSnapshot
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
    RuntimeSession,
)
from kepler_node.api.app import build_app
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
    InstallManifest,
)

# ------------------------------------------------------------------ #
# Fake adapters                                                        #
# ------------------------------------------------------------------ #


class _FakeNode:
    def network_mode(self) -> NetworkMode:
        return NetworkMode.FIELD_HOTSPOT

    def service_health(self) -> list[ServiceHealth]:
        return []

    def time_status(self) -> TimeStatus:
        return TimeStatus(trusted=True, source=TimeSource.NETWORK, summary="ok")

    def storage_status(self) -> StorageStatus:
        return StorageStatus(
            data_root=Path("/tmp"),
            free_bytes=10_000_000_000,
            total_bytes=100_000_000_000,
            writable=True,
            summary="ok",
        )

    def power_status(self) -> PowerStatus:
        return PowerStatus(healthy=True, summary="ok")

    def confirm_time(self, timestamp: datetime) -> TimeStatus:
        return self.time_status()


class _FakeMount:
    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def current_position(self) -> MountPosition:
        return MountPosition(ra_hours=0.0, dec_deg=0.0)

    def slew_to(self, p: MountPosition) -> None:
        pass

    def sync_to(self, p: MountPosition) -> None:
        pass

    def poll_activity(self) -> None:
        pass

    def activity_events(self) -> Iterable[DeviceActivityEvent]:
        return iter([])


class _FakeCamera:
    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def heartbeat(self) -> bool:
        return True

    def capture(self, r: CaptureRequest) -> CaptureResult:
        p = r.destination_dir / "f.jpg"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
        return CaptureResult(image_path=p, captured_at=datetime.now(UTC))

    def apply_settings(self, s: CameraSettings) -> CameraSettings:
        return s

    def activity_events(self) -> Iterable[DeviceActivityEvent]:
        return iter([])


class _FakeSolver:
    def solve(self, image_path: Path, **_: object) -> SolveResult:
        return SolveResult(success=True, solved_at=datetime.now(UTC))


class _EkosRunning(StubEkosAdapter):
    def status(self) -> NormalizedEkosSnapshot:
        return NormalizedEkosSnapshot(
            ekos_state=EkosRuntimeState.RUNNING,
            sequence_exists=True,
            snapshot_at=datetime.now(UTC),
        )

    def pause(self) -> bool:
        return True


class _EkosUnavailable(StubEkosAdapter):
    def status(self) -> NormalizedEkosSnapshot:
        return NormalizedEkosSnapshot(ekos_state=EkosRuntimeState.UNAVAILABLE)


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


def _make_profile() -> EquipmentProfile:
    return EquipmentProfile(
        profile_id="test-profile",
        display_name="Test",
        is_default=True,
        hardware=EquipmentProfileHardware(
            mount=EquipmentProfileHardwareMount(model="EQ6-R"),
            camera=EquipmentProfileHardwareCamera(make="ZWO", model="ASI294MC"),
            lens=EquipmentProfileHardwareLens(
                model="Rokinon 135mm",
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


def _make_ctrl(
    tmp_path: Path,
    *,
    state: ClawState = ClawState.READY,
    ekos: object | None = None,
    broker: object | None = None,
) -> ClawController:
    base = tmp_path / "kepler"
    base.mkdir(parents=True, exist_ok=True)
    ctrl = ClawController(
        session=RuntimeSession(state=state),
        node_backend=_FakeNode(),
        mount_backend=_FakeMount(),
        camera_backend=_FakeCamera(),
        solver_backend=_FakeSolver(),
        store=FilesystemSessionStore(data_root=base),
        authorship_tracker=AuthorshipTracker(),
        verification_dir=base / "verify",
        test_exposure_seconds=1.0,
        ekos_adapter=ekos or _EkosRunning(),
        broker_backend=broker or StubBrokerBackend(),
    )
    ctrl.active_equipment_profile = _make_profile()
    return ctrl


# ------------------------------------------------------------------ #
# GET /api/v1/planner-mode                                             #
# ------------------------------------------------------------------ #


def test_planner_mode_returns_null_when_no_manifest(tmp_path: Path) -> None:
    ctrl = _make_ctrl(tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.get("/api/v1/planner-mode")
    assert resp.status_code == 200
    data = resp.json()
    assert data["planner_mode"] is None


def test_planner_mode_returns_manifest_bootstrap_profile(tmp_path: Path) -> None:
    ctrl = _make_ctrl(tmp_path)
    ctrl.store.write_install_manifest(
        InstallManifest(
            kepler_version="1.0.0",
            release_id="v1.0.0",
            bootstrap_profile="headless-node",
            installed_at=datetime.now(UTC),
        )
    )

    client = TestClient(build_app(controller=ctrl))
    resp = client.get("/api/v1/planner-mode")
    assert resp.status_code == 200
    data = resp.json()
    assert data["planner_mode"] == "headless-node"


# ------------------------------------------------------------------ #
# POST /api/v1/session/attach                                          #
# ------------------------------------------------------------------ #


def test_session_attach_transitions_to_ekos_wait(tmp_path: Path) -> None:
    ctrl = _make_ctrl(tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.post("/api/v1/session/attach")
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "ekos_wait"
    assert data["control_locked"] is True


def test_session_attach_409_when_not_in_ready(tmp_path: Path) -> None:
    ctrl = _make_ctrl(tmp_path, state=ClawState.BOOT)
    client = TestClient(build_app(controller=ctrl))
    resp = client.post("/api/v1/session/attach")
    assert resp.status_code == 409


def test_session_attach_422_when_ekos_unavailable(tmp_path: Path) -> None:
    ctrl = _make_ctrl(tmp_path, ekos=_EkosUnavailable())
    client = TestClient(build_app(controller=ctrl))
    resp = client.post("/api/v1/session/attach")
    assert resp.status_code == 422


def test_session_attach_422_when_broker_unknown(tmp_path: Path) -> None:
    ctrl = _make_ctrl(tmp_path, broker=_BrokerUnknown())
    client = TestClient(build_app(controller=ctrl))
    resp = client.post("/api/v1/session/attach")
    assert resp.status_code == 422


def test_session_attach_422_when_no_active_profile(tmp_path: Path) -> None:
    ctrl = _make_ctrl(tmp_path)
    ctrl.active_equipment_profile = None
    client = TestClient(build_app(controller=ctrl))
    resp = client.post("/api/v1/session/attach")
    assert resp.status_code == 422
    assert "equipment profile" in resp.json()["detail"].lower()


# ------------------------------------------------------------------ #
# GET /api/v1/session/current/intervention                             #
# ------------------------------------------------------------------ #


def test_intervention_returns_null_when_no_supervised_session(tmp_path: Path) -> None:
    ctrl = _make_ctrl(tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.get("/api/v1/session/current/intervention")
    assert resp.status_code == 200
    assert resp.json() is None


def test_intervention_returns_state_during_supervised_session(tmp_path: Path) -> None:
    ctrl = _make_ctrl(tmp_path)
    ctrl.attach_session()
    assert ctrl.session.state == ClawState.EKOS_WAIT

    client = TestClient(build_app(controller=ctrl))
    resp = client.get("/api/v1/session/current/intervention")
    assert resp.status_code == 200
    data = resp.json()
    assert data is not None
    assert data["active_kind"] is None
    assert data["retry_count"] == 0
    assert "intervention_window" in data


# ------------------------------------------------------------------ #
# GET /api/v1/session/current/state — supervisory fields               #
# ------------------------------------------------------------------ #


def test_session_state_includes_supervisory_next_action(tmp_path: Path) -> None:
    ctrl = _make_ctrl(tmp_path)
    ctrl.attach_session()
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/session/current/state").json()
    assert data["supervisory_next_action"] == "wait_for_ekos_session"
    assert data["state"] == "ekos_wait"


def test_session_state_includes_intervention_summary(tmp_path: Path) -> None:
    ctrl = _make_ctrl(tmp_path)
    ctrl.attach_session()
    ctrl.session.state = ClawState.MONITORING
    ctrl.session.supervisory_next_action = "monitor_ekos_session"
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/session/current/state").json()
    assert data["intervention_summary"] is not None
    assert data["intervention_summary"]["active_kind"] is None


# ------------------------------------------------------------------ #
# GET /api/v1/readiness — supervision_ready field                      #
# ------------------------------------------------------------------ #


def test_readiness_supervision_ready_true_when_in_ready(tmp_path: Path) -> None:
    ctrl = _make_ctrl(tmp_path)
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/readiness").json()
    assert data["supervision_ready"] is True


def test_readiness_supervision_ready_false_when_not_ready_state(tmp_path: Path) -> None:
    ctrl = _make_ctrl(tmp_path, state=ClawState.BOOT)
    ctrl.active_equipment_profile = _make_profile()
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/readiness").json()
    assert data["supervision_ready"] is False


def test_readiness_supervision_ready_false_when_no_profile(tmp_path: Path) -> None:
    ctrl = _make_ctrl(tmp_path)
    ctrl.active_equipment_profile = None
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/readiness").json()
    assert data["supervision_ready"] is False
    blocker_names = [b["name"] for b in data["supervision_blockers"]]
    assert "active_equipment_profile_missing" in blocker_names


def test_readiness_supervision_ready_false_when_ekos_unavailable(tmp_path: Path) -> None:
    ctrl = _make_ctrl(tmp_path, ekos=_EkosUnavailable())
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/readiness").json()
    assert data["supervision_ready"] is False


def test_readiness_supervision_ready_false_when_broker_unknown(tmp_path: Path) -> None:
    ctrl = _make_ctrl(tmp_path, broker=_BrokerUnknown())
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/readiness").json()
    assert data["supervision_ready"] is False


def test_readiness_supervision_ready_false_when_broker_unavailable(tmp_path: Path) -> None:
    ctrl = _make_ctrl(tmp_path, broker=_BrokerUnavailable())
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/readiness").json()
    assert data["supervision_ready"] is False


def test_readiness_supervision_ready_false_when_broker_degraded_no_device(tmp_path: Path) -> None:
    ctrl = _make_ctrl(tmp_path, broker=_BrokerDegradedNoDevice())
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/readiness").json()
    assert data["supervision_ready"] is False


# ------------------------------------------------------------------ #
# GET /api/v1/readiness — supervision_blockers field                   #
# ------------------------------------------------------------------ #


def test_readiness_supervision_blockers_empty_when_ready(tmp_path: Path) -> None:
    ctrl = _make_ctrl(tmp_path)
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/readiness").json()
    assert data["supervision_ready"] is True
    assert data["supervision_blockers"] == []


def test_readiness_supervision_blockers_include_missing_profile(tmp_path: Path) -> None:
    ctrl = _make_ctrl(tmp_path)
    ctrl.active_equipment_profile = None
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/readiness").json()
    assert data["supervision_ready"] is False
    blocker_names = [b["name"] for b in data["supervision_blockers"]]
    assert "active_equipment_profile_missing" in blocker_names


def test_readiness_supervision_blockers_includes_ekos_unavailable(tmp_path: Path) -> None:
    ctrl = _make_ctrl(tmp_path, ekos=_EkosUnavailable())
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/readiness").json()
    assert data["supervision_ready"] is False
    blocker_names = [b["name"] for b in data["supervision_blockers"]]
    assert "ekos_unavailable" in blocker_names


def test_readiness_supervision_blockers_includes_broker_unknown(tmp_path: Path) -> None:
    ctrl = _make_ctrl(tmp_path, broker=_BrokerUnknown())
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/readiness").json()
    assert data["supervision_ready"] is False
    blocker_names = [b["name"] for b in data["supervision_blockers"]]
    assert "broker_unknown" in blocker_names


def test_readiness_supervision_blockers_includes_broker_unavailable(tmp_path: Path) -> None:
    ctrl = _make_ctrl(tmp_path, broker=_BrokerUnavailable())
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/readiness").json()
    assert data["supervision_ready"] is False
    blocker_names = [b["name"] for b in data["supervision_blockers"]]
    assert "broker_unavailable" in blocker_names


def test_readiness_supervision_blockers_includes_broker_degraded_no_device(tmp_path: Path) -> None:
    ctrl = _make_ctrl(tmp_path, broker=_BrokerDegradedNoDevice())
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/readiness").json()
    assert data["supervision_ready"] is False
    blocker_names = [b["name"] for b in data["supervision_blockers"]]
    assert "broker_device_path_unavailable" in blocker_names


def test_readiness_supervision_blockers_empty_when_not_in_ready_state(tmp_path: Path) -> None:
    # When state != READY, supervision_blockers is empty (not checked) but supervision_ready is False.
    ctrl = _make_ctrl(tmp_path, state=ClawState.BOOT)
    ctrl.active_equipment_profile = _make_profile()
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/readiness").json()
    assert data["supervision_ready"] is False
    # Blockers are not evaluated when not in READY state.
    assert data["supervision_blockers"] == []


# ------------------------------------------------------------------ #
# POST /api/v1/session/attach — extended broker gating                 #
# ------------------------------------------------------------------ #


def test_session_attach_422_when_broker_unavailable(tmp_path: Path) -> None:
    ctrl = _make_ctrl(tmp_path, broker=_BrokerUnavailable())
    client = TestClient(build_app(controller=ctrl))
    resp = client.post("/api/v1/session/attach")
    assert resp.status_code == 422


def test_session_attach_422_when_broker_degraded_no_device(tmp_path: Path) -> None:
    ctrl = _make_ctrl(tmp_path, broker=_BrokerDegradedNoDevice())
    client = TestClient(build_app(controller=ctrl))
    resp = client.post("/api/v1/session/attach")
    assert resp.status_code == 422

