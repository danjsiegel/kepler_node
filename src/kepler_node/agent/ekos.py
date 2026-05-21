"""Ekos intervention adapter and read-only observation surface for Kepler v1.1.

Kepler owns *when* to intervene and *why*.  This module provides the write seam
(pause, resume, autofocus, re-verify requests) and the read-only observation
surface (focus position, temperature, capture-state) so the supervisory layer
can make informed decisions without owning the execution path.

The DBusEkosAdapter is the concrete v1.1 transport.  The EkosAdapterProtocol
defines the replaceable boundary so tests and future transports can swap in
without touching orchestration.

The normalized ``NormalizedEkosSnapshot`` (imported from ``absolute_state``)
replaces the earlier boolean ``EkosSequenceStatus``.  It carries an explicit
state enum, freshness metadata, and conservative unknown defaults so the
supervisory layer can distinguish requested pause from confirmed pause, idle
from unavailable, and running from stale last-known state.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from kepler_node.agent.absolute_state import EkosRuntimeState, NormalizedEkosSnapshot
from kepler_node.agent.interfaces import DeviceActivityEvent, DeviceActivityEventType

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Backward-compatibility shim
# ---------------------------------------------------------------------------


class EkosSequenceStatus(NormalizedEkosSnapshot):
    """Deprecated: use NormalizedEkosSnapshot directly.

    Retained as a thin subclass so callers that import or isinstance-check
    ``EkosSequenceStatus`` continue to work while the codebase migrates to
    ``NormalizedEkosSnapshot``.
    """


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

    def status(self) -> NormalizedEkosSnapshot:
        """Return a normalized snapshot of the current sequence/capture state.

        The adapter must return ``EkosRuntimeState.UNKNOWN`` rather than
        fabricate certainty when it cannot provide a trustworthy reading
        (spec lines 237-240).
        """
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

    def status(self) -> NormalizedEkosSnapshot:
        return NormalizedEkosSnapshot(ekos_state=EkosRuntimeState.IDLE)

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

    def status(self) -> NormalizedEkosSnapshot:
        """Return a normalized Ekos sequence snapshot from DBus.

        Maps raw KStars/Ekos status strings to explicit ``EkosRuntimeState``
        values so supervisory policy can distinguish running from paused from
        idle without relying on booleans.  Returns ``EkosRuntimeState.UNKNOWN``
        on any transport failure rather than fabricating certainty.

        Additional sequence fields (``sequence_exists``, ``exposure_active``,
        ``job_name``, ``frames_done``, ``frames_total``, ``autofocus_active``,
        ``align_active``) are queried defensively: each call is wrapped in its
        own try/except so a partial DBus failure does not discard the fields
        that did succeed.
        """
        try:
            iface = self._capture_iface()
            state_str: str = str(iface.getSequenceQueueStatus())  # type: ignore[attr-defined]
            state_lower = state_str.lower()

            if "pause" in state_lower:
                ekos_state = EkosRuntimeState.PAUSED
            elif state_lower in {"running", "capturing", "active"}:
                ekos_state = EkosRuntimeState.RUNNING
            elif state_lower == "aborted":
                ekos_state = EkosRuntimeState.ABORTED
            elif state_lower in {"idle", "complete", "stopped"}:
                ekos_state = EkosRuntimeState.IDLE
            elif state_lower in {"resuming"}:
                ekos_state = EkosRuntimeState.RESUMING
            else:
                ekos_state = EkosRuntimeState.UNKNOWN

        except Exception as exc:
            _logger.debug("Ekos status query failed: %s", exc)
            return NormalizedEkosSnapshot(
                ekos_state=EkosRuntimeState.UNAVAILABLE,
                confirmed_at=datetime.now(UTC),
                details={"error": str(exc)},
            )

        # A sequence exists when Ekos is actively running/paused/etc., OR when
        # the job queue has entries even in the idle state.  Compute this after
        # querying job count so "idle with a loaded queue" is distinguishable
        # from "idle with an empty queue" (spec lines 219-240).
        #
        # Sentinel -1 means the job-count query has not yet been attempted.
        _job_count_sentinel: int = -1

        exposure_active = False
        try:
            cap_iface = self._capture_iface()
            exposure_active = bool(cap_iface.isCapturing())  # type: ignore[attr-defined]
        except Exception as exc:
            _logger.debug("Ekos isCapturing query failed: %s", exc)

        job_name: str | None = None
        try:
            cap_iface = self._capture_iface()
            raw_name = str(cap_iface.getJobName())  # type: ignore[attr-defined]
            job_name = raw_name if raw_name else None
        except Exception as exc:
            _logger.debug("Ekos getJobName query failed: %s", exc)

        frames_done = 0
        frames_total = 0
        try:
            cap_iface = self._capture_iface()
            frames_done = int(cap_iface.getProcessedCount())  # type: ignore[attr-defined]
            frames_total = int(cap_iface.getJobCount())  # type: ignore[attr-defined]
            _job_count_sentinel = frames_total
        except Exception as exc:
            _logger.debug("Ekos frame-count query failed: %s", exc)

        # sequence_exists: derive from actual job-queue presence when possible so
        # "idle with a loaded sequence" is represented correctly.
        if ekos_state in {
            EkosRuntimeState.RUNNING,
            EkosRuntimeState.PAUSED,
            EkosRuntimeState.RESUMING,
            EkosRuntimeState.ABORTED,
        }:
            # Sequence is definitely loaded and active.
            sequence_exists = True
        elif ekos_state == EkosRuntimeState.IDLE and _job_count_sentinel >= 0:
            # Idle but queue may still contain jobs (e.g. between runs).
            sequence_exists = _job_count_sentinel > 0
        else:
            # UNKNOWN / UNAVAILABLE, or job-count query failed: conservative false.
            sequence_exists = False

        autofocus_active = False
        try:
            focus_iface = self._focus_iface()
            # isAutoFocusDone() returns True when the run is complete or idle;
            # False means a run is currently in progress.
            autofocus_active = not bool(focus_iface.isAutoFocusDone())  # type: ignore[attr-defined]
        except Exception as exc:
            _logger.debug("Ekos autofocus-active query failed: %s", exc)

        align_active = False
        try:
            align_iface = self._align_iface()
            # isSolverComplete() returns True when no solve is running.
            align_active = not bool(align_iface.isSolverComplete())  # type: ignore[attr-defined]
        except Exception as exc:
            _logger.debug("Ekos align-active query failed: %s", exc)

        return NormalizedEkosSnapshot(
            ekos_state=ekos_state,
            confirmed_at=datetime.now(UTC),
            sequence_exists=sequence_exists,
            exposure_active=exposure_active,
            job_name=job_name,
            frames_done=frames_done,
            frames_total=frames_total,
            autofocus_active=autofocus_active,
            align_active=align_active,
            details={"raw_status": state_str},
        )

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
