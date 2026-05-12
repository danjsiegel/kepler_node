"""Command-authorship tracking and conflict detection for Kepler v1."""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime, timedelta

from kepler_node.agent.interfaces import DeviceActivityEvent, DeviceActivityEventType

# Event types that require authorship matching when control is locked.
_CONFLICT_ELIGIBLE_TYPES = {
    DeviceActivityEventType.MOUNT_SLEW_STARTED,
    DeviceActivityEventType.MOUNT_SYNC_APPLIED,
    DeviceActivityEventType.CAPTURE_STARTED,
}

_DEFAULT_WINDOW_SECONDS = 30.0


class AuthorshipTracker:
    """Maintains a rolling window of Kepler-issued motion and capture commands.

    Adapters call ``record`` when Kepler issues a command.  The orchestration
    layer calls ``is_conflict`` when an observed event arrives to decide whether
    to raise ``external_control_conflict``.
    """

    def __init__(self, window_seconds: float = _DEFAULT_WINDOW_SECONDS) -> None:
        self._window = timedelta(seconds=window_seconds)
        self._authored: deque[DeviceActivityEvent] = deque()

    def record(self, event: DeviceActivityEvent) -> None:
        """Record a Kepler-authored command into the rolling window."""
        self._prune()
        self._authored.append(event)

    def is_authored(self, event: DeviceActivityEvent) -> bool:
        """Return True if an observed event matches a recent Kepler-issued command."""
        self._prune()
        return any(authored.event_type == event.event_type for authored in self._authored)

    def is_conflict(self, event: DeviceActivityEvent, *, control_locked: bool) -> bool:
        """Return True if the observed event looks like an external control conflict.

        Only events of conflict-eligible types are checked.  Expected sidereal
        tracking alone is not a conflict-eligible event.
        """
        if not control_locked:
            return False
        if event.event_type not in _CONFLICT_ELIGIBLE_TYPES:
            return False
        return not self.is_authored(event)

    def _prune(self) -> None:
        """Remove authored events that have aged out of the rolling window."""
        cutoff = datetime.now(UTC) - self._window
        while self._authored and self._authored[0].observed_at < cutoff:
            self._authored.popleft()
