"""Tests for GET /api/v1/session/current and /state (Phase 4)."""

from __future__ import annotations

from datetime import UTC, datetime
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
from kepler_node.agent.session import ClawState, ResumeContext, RuntimeSession, WorkflowIntent
from kepler_node.api.app import build_app
from kepler_node.camera.protocols import CameraSettings, CaptureRequest, CaptureResult
from kepler_node.imaging.protocols import SolveResult
from kepler_node.mount.protocols import MountPosition
from kepler_node.storage.filesystem import FilesystemSessionStore


# ------------------------------------------------------------------ #
# Shared fakes (minimal inline versions)                              #
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
            free_bytes=50_000_000_000,
            total_bytes=100_000_000_000,
            writable=True,
            summary="ok",
        )

    def power_status(self) -> PowerStatus:
        return PowerStatus(healthy=True, summary="ok")

    def confirm_time(self, timestamp: datetime) -> TimeStatus:
        return self.time_status()


class _FakeNodeWithTimeUntrusted(_FakeNode):
    def time_status(self) -> TimeStatus:
        return TimeStatus(trusted=False, source=TimeSource.NETWORK, summary="time not synchronized")


class _FakeMount:

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def current_position(self) -> MountPosition:
        return MountPosition(ra_hours=0.0, dec_deg=0.0)

    def slew_to(self, p: MountPosition) -> None:
        pass

    def sync_to(self, p: MountPosition) -> None:
        pass

    def activity_events(self) -> list:
        return []

    def poll_activity(self) -> None:
        pass


class _FakeCamera:
    def connect(self, s: CameraSettings) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def capture(self, r: CaptureRequest) -> CaptureResult:
        return CaptureResult(image_path=Path("/tmp/t.jpg"), metadata={})

    def activity_events(self) -> list:
        return []


class _FakeSolver:
    def solve(self, image_path: Path, **_: object) -> SolveResult:
        return SolveResult(success=True, ra_hours=10.0, dec_deg=45.0, residual_arcmin=2.0)


def _make(session: RuntimeSession, tmp_path: Path) -> ClawController:
    base = tmp_path
    base.mkdir(parents=True, exist_ok=True)
    vdir = base / "verify"
    vdir.mkdir(parents=True, exist_ok=True)
    return ClawController(
        session=session,
        node_backend=_FakeNode(),
        mount_backend=_FakeMount(),
        camera_backend=_FakeCamera(),
        solver_backend=_FakeSolver(),
        store=FilesystemSessionStore(data_root=base),
        authorship_tracker=AuthorshipTracker(),
        verification_dir=vdir,
        test_exposure_seconds=1.0,
    )


# ------------------------------------------------------------------ #
# GET /api/v1/session/current                                          #
# ------------------------------------------------------------------ #


