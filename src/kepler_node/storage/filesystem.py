"""Filesystem-backed session persistence for Kepler v1."""

from __future__ import annotations

from pathlib import Path

from kepler_node.storage.models import (
    EquipmentProfile,
    EventRecord,
    FrameRecord,
    InstallManifest,
    SessionRecord,
)


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

    def list_events(
        self,
        session_id: str,
        *,
        limit: int = 50,
        before_sequence: int | None = None,
    ) -> tuple[list[EventRecord], int | None]:
        """Return session events newest-first with optional cursor pagination.

        Returns (page, next_before_sequence).  An unknown cursor raises ValueError.
        """
        session_dir = self._resolve_session_dir(session_id)
        event_path = session_dir / "events.ndjson"
        if not event_path.exists():
            return [], None

        records: list[EventRecord] = []
        for raw in event_path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if raw:
                records.append(EventRecord.model_validate_json(raw))

        records.sort(key=lambda r: r.sequence, reverse=True)

        if before_sequence is not None:
            known = {r.sequence for r in records}
            if before_sequence not in known:
                raise ValueError(f"Unknown before_sequence cursor: {before_sequence}")
            records = [r for r in records if r.sequence < before_sequence]

        page = records[:limit]
        next_cursor: int | None = records[limit - 1].sequence if len(records) > limit else None
        return page, next_cursor

    def list_frames(
        self,
        session_id: str,
        *,
        limit: int = 50,
        before_frame_id: str | None = None,
    ) -> tuple[list[FrameRecord], str | None]:
        """Return frame records newest-first with optional cursor pagination.

        Returns (page, next_before_frame_id).  An unknown cursor raises ValueError.
        """
        session_dir = self._resolve_session_dir(session_id)
        frames_dir = session_dir / "frames"
        if not frames_dir.exists():
            return [], None

        records: list[FrameRecord] = []
        for frame_dir in frames_dir.iterdir():
            frame_json = frame_dir / "frame.json"
            if frame_json.exists():
                records.append(
                    FrameRecord.model_validate_json(frame_json.read_text(encoding="utf-8"))
                )

        records.sort(key=lambda r: r.capture_timestamp, reverse=True)

        if before_frame_id is not None:
            ids = [r.frame_id for r in records]
            if before_frame_id not in ids:
                raise ValueError(f"Unknown before_frame_id cursor: {before_frame_id}")
            idx = ids.index(before_frame_id)
            records = records[idx + 1 :]

        page = records[:limit]
        next_cursor: str | None = records[limit - 1].frame_id if len(records) > limit else None
        return page, next_cursor

    def list_artifacts(self, session_id: str) -> list[dict]:
        """Return typed artifact summaries aggregated from all frame records.

        Frames are visited in ascending capture-time order; callers may re-sort.
        """
        session_dir = self._resolve_session_dir(session_id)
        frames_dir = session_dir / "frames"
        if not frames_dir.exists():
            return []

        frames: list[FrameRecord] = []
        for frame_dir in frames_dir.iterdir():
            frame_json = frame_dir / "frame.json"
            if frame_json.exists():
                frames.append(
                    FrameRecord.model_validate_json(frame_json.read_text(encoding="utf-8"))
                )

        frames.sort(key=lambda r: r.capture_timestamp)

        result: list[dict] = []
        for frame in frames:
            for artifact in frame.artifact_references:
                created_at = artifact.created_at or frame.capture_timestamp
                result.append({
                    "artifact_kind": artifact.artifact_kind,
                    "relative_path": artifact.relative_path,
                    "frame_id": frame.frame_id,
                    "created_at": created_at.isoformat(),
                })
        return result

    def _resolve_session_dir(self, session_id: str) -> Path:
        """Resolve a session directory from the canonical sessions root."""

        matches = list(self.sessions_root.glob(f"*/{session_id}"))
        if not matches:
            raise FileNotFoundError(f"No session directory found for {session_id}")

        return matches[0]

    # ------------------------------------------------------------------ #
    # Equipment profiles                                                   #
    # ------------------------------------------------------------------ #

    @property
    def profiles_root(self) -> Path:
        """Return the root directory that holds equipment profiles."""
        return self.data_root / "profiles"

    def write_profile(self, profile: EquipmentProfile) -> Path:
        """Persist an equipment profile as profiles/<profile_id>.json.

        If ``profile.is_default`` is True, clears ``is_default`` on every
        other stored profile first so at most one profile carries the flag.
        """
        self.profiles_root.mkdir(parents=True, exist_ok=True)
        if profile.is_default:
            self._clear_default_flag(exclude_id=profile.profile_id)
        profile_path = self.profiles_root / f"{profile.profile_id}.json"
        profile_path.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
        return profile_path

    def read_profile(self, profile_id: str) -> EquipmentProfile | None:
        """Return the equipment profile for *profile_id*, or None if not found."""
        profile_path = self.profiles_root / f"{profile_id}.json"
        if not profile_path.exists():
            return None
        return EquipmentProfile.model_validate_json(
            profile_path.read_text(encoding="utf-8")
        )

    def list_profiles(self) -> list[EquipmentProfile]:
        """Return all stored equipment profiles in creation-time order."""
        if not self.profiles_root.exists():
            return []
        profiles: list[EquipmentProfile] = []
        for p in sorted(self.profiles_root.glob("*.json")):
            try:
                profiles.append(
                    EquipmentProfile.model_validate_json(p.read_text(encoding="utf-8"))
                )
            except Exception:
                pass
        return profiles

    def delete_profile(self, profile_id: str) -> bool:
        """Remove the profile file for *profile_id*.  Returns True if found."""
        profile_path = self.profiles_root / f"{profile_id}.json"
        if profile_path.exists():
            profile_path.unlink()
            return True
        return False

    def _clear_default_flag(self, *, exclude_id: str) -> None:
        """Clear ``is_default`` on all profiles except *exclude_id*."""
        if not self.profiles_root.exists():
            return
        for p in self.profiles_root.glob("*.json"):
            if p.stem == exclude_id:
                continue
            try:
                profile = EquipmentProfile.model_validate_json(
                    p.read_text(encoding="utf-8")
                )
                if profile.is_default:
                    profile.is_default = False
                    p.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Install manifest                                                     #
    # ------------------------------------------------------------------ #

    @property
    def install_manifest_path(self) -> Path:
        """Return the canonical path for the install manifest."""
        return self.data_root / "install_manifest.json"

    def write_install_manifest(self, manifest: InstallManifest) -> Path:
        """Persist the install manifest."""
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.install_manifest_path.write_text(
            manifest.model_dump_json(indent=2), encoding="utf-8"
        )
        return self.install_manifest_path

    def read_install_manifest(self) -> InstallManifest | None:
        """Return the stored install manifest, or None if not found."""
        if not self.install_manifest_path.exists():
            return None
        try:
            return InstallManifest.model_validate_json(
                self.install_manifest_path.read_text(encoding="utf-8")
            )
        except Exception:
            return None
