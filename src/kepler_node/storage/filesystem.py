"""Filesystem-backed session persistence for Kepler v1."""

from __future__ import annotations

from pathlib import Path

from kepler_node.storage.models import EventRecord, FrameRecord, SessionRecord


class FilesystemSessionStore:
    """Persist session records in the v1 filesystem layout."""

    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root

    @property
    def sessions_root(self) -> Path:
        """Return the root directory that holds all sessions."""

        return self.data_root / "sessions"

    def write_session_record(self, record: SessionRecord) -> Path:
        """Persist session.json and ensure the canonical directory layout exists."""

        session_dir = self.sessions_root / f"{record.started_at:%Y}" / record.session_id
        (session_dir / "frames").mkdir(parents=True, exist_ok=True)
        (session_dir / "artifacts").mkdir(parents=True, exist_ok=True)
        (session_dir / "session.json").write_text(
            record.model_dump_json(indent=2),
            encoding="utf-8",
        )
        return session_dir

    def read_session_record(self, session_id: str) -> SessionRecord | None:
        """Load and return session.json for *session_id*, or None if not found."""
        try:
            session_dir = self._resolve_session_dir(session_id)
            data = (session_dir / "session.json").read_text(encoding="utf-8")
            return SessionRecord.model_validate_json(data)
        except (FileNotFoundError, Exception):
            return None

    def append_event(self, session_id: str, event: EventRecord) -> Path:
        """Append one event record to the canonical NDJSON event stream."""

        session_dir = self._resolve_session_dir(session_id)
        event_path = session_dir / "events.ndjson"
        with event_path.open("a", encoding="utf-8") as stream:
            stream.write(event.model_dump_json())
            stream.write("\n")
        return event_path

    def write_frame_record(self, session_id: str, frame: FrameRecord) -> Path:
        """Persist frame.json in the canonical per-frame directory."""

        session_dir = self._resolve_session_dir(session_id)
        frame_dir = session_dir / "frames" / frame.frame_id
        frame_dir.mkdir(parents=True, exist_ok=True)
        (frame_dir / "frame.json").write_text(
            frame.model_dump_json(indent=2),
            encoding="utf-8",
        )
        return frame_dir

    def _resolve_session_dir(self, session_id: str) -> Path:
        """Resolve a session directory from the canonical sessions root."""

        matches = list(self.sessions_root.glob(f"*/{session_id}"))
        if not matches:
            raise FileNotFoundError(f"No session directory found for {session_id}")

        return matches[0]
