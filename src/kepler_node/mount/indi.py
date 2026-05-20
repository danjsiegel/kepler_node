"""INDI-backed mount adapter for Kepler v1."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from typing import Iterable

from kepler_node.agent.interfaces import DeviceActivityEvent, DeviceActivityEventType
from kepler_node.mount.protocols import MountPosition


class INDIMountBackend:
    """Mount adapter using indi_getprop and indi_setprop subprocesses.

    All motion and sync commands authored by Kepler are recorded as normalized
    ``DeviceActivityEvent`` values available via ``activity_events()``.  The
    authorship tracker (``agent.authorship.AuthorshipTracker``) consumes these
    events to enable conflict detection by orchestration.
    """

    def __init__(
        self,
        *,
        host: str = "localhost",
        port: int = 7624,
        device_name: str = "PMC Eight",
        slew_complete_tolerance_deg: float = 1 / 60,  # 1 arcminute
    ) -> None:
        self._host = host
        self._port = port
        self._device_name = device_name
        self._slew_complete_tolerance_deg = slew_complete_tolerance_deg
        self._connected = False
        self._pending_events: list[DeviceActivityEvent] = []
        # Tracks the pending slew target so poll_activity() can detect completion.
        self._slewing_to: MountPosition | None = None
        # True while an externally-initiated (not Kepler-authored) slew is ongoing.
        # Used to avoid emitting repeated events for the same external slew.
        self._external_slew_active: bool = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_mount_slewing(self) -> bool:
        """Return True when INDI reports the mount is currently slewing.

        Reads the TELESCOPE_SLEWING.SLEWING property, which is set to ``On``
        when the mount is in motion.  Returns False on any read failure.
        """
        try:
            raw = self._getprop(f"{self._device_name}.TELESCOPE_SLEWING.SLEWING")
            return "On" in raw
        except Exception:
            return False

    def _getprop(self, property_path: str, timeout: int = 5) -> str:
        result = subprocess.run(
            [
                "indi_getprop",
                "-h",
                self._host,
                "-p",
                str(self._port),
                property_path,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout.strip()

    def _setprop(self, property_assignment: str, timeout: int = 10) -> int:
        result = subprocess.run(
            [
                "indi_setprop",
                "-h",
                self._host,
                "-p",
                str(self._port),
                property_assignment,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode

    # ------------------------------------------------------------------
    # MountBackend implementation
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Connect to the INDI mount driver."""
        self._setprop(f"{self._device_name}.CONNECTION.CONNECT=On")
        self._connected = True

    def disconnect(self) -> None:
        """Disconnect the INDI mount driver."""
        try:
            self._setprop(f"{self._device_name}.CONNECTION.DISCONNECT=On")
        finally:
            self._connected = False

    def current_position(self) -> MountPosition:
        """Return the current mount equatorial coordinates from INDI."""
        ra_raw = self._getprop(f"{self._device_name}.EQUATORIAL_EOD_COORD.RA")
        dec_raw = self._getprop(f"{self._device_name}.EQUATORIAL_EOD_COORD.DEC")
        try:
            ra_hours = float(ra_raw.split("=")[-1])
            dec_deg = float(dec_raw.split("=")[-1])
        except (ValueError, IndexError) as exc:
            raise RuntimeError(f"Could not parse mount position from INDI output: {exc!r}") from exc
        return MountPosition(ra_hours=ra_hours, dec_deg=dec_deg)

    def slew_to(self, position: MountPosition) -> None:
        """Slew the mount to the target position and record the authored event."""
        self._setprop(f"{self._device_name}.ON_COORD_SET.TRACK=On")
        self._setprop(
            f"{self._device_name}.EQUATORIAL_EOD_COORD.RA={position.ra_hours};"
            f"DEC={position.dec_deg}"
        )
        self._slewing_to = position
        # Clear any external-slew tracking so the Kepler-authored slew takes
        # precedence in poll_activity().
        self._external_slew_active = False
        self._pending_events.append(
            DeviceActivityEvent(
                event_type=DeviceActivityEventType.MOUNT_SLEW_STARTED,
                observed_at=datetime.now(UTC),
                details={
                    "ra_hours": str(position.ra_hours),
                    "dec_deg": str(position.dec_deg),
                    "authored_by": "kepler",
                },
            )
        )

    def sync_to(self, position: MountPosition) -> None:
        """Apply a sync correction and record the authored event."""
        self._setprop(f"{self._device_name}.ON_COORD_SET.SYNC=On")
        self._setprop(
            f"{self._device_name}.EQUATORIAL_EOD_COORD.RA={position.ra_hours};"
            f"DEC={position.dec_deg}"
        )
        self._pending_events.append(
            DeviceActivityEvent(
                event_type=DeviceActivityEventType.MOUNT_SYNC_APPLIED,
                observed_at=datetime.now(UTC),
                details={
                    "ra_hours": str(position.ra_hours),
                    "dec_deg": str(position.dec_deg),
                    "authored_by": "kepler",
                },
            )
        )

    def activity_events(self) -> Iterable[DeviceActivityEvent]:
        """Yield and drain normalized device-activity events authored by Kepler."""
        events, self._pending_events = self._pending_events, []
        yield from events

    def poll_activity(self) -> None:
        """Poll INDI for observed mount activity and queue normalized events.

        When a Kepler-authored slew is pending: compares the current mount
        position against the target to detect completion; emits
        ``MOUNT_SLEW_COMPLETED`` once within tolerance.

        When no Kepler-authored slew is pending: polls the INDI
        ``TELESCOPE_SLEWING.SLEWING`` property to detect externally-initiated
        motion.  If the mount is slewing but Kepler did not initiate it, emits
        ``MOUNT_SLEW_STARTED`` with ``authored_by="external"`` so the
        orchestration layer's conflict detection can react.

        Call this periodically from the orchestration loop.
        """
        if self._slewing_to is None:
            # No Kepler-authored slew pending; check for external motion.
            currently_slewing = self._is_mount_slewing()
            if currently_slewing and not self._external_slew_active:
                self._external_slew_active = True
                self._pending_events.append(
                    DeviceActivityEvent(
                        event_type=DeviceActivityEventType.MOUNT_SLEW_STARTED,
                        observed_at=datetime.now(UTC),
                        details={"authored_by": "external"},
                    )
                )
            elif not currently_slewing:
                self._external_slew_active = False
            return

        try:
            current = self.current_position()
        except (RuntimeError, subprocess.TimeoutExpired, FileNotFoundError):
            return

        ra_diff_deg = abs(current.ra_hours - self._slewing_to.ra_hours) * 15.0
        dec_diff_deg = abs(current.dec_deg - self._slewing_to.dec_deg)

        if (
            ra_diff_deg <= self._slew_complete_tolerance_deg
            and dec_diff_deg <= self._slew_complete_tolerance_deg
        ):
            self._pending_events.append(
                DeviceActivityEvent(
                    event_type=DeviceActivityEventType.MOUNT_SLEW_COMPLETED,
                    observed_at=datetime.now(UTC),
                    details={
                        "ra_hours": str(current.ra_hours),
                        "dec_deg": str(current.dec_deg),
                        "authored_by": "kepler",
                    },
                )
            )
            self._slewing_to = None
