"""INDI broker / semaphore boundary for Kepler v1.1.

The broker owns:
- indiserver lifecycle
- driver-profile selection
- driver startup and connection posture
- basic driver-management API or UI for maintenance use

The broker is the semaphore or gate around the shared INDI path.

``indiwebmanager`` is explicitly accepted as a v1.1 implementation choice.
In that posture the broker owns the indiserver lifecycle and profile
management around INDI on port 7624.

ClawController only consumes the normalized ``BrokerSnapshot``; it does
not replace the broker's job, and the broker does not replace Kepler's
supervisory state or intervention policy.

Spec reference: V1_1_HANDOFF.md §INDI Broker / Semaphore (lines 139-165)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from kepler_node.agent.absolute_state import BrokerRuntimeState

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Broker snapshot
# ---------------------------------------------------------------------------


class BrokerSnapshot:
    """Normalized snapshot of the INDI broker layer.

    Attributes:
        broker_state:          Normalized state of the broker.
        profile_active:        Name of the active INDI driver profile, or None.
        device_path_available: True when the underlying indiserver / device
                               path appears to be accepting connections.
        confirmed_at:          UTC timestamp when this snapshot was taken.
    """

    __slots__ = ("broker_state", "profile_active", "device_path_available", "confirmed_at")

    def __init__(
        self,
        *,
        broker_state: BrokerRuntimeState = BrokerRuntimeState.UNKNOWN,
        profile_active: str | None = None,
        device_path_available: bool = False,
        confirmed_at: datetime | None = None,
    ) -> None:
        self.broker_state = broker_state
        self.profile_active = profile_active
        self.device_path_available = device_path_available
        self.confirmed_at = confirmed_at or datetime.now(UTC)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"BrokerSnapshot(state={self.broker_state}, "
            f"profile={self.profile_active!r}, "
            f"device_available={self.device_path_available})"
        )


# ---------------------------------------------------------------------------
# Protocol (replaceable boundary)
# ---------------------------------------------------------------------------


@runtime_checkable
class BrokerBackend(Protocol):
    """Replaceable boundary for the INDI broker / semaphore layer.

    ClawController depends on this protocol, never on a concrete class, so
    implementations can be swapped without touching orchestration.
    """

    def snapshot(self) -> BrokerSnapshot:
        """Return a normalized snapshot of the current broker state."""
        ...

    def is_reachable(self) -> bool:
        """Return True when the broker endpoint is reachable.

        This is a cheap liveness probe that does not parse full state.
        """
        ...


# ---------------------------------------------------------------------------
# Stub / null backend (safe default)
# ---------------------------------------------------------------------------


class StubBrokerBackend:
    """No-op broker backend used in tests and environments without a broker.

    Returns a ``READY`` snapshot so the supervisory layer can operate
    without a live broker.  Tests that require a specific broker state
    should build their own fake.
    """

    def snapshot(self) -> BrokerSnapshot:
        return BrokerSnapshot(
            broker_state=BrokerRuntimeState.READY,
            device_path_available=True,
        )

    def is_reachable(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# indiwebmanager-backed production backend
# ---------------------------------------------------------------------------


class IndiWebManagerBrokerBackend:
    """HTTP-backed broker backend using the indiwebmanager REST API.

    indiwebmanager exposes a simple HTTP API (default port 8624) that
    reports server status, lists profiles, and starts / stops the server.
    This backend polls the status endpoint to build a normalized
    ``BrokerSnapshot``.

    The HTTP client is the standard-library ``urllib`` so no additional
    dependencies are introduced.

    Configuration:
        host: hostname or IP where indiwebmanager is running (default: localhost)
        port: port where indiwebmanager is listening (default: 8624)
        timeout_seconds: per-request timeout (default: 3.0)
    """

    def __init__(
        self,
        *,
        host: str = "localhost",
        port: int = 8624,
        timeout_seconds: float = 3.0,
    ) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout_seconds
        self._base_url = f"http://{host}:{port}"

    def _get_json(self, path: str) -> dict | None:
        """Fetch JSON from the given path; return None on any error."""
        import json
        import urllib.error
        import urllib.request

        url = f"{self._base_url}{path}"
        try:
            with urllib.request.urlopen(url, timeout=self._timeout) as resp:
                return json.loads(resp.read().decode())
        except (urllib.error.URLError, OSError, ValueError) as exc:
            _logger.debug("IndiWebManagerBrokerBackend: GET %s failed: %s", path, exc)
            return None

    def snapshot(self) -> BrokerSnapshot:
        """Return a normalized broker snapshot from indiwebmanager.

        Endpoint semantics:
        - ``GET /api/server/status`` returns ``{"status": "True"/"False"}``
        - ``GET /api/profiles`` returns a list of profile objects with ``name`` and
          ``autostart``; the first autostart profile is considered active.

        When the endpoint is unreachable, returns an UNAVAILABLE snapshot.
        When the server reports stopped, returns a DEGRADED snapshot.
        """
        now = datetime.now(UTC)

        status_data = self._get_json("/api/server/status")
        if status_data is None:
            return BrokerSnapshot(
                broker_state=BrokerRuntimeState.UNAVAILABLE,
                confirmed_at=now,
            )

        server_running = str(status_data.get("status", "False")).lower() == "true"
        if not server_running:
            return BrokerSnapshot(
                broker_state=BrokerRuntimeState.DEGRADED,
                device_path_available=False,
                confirmed_at=now,
            )

        # Server is running; try to identify the active profile.
        profile_active: str | None = None
        profiles_data = self._get_json("/api/profiles")
        if isinstance(profiles_data, list):
            for profile in profiles_data:
                if profile.get("autostart"):
                    profile_active = profile.get("name")
                    break
            if profile_active is None and profiles_data:
                # Fallback: first profile if none is marked autostart
                profile_active = profiles_data[0].get("name")

        return BrokerSnapshot(
            broker_state=BrokerRuntimeState.READY,
            profile_active=profile_active,
            device_path_available=True,
            confirmed_at=now,
        )

    def is_reachable(self) -> bool:
        """Return True when indiwebmanager responds on the status endpoint."""
        return self._get_json("/api/server/status") is not None
