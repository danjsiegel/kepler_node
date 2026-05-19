"""Ekos intervention adapter and read-only observation surface for Kepler v1.1.

Kepler owns *when* to intervene and *why*.  This module provides the write seam
(pause, resume, autofocus, re-verify requests) and the read-only observation
surface (focus position, temperature, capture-state) so the supervisory layer
can make informed decisions without owning the execution path.

The DBusEkosAdapter is the concrete v1.1 transport.  The EkosAdapterProtocol
defines the replaceable boundary so tests and future transports can swap in
without touching orchestration.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from kepler_node.agent.interfaces import DeviceActivityEvent, DeviceActivityEventType

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared data models
# ---------------------------------------------------------------------------


class EkosSequenceStatus:
    """Normalized snapshot of Ekos sequence/capture state.

    Attributes:
        active:       True while a capture sequence is executing.
        paused:       True when the sequence is explicitly paused.
        job_name:     Current job name or None when idle.
        frames_done:  Frames completed in the current job (0 when idle).
        frames_total: Total frames planned for the current job (0 when idle).
        details:      Provider-specific metadata.
    """

    __slots__ = ("active", "paused", "job_name", "frames_done", "frames_total", "details")

    def __init__(
        self,
        *,
        active: bool = False,
        paused: bool = False,
        job_name: str | None = None,
        frames_done: int = 0,
        frames_total: int = 0,
        details: dict[str, str] | None = None,
    ) -> None:
        self.active = active
        self.paused = paused
        self.job_name = job_name
        self.frames_done = frames_done
        self.frames_total = frames_total
        self.details: dict[str, str] = details or {}

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"EkosSequenceStatus(active={self.active}, paused={self.paused}, "
            f"job={self.job_name!r}, done={self.frames_done}/{self.frames_total})"
        )


# ---------------------------------------------------------------------------
# Protocol (replaceable boundary)
# ---------------------------------------------------------------------------


@runtime_checkable
class EkosAdapterProtocol(Protocol):
    """Replaceable boundary for Ekos intervention and observation."""

    def pause(self) -> bool:
        """Request Ekos to pause the active capture sequence.

        Returns True on success, False if the request could not be delivered.
        """
        ...

    def resume(self) -> bool:
        """Request Ekos to resume a paused capture sequence.

        Returns True on success, False if the request could not be delivered.
        """
        ...

    def request_autofocus(self) -> bool:
        """Request Ekos to run its autofocus routine.

        Returns True on success, False if the request could not be delivered.
        """
        ...

    def request_reverify(self) -> bool:
        """Request Ekos to run a re-solve / re-center workflow.

        Returns True on success, False if the request could not be delivered.
        """
        ...

    def status(self) -> EkosSequenceStatus:
        """Return a normalized snapshot of the current sequence/capture state."""
        ...

    def observe(self) -> list[DeviceActivityEvent]:
        """Return any new normalized observation events since the last call.

        Events are drained from the internal queue; each call returns only new
        events.  Observation events (focus, temperature, capture-state changes)
        are yielded here so the supervisory layer can consume them without
        owning the execution path.
        """
        ...

    def poll_focus(self) -> None:
        """Poll for current focus position and queue an observation event."""
        ...

    def poll_temperature(self) -> None:
        """Poll for current camera temperature and queue an observation event."""
        ...

    def poll_sequence_status(self) -> None:
        """Poll for current sequence state and queue a capture-state observation event."""
        ...


# ---------------------------------------------------------------------------
# Stub / null adapter (safe default when Ekos is not reachable)
# ---------------------------------------------------------------------------


class StubEkosAdapter:
    """No-op Ekos adapter used in tests and pre-Ekos readiness stages.

    All write calls log the intent and return True (success) so the policy
    layer can operate without a live Ekos instance.  Observation yields
    no events.
    """

    def pause(self) -> bool:
        _logger.info("StubEkosAdapter.pause() called")
        return True

    def resume(self) -> bool:
        _logger.info("StubEkosAdapter.resume() called")
        return True

    def request_autofocus(self) -> bool:
        _logger.info("StubEkosAdapter.request_autofocus() called")
        return True

    def request_reverify(self) -> bool:
        _logger.info("StubEkosAdapter.request_reverify() called")
        return True

    def status(self) -> EkosSequenceStatus:
        return EkosSequenceStatus()

    def observe(self) -> list[DeviceActivityEvent]:
        return []

    def poll_focus(self) -> None:
        pass

    def poll_temperature(self) -> None:
        pass

    def poll_sequence_status(self) -> None:
        pass


# ---------------------------------------------------------------------------
# DBus-backed production adapter
# ---------------------------------------------------------------------------

# DBus property and interface paths used by KStars/Ekos
_EKOS_DBUS_SERVICE = "org.kde.kstars"
_EKOS_CAPTURE_IFACE = "org.kde.kstars.Ekos.Capture"
_EKOS_FOCUS_IFACE = "org.kde.kstars.Ekos.Focus"
_EKOS_ALIGN_IFACE = "org.kde.kstars.Ekos.Align"
_EKOS_CAPTURE_PATH = "/KStars/Ekos/Capture"
_EKOS_FOCUS_PATH = "/KStars/Ekos/Focus"
_EKOS_ALIGN_PATH = "/KStars/Ekos/Align"


class DBusEkosAdapter:
    """DBus-backed Ekos intervention adapter for Kepler v1.1.

    Uses the KStars/Ekos DBus API to request bounded actions (pause, resume,
    autofocus, re-verify) and to observe device state (focus position,
    temperature, sequence status).

    The dbus-python library (``dbus``) is imported lazily so the module loads
    successfully on systems where the library is absent — in that case the
    adapter degrades gracefully and logs a warning.  Tests can inject a fake
    dbus session via ``_bus`` at construction time.
    """

    def __init__(
        self,
        *,
        session_bus: object | None = None,
        service_name: str = _EKOS_DBUS_SERVICE,
    ) -> None:
        self._service = service_name
        self._bus = session_bus  # injected in tests; lazily acquired otherwise
        self._pending_events: list[DeviceActivityEvent] = []

    # ------------------------------------------------------------------
    # Internal DBus access helpers
    # ------------------------------------------------------------------

    def _get_bus(self) -> object:
        if self._bus is not None:
            return self._bus
        try:
            import dbus  # type: ignore[import-untyped]

            self._bus = dbus.SessionBus()
            return self._bus
        except Exception as exc:
            raise RuntimeError(f"DBus session bus unavailable: {exc}") from exc

    def _capture_iface(self) -> object:
        bus = self._get_bus()
        obj = bus.get_object(self._service, _EKOS_CAPTURE_PATH)  # type: ignore[attr-defined]
        import dbus  # type: ignore[import-untyped]

        return dbus.Interface(obj, dbus_interface=_EKOS_CAPTURE_IFACE)

    def _focus_iface(self) -> object:
        bus = self._get_bus()
        obj = bus.get_object(self._service, _EKOS_FOCUS_PATH)  # type: ignore[attr-defined]
        import dbus  # type: ignore[import-untyped]

        return dbus.Interface(obj, dbus_interface=_EKOS_FOCUS_IFACE)

    def _align_iface(self) -> object:
        bus = self._get_bus()
        obj = bus.get_object(self._service, _EKOS_ALIGN_PATH)  # type: ignore[attr-defined]
        import dbus  # type: ignore[import-untyped]

        return dbus.Interface(obj, dbus_interface=_EKOS_ALIGN_IFACE)

    # ------------------------------------------------------------------
    # Intervention write surface
    # ------------------------------------------------------------------

    def pause(self) -> bool:
        """Request Ekos to pause the active capture sequence via DBus."""
        try:
            iface = self._capture_iface()
            iface.pause()  # type: ignore[attr-defined]
            _logger.info("Ekos capture pause requested via DBus")
            self._pending_events.append(
                DeviceActivityEvent(
                    event_type=DeviceActivityEventType.CAPTURE_SEQUENCE_PAUSED,
                    observed_at=datetime.now(UTC),
                    details={"source": "kepler_intervention"},
                )
            )
            return True
        except Exception as exc:
            _logger.warning("Ekos pause request failed: %s", exc)
            return False

    def resume(self) -> bool:
        """Request Ekos to resume a paused capture sequence via DBus."""
        try:
            iface = self._capture_iface()
            iface.resume()  # type: ignore[attr-defined]
            _logger.info("Ekos capture resume requested via DBus")
            self._pending_events.append(
                DeviceActivityEvent(
                    event_type=DeviceActivityEventType.CAPTURE_SEQUENCE_RESUMED,
                    observed_at=datetime.now(UTC),
                    details={"source": "kepler_intervention"},
                )
            )
            return True
        except Exception as exc:
            _logger.warning("Ekos resume request failed: %s", exc)
            return False

    def request_autofocus(self) -> bool:
        """Request Ekos to run its autofocus routine via DBus."""
        try:
            iface = self._focus_iface()
            iface.start()  # type: ignore[attr-defined]
            _logger.info("Ekos autofocus requested via DBus")
            return True
        except Exception as exc:
            _logger.warning("Ekos autofocus request failed: %s", exc)
            return False

    def request_reverify(self) -> bool:
        """Request Ekos to run a re-solve / re-center workflow via DBus."""
        try:
            iface = self._align_iface()
            iface.captureAndSolve()  # type: ignore[attr-defined]
            _logger.info("Ekos re-verify (captureAndSolve) requested via DBus")
            return True
        except Exception as exc:
            _logger.warning("Ekos re-verify request failed: %s", exc)
            return False

    def status(self) -> EkosSequenceStatus:
        """Return a normalized Ekos sequence status snapshot from DBus."""
        try:
            iface = self._capture_iface()
            state_str: str = str(iface.getSequenceQueueStatus())  # type: ignore[attr-defined]
            paused = "pause" in state_str.lower()
            active = state_str.lower() in {"running", "capturing", "active"}
            return EkosSequenceStatus(active=active, paused=paused, details={"raw_status": state_str})
        except Exception as exc:
            _logger.debug("Ekos status query failed: %s", exc)
            return EkosSequenceStatus(details={"error": str(exc)})

    # ------------------------------------------------------------------
    # Read-only observation surface
    # ------------------------------------------------------------------

    def poll_focus(self) -> None:
        """Poll Ekos for the current focus position and queue an observation event."""
        try:
            iface = self._focus_iface()
            position: int = int(iface.getAutoFocusPosition())  # type: ignore[attr-defined]
            self._pending_events.append(
                DeviceActivityEvent(
                    event_type=DeviceActivityEventType.FOCUS_POSITION_CHANGED,
                    observed_at=datetime.now(UTC),
                    details={"focus_position": str(position)},
                )
            )
        except Exception as exc:
            _logger.debug("Ekos focus poll failed: %s", exc)

    def poll_temperature(self) -> None:
        """Poll INDI/Ekos for the current camera temperature and queue an event."""
        try:
            iface = self._capture_iface()
            temp: float = float(iface.getCoolerTemperature())  # type: ignore[attr-defined]
            self._pending_events.append(
                DeviceActivityEvent(
                    event_type=DeviceActivityEventType.TEMPERATURE_READING,
                    observed_at=datetime.now(UTC),
                    details={"temperature_celsius": f"{temp:.2f}"},
                )
            )
        except Exception as exc:
            _logger.debug("Ekos temperature poll failed: %s", exc)

    def poll_sequence_status(self) -> None:
        """Poll Ekos sequence state and queue capture-state observation events."""
        try:
            current = self.status()
            if current.active or current.paused:
                self._pending_events.append(
                    DeviceActivityEvent(
                        event_type=DeviceActivityEventType.SEQUENCE_STATUS_UPDATED,
                        observed_at=datetime.now(UTC),
                        details={
                            "active": str(current.active),
                            "paused": str(current.paused),
                            "job_name": current.job_name or "",
                        },
                    )
                )
        except Exception as exc:
            _logger.debug("Ekos sequence status poll failed: %s", exc)

    def observe(self) -> list[DeviceActivityEvent]:
        """Drain and return all pending observation events."""
        events, self._pending_events = self._pending_events, []
        return events
