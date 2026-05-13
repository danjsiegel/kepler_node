"""Tests for GET /api/v1/health, /node/status, and /readiness (Phase 4)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from kepler_node.agent.authorship import AuthorshipTracker
from kepler_node.agent.claw import ClawController
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
from kepler_node.camera.protocols import CameraSettings, CaptureRequest, CaptureResult
from kepler_node.imaging.protocols import SolveResult
from kepler_node.mount.protocols import MountPosition
from kepler_node.storage.filesystem import FilesystemSessionStore

# ------------------------------------------------------------------ #
# Shared fake adapters                                                 #
# ------------------------------------------------------------------ #


class FakeNodeBackend:
    def __init__(
        self,
        *,
        time_trusted: bool = True,
        storage_summary: str = "ok",
        storage_writable: bool = True,
        power_healthy: bool = True,
        service_healths: list[ServiceHealth] | None = None,
    ) -> None:
        self._time_trusted = time_trusted
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
            source=TimeSource.NETWORK,
            summary="ok" if self._time_trusted else "time not synchronized",
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
    def __init__(self) -> None:
        pass

    def connect(self, settings: CameraSettings) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def capture(self, request: CaptureRequest) -> CaptureResult:
        return CaptureResult(image_path=Path("/tmp/test.jpg"), metadata={})

    def activity_events(self) -> list:
        return []


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
        camera_backend=FakeCameraBackend(),
        solver_backend=FakeSolverBackend(),
        store=FilesystemSessionStore(data_root=base),
        authorship_tracker=AuthorshipTracker(),
        verification_dir=vdir,
        test_exposure_seconds=1.0,
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


def test_node_status_detected_devices_not_connected_before_connect(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path=tmp_path)
    ctrl.session.state = ClawState.DISCOVER
    client = TestClient(build_app(controller=ctrl))
    resp = client.get("/api/v1/node/status")
    assert resp.status_code == 200
    devices = resp.json()["detected_devices"]
    assert devices["mount"]["connected"] is False
    assert devices["camera"]["connected"] is False


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

