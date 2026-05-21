"""Tests for the frame ingestion seam (imaging/ingestion.py).

Covers:
- ingest_frame() creates and persists a FrameRecord in the session store
- quality_metrics and action_decision are populated from QualityCheckResult
- rolling session recommendation is reflected in action_decision
- frame_id is deterministic and collision-resistant
- ingestion survives a store write failure without raising
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from kepler_node.imaging.frame_quality import FrameQualitySession
from kepler_node.imaging.ingestion import _make_frame_id, ingest_frame
from kepler_node.imaging.protocols import QualityCheckResult, QualityClassification
from kepler_node.storage.filesystem import FilesystemSessionStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pass_result(hfr: float = 2.0, stars: int = 8, hot_px: int = 5) -> QualityCheckResult:
    return QualityCheckResult(
        overall=QualityClassification.PASS,
        checks={"focus": QualityClassification.PASS, "tracking": QualityClassification.PASS},
        metrics={"hfr_mean": hfr, "star_count": float(stars), "hot_pixel_count": float(hot_px)},
        summary=f"pass — {stars} stars, HFR {hfr:.1f}px",
    )


def _fail_result() -> QualityCheckResult:
    return QualityCheckResult(
        overall=QualityClassification.FAIL,
        checks={"focus": QualityClassification.FAIL},
        metrics={"hfr_mean": 12.0, "star_count": 3.0, "hot_pixel_count": 2.0},
        summary="focus fail HFR=12.0px",
    )


# ---------------------------------------------------------------------------
# frame_id generation
# ---------------------------------------------------------------------------


def test_frame_id_is_deterministic() -> None:
    p = Path("/fake/frame_0001.fits")
    ts = datetime(2025, 6, 1, 2, 0, 0, tzinfo=UTC)
    assert _make_frame_id(p, ts) == _make_frame_id(p, ts)


def test_frame_id_differs_for_different_timestamps() -> None:
    p = Path("/fake/frame_0001.fits")
    ts1 = datetime(2025, 6, 1, 2, 0, 0, tzinfo=UTC)
    ts2 = datetime(2025, 6, 1, 2, 0, 1, tzinfo=UTC)
    assert _make_frame_id(p, ts1) != _make_frame_id(p, ts2)


def test_frame_id_differs_for_different_paths() -> None:
    ts = datetime(2025, 6, 1, 2, 0, 0, tzinfo=UTC)
    p1 = Path("/fake/frame_0001.fits")
    p2 = Path("/fake/frame_0002.fits")
    assert _make_frame_id(p1, ts) != _make_frame_id(p2, ts)


# ---------------------------------------------------------------------------
# ingest_frame: basic persistence
# ---------------------------------------------------------------------------


def test_ingest_frame_creates_frame_record(tmp_path: Path) -> None:
    store = FilesystemSessionStore(tmp_path)
    result = _pass_result()
    image_path = Path("/ekos/output/frame_0001.fits")

    record = ingest_frame(
        session_id="sess-001",
        image_path=image_path,
        quality_result=result,
        store=store,
    )

    assert record.frame_id
    assert record.image_path == str(image_path)
    assert record.frame_role == "science"
    assert record.quality_metrics["hfr_mean"] == 2.0
    assert record.action_decision is None  # no rolling session → no action


def test_ingest_frame_writes_to_store(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from kepler_node.storage.models import SessionRecord
    from kepler_node.agent.session import ClawState

    store = FilesystemSessionStore(tmp_path)
    ts = datetime(2025, 6, 1, 2, 0, 0, tzinfo=UTC)
    # Create the session directory first so _resolve_session_dir can find it
    store.write_session_record(
        SessionRecord(
            session_id="sess-001",
            started_at=ts,
            updated_at=ts,
            state=ClawState.CAPTURE,
        )
    )

    result = _pass_result()
    image_path = Path("/ekos/output/frame_0001.fits")

    record = ingest_frame(
        session_id="sess-001",
        image_path=image_path,
        quality_result=result,
        store=store,
        capture_timestamp=ts,
    )

    # Verify the frame.json was actually written
    frame_dir = store.sessions_root / "2025" / "sess-001" / "frames" / record.frame_id
    assert (frame_dir / "frame.json").exists()


def test_ingest_frame_raw_metadata_contains_overall(tmp_path: Path) -> None:
    store = FilesystemSessionStore(tmp_path)
    result = _fail_result()
    record = ingest_frame(
        session_id="sess-002",
        image_path=Path("/fake/frame.fits"),
        quality_result=result,
        store=store,
    )
    assert record.raw_metadata["overall"] == "fail"
    assert "focus fail" in record.raw_metadata["summary"]


def test_ingest_frame_custom_role(tmp_path: Path) -> None:
    store = FilesystemSessionStore(tmp_path)
    record = ingest_frame(
        session_id="sess-003",
        image_path=Path("/fake/frame.fits"),
        quality_result=_pass_result(),
        store=store,
        frame_role="verification",
    )
    assert record.frame_role == "verification"


def test_ingest_frame_explicit_timestamp(tmp_path: Path) -> None:
    store = FilesystemSessionStore(tmp_path)
    ts = datetime(2025, 8, 15, 3, 30, 0, tzinfo=UTC)
    record = ingest_frame(
        session_id="sess-004",
        image_path=Path("/fake/frame.fits"),
        quality_result=_pass_result(),
        store=store,
        capture_timestamp=ts,
    )
    assert record.capture_timestamp == ts


# ---------------------------------------------------------------------------
# ingest_frame: rolling session recommendation → action_decision
# ---------------------------------------------------------------------------


def test_ingest_frame_action_continue_is_none(tmp_path: Path) -> None:
    """'continue' recommendation should not produce an action_decision."""
    session = FrameQualitySession()
    # Fewer than 3 frames → CONTINUE
    store = FilesystemSessionStore(tmp_path)
    record = ingest_frame(
        session_id="sess-005",
        image_path=Path("/fake/frame.fits"),
        quality_result=_pass_result(),
        store=store,
        quality_session=session,
    )
    assert record.action_decision is None


def test_ingest_frame_action_trigger_autofocus(tmp_path: Path) -> None:
    """HFR drift should produce action_decision='trigger_autofocus'."""
    import numpy as np

    session = FrameQualitySession(window_size=10, hfr_drift_fraction=0.25)

    def _hfr_result(hfr: float) -> QualityCheckResult:
        return QualityCheckResult(
            overall=QualityClassification.PASS,
            checks={"focus": QualityClassification.PASS},
            metrics={"hfr_mean": hfr, "star_count": 6.0, "hot_pixel_count": 0.0},
            summary="pass",
        )

    # 3 baseline frames at HFR 3.0 then 3 drifted frames at HFR 5.0 (>25% drift)
    for _ in range(3):
        session.add(_hfr_result(3.0))
    for _ in range(3):
        session.add(_hfr_result(5.0))

    store = FilesystemSessionStore(tmp_path)
    record = ingest_frame(
        session_id="sess-006",
        image_path=Path("/fake/frame.fits"),
        quality_result=_hfr_result(5.0),
        store=store,
        quality_session=session,
    )
    assert record.action_decision == "trigger_autofocus"


def test_ingest_frame_action_pause_sensor(tmp_path: Path) -> None:
    """Hot-pixel accumulation should produce action_decision='pause_sensor'."""
    session = FrameQualitySession(window_size=10, hot_pixel_accumulation=30)

    def _hp_result(hp: float) -> QualityCheckResult:
        return QualityCheckResult(
            overall=QualityClassification.PASS,
            checks={},
            metrics={"hfr_mean": 3.0, "star_count": 6.0, "hot_pixel_count": hp},
            summary="pass",
        )

    for _ in range(3):
        session.add(_hp_result(5.0))
    for _ in range(4):
        session.add(_hp_result(60.0))

    store = FilesystemSessionStore(tmp_path)
    record = ingest_frame(
        session_id="sess-007",
        image_path=Path("/fake/frame.fits"),
        quality_result=_hp_result(60.0),
        store=store,
        quality_session=session,
    )
    assert record.action_decision == "pause_sensor"


# ---------------------------------------------------------------------------
# ingest_frame: survives store failure
# ---------------------------------------------------------------------------


def test_ingest_frame_survives_write_failure(tmp_path: Path) -> None:
    """ingest_frame should not raise when the store write fails."""
    from unittest.mock import patch

    store = FilesystemSessionStore(tmp_path)
    with patch.object(store, "write_frame_record", side_effect=OSError("disk full")):
        # Must not raise
        record = ingest_frame(
            session_id="sess-008",
            image_path=Path("/fake/frame.fits"),
            quality_result=_pass_result(),
            store=store,
        )
    assert record.frame_id  # record still returned even though write failed
