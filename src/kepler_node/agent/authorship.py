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

# Detail keys used as fingerprints when matching authored vs observed events.
# When both the authored record and the observed event carry any of these keys,
# the values must agree for the event to be considered self-authored.
_FINGERPRINT_KEYS = frozenset({"ra_hours", "dec_deg", "action", "frame_label"})


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
        """Return True if an observed event matches a recent Kepler-issued command.

        Matching first requires the same ``event_type``.  When both the authored
        record and the observed event carry overlapping fingerprint keys (ra_hours,
        dec_deg, action, frame_label), all overlapping key-values must agree.
        If neither side has fingerprint keys the match falls back to event_type
        alone, preserving backward compatibility with adapters that do not yet
        populate details.
        """
        self._prune()
        for authored in self._authored:
            if authored.event_type != event.event_type:
                continue
            authored_fp = {k: v for k, v in authored.details.items() if k in _FINGERPRINT_KEYS}
            observed_fp = {k: v for k, v in event.details.items() if k in _FINGERPRINT_KEYS}
            overlap_keys = authored_fp.keys() & observed_fp.keys()
            if overlap_keys:
                if all(authored_fp[k] == observed_fp[k] for k in overlap_keys):
                    return True
            else:
                # No fingerprint keys on either or both sides: event_type match suffices.
                return True
        return False

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
