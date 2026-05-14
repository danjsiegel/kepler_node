"""Cross-cutting service interfaces owned by orchestration."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field


class NetworkMode(StrEnum):
    """Supported high-level node network modes."""

    HOME_WIFI_CLIENT = "home_wifi_client"
    FIELD_HOTSPOT = "field_hotspot"


class TimeSource(StrEnum):
    """Trusted time sources supported by the v1 spec."""

    GPS = "gps"
    NETWORK = "network"
    RTC = "rtc"
    OPERATOR_CONFIRMED = "operator_confirmed"
    UNTRUSTED = "untrusted"


class DeviceActivityEventType(StrEnum):
    """Normalized device-activity events used for authorship and conflict checks."""

    MOUNT_SLEW_STARTED = "mount_slew_started"
    MOUNT_SLEW_COMPLETED = "mount_slew_completed"
    MOUNT_SYNC_APPLIED = "mount_sync_applied"
    CAPTURE_STARTED = "capture_started"
    CAPTURE_COMPLETED = "capture_completed"


class DeviceActivityEvent(BaseModel):
    """Semantic device event emitted by hardware adapters."""

    event_type: DeviceActivityEventType
    observed_at: datetime
    details: dict[str, str] = Field(default_factory=dict)


class ServiceHealth(BaseModel):
    """Health status for a managed local service."""

    name: str
    healthy: bool
    summary: str
    details: dict[str, str] = Field(default_factory=dict)


class TimeStatus(BaseModel):
    """Time trust and source summary for readiness and status surfaces."""

    trusted: bool
    source: TimeSource
    summary: str
    observed_at: datetime | None = None
    # Non-None when both GPS (valid fix) and NTP are available and disagree by >5 s.
    # Used by _get_degraded() to surface the time_source_mismatch degraded condition.
    gps_ntp_mismatch_seconds: float | None = None


class StorageStatus(BaseModel):
    """Storage readiness summary for the active data root."""

    data_root: Path
    free_bytes: int
    total_bytes: int
    writable: bool
    summary: str


class PowerStatus(BaseModel):
    """Power-integrity summary used by readiness and guard logic."""

    healthy: bool
    summary: str
    undervoltage_detected: bool = False


class ReadinessCondition(BaseModel):
    """Structured blocker or degraded condition for operator-facing APIs."""

    name: str
    severity: str
    summary: str
    operator_action_required: str | None = None
    details: dict[str, str] = Field(default_factory=dict)


class NodeManagementBackend(Protocol):
    """Node-management adapter contract used by Kepler orchestration."""

    def network_mode(self) -> NetworkMode:
        """Return the current node network mode."""

    def service_health(self) -> list[ServiceHealth]:
        """Return the current local service-health summary."""

    def time_status(self) -> TimeStatus:
        """Return the current trusted-time summary."""

    def storage_status(self) -> StorageStatus:
        """Return the current storage-readiness summary."""

    def power_status(self) -> PowerStatus:
        """Return the current power-integrity summary."""

    def confirm_time(self, timestamp: datetime) -> TimeStatus:
        """Apply an operator-confirmed timestamp and return the resulting summary."""
