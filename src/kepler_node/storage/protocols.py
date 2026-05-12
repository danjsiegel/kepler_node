"""Storage backend protocols for Kepler v1."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from kepler_node.storage.models import EventRecord, FrameRecord, SessionRecord


class SessionStore(Protocol):
    """File-backed session persistence contract used by orchestration."""

    def write_session_record(self, record: SessionRecord) -> Path:
        """Persist session.json and return the session directory."""

    def append_event(self, session_id: str, event: EventRecord) -> Path:
        """Append an event record to the session event stream."""

    def write_frame_record(self, session_id: str, frame: FrameRecord) -> Path:
        """Persist frame.json and return the frame directory."""
