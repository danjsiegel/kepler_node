"""Tests for POST session action endpoints.

Covers release-control, acknowledge-complete, clear-failure, stop,
pause, and resume, including 409/422 error cases.
"""

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
from kepler_node.storage.models import SessionRecord

# ------------------------------------------------------------------ #
# Minimal shared fakes                                                 #
# ------------------------------------------------------------------ #


class _FakeNode:
    def __init__(self, *, power_healthy: bool = True, storage_ok: bool = True) -> None:
        self._power_healthy = power_healthy
        self._storage_ok = storage_ok

    def network_mode(self) -> NetworkMode:
        return NetworkMode.FIELD_HOTSPOT

    def service_health(self) -> list[ServiceHealth]:
        return []

    def time_status(self) -> TimeStatus:
        return TimeStatus(trusted=True, source=TimeSource.NETWORK, summary="ok")

    def storage_status(self) -> StorageStatus:
        summary = "ok" if self._storage_ok else "critically low"
        return StorageStatus(
            data_root=Path("/tmp"),
            free_bytes=50_000_000_000 if self._storage_ok else 100_000,
            total_bytes=100_000_000_000,
            writable=self._storage_ok,
            summary=summary,
        )

    def power_status(self) -> PowerStatus:
        return PowerStatus(
            healthy=self._power_healthy,
            summary="ok" if self._power_healthy else "undervoltage",
            undervoltage_detected=not self._power_healthy,
        )

    def confirm_time(self, timestamp: datetime) -> TimeStatus:
        return self.time_status()


class _FakeMount:
    connected = False

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


def _make(
    session: RuntimeSession,
    tmp_path: Path,
    *,
    node: _FakeNode | None = None,
) -> ClawController:
    base = tmp_path
    base.mkdir(parents=True, exist_ok=True)
    vdir = base / "verify"
    vdir.mkdir(parents=True, exist_ok=True)
    return ClawController(
        session=session,
        node_backend=node or _FakeNode(),
        mount_backend=_FakeMount(),
        camera_backend=_FakeCamera(),
        solver_backend=_FakeSolver(),
        store=FilesystemSessionStore(data_root=base),
        authorship_tracker=AuthorshipTracker(),
        verification_dir=vdir,
        test_exposure_seconds=1.0,
    )


# ------------------------------------------------------------------ #
# POST /api/v1/session/release-control                                 #
# ------------------------------------------------------------------ #