def test_session_current_returns_null_when_no_active_session(tmp_path: Path) -> None:
    session = RuntimeSession(state=ClawState.READY)
    ctrl = _make(session, tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.get("/api/v1/session/current")
    assert resp.status_code == 200
    assert resp.json() is None


def test_session_current_returns_summary_when_session_active(tmp_path: Path) -> None:
    session = RuntimeSession(
        session_id="sess-001",
        state=ClawState.CAPTURE,
        workflow_intent=WorkflowIntent.CAPTURE,
        control_locked=True,
    )
    ctrl = _make(session, tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.get("/api/v1/session/current")
    assert resp.status_code == 200
    data = resp.json()
    assert data is not None
    assert data["session_id"] == "sess-001"
    assert data["state"] == "capture"
    assert data["workflow_intent"] == "capture"
    assert data["control_locked"] is True


def test_session_current_includes_target_summary_when_staged(tmp_path: Path) -> None:
    session = RuntimeSession(
        session_id="sess-002",
        state=ClawState.TARGET_ACQUIRED,
        staged_target_ra_hours=10.5,
        staged_target_dec_deg=45.0,
        staged_target_id="M51",
    )
    ctrl = _make(session, tmp_path)
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/session/current").json()
    assert data["target_summary"]["target_id"] == "M51"
    assert data["target_summary"]["ra_hours"] == 10.5


def test_session_current_returns_terminal_outcome_when_completed(tmp_path: Path) -> None:
    from kepler_node.agent.session import TerminalOutcome

    session = RuntimeSession(
        session_id="sess-003",
        state=ClawState.COMPLETED,
        terminal_outcome=TerminalOutcome.STOPPED_BY_OPERATOR,
    )
    ctrl = _make(session, tmp_path)
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/session/current").json()
    assert data["terminal_outcome"] == "stopped_by_operator"


# ------------------------------------------------------------------ #
# GET /api/v1/session/current/state                                    #
# ------------------------------------------------------------------ #


def test_session_state_returns_null_when_no_active_session(tmp_path: Path) -> None:
    session = RuntimeSession(state=ClawState.READY)
    ctrl = _make(session, tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.get("/api/v1/session/current/state")
    assert resp.status_code == 200
    assert resp.json() is None


def test_session_state_returns_lightweight_view_when_active(tmp_path: Path) -> None:
    session = RuntimeSession(
        session_id="sess-004",
        state=ClawState.GUARD,
        workflow_intent=WorkflowIntent.CAPTURE,
        control_locked=True,
    )
    ctrl = _make(session, tmp_path)
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/session/current/state").json()
    assert data["state"] == "guard"
    assert data["workflow_intent"] == "capture"
    assert data["control_locked"] is True
    assert "latest_message" in data


def test_session_state_includes_pause_summary_when_paused(tmp_path: Path) -> None:
    session = RuntimeSession(
        session_id="sess-005",
        state=ClawState.PAUSED,
        workflow_intent=WorkflowIntent.CALIBRATION,
    )
    session.resume_context = ResumeContext(
        resume_state=ClawState.CALIBRATE,
        workflow_intent=WorkflowIntent.CALIBRATION,
        pause_reason="time_uncertain",
        operator_action_required="Confirm time before resuming",
    )
    ctrl = _make(session, tmp_path)
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/session/current/state").json()
    assert data["state"] == "paused"
    assert data["pause_summary"]["pause_reason"] == "time_uncertain"
    assert data["pause_summary"]["operator_action_required"] == "Confirm time before resuming"


def test_session_state_includes_blockers_when_present(tmp_path: Path) -> None:
    node = _FakeNodeWithTimeUntrusted()
    session = RuntimeSession(session_id="sess-006", state=ClawState.PAUSED)
    base = tmp_path
    base.mkdir(parents=True, exist_ok=True)
    vdir = base / "verify"
    vdir.mkdir(parents=True, exist_ok=True)
    ctrl = ClawController(
        session=session,
        node_backend=node,
        mount_backend=_FakeMount(),
        camera_backend=_FakeCamera(),
        solver_backend=_FakeSolver(),
        store=FilesystemSessionStore(data_root=base),
        authorship_tracker=AuthorshipTracker(),
        verification_dir=vdir,
    )
    data = TestClient(build_app(controller=ctrl)).get("/api/v1/session/current/state").json()
    blocker_names = [b["name"] for b in data["blockers"]]
    assert "time_uncertain" in blocker_names


def test_session_state_latest_message_reflects_real_transition(tmp_path: Path) -> None:
    """latest_message should carry the last transition message, not a placeholder."""
    session = RuntimeSession(state=ClawState.READY)
    ctrl = _make(session, tmp_path)
    # Drive a real transition so latest_message is populated
    ctrl.boot()  # BOOT -> DISCOVER; but session starts at READY so the state is READY already
    # Use acknowledge_complete path: put session in COMPLETED then acknowledge
    from kepler_node.agent.session import TerminalOutcome
    session.state = ClawState.COMPLETED
    session.terminal_outcome = TerminalOutcome.STOPPED_BY_OPERATOR
    session.session_id = "sess-msg-01"
    result = ctrl.acknowledge_complete()
    # After acknowledge_complete, state is READY and latest_message is set
    assert session.latest_message == result.message
    assert session.latest_message != f"state: {session.state}"
    assert "acknowledged" in session.latest_message or "ready" in session.latest_message


def test_session_state_api_returns_real_latest_message(tmp_path: Path) -> None:
    """GET /session/current/state returns the real transition message, not a placeholder."""
    session = RuntimeSession(
        session_id="sess-msg-02",
        state=ClawState.PAUSED,
        workflow_intent=WorkflowIntent.CALIBRATION,
    )
    session.latest_message = "calibrate blocked by readiness: time_uncertain"
    ctrl = _make(session, tmp_path)
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/session/current/state").json()
    assert data["latest_message"] == "calibrate blocked by readiness: time_uncertain"
    assert data["latest_message"] != "state: paused"


def test_session_state_control_locked_true_during_calibrate(tmp_path: Path) -> None:
    """GET /api/v1/session/current/state must expose control_locked: true during calibration."""
    session = RuntimeSession(
        session_id="sess-cal-lock-01",
        state=ClawState.CALIBRATE,
        workflow_intent=WorkflowIntent.CALIBRATION,
        control_locked=True,
    )
    ctrl = _make(session, tmp_path)
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/session/current/state").json()
    assert data["state"] == "calibrate"
    assert data["control_locked"] is True
