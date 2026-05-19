"""Frame ingestion seam for Kepler v1.1.

Converts a newly landed Ekos frame (path + quality analysis result) into a
persistent ``FrameRecord`` and writes it to the session store.

This module is the bridge between the ``FrameWatcher`` observation path and the
canonical storage layer.  Orchestration or the watcher callback calls
``ingest_frame()`` after each new frame is analyzed.

Design constraints (v1.1 spec):
- Kepler does not own the capture loop; ingestion is purely observational.
- Every ingested frame gets a deterministic, collision-resistant ``frame_id``
  derived from the file path and capture timestamp.
- ``quality_metrics`` and ``action_decision`` on the record capture the
  analysis outputs so the intervention policy has a durable record.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path

from kepler_node.imaging.frame_quality import FrameQualitySession
from kepler_node.imaging.protocols import QualityCheckResult
from kepler_node.storage.filesystem import FilesystemSessionStore
from kepler_node.storage.models import FrameRecord

_logger = logging.getLogger(__name__)


def _make_frame_id(image_path: Path, capture_timestamp: datetime) -> str:
    """Return a deterministic frame_id from path stem + timestamp."""
    raw = f"{image_path.stem}_{capture_timestamp.isoformat()}"
    digest = hashlib.sha1(raw.encode()).hexdigest()[:12]
    return f"{image_path.stem[:24]}_{digest}"


def _recommendation_to_action(
    session: FrameQualitySession | None,
) -> str | None:
    """Translate the current session recommendation into an action_decision string."""
    if session is None:
        return None
    rec = session.recommendation()
    Rec = FrameQualitySession.Recommendation
    return {
        Rec.CONTINUE: None,
        Rec.WARN: "warn",
        Rec.TRIGGER_AUTOFOCUS: "trigger_autofocus",
        Rec.PAUSE_SENSOR: "pause_sensor",
        Rec.PAUSE_WEATHER: "pause_weather",
    }.get(rec)


def ingest_frame(
    *,
    session_id: str,
    image_path: Path,
    quality_result: QualityCheckResult,
    store: FilesystemSessionStore,
    quality_session: FrameQualitySession | None = None,
    frame_role: str = "science",
    capture_timestamp: datetime | None = None,
    solve_result_summary: dict | None = None,
) -> FrameRecord:
    """Persist a newly landed frame as a ``FrameRecord`` in the session store.

    Args:
        session_id:       Active Kepler session identifier.
        image_path:       Absolute path to the landed frame file.
        quality_result:   Output of ``FrameQualityAnalyzer.analyze()``.
        store:            Filesystem session store to persist into.
        quality_session:  Optional rolling-session tracker; when provided its
                          current ``recommendation()`` is recorded as
                          ``action_decision`` so the intervention policy has a
                          durable record.
        frame_role:       Semantic role of the frame (default: "science").
        capture_timestamp: Timestamp of the capture; defaults to UTC now.
        solve_result_summary: Optional pre-computed solve summary dict; when
                          provided, populates ``FrameRecord.solve_result_summary``
                          so verification solves have an explainable audit trail.

    Returns:
        The persisted ``FrameRecord``.
    """
    ts = capture_timestamp or datetime.now(UTC)
    frame_id = _make_frame_id(image_path, ts)
    action = _recommendation_to_action(quality_session)

    record = FrameRecord(
        frame_id=frame_id,
        frame_role=frame_role,
        capture_timestamp=ts,
        image_path=str(image_path),
        quality_metrics=dict(quality_result.metrics),
        action_decision=action,
        solve_result_summary=solve_result_summary or {},
        raw_metadata={
            "overall": quality_result.overall,
            "summary": quality_result.summary or "",
            "checks": {k: str(v) for k, v in quality_result.checks.items()},
        },
    )

    try:
        store.write_frame_record(session_id, record)
        _logger.debug(
            "ingested frame %s → %s (action=%s)",
            image_path.name,
            quality_result.overall,
            action,
        )
    except Exception:
        _logger.exception("failed to persist frame record for %s", image_path.name)

    return record
