"""Tests for GET /api/v1/health, /node/status, and /readiness."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from kepler_node.agent.absolute_state import EkosRuntimeState
from kepler_node.agent.authorship import AuthorshipTracker
from kepler_node.agent.broker import BrokerRuntimeState, BrokerSnapshot, StubBrokerBackend
from kepler_node.agent.claw import ClawController
from kepler_node.agent.ekos import NormalizedEkosSnapshot, StubEkosAdapter
from kepler_node.agent.interfaces import (
    NetworkMode,
    PowerStatus,
    ServiceHealth,
    StorageStatus,
    TimeSource,
    TimeStatus,
)
from kepler_node.agent.session import ClawState, RuntimeSession
from kepler_node.api.app import build_app
from kepler_node.camera.protocols import (
    CameraSettings,
    CaptureRequest,
    CaptureResult,
    FocusCalibrationResult,
)
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
# Shared fake adapters                                                 #
# ------------------------------------------------------------------ #


class FakeNodeBackend:
    def __init__(
        self,
        *,
        time_trusted: bool = True,
        time_source: TimeSource = TimeSource.NETWORK,
        gps_ntp_mismatch_seconds: float | None = None,
        storage_summary: str = "ok",
        storage_writable: bool = True,
        power_healthy: bool = True,
        service_healths: list[ServiceHealth] | None = None,
    ) -> None:
        self._time_trusted = time_trusted
        self._time_source = time_source
        self._gps_ntp_mismatch_seconds = gps_ntp_mismatch_seconds
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
            gps_ntp_mismatch_seconds=self._gps_ntp_mismatch_seconds,
        )

    def storage_status(self) -> StorageStatus:
        return StorageStatus(
            data_root=Path("/tmp"),
            free_bytes=50_000_000_000,
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

    def confirm_time(self, timestamp: datetime) -> TimeStatus:
        return self.time_status()


class FakeMountBackend:
    def __init__(self) -> None:
        self.connected = False

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def current_position(self) -> MountPosition:
        return MountPosition(ra_hours=0.0, dec_deg=0.0)

    def slew_to(self, position: MountPosition) -> None:
        pass

    def sync_to(self, position: MountPosition) -> None:
        pass

    def activity_events(self) -> list:
        return []

    def poll_activity(self) -> None:
        pass


class FakeCameraBackend:
    def __init__(self, diagnostic_status: dict | None = None) -> None:
        self._diagnostic_status = diagnostic_status
        self.diagnostic_calls = 0

    def connect(self, settings: CameraSettings) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def diagnostic_status(self) -> dict | None:
        self.diagnostic_calls += 1
        return self._diagnostic_status

    def capture(self, request: CaptureRequest) -> CaptureResult:
        return CaptureResult(image_path=Path("/tmp/test.jpg"), metadata={})

    def activity_events(self) -> list:
        return []


class FakeFocusCalibrationCamera(FakeCameraBackend):
    def __init__(self) -> None:
        super().__init__()
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.calibration_calls = 0

    def connect(self) -> None:
        self.connect_calls += 1

    def disconnect(self) -> None:
        self.disconnect_calls += 1

    def heartbeat(self) -> bool:
        return True

    def apply_settings(self, settings: CameraSettings) -> CameraSettings:
        return settings

    def calibrate_focus_range(self) -> FocusCalibrationResult:
        self.calibration_calls += 1
        return FocusCalibrationResult(
            profile_id="xf55-200@55mm-mf",
            camera_model="X-T5",
            lens_model="XF55-200mmF3.5-4.8 R LM OIS",
            focal_length_mm=55.0,
            focus_mode="manual",
            raw_min=-428,
            raw_max=10442,
            calibrated_at=datetime(2026, 5, 23),
        )


class FakeFocusBroker(StubBrokerBackend):
    def __init__(self) -> None:
        self.stop_calls = 0
        self.start_calls = 0

    def snapshot(self) -> BrokerSnapshot:
        return BrokerSnapshot(
            broker_state=BrokerRuntimeState.READY,
            profile_active="Kepler-Starter-Rig",
            device_path_available=True,
        )

    def stop_active_profile(self) -> str | None:
        self.stop_calls += 1
        return "Kepler-Starter-Rig"

    def start_profile(self, profile_name: str) -> None:
        self.start_calls += 1


class FakeSolverBackend:
    def solve(self, image_path: Path, **_: object) -> SolveResult:
        return SolveResult(
            success=True,
            ra_hours=10.0,
            dec_deg=45.0,
            residual_arcmin=2.0,
        )


def _make_controller(
    *,
    session: RuntimeSession | None = None,
    node: FakeNodeBackend | None = None,
    camera: FakeCameraBackend | None = None,
    ekos_adapter: object | None = None,
    broker_backend: object | None = None,
    tmp_path: Path,
) -> ClawController:
    base = tmp_path
    base.mkdir(parents=True, exist_ok=True)
    vdir = base / "verify"
    vdir.mkdir(parents=True, exist_ok=True)
    return ClawController(
        session=session or RuntimeSession(),
        node_backend=node or FakeNodeBackend(),
        mount_backend=FakeMountBackend(),
        camera_backend=camera or FakeCameraBackend(),
        solver_backend=FakeSolverBackend(),
        store=FilesystemSessionStore(data_root=base),
        authorship_tracker=AuthorshipTracker(),
        verification_dir=vdir,
        test_exposure_seconds=1.0,
        ekos_adapter=ekos_adapter,
        broker_backend=broker_backend,
    )


def _make_fuji_profile() -> EquipmentProfile:
    return EquipmentProfile(
        profile_id="fuji-rig",
        display_name="Fuji Rig",
        hardware=EquipmentProfileHardware(
            mount=EquipmentProfileHardwareMount(model="iEXOS-100"),
            camera=EquipmentProfileHardwareCamera(make="Fujifilm", model="X-T5"),
            lens=EquipmentProfileHardwareLens(
                model="XF55-200mmF3.5-4.8 R LM OIS",
                is_zoom=True,
                default_focal_length_mm=55.0,
            ),
            gps=EquipmentProfileHardwareGps(),
        ),
        site_defaults=EquipmentProfileSiteDefaults(),
        solving_hints=EquipmentProfileSolvingHints(focal_length_assumption_mm=55.0),
        backend_preferences=EquipmentProfileBackendPreferences(),
        created_at=datetime(2026, 5, 23),
        updated_at=datetime(2026, 5, 23),
    )


# ------------------------------------------------------------------ #
# GET /api/v1/health                                                   #
# ------------------------------------------------------------------ #


def test_health_healthy_with_no_services(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path=tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["services"] == []


def test_health_degraded_when_service_unhealthy(tmp_path: Path) -> None:
    node = FakeNodeBackend(
        service_healths=[ServiceHealth(name="indiserver", healthy=False, summary="inactive")]
    )
    ctrl = _make_controller(node=node, tmp_path=tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "degraded"
    assert data["services"][0]["name"] == "indiserver"
    assert data["services"][0]["status"] == "degraded"


def test_health_includes_updated_at(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path=tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.get("/api/v1/health")
    assert "updated_at" in resp.json()


# ------------------------------------------------------------------ #
# GET /api/v1/node/status                                              #
# ------------------------------------------------------------------ #


def test_node_status_returns_state_and_time(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.state = ClawState.READY
    client = TestClient(build_app(controller=ctrl))
    resp = client.get("/api/v1/node/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "ready"
    assert data["workflow_intent"] is None
    assert data["control_locked"] is False
    assert data["network_mode"] == "field_hotspot"
    assert data["time_certainty"]["trusted"] is True
    assert data["power_integrity"]["healthy"] is True


def test_node_status_reflects_power_issue(tmp_path: Path) -> None:
    node = FakeNodeBackend(power_healthy=False)
    ctrl = _make_controller(node=node, tmp_path=tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.get("/api/v1/node/status")
    assert resp.status_code == 200
    assert resp.json()["power_integrity"]["healthy"] is False


def test_node_status_detected_devices_connected_after_ready(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.state = ClawState.READY
    client = TestClient(build_app(controller=ctrl))
    resp = client.get("/api/v1/node/status")
    assert resp.status_code == 200
    devices = resp.json()["detected_devices"]
    assert devices["mount"]["connected"] is True
    assert devices["camera"]["connected"] is True
    assert devices["mount"]["status"] == "connected"
    assert devices["camera"]["status"] == "connected"


def test_node_status_detected_devices_not_connected_before_connect(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.state = ClawState.DISCOVER
    client = TestClient(build_app(controller=ctrl))
    resp = client.get("/api/v1/node/status")
    assert resp.status_code == 200
    devices = resp.json()["detected_devices"]
    assert devices["mount"]["connected"] is False
    assert devices["camera"]["connected"] is False
    assert devices["mount"]["status"] == "not_initialized"
    assert devices["camera"]["status"] == "not_initialized"


def test_node_status_detected_devices_pending_connect_when_profile_selected(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.state = ClawState.CONNECT
    ctrl.active_equipment_profile = SimpleNamespace(
        profile_id="starter-rig",
        display_name="Starter Rig",
        hardware=SimpleNamespace(
            lens=SimpleNamespace(is_zoom=False, default_focal_length_mm=135),
        ),
        solving_hints=SimpleNamespace(focal_length_assumption_mm=None),
    )
    client = TestClient(build_app(controller=ctrl))
    resp = client.get("/api/v1/node/status")
    assert resp.status_code == 200
    devices = resp.json()["detected_devices"]
    assert devices["mount"]["connected"] is False
    assert devices["camera"]["connected"] is False
    assert devices["mount"]["status"] == "pending_connect"
    assert devices["camera"]["status"] == "pending_connect"


# ------------------------------------------------------------------ #
# GET /api/v1/readiness                                                #
# ------------------------------------------------------------------ #


def test_readiness_ready_when_clear(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path=tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.get("/api/v1/readiness")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ready"] is True
    assert data["blockers"] == []
    assert data["time_trusted"] is True
    assert data["calibrated"] is False


def test_readiness_blocked_when_time_not_trusted(tmp_path: Path) -> None:
    node = FakeNodeBackend(time_trusted=False)
    ctrl = _make_controller(node=node, tmp_path=tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.get("/api/v1/readiness")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ready"] is False
    assert data["time_trusted"] is False
    blocker_names = [b["name"] for b in data["blockers"]]
    assert "time_uncertain" in blocker_names


def test_readiness_blocked_when_storage_critically_low(tmp_path: Path) -> None:
    node = FakeNodeBackend(storage_summary="critically low")
    ctrl = _make_controller(node=node, tmp_path=tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.get("/api/v1/readiness")
    data = resp.json()
    assert data["ready"] is False
    names = [b["name"] for b in data["blockers"]]
    assert "storage_critically_low" in names


def test_readiness_blocked_when_power_unhealthy(tmp_path: Path) -> None:
    node = FakeNodeBackend(power_healthy=False)
    ctrl = _make_controller(node=node, tmp_path=tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.get("/api/v1/readiness")
    data = resp.json()
    assert data["ready"] is False
    names = [b["name"] for b in data["blockers"]]
    assert "power_integrity_warning" in names


def test_readiness_blocked_when_camera_in_card_reader_mode(tmp_path: Path) -> None:
    ctrl = _make_controller(
        camera=FakeCameraBackend(
            diagnostic_status={
                "status": "card_reader_mode",
                "connected": True,
                "ready": False,
                "summary": "Camera is detected but only exposing status/card-reader controls",
            }
        ),
        tmp_path=tmp_path,
    )
    client = TestClient(build_app(controller=ctrl))
    resp = client.get("/api/v1/readiness")
    data = resp.json()
    assert data["ready"] is False
    names = [b["name"] for b in data["blockers"]]
    assert "camera_remote_mode_required" in names


def test_readiness_blocked_when_camera_in_autocapture_mode(tmp_path: Path) -> None:
    ctrl = _make_controller(
        camera=FakeCameraBackend(
            diagnostic_status={
                "status": "autocapture_mode",
                "connected": True,
                "ready": False,
                "summary": "Camera is in Still Capture Mode 'Self-timer'; exit self-timer/autocapture mode on the body before capture",
            }
        ),
        tmp_path=tmp_path,
    )
    client = TestClient(build_app(controller=ctrl))
    resp = client.get("/api/v1/readiness")
    data = resp.json()
    assert data["ready"] is False
    names = [b["name"] for b in data["blockers"]]
    assert "camera_autocapture_mode_blocking" in names


def test_readiness_skips_direct_camera_probe_when_broker_owns_path(tmp_path: Path) -> None:
    camera = FakeCameraBackend(
        diagnostic_status={
            "status": "card_reader_mode",
            "connected": True,
            "ready": False,
            "summary": "Camera is detected but only exposing status/card-reader controls",
        }
    )
    ctrl = _make_controller(camera=camera, broker_backend=FakeFocusBroker(), tmp_path=tmp_path)

    client = TestClient(build_app(controller=ctrl))
    resp = client.get("/api/v1/readiness")
    data = resp.json()

    assert resp.status_code == 200
    assert camera.diagnostic_calls == 0
    names = [b["name"] for b in data["blockers"]]
    assert "camera_remote_mode_required" not in names


def test_node_status_surfaces_card_reader_mode(tmp_path: Path) -> None:
    ctrl = _make_controller(
        camera=FakeCameraBackend(
            diagnostic_status={
                "status": "card_reader_mode",
                "connected": True,
                "ready": False,
                "summary": "Camera is detected but only exposing status/card-reader controls",
            }
        ),
        tmp_path=tmp_path,
    )
    client = TestClient(build_app(controller=ctrl))
    resp = client.get("/api/v1/node/status")
    data = resp.json()
    assert data["detected_devices"]["camera"]["status"] == "card_reader_mode"
    assert "card-reader controls" in data["detected_devices"]["camera"]["summary"]


def test_readiness_includes_storage_summary(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path=tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.get("/api/v1/readiness")
    data = resp.json()
    assert "free_bytes" in data["storage_summary"]
    assert "total_bytes" in data["storage_summary"]


def test_readiness_reflects_calibration_state(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.calibration_accepted = True
    client = TestClient(build_app(controller=ctrl))
    resp = client.get("/api/v1/readiness")
    assert resp.json()["calibrated"] is True


def test_readiness_blocker_has_required_fields(tmp_path: Path) -> None:
    node = FakeNodeBackend(time_trusted=False)
    ctrl = _make_controller(node=node, tmp_path=tmp_path)
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/readiness").json()
    blocker = data["blockers"][0]
    assert "name" in blocker
    assert "severity" in blocker
    assert "summary" in blocker


def test_readiness_only_probes_camera_diagnostic_once(tmp_path: Path) -> None:
    camera = FakeCameraBackend(
        diagnostic_status={
            "status": "remote_control_ready",
            "connected": True,
            "ready": True,
            "summary": "Remote-control surface available",
        }
    )
    ctrl = _make_controller(camera=camera, tmp_path=tmp_path)
    client = TestClient(build_app(controller=ctrl))

    resp = client.get("/api/v1/readiness")

    assert resp.status_code == 200
    assert camera.diagnostic_calls == 1


# ------------------------------------------------------------------ #
# GET /api/v1/readiness — supervision_blockers                         #
# ------------------------------------------------------------------ #

_FAKE_PROFILE = SimpleNamespace(
    profile_id="test-profile",
    display_name="Test Profile",
    hardware=SimpleNamespace(
        lens=SimpleNamespace(is_zoom=False, default_focal_length_mm=135),
    ),
    solving_hints=SimpleNamespace(focal_length_assumption_mm=None),
)


class _UnavailableEkosAdapter(StubEkosAdapter):
    def status(self) -> NormalizedEkosSnapshot:
        return NormalizedEkosSnapshot(ekos_state=EkosRuntimeState.UNAVAILABLE)


class _UnknownBrokerBackend(StubBrokerBackend):
    def snapshot(self) -> BrokerSnapshot:
        return BrokerSnapshot(broker_state=BrokerRuntimeState.UNKNOWN)


class _UnavailableBrokerBackend(StubBrokerBackend):
    def snapshot(self) -> BrokerSnapshot:
        return BrokerSnapshot(broker_state=BrokerRuntimeState.UNAVAILABLE)


def test_readiness_supervision_blockers_when_ekos_unavailable(tmp_path: Path) -> None:
    """supervision_blockers must be non-empty when Ekos is UNAVAILABLE."""
    ctrl = _make_controller(
        session=RuntimeSession(state=ClawState.READY),
        ekos_adapter=_UnavailableEkosAdapter(),
        tmp_path=tmp_path,
    )
    ctrl.active_equipment_profile = _FAKE_PROFILE
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/readiness").json()
    assert data["supervision_ready"] is False
    blocker_names = [b["name"] for b in data["supervision_blockers"]]
    assert "ekos_unavailable" in blocker_names


def test_readiness_supervision_blockers_when_broker_unknown(tmp_path: Path) -> None:
    """supervision_blockers must be non-empty when the INDI broker state is UNKNOWN."""
    ctrl = _make_controller(
        session=RuntimeSession(state=ClawState.READY),
        broker_backend=_UnknownBrokerBackend(),
        tmp_path=tmp_path,
    )
    ctrl.active_equipment_profile = _FAKE_PROFILE
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/readiness").json()
    assert data["supervision_ready"] is False
    blocker_names = [b["name"] for b in data["supervision_blockers"]]
    assert "broker_unknown" in blocker_names


def test_readiness_supervision_blockers_when_broker_unavailable(tmp_path: Path) -> None:
    """supervision_blockers must be non-empty when the INDI broker is UNAVAILABLE."""
    ctrl = _make_controller(
        session=RuntimeSession(state=ClawState.READY),
        broker_backend=_UnavailableBrokerBackend(),
        tmp_path=tmp_path,
    )
    ctrl.active_equipment_profile = _FAKE_PROFILE
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/readiness").json()
    assert data["supervision_ready"] is False
    blocker_names = [b["name"] for b in data["supervision_blockers"]]
    assert "broker_unavailable" in blocker_names


def test_readiness_supervision_ready_false_without_profile(tmp_path: Path) -> None:
    """supervision_ready must be False when no active equipment profile is set."""
    ctrl = _make_controller(
        session=RuntimeSession(state=ClawState.READY),
        tmp_path=tmp_path,
    )
    ctrl.active_equipment_profile = None
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/readiness").json()
    assert data["supervision_ready"] is False


def test_readiness_supervision_ready_true_when_all_clear(tmp_path: Path) -> None:
    """supervision_ready is True when state=READY, profile set, and adapters reachable."""
    ctrl = _make_controller(
        session=RuntimeSession(state=ClawState.READY),
        tmp_path=tmp_path,
    )
    ctrl.active_equipment_profile = _FAKE_PROFILE
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/readiness").json()
    assert data["supervision_ready"] is True
    assert data["supervision_blockers"] == []


# ------------------------------------------------------------------ #
# POST /api/v1/time/confirm                                            #
# ------------------------------------------------------------------ #


def test_time_confirm_applies_when_time_untrusted(tmp_path: Path) -> None:
    """Confirm time succeeds from any non-active-motion/capture state."""
    node = FakeNodeBackend(time_trusted=False)
    ctrl = _make_controller(node=node, tmp_path=tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.post(
        "/api/v1/time/confirm",
        json={"confirmed_at": "2025-06-01T22:00:00Z"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "trusted" in data
    assert "source" in data
    assert "summary" in data
    assert "applied" in data


def test_time_confirm_returns_200_when_already_trusted(tmp_path: Path) -> None:
    """POST /api/v1/time/confirm is idempotent when time is already trusted."""
    ctrl = _make_controller(tmp_path=tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.post(
        "/api/v1/time/confirm",
        json={"confirmed_at": "2025-06-01T22:00:00Z"},
    )
    assert resp.status_code == 200


def test_time_confirm_rejected_during_active_capture(tmp_path: Path) -> None:
    """POST /api/v1/time/confirm returns 409 when session is in active capture."""
    from kepler_node.agent.session import ClawState, RuntimeSession

    session = RuntimeSession(session_id="sess-tc-01", state=ClawState.CAPTURE, control_locked=True)
    ctrl = _make_controller(session=session, tmp_path=tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.post(
        "/api/v1/time/confirm",
        json={"confirmed_at": "2025-06-01T22:00:00Z"},
    )
    assert resp.status_code == 409
    assert "active motion or capture" in resp.json()["detail"]


# ------------------------------------------------------------------ #
# POST /api/v1/calibrate                                               #
# ------------------------------------------------------------------ #


def test_calibrate_from_ready_transitions_to_calibrate(tmp_path: Path) -> None:
    """POST /api/v1/calibrate from ready returns state=calibrate."""
    from kepler_node.agent.session import ClawState, RuntimeSession

    session = RuntimeSession(state=ClawState.READY)
    ctrl = _make_controller(session=session, tmp_path=tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.post("/api/v1/calibrate")
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "calibrate"
    assert "blockers" in data
    assert "degraded" in data


def test_calibrate_from_target_acquired_transitions_to_calibrate(tmp_path: Path) -> None:
    """POST /api/v1/calibrate from target_acquired also transitions to calibrate."""
    from kepler_node.agent.session import ClawState, RuntimeSession

    session = RuntimeSession(state=ClawState.TARGET_ACQUIRED)
    ctrl = _make_controller(session=session, tmp_path=tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.post("/api/v1/calibrate")
    assert resp.status_code == 200
    assert resp.json()["state"] == "calibrate"


def test_calibrate_409_when_not_ready(tmp_path: Path) -> None:
    """POST /api/v1/calibrate returns 409 from an invalid state."""
    from kepler_node.agent.session import ClawState, RuntimeSession

    session = RuntimeSession(session_id="sess-cal-01", state=ClawState.CAPTURE)
    ctrl = _make_controller(session=session, tmp_path=tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.post("/api/v1/calibrate")
    assert resp.status_code == 409


def test_calibrate_422_when_readiness_blocker_exists(tmp_path: Path) -> None:
    """POST /api/v1/calibrate returns 422 when readiness blockers block calibration."""
    from kepler_node.agent.session import ClawState, RuntimeSession

    node = FakeNodeBackend(time_trusted=False)
    session = RuntimeSession(state=ClawState.READY)
    ctrl = _make_controller(session=session, node=node, tmp_path=tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.post("/api/v1/calibrate")
    assert resp.status_code == 422


def test_calibrate_response_sets_control_locked_true(tmp_path: Path) -> None:
    """POST /api/v1/calibrate must expose control_locked: true in its response."""
    from kepler_node.agent.session import ClawState, RuntimeSession

    session = RuntimeSession(state=ClawState.READY)
    ctrl = _make_controller(session=session, tmp_path=tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.post("/api/v1/calibrate")
    assert resp.status_code == 200
    assert resp.json()["control_locked"] is True


def test_focus_calibrate_from_ready_returns_to_ready_and_persists_profile(tmp_path: Path) -> None:
    session = RuntimeSession(state=ClawState.READY)
    camera = FakeFocusCalibrationCamera()
    broker = FakeFocusBroker()
    ctrl = _make_controller(
        session=session,
        camera=camera,
        broker_backend=broker,
        tmp_path=tmp_path,
    )
    profile = _make_fuji_profile()
    ctrl.store.write_profile(profile)
    ctrl.set_active_equipment_profile(profile)

    client = TestClient(build_app(controller=ctrl))
    resp = client.post("/api/v1/focus-calibrate")
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "ready"
    assert data["control_locked"] is False
    assert camera.calibration_calls == 1
    assert broker.stop_calls == 1
    assert broker.start_calls == 1

    stored = ctrl.store.read_profile("fuji-rig")
    assert stored is not None
    calibration = stored.hardware.camera.fuji_focus_calibration
    assert calibration is not None
    assert calibration.active_profile_id == "xf55-200@55mm-mf"

    runtime_path = tmp_path / "runtime" / "fuji_focus_calibration.json"
    projection = json.loads(runtime_path.read_text(encoding="utf-8"))
    assert projection["calibration"]["active_profile_id"] == "xf55-200@55mm-mf"
    assert projection["active_calibration"]["raw_max"] == 10442


def test_focus_calibrate_409_when_not_ready(tmp_path: Path) -> None:
    session = RuntimeSession(state=ClawState.CAPTURE)
    ctrl = _make_controller(session=session, camera=FakeFocusCalibrationCamera(), tmp_path=tmp_path)
    ctrl.set_active_equipment_profile(_make_fuji_profile())
    client = TestClient(build_app(controller=ctrl))
    resp = client.post("/api/v1/focus-calibrate")
    assert resp.status_code == 409


def test_focus_calibrate_422_without_active_profile(tmp_path: Path) -> None:
    session = RuntimeSession(state=ClawState.READY)
    ctrl = _make_controller(session=session, camera=FakeFocusCalibrationCamera(), tmp_path=tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.post("/api/v1/focus-calibrate")
    assert resp.status_code == 422


def test_node_status_control_locked_true_during_calibrate(tmp_path: Path) -> None:
    """GET /api/v1/node/status must expose control_locked: true once calibration begins."""
    from kepler_node.agent.session import ClawState, RuntimeSession

    session = RuntimeSession(state=ClawState.CALIBRATE, control_locked=True)
    ctrl = _make_controller(session=session, tmp_path=tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.get("/api/v1/node/status")
    assert resp.status_code == 200
    assert resp.json()["control_locked"] is True


# ------------------------------------------------------------------ #
# Readiness: session-state blockers                                    #
# ------------------------------------------------------------------ #


def test_readiness_not_ready_during_active_capture(tmp_path: Path) -> None:
    """ready is False when a managed session is in the capture state."""
    from kepler_node.agent.session import ClawState, RuntimeSession, WorkflowIntent

    session = RuntimeSession(
        session_id="sess-active-01",
        state=ClawState.CAPTURE,
        workflow_intent=WorkflowIntent.CAPTURE,
        control_locked=True,
    )
    ctrl = _make_controller(session=session, tmp_path=tmp_path)
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/readiness").json()
    assert data["ready"] is False
    names = [b["name"] for b in data["blockers"]]
    assert "active_session" in names
    assert data["external_control_summary"] is not None
    assert data["external_control_summary"]["state"] == "capture"


def test_readiness_not_ready_during_paused_session(tmp_path: Path) -> None:
    """ready is False when a managed session is paused."""
    from kepler_node.agent.session import ClawState, RuntimeSession, WorkflowIntent

    session = RuntimeSession(
        session_id="sess-paused-01",
        state=ClawState.PAUSED,
        workflow_intent=WorkflowIntent.CALIBRATION,
    )
    ctrl = _make_controller(session=session, tmp_path=tmp_path)
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/readiness").json()
    assert data["ready"] is False
    names = [b["name"] for b in data["blockers"]]
    assert "active_session" in names


def test_readiness_pre_session_paused_does_not_report_active_session(tmp_path: Path) -> None:
    """Startup pauses without a session_id are not active managed sessions."""
    from kepler_node.agent.session import ClawState, RuntimeSession, WorkflowIntent

    session = RuntimeSession(
        session_id=None,
        state=ClawState.PAUSED,
        workflow_intent=WorkflowIntent.CALIBRATION,
        control_locked=False,
    )
    ctrl = _make_controller(session=session, tmp_path=tmp_path)
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/readiness").json()
    names = [b["name"] for b in data["blockers"]]
    assert "active_session" not in names
    assert data["external_control_summary"] is None


def test_readiness_not_ready_when_session_completed_unacknowledged(tmp_path: Path) -> None:
    """ready is False when a session is in completed state awaiting acknowledgment."""
    from kepler_node.agent.session import ClawState, RuntimeSession, TerminalOutcome

    session = RuntimeSession(
        session_id="sess-done-01",
        state=ClawState.COMPLETED,
        terminal_outcome=TerminalOutcome.STOPPED_BY_OPERATOR,
    )
    ctrl = _make_controller(session=session, tmp_path=tmp_path)
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/readiness").json()
    assert data["ready"] is False
    names = [b["name"] for b in data["blockers"]]
    assert "terminal_session_uncleared" in names
    assert data["external_control_summary"] is not None


def test_readiness_not_ready_when_session_failed_uncleared(tmp_path: Path) -> None:
    """ready is False when a session is in failed state awaiting clear-failure."""
    from kepler_node.agent.session import ClawState, RuntimeSession, TerminalOutcome

    session = RuntimeSession(
        session_id="sess-fail-01",
        state=ClawState.FAILED,
        terminal_outcome=TerminalOutcome.FAILED,
    )
    ctrl = _make_controller(session=session, tmp_path=tmp_path)
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/readiness").json()
    assert data["ready"] is False
    names = [b["name"] for b in data["blockers"]]
    assert "terminal_session_uncleared" in names


# ------------------------------------------------------------------ #
# Degraded conditions: trusted-time sources                           #
# ------------------------------------------------------------------ #


def test_readiness_degraded_time_source_mismatch_when_gps_ntp_disagree(
    tmp_path: Path,
) -> None:
    """GET /api/v1/readiness returns time_source_mismatch degraded condition when
    both GPS and NTP are available but disagree by more than 5 seconds."""
    node = FakeNodeBackend(
        time_trusted=True,
        time_source=TimeSource.GPS,
        gps_ntp_mismatch_seconds=12.3,
    )
    ctrl = _make_controller(node=node, tmp_path=tmp_path)
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/readiness").json()

    # Node is still ready (mismatch is degraded, not blocking).
    assert data["ready"] is True
    degraded_names = [d["name"] for d in data["degraded"]]
    assert "time_source_mismatch" in degraded_names

    mismatch = next(d for d in data["degraded"] if d["name"] == "time_source_mismatch")
    assert "12.3" in mismatch["summary"] or "GPS" in mismatch["summary"]


def test_readiness_no_time_mismatch_when_gps_ntp_agree(tmp_path: Path) -> None:
    """No time_source_mismatch degraded condition when GPS and NTP agree."""
    node = FakeNodeBackend(
        time_trusted=True,
        time_source=TimeSource.GPS,
        gps_ntp_mismatch_seconds=None,
    )
    ctrl = _make_controller(node=node, tmp_path=tmp_path)
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/readiness").json()

    degraded_names = [d["name"] for d in data["degraded"]]
    assert "time_source_mismatch" not in degraded_names


def test_readiness_degraded_time_source_weaker_for_rtc(tmp_path: Path) -> None:
    """GET /api/v1/readiness surfaces time_source_weaker when source is RTC."""
    node = FakeNodeBackend(
        time_trusted=True,
        time_source=TimeSource.RTC,
    )
    ctrl = _make_controller(node=node, tmp_path=tmp_path)
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/readiness").json()

    # RTC is trusted but weaker than NTP/GPS — node remains ready.
    assert data["ready"] is True
    degraded_names = [d["name"] for d in data["degraded"]]
    assert "time_source_weaker" in degraded_names
    weaker = next(d for d in data["degraded"] if d["name"] == "time_source_weaker")
    assert "RTC" in weaker["summary"]
    assert "NTP or GPS preferred" in weaker["summary"]


def test_readiness_degraded_time_source_weaker_for_operator_confirmed(
    tmp_path: Path,
) -> None:
    """GET /api/v1/readiness surfaces time_source_weaker for operator_confirmed source."""
    node = FakeNodeBackend(
        time_trusted=True,
        time_source=TimeSource.OPERATOR_CONFIRMED,
    )
    ctrl = _make_controller(node=node, tmp_path=tmp_path)
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/readiness").json()

    assert data["ready"] is True
    degraded_names = [d["name"] for d in data["degraded"]]
    assert "time_source_weaker" in degraded_names
    weaker = next(d for d in data["degraded"] if d["name"] == "time_source_weaker")
    assert "operator-confirmed" in weaker["summary"]
    assert "NTP or GPS preferred" in weaker["summary"]


def test_readiness_no_degraded_time_condition_for_ntp(tmp_path: Path) -> None:
    """No time-related degraded condition when source is NTP (preferred)."""
    node = FakeNodeBackend(time_trusted=True, time_source=TimeSource.NETWORK)
    ctrl = _make_controller(node=node, tmp_path=tmp_path)
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/readiness").json()

    degraded_names = [d["name"] for d in data["degraded"]]
    assert "time_source_mismatch" not in degraded_names
    assert "time_source_weaker" not in degraded_names


def test_node_status_time_certainty_reflects_gps_source(tmp_path: Path) -> None:
    """GET /api/v1/node/status time_certainty reflects GPS as source when GPS is active."""
    node = FakeNodeBackend(time_trusted=True, time_source=TimeSource.GPS)
    ctrl = _make_controller(node=node, tmp_path=tmp_path)
    ctrl.session.state = ClawState.READY
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/node/status").json()

    assert data["time_certainty"]["trusted"] is True
    assert data["time_certainty"]["source"] == "gps"
