from datetime import UTC, datetime
from pathlib import Path

import pytest

from kepler_node.agent import ClawState, WorkflowIntent
from kepler_node.storage import (
    ArtifactKind,
    ArtifactReference,
    EventRecord,
    EventSeverity,
    EventType,
    FilesystemSessionStore,
    FrameRecord,
    SessionRecord,
    SessionScope,
)


def test_filesystem_session_store_writes_v1_layout(tmp_path: Path) -> None:
    store = FilesystemSessionStore(tmp_path / "data")
    started_at = datetime(2026, 5, 11, 21, 30, 45, tzinfo=UTC)
    session_id = "session-20260511T213045Z-a3f9b2"

    session = SessionRecord(
        session_id=session_id,
        started_at=started_at,
        updated_at=started_at,
        state=ClawState.TARGET_ACQUIRED,
        target_source="manual",
        target_label="M51",
        ra_hours=13.5,
        dec_deg=47.2,
        equipment_profile_id="starter-rig-home",
        operating_mode="headless_remote_ekos",
        site_summary={"site_name": "Home"},
        time_source_summary={"source": "gps", "trusted": True},
        selected_inline_run_parameters={"exposure_seconds": 30, "stop_condition": "frame_count"},
    )

    session_dir = store.write_session_record(session)
    assert (session_dir / "session.json").exists()
    assert (session_dir / "frames").is_dir()
    assert (session_dir / "artifacts").is_dir()

    event = EventRecord(
        timestamp=started_at,
        session_scope=SessionScope.SESSION,
        session_id=session_id,
        sequence=1,
        event_type=EventType.STATE_TRANSITION,
        state=ClawState.TARGET_ACQUIRED,
        severity=EventSeverity.INFO,
        message="Target accepted",
    )
    event_path = store.append_event(session_id, event)
    assert event_path.exists()
    lines = event_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert '"event_type":"state_transition"' in lines[0]
    assert '"site_name": "Home"' in (session_dir / "session.json").read_text(encoding="utf-8")

    frame = FrameRecord(
        frame_id="frame-000001",
        frame_role="target_centering",
        workflow_intent=WorkflowIntent.TARGET_CENTERING,
        capture_timestamp=started_at,
        image_path="frames/frame-000001/IMG_0001.RAF",
        artifact_references=[
            ArtifactReference(
                artifact_kind=ArtifactKind.SOLVE_PROXY,
                relative_path="artifacts/frame-000001-solve.jpg",
            )
        ],
        action_decision="continue",
    )
    frame_dir = store.write_frame_record(session_id, frame)
    assert (frame_dir / "frame.json").exists()
    frame_text = (frame_dir / "frame.json").read_text(encoding="utf-8")
    assert '"frame_id": "frame-000001"' in frame_text
    assert '"artifact_kind": "solve_proxy"' in frame_text


def test_filesystem_session_store_raises_for_unknown_session(tmp_path: Path) -> None:
    store = FilesystemSessionStore(tmp_path / "data")

    with pytest.raises(FileNotFoundError):
        store.append_event(
            "session-missing",
            EventRecord(
                timestamp=datetime(2026, 5, 11, 21, 30, 45, tzinfo=UTC),
                session_scope=SessionScope.SESSION,
                session_id="session-missing",
                sequence=1,
                event_type=EventType.WARNING,
                state=ClawState.READY,
                severity=EventSeverity.WARNING,
                message="Missing session",
            ),
        )


def test_storage_and_quality_models_enforce_phase1_contracts() -> None:
    from kepler_node.agent.interfaces import (
        DeviceActivityEvent,
        DeviceActivityEventType,
        StorageStatus,
    )
    from kepler_node.camera import CameraSettings, CaptureRequest, ShutterPreference
    from kepler_node.imaging import QualityCheckResult, QualityClassification

    storage_status = StorageStatus(
        data_root=Path("/tmp/kepler-data"),
        free_bytes=50,
        total_bytes=100,
        writable=True,
        summary="healthy",
    )
    assert storage_status.total_bytes == 100

    capture_request = CaptureRequest(
        exposure_seconds=5.0,
        settings=CameraSettings(iso=400),
        destination_dir=Path("/tmp/frames"),
        shutter_preference=ShutterPreference.ELECTRONIC_PREFERRED,
    )
    assert capture_request.shutter_preference == ShutterPreference.ELECTRONIC_PREFERRED

    quality_result = QualityCheckResult(
        overall=QualityClassification.WARN,
        checks={"focus": QualityClassification.PASS, "trailing": QualityClassification.WARN},
    )
    assert quality_result.checks["trailing"] == QualityClassification.WARN

    event = DeviceActivityEvent(
        event_type=DeviceActivityEventType.CAPTURE_STARTED,
        observed_at=datetime(2026, 5, 11, 21, 30, 45, tzinfo=UTC),
    )
    assert event.event_type == DeviceActivityEventType.CAPTURE_STARTED