def test_release_control_from_paused_returns_completed(tmp_path: Path) -> None:
    session = RuntimeSession(
        session_id="sess-rc-01",
        state=ClawState.PAUSED,
        control_locked=True,
    )
    session.resume_context = ResumeContext(
        resume_state=ClawState.CAPTURE,
        workflow_intent=WorkflowIntent.CAPTURE,
        pause_reason="operator",
    )
    ctrl = _make(session, tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.post("/api/v1/session/release-control")
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "completed"
    assert data["control_locked"] is False


def test_release_control_409_when_not_paused(tmp_path: Path) -> None:
    session = RuntimeSession(session_id="sess-rc-02", state=ClawState.CAPTURE)
    ctrl = _make(session, tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.post("/api/v1/session/release-control")
    assert resp.status_code == 409


def test_release_control_409_when_no_managed_session(tmp_path: Path) -> None:
    """release-control must reject when paused without an active managed session (session_id=None)."""
    session = RuntimeSession(
        session_id=None,
        state=ClawState.PAUSED,
        control_locked=True,
    )
    session.resume_context = ResumeContext(
        resume_state=ClawState.CALIBRATE,
        workflow_intent=WorkflowIntent.CALIBRATION,
        pause_reason="operator pause",
    )
    ctrl = _make(session, tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.post("/api/v1/session/release-control")
    assert resp.status_code == 409


# ------------------------------------------------------------------ #
# POST /api/v1/session/acknowledge-complete                            #
# ------------------------------------------------------------------ #


def test_acknowledge_complete_from_completed_returns_ready(tmp_path: Path) -> None:
    from kepler_node.agent.session import TerminalOutcome

    session = RuntimeSession(
        session_id="sess-ack-01",
        state=ClawState.COMPLETED,
        terminal_outcome=TerminalOutcome.STOPPED_BY_OPERATOR,
    )
    ctrl = _make(session, tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.post("/api/v1/session/acknowledge-complete")
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "ready"
    assert "acknowledged" in data["message"].lower() or "ready" in data["message"].lower()


def test_acknowledge_complete_409_when_not_completed(tmp_path: Path) -> None:
    session = RuntimeSession(session_id="sess-ack-02", state=ClawState.CAPTURE)
    ctrl = _make(session, tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.post("/api/v1/session/acknowledge-complete")
    assert resp.status_code == 409


def test_acknowledge_complete_409_when_failed(tmp_path: Path) -> None:
    session = RuntimeSession(session_id="sess-ack-03", state=ClawState.FAILED)
    ctrl = _make(session, tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.post("/api/v1/session/acknowledge-complete")
    assert resp.status_code == 409


# ------------------------------------------------------------------ #
# POST /api/v1/session/clear-failure                                   #
# ------------------------------------------------------------------ #


def test_clear_failure_from_failed_returns_ready_when_no_hardware_blocks(tmp_path: Path) -> None:
    from kepler_node.agent.session import TerminalOutcome

    session = RuntimeSession(
        session_id="sess-cf-01",
        state=ClawState.FAILED,
        terminal_outcome=TerminalOutcome.FAILED,
    )
    ctrl = _make(session, tmp_path)  # default node: no hardware blocks
    client = TestClient(build_app(controller=ctrl))
    resp = client.post("/api/v1/session/clear-failure")
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "ready"


def test_clear_failure_422_when_power_block_remains(tmp_path: Path) -> None:
    from kepler_node.agent.session import TerminalOutcome

    session = RuntimeSession(
        session_id="sess-cf-02",
        state=ClawState.FAILED,
        terminal_outcome=TerminalOutcome.FAILED,
    )
    node = _FakeNode(power_healthy=False)
    ctrl = _make(session, tmp_path, node=node)
    client = TestClient(build_app(controller=ctrl))
    resp = client.post("/api/v1/session/clear-failure")
    assert resp.status_code == 422


def test_clear_failure_422_when_storage_critically_low(tmp_path: Path) -> None:
    from kepler_node.agent.session import TerminalOutcome

    session = RuntimeSession(
        session_id="sess-cf-03",
        state=ClawState.FAILED,
        terminal_outcome=TerminalOutcome.FAILED,
    )
    node = _FakeNode(storage_ok=False)
    ctrl = _make(session, tmp_path, node=node)
    client = TestClient(build_app(controller=ctrl))
    resp = client.post("/api/v1/session/clear-failure")
    assert resp.status_code == 422


def test_clear_failure_409_when_not_failed(tmp_path: Path) -> None:
    session = RuntimeSession(session_id="sess-cf-04", state=ClawState.READY)
    ctrl = _make(session, tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.post("/api/v1/session/clear-failure")
    assert resp.status_code == 409


# ------------------------------------------------------------------ #
# POST /api/v1/session/stop                                            #
# ------------------------------------------------------------------ #


def test_stop_active_session_returns_completed(tmp_path: Path) -> None:
    session = RuntimeSession(
        session_id="sess-stop-01",
        state=ClawState.CAPTURE,
        control_locked=True,
    )
    ctrl = _make(session, tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.post("/api/v1/session/stop")
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "completed"


def test_stop_409_when_no_active_session(tmp_path: Path) -> None:
    session = RuntimeSession(state=ClawState.READY)
    ctrl = _make(session, tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.post("/api/v1/session/stop")
    assert resp.status_code == 409


def test_stop_409_when_pre_session_calibrate_no_managed_session(tmp_path: Path) -> None:
    """stop must reject standalone pre-session calibration that has no session_id."""
    session = RuntimeSession(session_id=None, state=ClawState.CALIBRATE, control_locked=True)
    ctrl = _make(session, tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.post("/api/v1/session/stop")
    assert resp.status_code == 409


def test_stop_409_when_already_terminal(tmp_path: Path) -> None:
    session = RuntimeSession(session_id="sess-stop-02", state=ClawState.COMPLETED)
    ctrl = _make(session, tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.post("/api/v1/session/stop")
    assert resp.status_code == 409


# ------------------------------------------------------------------ #
# POST /api/v1/session/pause                                           #
# ------------------------------------------------------------------ #


def test_pause_active_session_returns_paused(tmp_path: Path) -> None:
    session = RuntimeSession(
        session_id="sess-pause-01",
        state=ClawState.GUARD,
        workflow_intent=WorkflowIntent.CAPTURE,
    )
    ctrl = _make(session, tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.post("/api/v1/session/pause")
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "paused"


def test_pause_idempotent_when_already_paused(tmp_path: Path) -> None:
    session = RuntimeSession(
        session_id="sess-pause-02",
        state=ClawState.PAUSED,
        workflow_intent=WorkflowIntent.CALIBRATION,
    )
    ctrl = _make(session, tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.post("/api/v1/session/pause")
    assert resp.status_code == 200
    assert resp.json()["state"] == "paused"


def test_pause_writes_event_to_session_ndjson(tmp_path: Path) -> None:
    session_id = "sess-pause-trail-01"
    session = RuntimeSession(
        session_id=session_id,
        state=ClawState.CAPTURE,
        workflow_intent=WorkflowIntent.CAPTURE,
        latest_message="capturing frame 1",
    )
    base = tmp_path
    base.mkdir(parents=True, exist_ok=True)
    vdir = base / "verify"
    vdir.mkdir(parents=True, exist_ok=True)
    store = FilesystemSessionStore(data_root=base)
    store.write_session_record(
        SessionRecord(
            session_id=session_id,
            started_at=datetime(2026, 5, 12, tzinfo=UTC),
            updated_at=datetime(2026, 5, 12, tzinfo=UTC),
            state=ClawState.CAPTURE,
        )
    )
    ctrl = ClawController(
        session=session,
        node_backend=_FakeNode(),
        mount_backend=_FakeMount(),
        camera_backend=_FakeCamera(),
        solver_backend=_FakeSolver(),
        store=store,
        authorship_tracker=AuthorshipTracker(),
        verification_dir=vdir,
        test_exposure_seconds=1.0,
    )
    client = TestClient(build_app(controller=ctrl))

    resp = client.post("/api/v1/session/pause")
    assert resp.status_code == 200
    assert resp.json()["state"] == "paused"

    assert ctrl._node_events == [], "pause transition should not be written to node buffer"
    events, _ = store.list_events(session_id)
    assert len(events) >= 1, "pause transition event must be written to events.ndjson"
    last_event = max(events, key=lambda e: e.sequence)
    assert last_event.event_type == "state_transition"
    assert "paused" in last_event.message.lower()
    assert ctrl.session.latest_message == "session paused by operator"


def test_pause_409_when_no_active_session(tmp_path: Path) -> None:
    session = RuntimeSession(state=ClawState.READY)
    ctrl = _make(session, tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.post("/api/v1/session/pause")
    assert resp.status_code == 409


def test_pause_409_when_pre_session_calibrate_no_managed_session(tmp_path: Path) -> None:
    """pause must reject standalone pre-session calibration that has no session_id."""
    session = RuntimeSession(session_id=None, state=ClawState.CALIBRATE, control_locked=True)
    ctrl = _make(session, tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.post("/api/v1/session/pause")
    assert resp.status_code == 409


# ------------------------------------------------------------------ #
# POST /api/v1/session/resume                                          #
# ------------------------------------------------------------------ #


def test_resume_from_paused_returns_resume_state(tmp_path: Path) -> None:
    session = RuntimeSession(
        session_id="sess-resume-01",
        state=ClawState.PAUSED,
        workflow_intent=WorkflowIntent.CALIBRATION,
    )
    session.resume_context = ResumeContext(
        resume_state=ClawState.CALIBRATE,
        workflow_intent=WorkflowIntent.CALIBRATION,
        pause_reason="operator",
    )
    ctrl = _make(session, tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.post("/api/v1/session/resume")
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "calibrate"


def test_resume_409_when_not_paused(tmp_path: Path) -> None:
    session = RuntimeSession(session_id="sess-resume-02", state=ClawState.CAPTURE)
    ctrl = _make(session, tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.post("/api/v1/session/resume")
    assert resp.status_code == 409


def test_resume_409_when_paused_with_no_resume_context(tmp_path: Path) -> None:
    session = RuntimeSession(
        session_id="sess-resume-03",
        state=ClawState.PAUSED,
        workflow_intent=WorkflowIntent.CALIBRATION,
    )
    # No resume_context set
    ctrl = _make(session, tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.post("/api/v1/session/resume")
    assert resp.status_code == 409


# ------------------------------------------------------------------ #
# Action response contract validation                                  #
# ------------------------------------------------------------------ #


def test_action_response_has_required_fields(tmp_path: Path) -> None:
    """Every action response must include state, workflow_intent, control_locked, message."""
    from kepler_node.agent.session import TerminalOutcome

    session = RuntimeSession(
        session_id="sess-contract-01",
        state=ClawState.COMPLETED,
        terminal_outcome=TerminalOutcome.STOPPED_BY_OPERATOR,
    )
    ctrl = _make(session, tmp_path)
    client = TestClient(build_app(controller=ctrl))
    data = client.post("/api/v1/session/acknowledge-complete").json()
    for field in ("state", "workflow_intent", "control_locked", "message"):
        assert field in data, f"Missing required field: {field}"


def test_action_response_includes_blockers_and_degraded(tmp_path: Path) -> None:
    """Successful action responses must include blockers and degraded lists (spec line 1593)."""
    from kepler_node.agent.session import TerminalOutcome

    session = RuntimeSession(
        session_id="sess-contract-02",
        state=ClawState.COMPLETED,
        terminal_outcome=TerminalOutcome.STOPPED_BY_OPERATOR,
    )
    ctrl = _make(session, tmp_path)
    client = TestClient(build_app(controller=ctrl))
    data = client.post("/api/v1/session/acknowledge-complete").json()
    assert "blockers" in data
    assert "degraded" in data
    assert isinstance(data["blockers"], list)
    assert isinstance(data["degraded"], list)


def test_clear_failure_response_includes_blockers_when_time_untrusted(tmp_path: Path) -> None:
    """clear-failure succeeds even with time_uncertain, but blockers list reflects remaining state."""
    from kepler_node.agent.session import TerminalOutcome

    class _UntrustedTimeNode(_FakeNode):
        def time_status(self) -> TimeStatus:
            return TimeStatus(
                trusted=False,
                source=TimeSource.UNTRUSTED,
                summary="time not synchronized",
            )

    session = RuntimeSession(
        session_id="sess-cf-blockers",
        state=ClawState.FAILED,
        terminal_outcome=TerminalOutcome.FAILED,
    )
    # time_uncertain is not a hardware block, so clear_failure() should succeed
    ctrl = _make(session, tmp_path, node=_UntrustedTimeNode())
    client = TestClient(build_app(controller=ctrl))
    resp = client.post("/api/v1/session/clear-failure")
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "ready"
    # The response must include the remaining blocker (time_uncertain) in blockers
    assert "blockers" in data
    blocker_names = [b["name"] for b in data["blockers"]]
    assert "time_uncertain" in blocker_names


def test_stop_response_includes_blockers_and_degraded(tmp_path: Path) -> None:
    """POST /api/v1/session/stop response must include blockers and degraded."""
    session = RuntimeSession(
        session_id="sess-stop-blockers",
        state=ClawState.CAPTURE,
        control_locked=True,
    )
    ctrl = _make(session, tmp_path)
    client = TestClient(build_app(controller=ctrl))
    data = client.post("/api/v1/session/stop").json()
    assert "blockers" in data
    assert "degraded" in data


# ------------------------------------------------------------------ #
# Event-trail regression: ack/clear must write to events.ndjson        #
# ------------------------------------------------------------------ #


def test_acknowledge_complete_writes_event_to_session_ndjson(tmp_path: Path) -> None:
    """acknowledge-complete must persist its READY transition event to the
    session's events.ndjson, not to the controller's node-level buffer.

    Previously, session.acknowledge_complete() cleared session_id before
    _make_transition() was called, causing _emit_event() to route the event
    into the node buffer instead of events.ndjson.
    """
    from kepler_node.agent.session import TerminalOutcome
    from kepler_node.storage.filesystem import FilesystemSessionStore
    from kepler_node.storage.models import SessionRecord

    session_id = "sess-ack-trail-01"
    session = RuntimeSession(
        session_id=session_id,
        state=ClawState.COMPLETED,
        terminal_outcome=TerminalOutcome.STOPPED_BY_OPERATOR,
    )
    base = tmp_path
    base.mkdir(parents=True, exist_ok=True)
    vdir = base / "verify"
    vdir.mkdir(parents=True, exist_ok=True)
    store = FilesystemSessionStore(data_root=base)
    # Pre-seed session directory so append_event has a place to write.
    from datetime import UTC, datetime
    store.write_session_record(
        SessionRecord(
            session_id=session_id,
            started_at=datetime(2026, 5, 12, tzinfo=UTC),
            updated_at=datetime(2026, 5, 12, tzinfo=UTC),
            state=ClawState.COMPLETED,
        )
    )
    ctrl = ClawController(
        session=session,
        node_backend=_FakeNode(),
        mount_backend=_FakeMount(),
        camera_backend=_FakeCamera(),
        solver_backend=_FakeSolver(),
        store=store,
        authorship_tracker=AuthorshipTracker(),
        verification_dir=vdir,
        test_exposure_seconds=1.0,
    )
    client = TestClient(build_app(controller=ctrl))
    resp = client.post("/api/v1/session/acknowledge-complete")
    assert resp.status_code == 200

    # Event must have been written to the session's events.ndjson, not node buffer.
    assert ctrl._node_events == [], "READY event should not be in node buffer after acknowledge"
    events, _ = store.list_events(session_id)
    assert len(events) >= 1, "READY transition event must be in events.ndjson"
    last_event = min(events, key=lambda e: e.sequence)
    assert "ready" in last_event.message.lower() or "acknowledged" in last_event.message.lower()


def test_clear_failure_writes_event_to_session_ndjson(tmp_path: Path) -> None:
    """clear-failure must persist its READY transition event to the session's
    events.ndjson, not to the controller's node-level buffer.
    """
    from kepler_node.agent.session import TerminalOutcome
    from kepler_node.storage.filesystem import FilesystemSessionStore
    from kepler_node.storage.models import SessionRecord

    session_id = "sess-cf-trail-01"
    session = RuntimeSession(
        session_id=session_id,
        state=ClawState.FAILED,
        terminal_outcome=TerminalOutcome.FAILED,
    )
    base = tmp_path
    base.mkdir(parents=True, exist_ok=True)
    vdir = base / "verify"
    vdir.mkdir(parents=True, exist_ok=True)
    store = FilesystemSessionStore(data_root=base)
    from datetime import UTC, datetime
    store.write_session_record(
        SessionRecord(
            session_id=session_id,
            started_at=datetime(2026, 5, 12, tzinfo=UTC),
            updated_at=datetime(2026, 5, 12, tzinfo=UTC),
            state=ClawState.FAILED,
        )
    )
    ctrl = ClawController(
        session=session,
        node_backend=_FakeNode(),
        mount_backend=_FakeMount(),
        camera_backend=_FakeCamera(),
        solver_backend=_FakeSolver(),
        store=store,
        authorship_tracker=AuthorshipTracker(),
        verification_dir=vdir,
        test_exposure_seconds=1.0,
    )
    client = TestClient(build_app(controller=ctrl))
    resp = client.post("/api/v1/session/clear-failure")
    assert resp.status_code == 200

    assert ctrl._node_events == [], "READY event should not be in node buffer after clear-failure"
    events, _ = store.list_events(session_id)
    assert len(events) >= 1, "READY transition event must be in events.ndjson"
    last_event = min(events, key=lambda e: e.sequence)
    assert "ready" in last_event.message.lower() or "cleared" in last_event.message.lower()



# ------------------------------------------------------------------ #
# Session-blocker inclusion in action responses                        #
# ------------------------------------------------------------------ #


def test_pause_action_response_includes_active_session_blocker(tmp_path: Path) -> None:
    """POST /session/pause response must include the active_session blocker."""
    session = RuntimeSession(
        session_id="sess-pause-bl-01",
        state=ClawState.CAPTURE,
        control_locked=True,
        workflow_intent=WorkflowIntent.CAPTURE,
    )
    ctrl = _make(session, tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.post("/api/v1/session/pause")
    assert resp.status_code == 200
    data = resp.json()
    blocker_names = [b["name"] for b in data["blockers"]]
    assert "active_session" in blocker_names, (
        "pause action response must include active_session blocker (paused session still active)"
    )


def test_stop_action_response_includes_terminal_session_uncleared_blocker(tmp_path: Path) -> None:
    """POST /session/stop response must include terminal_session_uncleared blocker."""
    session = RuntimeSession(
        session_id="sess-stop-bl-01",
        state=ClawState.CAPTURE,
        control_locked=True,
        workflow_intent=WorkflowIntent.CAPTURE,
    )
    ctrl = _make(session, tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.post("/api/v1/session/stop")
    assert resp.status_code == 200
    data = resp.json()
    blocker_names = [b["name"] for b in data["blockers"]]
    assert "terminal_session_uncleared" in blocker_names, (
        "stop action response must include terminal_session_uncleared blocker "
        "(completed session needs acknowledge-complete)"
    )


def test_release_control_action_response_includes_terminal_session_uncleared_blocker(tmp_path: Path) -> None:
    """POST /session/release-control response must include terminal_session_uncleared blocker."""
    session = RuntimeSession(
        session_id="sess-rc-bl-01",
        state=ClawState.PAUSED,
        control_locked=True,
    )
    session.resume_context = ResumeContext(
        resume_state=ClawState.CAPTURE,
        workflow_intent=WorkflowIntent.CAPTURE,
        pause_reason="operator",
    )
    ctrl = _make(session, tmp_path)
    client = TestClient(build_app(controller=ctrl))
    resp = client.post("/api/v1/session/release-control")
    assert resp.status_code == 200
    data = resp.json()
    blocker_names = [b["name"] for b in data["blockers"]]
    assert "terminal_session_uncleared" in blocker_names, (
        "release-control response must include terminal_session_uncleared blocker"
    )
