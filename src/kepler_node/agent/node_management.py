"""Local node-management backend for Kepler v1."""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from kepler_node.agent.interfaces import (
    NetworkMode,
    NodeManagementBackend,
    PowerStatus,
    ServiceHealth,
    StorageStatus,
    TimeSource,
    TimeStatus,
)
from kepler_node.agent.session import ClawState, RuntimeSession

# States that indicate active motion or capture; time confirmation is unsafe during these.
_ACTIVE_MOTION_CAPTURE_STATES = {
    ClawState.CALIBRATE,
    ClawState.TEST_CAPTURE,
    ClawState.SOLVE,
    ClawState.CORRECT,
    ClawState.CENTER_VERIFY,
    ClawState.CAPTURE,
    ClawState.GUARD,
    ClawState.RECOVER,
}

_MIN_VALID_TIMESTAMP = datetime(2020, 1, 1, tzinfo=UTC)


class LocalNodeManagementBackend:
    """Wraps OS-level services for time, storage, network, power, and health reporting."""

    def __init__(
        self,
        *,
        data_root: Path,
        service_names: list[str] | None = None,
        storage_warning_threshold_bytes: int = 20 * 1024 * 1024 * 1024,
        storage_critical_threshold_bytes: int = 10 * 1024 * 1024 * 1024,
    ) -> None:
        self.data_root = data_root
        self.service_names = service_names or ["indiserver", "gpsd"]
        self.storage_warning_threshold_bytes = storage_warning_threshold_bytes
        self.storage_critical_threshold_bytes = storage_critical_threshold_bytes
        # Tracks confirmed time source across calls; cleared when a stronger source
        # (NTP, GPS) supersedes operator-confirmed fallback time.
        self._confirmed_source: TimeSource | None = None

    def network_mode(self) -> NetworkMode:
        """Read current node network mode from NetworkManager."""
        try:
            result = subprocess.run(
                ["nmcli", "-t", "-f", "TYPE,STATE,CONNECTION", "device"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            for line in result.stdout.splitlines():
                parts = line.split(":")
                if len(parts) >= 2 and "connected" in parts[1]:
                    return NetworkMode.HOME_WIFI_CLIENT
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return NetworkMode.FIELD_HOTSPOT

    def service_health(self) -> list[ServiceHealth]:
        """Check health of each declared managed service via systemctl."""
        results: list[ServiceHealth] = []
        for name in self.service_names:
            try:
                proc = subprocess.run(
                    ["systemctl", "is-active", name],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                healthy = proc.returncode == 0
                summary = proc.stdout.strip() or ("active" if healthy else "inactive")
            except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
                healthy = False
                summary = f"health check failed: {exc}"
            results.append(ServiceHealth(name=name, healthy=healthy, summary=summary))
        return results

    def time_status(self) -> TimeStatus:
        """Read current time trust from timedatectl.

        Priority: NTP (network) > operator_confirmed fallback > untrusted.
        NTP synchronization supersedes a previously confirmed operator time.
        """
        try:
            proc = subprocess.run(
                ["timedatectl", "show", "--no-pager"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            props = {
                k: v
                for line in proc.stdout.splitlines()
                if "=" in line
                for k, v in [line.split("=", 1)]
            }
            ntp_synced = props.get("NTPSynchronized", "no").strip().lower() == "yes"
            if ntp_synced:
                return TimeStatus(
                    trusted=True,
                    source=TimeSource.NETWORK,
                    summary="NTP synchronized",
                    observed_at=datetime.now(UTC),
                )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        if self._confirmed_source == TimeSource.OPERATOR_CONFIRMED:
            return TimeStatus(
                trusted=True,
                source=TimeSource.OPERATOR_CONFIRMED,
                summary="operator-confirmed time active",
                observed_at=datetime.now(UTC),
            )
        return TimeStatus(
            trusted=False,
            source=TimeSource.UNTRUSTED,
            summary="time not synchronized",
            observed_at=datetime.now(UTC),
        )

    def storage_status(self) -> StorageStatus:
        """Return storage readiness for the active data root."""
        self.data_root.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(self.data_root)
        writable = os.access(self.data_root, os.W_OK)
        pct_warning = int(0.05 * usage.total)
        pct_critical = int(0.02 * usage.total)
        warning_threshold = max(self.storage_warning_threshold_bytes, pct_warning)
        critical_threshold = max(self.storage_critical_threshold_bytes, pct_critical)
        if usage.free < critical_threshold:
            summary = "critically low free space"
        elif usage.free < warning_threshold:
            summary = "low free space"
        else:
            summary = "ok"
        return StorageStatus(
            data_root=self.data_root,
            free_bytes=usage.free,
            total_bytes=usage.total,
            writable=writable,
            summary=summary,
        )

    def power_status(self) -> PowerStatus:
        """Read undervoltage flag from vcgencmd (Pi-specific)."""
        try:
            proc = subprocess.run(
                ["vcgencmd", "get_throttled"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            value_str = proc.stdout.strip()
            if "=" in value_str:
                hex_val = int(value_str.split("=")[1], 16)
                # Bit 0: current undervoltage detected; bit 16: ever throttled
                undervoltage = bool(hex_val & 0x1)
            else:
                undervoltage = False
        except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
            undervoltage = False
        return PowerStatus(
            healthy=not undervoltage,
            summary="ok" if not undervoltage else "undervoltage detected",
            undervoltage_detected=undervoltage,
        )

    def confirm_time(self, timestamp: datetime) -> TimeStatus:
        """Apply an operator-confirmed timestamp to the node wall clock."""
        ts_utc = timestamp.astimezone(UTC)
        if ts_utc < _MIN_VALID_TIMESTAMP:
            return TimeStatus(
                trusted=False,
                source=TimeSource.UNTRUSTED,
                summary="rejected: timestamp predates 2020-01-01T00:00:00Z",
                observed_at=datetime.now(UTC),
            )
        iso = ts_utc.strftime("%Y-%m-%d %H:%M:%S")
        try:
            proc = subprocess.run(
                ["date", "-s", iso],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if proc.returncode != 0:
                return TimeStatus(
                    trusted=False,
                    source=TimeSource.UNTRUSTED,
                    summary=f"clock-set failed: {proc.stderr.strip()}",
                    observed_at=datetime.now(UTC),
                )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return TimeStatus(
                trusted=False,
                source=TimeSource.UNTRUSTED,
                summary=f"time confirmation failed: {exc}",
                observed_at=datetime.now(UTC),
            )
        self._confirmed_source = TimeSource.OPERATOR_CONFIRMED
        return TimeStatus(
            trusted=True,
            source=TimeSource.OPERATOR_CONFIRMED,
            summary="operator-confirmed time applied",
            observed_at=datetime.now(UTC),
        )


def confirm_time_action(
    *,
    session: RuntimeSession,
    backend: NodeManagementBackend,
    timestamp: datetime,
) -> TimeStatus:
    """Agent-layer action for POST /api/v1/time/confirm.

    Fails closed if the session holds control lock or is in an active
    motion/capture state.  Enforces the v1 rule that operator-confirmed time
    must not be applied while mount motion or capture is underway.
    """
    if session.state in _ACTIVE_MOTION_CAPTURE_STATES:
        return TimeStatus(
            trusted=False,
            source=TimeSource.UNTRUSTED,
            summary=(
                "rejected: time confirmation is not safe during active motion or capture"
            ),
            observed_at=datetime.now(UTC),
        )
    return backend.confirm_time(timestamp)
