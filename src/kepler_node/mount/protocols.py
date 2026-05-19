"""Mount adapter contracts for Kepler v1."""

from __future__ import annotations

from typing import Iterable, Protocol

from pydantic import BaseModel

from kepler_node.agent.interfaces import DeviceActivityEvent


class MountPosition(BaseModel):
    """Normalized mount pointing coordinates."""

    ra_hours: float
    dec_deg: float


class PointingOffset(BaseModel):
    """Residual pointing error relative to an intended target."""

    delta_ra_arcmin: float
    delta_dec_arcmin: float
    total_arcmin: float


class MountBackend(Protocol):
    """Mount backend contract used by orchestration."""

    def connect(self) -> None:
        """Connect the active mount backend."""

    def disconnect(self) -> None:
        """Disconnect the active mount backend."""

    def current_position(self) -> MountPosition:
        """Return the current mount pointing."""

    def slew_to(self, position: MountPosition) -> None:
        """Physically slew the mount to the target position."""

    def sync_to(self, position: MountPosition) -> None:
        """Apply a sync correction to the mount model."""

    def poll_activity(self) -> None:
        """Poll the mount for current state and populate the activity event queue.

        Call this before draining ``activity_events()`` so the conflict-detection
        pass sees up-to-date slew-completion events from the INDI backend.
        Implementations that push events asynchronously may treat this as a no-op.
        """
        ...

    def activity_events(self) -> Iterable[DeviceActivityEvent]:
        """Yield normalized observed device activity for conflict detection."""
