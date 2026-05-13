"""Tests for GET /api/v1/session/current/frames, /artifacts, and /outcome."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
from kepler_node.agent.session import ClawState, RuntimeSession, TerminalOutcome, WorkflowIntent
from kepler_node.api.app import build_app
from kepler_node.camera.protocols import CameraSettings, CaptureRequest, CaptureResult
from kepler_node.imaging.protocols import SolveResult
from kepler_node.mount.protocols import MountPosition
from kepler_node.storage.filesystem import FilesystemSessionStore
from kepler_node.storage.models import ArtifactKind, ArtifactReference, FrameRecord, SessionRecord

# ------------------------------------------------------------------ #
# Shared fakes                                                         #
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

    def confirm_time(self, ts: datetime) -> TimeStatus:
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


def _make(session: RuntimeSession, tmp_path: Path) -> tuple[ClawController, FilesystemSessionStore]:
    base = tmp_path
    base.mkdir(parents=True, exist_ok=True)
    vdir = base / "verify"
    vdir.mkdir(parents=True, exist_ok=True)
    store = FilesystemSessionStore(data_root=base)
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
    return ctrl, store


_BASE_TIME = datetime(2026, 5, 12, 20, 0, 0, tzinfo=UTC)


def _write_frames(store: FilesystemSessionStore, session_id: str, count: int) -> list[str]:
    """Write *count* frame records and return their frame_ids."""
    ids = []
    from kepler_node.storage.models import SessionRecord

    # Ensure session directory exists
    store.write_session_record(
        SessionRecord(
            session_id=session_id,
            started_at=_BASE_TIME,
            updated_at=_BASE_TIME,
            state=ClawState.CAPTURE,
        )
    )
    for i in range(count):
        frame_id = f"frame-{i + 1:06d}"
        frame = FrameRecord(
            frame_id=frame_id,
            frame_role="capture",
            workflow_intent=WorkflowIntent.CAPTURE,
            capture_timestamp=_BASE_TIME + timedelta(minutes=i),
            image_path=f"frames/{frame_id}/IMG_{i}.RAF",
            artifact_references=[
                ArtifactReference(
                    artifact_kind=ArtifactKind.PREVIEW_PROXY,
                    relative_path=f"artifacts/{frame_id}-preview.jpg",
                    source_frame_id=frame_id,
                    created_at=_BASE_TIME + timedelta(minutes=i, seconds=1),
                )
            ],
            action_decision="continue",
        )
        store.write_frame_record(session_id, frame)
        ids.append(frame_id)
    return ids


# ------------------------------------------------------------------ #
# GET /api/v1/session/current/frames                                   #
# ------------------------------------------------------------------ #


def test_frames_returns_empty_when_no_session(tmp_path: Path) -> None:
    session = RuntimeSession(state=ClawState.READY)
    ctrl, _ = _make(session, tmp_path)
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/session/current/frames").json()
    assert data["frames"] == []
    assert data["next_before_frame_id"] is None


def test_frames_returns_newest_first(tmp_path: Path) -> None:
    session_id = "sess-frames-01"
    session = RuntimeSession(session_id=session_id, state=ClawState.CAPTURE)
    ctrl, store = _make(session, tmp_path)
    ids = _write_frames(store, session_id, count=3)
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/session/current/frames").json()
    returned_ids = [f["frame_id"] for f in data["frames"]]
    # Should be newest-first (highest index first)
    assert returned_ids == list(reversed(ids))


def test_frames_respects_limit(tmp_path: Path) -> None:
    session_id = "sess-frames-02"
    session = RuntimeSession(session_id=session_id, state=ClawState.CAPTURE)
    ctrl, store = _make(session, tmp_path)
    _write_frames(store, session_id, count=5)
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/session/current/frames", params={"limit": 2}).json()
    assert len(data["frames"]) == 2
    assert data["next_before_frame_id"] is not None


def test_frames_cursor_pagination(tmp_path: Path) -> None:
    session_id = "sess-frames-03"
    session = RuntimeSession(session_id=session_id, state=ClawState.CAPTURE)
    ctrl, store = _make(session, tmp_path)
    _write_frames(store, session_id, count=4)
    client = TestClient(build_app(controller=ctrl))

    # Get first page
    page1 = client.get("/api/v1/session/current/frames", params={"limit": 2}).json()
    assert len(page1["frames"]) == 2
    cursor = page1["next_before_frame_id"]
    assert cursor is not None

    # Get second page
    page2 = client.get(
        "/api/v1/session/current/frames",
        params={"limit": 2, "before_frame_id": cursor},
    ).json()
    assert len(page2["frames"]) == 2
    assert page2["next_before_frame_id"] is None

    # No overlap
    page1_ids = {f["frame_id"] for f in page1["frames"]}
    page2_ids = {f["frame_id"] for f in page2["frames"]}
    assert page1_ids.isdisjoint(page2_ids)


def test_frames_422_on_unknown_cursor(tmp_path: Path) -> None:
    session_id = "sess-frames-04"
    session = RuntimeSession(session_id=session_id, state=ClawState.CAPTURE)
    ctrl, store = _make(session, tmp_path)
    _write_frames(store, session_id, count=2)
    client = TestClient(build_app(controller=ctrl))
    resp = client.get(
        "/api/v1/session/current/frames",
        params={"before_frame_id": "frame-unknown-9999"},
    )
    assert resp.status_code == 422


def test_frames_each_entry_has_required_fields(tmp_path: Path) -> None:
    session_id = "sess-frames-05"
    session = RuntimeSession(session_id=session_id, state=ClawState.CAPTURE)
    ctrl, store = _make(session, tmp_path)
    _write_frames(store, session_id, count=1)
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/session/current/frames").json()
    frame = data["frames"][0]
    for field in ("frame_id", "capture_timestamp", "acceptance_summary"):
        assert field in frame, f"Missing required field: {field}"


# ------------------------------------------------------------------ #
# GET /api/v1/session/current/artifacts                                #
# ------------------------------------------------------------------ #


def test_artifacts_returns_empty_when_no_session(tmp_path: Path) -> None:
    session = RuntimeSession(state=ClawState.READY)
    ctrl, _ = _make(session, tmp_path)
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/session/current/artifacts").json()
    assert data["artifacts"] == []


def test_artifacts_returns_entries_from_frames(tmp_path: Path) -> None:
    session_id = "sess-artifacts-01"
    session = RuntimeSession(session_id=session_id, state=ClawState.CAPTURE)
    ctrl, store = _make(session, tmp_path)
    _write_frames(store, session_id, count=2)
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/session/current/artifacts").json()
    # Each frame has 1 artifact
    assert len(data["artifacts"]) == 2
    for art in data["artifacts"]:
        assert art["artifact_kind"] == "preview_proxy"
        assert "relative_path" in art
        assert "frame_id" in art
        assert "created_at" in art, "ArtifactSummary must expose created_at"
        assert "frame_capture_timestamp" not in art, (
            "frame_capture_timestamp must not appear; use created_at instead"
        )


def test_artifacts_created_at_matches_persisted_timestamp(tmp_path: Path) -> None:
    """created_at in the API response must reflect the persisted artifact timestamp."""
    session_id = "sess-artifacts-ts-01"
    session = RuntimeSession(session_id=session_id, state=ClawState.CAPTURE)
    ctrl, store = _make(session, tmp_path)
    artifact_time = _BASE_TIME + timedelta(seconds=42)
    store.write_session_record(
        SessionRecord(
            session_id=session_id,
            started_at=_BASE_TIME,
            updated_at=_BASE_TIME,
            state=ClawState.CAPTURE,
        ),
    )
    frame = FrameRecord(
        frame_id="frame-000001",
        frame_role="capture",
        workflow_intent=WorkflowIntent.CAPTURE,
        capture_timestamp=_BASE_TIME,
        image_path="frames/frame-000001/IMG_0001.RAF",
        artifact_references=[
            ArtifactReference(
                artifact_kind=ArtifactKind.PREVIEW_PROXY,
                relative_path="artifacts/frame-000001-preview.jpg",
                source_frame_id="frame-000001",
                created_at=artifact_time,
            )
        ],
        action_decision="continue",
    )
    store.write_frame_record(session_id, frame)
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/session/current/artifacts").json()
    assert len(data["artifacts"]) == 1
    art = data["artifacts"][0]
    assert art["created_at"] == artifact_time.isoformat()


# ------------------------------------------------------------------ #
# GET /api/v1/session/current/outcome                                  #
# ------------------------------------------------------------------ #


def test_outcome_returns_null_when_not_terminal(tmp_path: Path) -> None:
    session = RuntimeSession(session_id="sess-outcome-01", state=ClawState.CAPTURE)
    ctrl, _ = _make(session, tmp_path)
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/session/current/outcome").json()
    assert data is None


def test_outcome_returns_summary_when_completed(tmp_path: Path) -> None:
    session = RuntimeSession(
        session_id="sess-outcome-02",
        state=ClawState.COMPLETED,
        terminal_outcome=TerminalOutcome.STOPPED_BY_OPERATOR,
    )
    ctrl, _ = _make(session, tmp_path)
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/session/current/outcome").json()
    assert data is not None
    assert data["terminal_outcome"] == "stopped_by_operator"
    assert data["state"] == "completed"
    assert data["session_id"] == "sess-outcome-02"


def test_outcome_returns_summary_when_failed(tmp_path: Path) -> None:
    session = RuntimeSession(
        session_id="sess-outcome-03",
        state=ClawState.FAILED,
        terminal_outcome=TerminalOutcome.FAILED,
    )
    ctrl, _ = _make(session, tmp_path)
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/session/current/outcome").json()
    assert data is not None
    assert data["terminal_outcome"] == "failed"


# ------------------------------------------------------------------ #
# Outcome explanation fields                                           #
# ------------------------------------------------------------------ #


def test_outcome_stop_reason_populated_for_stopped_by_operator(tmp_path: Path) -> None:
    """GET /api/v1/session/current/outcome must populate stop_reason for operator stops."""
    session = RuntimeSession(
        session_id="sess-outcome-04",
        state=ClawState.COMPLETED,
        terminal_outcome=TerminalOutcome.STOPPED_BY_OPERATOR,
    )
    ctrl, _ = _make(session, tmp_path)
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/session/current/outcome").json()
    assert data is not None
    assert data["stop_reason"] == "stopped_by_operator", (
        "stop_reason must carry the terminal outcome value for operator-initiated stops"
    )
    assert data["failure_explanation"] is None


def test_outcome_stop_reason_populated_for_released_control(tmp_path: Path) -> None:
    """GET /api/v1/session/current/outcome must populate stop_reason for released control."""
    session = RuntimeSession(
        session_id="sess-outcome-05",
        state=ClawState.COMPLETED,
        terminal_outcome=TerminalOutcome.RELEASED_CONTROL,
    )
    ctrl, _ = _make(session, tmp_path)
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/session/current/outcome").json()
    assert data is not None
    assert data["stop_reason"] == "released_control"
    assert data["failure_explanation"] is None


def test_outcome_failure_explanation_populated_for_failed_session(tmp_path: Path) -> None:
    """GET /api/v1/session/current/outcome must populate failure_explanation for failed sessions."""
    session = RuntimeSession(
        session_id="sess-outcome-06",
        state=ClawState.FAILED,
        terminal_outcome=TerminalOutcome.FAILED,
        latest_message="consecutive bad frames exceeded limit",
    )
    ctrl, _ = _make(session, tmp_path)
    client = TestClient(build_app(controller=ctrl))
    data = client.get("/api/v1/session/current/outcome").json()
    assert data is not None
    assert data["failure_explanation"] is not None, (
        "failure_explanation must be populated so operator can review why the session failed"
    )
    assert (
        "bad frames" in data["failure_explanation"].lower()
        or "failed" in data["failure_explanation"].lower()
    )
    assert data["stop_reason"] is None
