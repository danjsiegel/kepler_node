"""Tests for the Ekos intervention adapter and observation surface.

Covers:
- EkosAdapterProtocol structural compliance
- StubEkosAdapter contract behaviour (no-op safe defaults)
- DBusEkosAdapter with a fake DBus transport
- DeviceActivityEventType phase-2 observation events are present
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from types import ModuleType
from unittest.mock import MagicMock

import pytest

from kepler_node.agent.absolute_state import EkosRuntimeState, NormalizedEkosSnapshot
from kepler_node.agent.ekos import (
    DBusEkosAdapter,
    EkosAdapterProtocol,
    EkosSequenceStatus,
    StubEkosAdapter,
)
from kepler_node.agent.interfaces import DeviceActivityEventType


# ---------------------------------------------------------------------------
# DeviceActivityEventType: Phase 2 observation events exist
# ---------------------------------------------------------------------------


def test_observation_event_types_present() -> None:
    """Phase 2 requires focus, temperature, and capture-state event types."""
    assert DeviceActivityEventType.FOCUS_POSITION_CHANGED == "focus_position_changed"
    assert DeviceActivityEventType.TEMPERATURE_READING == "temperature_reading"
    assert DeviceActivityEventType.CAPTURE_SEQUENCE_PAUSED == "capture_sequence_paused"
    assert DeviceActivityEventType.CAPTURE_SEQUENCE_RESUMED == "capture_sequence_resumed"
    assert DeviceActivityEventType.SEQUENCE_STATUS_UPDATED == "sequence_status_updated"


# ---------------------------------------------------------------------------
# StubEkosAdapter: no-op safe defaults
# ---------------------------------------------------------------------------


def test_stub_adapter_protocol_compliance() -> None:
    """StubEkosAdapter must satisfy EkosAdapterProtocol at runtime."""
    assert isinstance(StubEkosAdapter(), EkosAdapterProtocol)


def test_stub_pause_returns_true() -> None:
    assert StubEkosAdapter().pause() is True


def test_stub_resume_returns_true() -> None:
    assert StubEkosAdapter().resume() is True


def test_stub_request_autofocus_returns_true() -> None:
    assert StubEkosAdapter().request_autofocus() is True


def test_stub_request_reverify_returns_true() -> None:
    assert StubEkosAdapter().request_reverify() is True


def test_stub_status_returns_inactive() -> None:
    status = StubEkosAdapter().status()
    assert isinstance(status, NormalizedEkosSnapshot)
    assert status.active is False
    assert status.paused is False


def test_stub_observe_returns_empty() -> None:
    events = StubEkosAdapter().observe()
    assert events == []


# ---------------------------------------------------------------------------
# Fake DBus module helper
# ---------------------------------------------------------------------------


def _make_fake_dbus(
    *,
    capture_status: str = "Running",
    focus_position: int = 5000,
    temperature: float = -10.0,
    pause_raises: bool = False,
    is_capturing: bool = False,
    job_name: str = "",
    processed_count: int = 0,
    job_count: int = 0,
    autofocus_done: bool = True,
    solver_complete: bool = True,
) -> tuple[ModuleType, MagicMock, MagicMock, MagicMock]:
    """Build a fake dbus module for sys.modules injection.

    Returns (fake_dbus_module, cap_iface, foc_iface, align_iface).
    """
    cap_iface = MagicMock()
    cap_iface.getSequenceQueueStatus.return_value = capture_status
    cap_iface.getCoolerTemperature.return_value = temperature
    cap_iface.isCapturing.return_value = is_capturing
    cap_iface.getJobName.return_value = job_name
    cap_iface.getProcessedCount.return_value = processed_count
    cap_iface.getJobCount.return_value = job_count
    if pause_raises:
        cap_iface.pause.side_effect = RuntimeError("DBus call failed")

    foc_iface = MagicMock()
    foc_iface.getAutoFocusPosition.return_value = focus_position
    foc_iface.isAutoFocusDone.return_value = autofocus_done

    align_iface = MagicMock()
    align_iface.isSolverComplete.return_value = solver_complete

    obj = MagicMock()
    session_bus = MagicMock()
    session_bus.get_object.return_value = obj

    def _make_interface(obj_, *, dbus_interface: str) -> MagicMock:  # noqa: ARG001
        if "Capture" in dbus_interface:
            return cap_iface
        if "Focus" in dbus_interface:
            return foc_iface
        if "Align" in dbus_interface:
            return align_iface
        return MagicMock()

    fake_dbus = ModuleType("dbus")
    fake_dbus.Interface = _make_interface  # type: ignore[attr-defined]
    fake_dbus.SessionBus = MagicMock(return_value=session_bus)  # type: ignore[attr-defined]

    return fake_dbus, session_bus, cap_iface, foc_iface, align_iface


# ---------------------------------------------------------------------------
# DBusEkosAdapter: fake DBus transport via sys.modules injection
# ---------------------------------------------------------------------------


def test_dbus_adapter_pause_queues_event() -> None:
    fake_dbus, bus, cap_iface, foc_iface, align_iface = _make_fake_dbus()
    adapter = DBusEkosAdapter(session_bus=bus)
    with _dbus_patched(fake_dbus):
        result = adapter.pause()
    assert result is True
    cap_iface.pause.assert_called_once()
    events = adapter.observe()
    assert len(events) == 1
    assert events[0].event_type == DeviceActivityEventType.CAPTURE_SEQUENCE_PAUSED
    assert events[0].details["source"] == "kepler_intervention"


def test_dbus_adapter_resume_queues_event() -> None:
    fake_dbus, bus, cap_iface, foc_iface, align_iface = _make_fake_dbus()
    adapter = DBusEkosAdapter(session_bus=bus)
    with _dbus_patched(fake_dbus):
        result = adapter.resume()
    assert result is True
    cap_iface.resume.assert_called_once()
    events = adapter.observe()
    assert len(events) == 1
    assert events[0].event_type == DeviceActivityEventType.CAPTURE_SEQUENCE_RESUMED


def test_dbus_adapter_request_autofocus_calls_start() -> None:
    fake_dbus, bus, cap_iface, foc_iface, align_iface = _make_fake_dbus()
    adapter = DBusEkosAdapter(session_bus=bus)
    with _dbus_patched(fake_dbus):
        result = adapter.request_autofocus()
    assert result is True
    foc_iface.start.assert_called_once()


def test_dbus_adapter_request_reverify_calls_capture_and_solve() -> None:
    fake_dbus, bus, cap_iface, foc_iface, align_iface = _make_fake_dbus()
    adapter = DBusEkosAdapter(session_bus=bus)
    with _dbus_patched(fake_dbus):
        result = adapter.request_reverify()
    assert result is True
    align_iface.captureAndSolve.assert_called_once()


def test_dbus_adapter_status_active() -> None:
    fake_dbus, bus, cap_iface, foc_iface, align_iface = _make_fake_dbus(capture_status="Running")
    adapter = DBusEkosAdapter(session_bus=bus)
    with _dbus_patched(fake_dbus):
        status = adapter.status()
    assert status.active is True
    assert status.paused is False


def test_dbus_adapter_status_paused() -> None:
    fake_dbus, bus, cap_iface, foc_iface, align_iface = _make_fake_dbus(capture_status="Paused")
    adapter = DBusEkosAdapter(session_bus=bus)
    with _dbus_patched(fake_dbus):
        status = adapter.status()
    assert status.paused is True


def test_dbus_adapter_status_aborted_maps_to_aborted_state() -> None:
    """Raw 'Aborted' status must map to EkosRuntimeState.ABORTED, not IDLE.

    Spec line 222 requires aborted to be a distinct normalized state.
    """
    fake_dbus, bus, cap_iface, foc_iface, align_iface = _make_fake_dbus(capture_status="Aborted")
    adapter = DBusEkosAdapter(session_bus=bus)
    with _dbus_patched(fake_dbus):
        status = adapter.status()
    assert status.ekos_state == EkosRuntimeState.ABORTED
    assert status.active is False
    assert status.paused is False


def test_dbus_adapter_poll_focus_queues_event() -> None:
    fake_dbus, bus, cap_iface, foc_iface, align_iface = _make_fake_dbus(focus_position=7200)
    adapter = DBusEkosAdapter(session_bus=bus)
    with _dbus_patched(fake_dbus):
        adapter.poll_focus()
    events = adapter.observe()
    assert len(events) == 1
    assert events[0].event_type == DeviceActivityEventType.FOCUS_POSITION_CHANGED
    assert events[0].details["focus_position"] == "7200"


def test_dbus_adapter_poll_temperature_queues_event() -> None:
    fake_dbus, bus, cap_iface, foc_iface, align_iface = _make_fake_dbus(temperature=-15.5)
    adapter = DBusEkosAdapter(session_bus=bus)
    with _dbus_patched(fake_dbus):
        adapter.poll_temperature()
    events = adapter.observe()
    assert len(events) == 1
    assert events[0].event_type == DeviceActivityEventType.TEMPERATURE_READING
    assert "-15.50" in events[0].details["temperature_celsius"]


def test_dbus_adapter_observe_drains_queue() -> None:
    """observe() drains the pending event queue; second call returns empty."""
    fake_dbus, bus, cap_iface, foc_iface, align_iface = _make_fake_dbus()
    adapter = DBusEkosAdapter(session_bus=bus)
    with _dbus_patched(fake_dbus):
        adapter.pause()
    first = adapter.observe()
    second = adapter.observe()
    assert len(first) == 1
    assert second == []


def test_dbus_adapter_pause_failure_returns_false() -> None:
    """When DBus raises, pause() returns False instead of propagating."""
    fake_dbus, bus, cap_iface, foc_iface, align_iface = _make_fake_dbus(pause_raises=True)
    adapter = DBusEkosAdapter(session_bus=bus)
    with _dbus_patched(fake_dbus):
        result = adapter.pause()
    assert result is False
    # No event should have been queued on failure
    assert adapter.observe() == []


def test_dbus_adapter_no_bus_returns_false_on_pause() -> None:
    """When no bus is available and dbus import fails, pause() returns False."""
    adapter = DBusEkosAdapter(session_bus=None)
    # Remove dbus from sys.modules so the lazy import fails
    old = sys.modules.pop("dbus", None)
    try:
        result = adapter.pause()
        assert result is False
    finally:
        if old is not None:
            sys.modules["dbus"] = old


def test_dbus_adapter_poll_sequence_status_queues_event_when_active() -> None:
    """poll_sequence_status emits a SEQUENCE_STATUS_UPDATED event for running state."""
    fake_dbus, bus, cap_iface, foc_iface, align_iface = _make_fake_dbus(capture_status="Running")
    adapter = DBusEkosAdapter(session_bus=bus)
    with _dbus_patched(fake_dbus):
        adapter.poll_sequence_status()
    events = adapter.observe()
    assert len(events) == 1
    assert events[0].event_type == DeviceActivityEventType.SEQUENCE_STATUS_UPDATED
    assert events[0].details["active"] == "True"
    assert events[0].details["paused"] == "False"


def test_dbus_adapter_poll_sequence_status_queues_event_when_paused() -> None:
    """poll_sequence_status emits a SEQUENCE_STATUS_UPDATED event for paused state."""
    fake_dbus, bus, cap_iface, foc_iface, align_iface = _make_fake_dbus(capture_status="Paused")
    adapter = DBusEkosAdapter(session_bus=bus)
    with _dbus_patched(fake_dbus):
        adapter.poll_sequence_status()
    events = adapter.observe()
    assert len(events) == 1
    assert events[0].event_type == DeviceActivityEventType.SEQUENCE_STATUS_UPDATED
    assert events[0].details["paused"] == "True"


def test_dbus_adapter_poll_sequence_status_no_event_when_idle() -> None:
    """poll_sequence_status emits no event when the sequence is neither active nor paused."""
    fake_dbus, bus, cap_iface, foc_iface, align_iface = _make_fake_dbus(capture_status="Idle")
    adapter = DBusEkosAdapter(session_bus=bus)
    with _dbus_patched(fake_dbus):
        adapter.poll_sequence_status()
    assert adapter.observe() == []


# ---------------------------------------------------------------------------
# Context manager for dbus module injection
# ---------------------------------------------------------------------------


class _dbus_patched:
    """Context manager that temporarily injects a fake dbus module."""

    def __init__(self, fake_dbus: ModuleType) -> None:
        self._fake = fake_dbus
        self._prev: object = None

    def __enter__(self) -> None:
        self._prev = sys.modules.get("dbus")
        sys.modules["dbus"] = self._fake  # type: ignore[assignment]

    def __exit__(self, *_: object) -> None:
        if self._prev is None:
            sys.modules.pop("dbus", None)
        else:
            sys.modules["dbus"] = self._prev  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Phase 3: DBusEkosAdapter.status() populates full NormalizedEkosSnapshot
# ---------------------------------------------------------------------------


def test_dbus_adapter_status_populates_sequence_exists_when_running() -> None:
    """status() must set sequence_exists=True when Ekos reports a running state."""
    fake_dbus, bus, cap_iface, foc_iface, align_iface = _make_fake_dbus(capture_status="Running")
    adapter = DBusEkosAdapter(session_bus=bus)
    with _dbus_patched(fake_dbus):
        status = adapter.status()
    assert status.sequence_exists is True


def test_dbus_adapter_status_sequence_exists_false_when_idle() -> None:
    """status() must set sequence_exists=False when Ekos is idle."""
    fake_dbus, bus, cap_iface, foc_iface, align_iface = _make_fake_dbus(capture_status="Idle")
    adapter = DBusEkosAdapter(session_bus=bus)
    with _dbus_patched(fake_dbus):
        status = adapter.status()
    assert status.sequence_exists is False


def test_dbus_adapter_status_exposure_active() -> None:
    """status() must populate exposure_active from isCapturing()."""
    fake_dbus, bus, cap_iface, foc_iface, align_iface = _make_fake_dbus(
        capture_status="Running",
        is_capturing=True,
    )
    adapter = DBusEkosAdapter(session_bus=bus)
    with _dbus_patched(fake_dbus):
        status = adapter.status()
    assert status.exposure_active is True


def test_dbus_adapter_status_exposure_inactive() -> None:
    """status() must set exposure_active=False when isCapturing() returns False."""
    fake_dbus, bus, cap_iface, foc_iface, align_iface = _make_fake_dbus(
        capture_status="Running",
        is_capturing=False,
    )
    adapter = DBusEkosAdapter(session_bus=bus)
    with _dbus_patched(fake_dbus):
        status = adapter.status()
    assert status.exposure_active is False


def test_dbus_adapter_status_job_name_populated() -> None:
    """status() must populate job_name from getJobName()."""
    fake_dbus, bus, cap_iface, foc_iface, align_iface = _make_fake_dbus(
        capture_status="Running",
        job_name="Andromeda_L",
    )
    adapter = DBusEkosAdapter(session_bus=bus)
    with _dbus_patched(fake_dbus):
        status = adapter.status()
    assert status.job_name == "Andromeda_L"


def test_dbus_adapter_status_job_name_empty_is_none() -> None:
    """status() must return job_name=None when getJobName() returns empty string."""
    fake_dbus, bus, cap_iface, foc_iface, align_iface = _make_fake_dbus(
        capture_status="Running",
        job_name="",
    )
    adapter = DBusEkosAdapter(session_bus=bus)
    with _dbus_patched(fake_dbus):
        status = adapter.status()
    assert status.job_name is None


def test_dbus_adapter_status_frame_counts_populated() -> None:
    """status() must populate frames_done and frames_total from DBus."""
    fake_dbus, bus, cap_iface, foc_iface, align_iface = _make_fake_dbus(
        capture_status="Running",
        processed_count=42,
        job_count=100,
    )
    adapter = DBusEkosAdapter(session_bus=bus)
    with _dbus_patched(fake_dbus):
        status = adapter.status()
    assert status.frames_done == 42
    assert status.frames_total == 100


def test_dbus_adapter_status_autofocus_active_when_not_done() -> None:
    """status() must set autofocus_active=True when isAutoFocusDone() returns False."""
    fake_dbus, bus, cap_iface, foc_iface, align_iface = _make_fake_dbus(
        capture_status="Running",
        autofocus_done=False,  # still running
    )
    adapter = DBusEkosAdapter(session_bus=bus)
    with _dbus_patched(fake_dbus):
        status = adapter.status()
    assert status.autofocus_active is True


def test_dbus_adapter_status_autofocus_inactive_when_done() -> None:
    """status() must set autofocus_active=False when isAutoFocusDone() returns True."""
    fake_dbus, bus, cap_iface, foc_iface, align_iface = _make_fake_dbus(
        capture_status="Running",
        autofocus_done=True,
    )
    adapter = DBusEkosAdapter(session_bus=bus)
    with _dbus_patched(fake_dbus):
        status = adapter.status()
    assert status.autofocus_active is False


def test_dbus_adapter_status_align_active_when_solver_running() -> None:
    """status() must set align_active=True when isSolverComplete() returns False."""
    fake_dbus, bus, cap_iface, foc_iface, align_iface = _make_fake_dbus(
        capture_status="Running",
        solver_complete=False,  # solver still running
    )
    adapter = DBusEkosAdapter(session_bus=bus)
    with _dbus_patched(fake_dbus):
        status = adapter.status()
    assert status.align_active is True


def test_dbus_adapter_status_align_inactive_when_solver_done() -> None:
    """status() must set align_active=False when isSolverComplete() returns True."""
    fake_dbus, bus, cap_iface, foc_iface, align_iface = _make_fake_dbus(
        capture_status="Running",
        solver_complete=True,
    )
    adapter = DBusEkosAdapter(session_bus=bus)
    with _dbus_patched(fake_dbus):
        status = adapter.status()
    assert status.align_active is False


def test_dbus_adapter_status_partial_failure_graceful_fallback() -> None:
    """status() must return a valid snapshot even when secondary DBus calls fail.

    Phase 3 requirement: individual field queries are wrapped independently so
    a partial failure does not discard the fields that succeeded.
    """
    fake_dbus, bus, cap_iface, foc_iface, align_iface = _make_fake_dbus(
        capture_status="Running",
        processed_count=10,
        job_count=50,
    )
    # Make isCapturing() raise to simulate a partial DBus failure
    cap_iface.isCapturing.side_effect = RuntimeError("property unavailable")

    adapter = DBusEkosAdapter(session_bus=bus)
    with _dbus_patched(fake_dbus):
        status = adapter.status()

    # Primary state must still be populated
    assert status.ekos_state == EkosRuntimeState.RUNNING
    assert status.sequence_exists is True
    # Failed field gracefully falls back to default
    assert status.exposure_active is False
    # Unaffected fields still populated from their own successful calls
    assert status.frames_done == 10
    assert status.frames_total == 50


def test_dbus_adapter_status_unavailable_on_primary_failure() -> None:
    """status() must return UNAVAILABLE snapshot when the primary status call fails."""
    fake_dbus, bus, cap_iface, foc_iface, align_iface = _make_fake_dbus()
    cap_iface.getSequenceQueueStatus.side_effect = RuntimeError("Ekos not running")

    adapter = DBusEkosAdapter(session_bus=bus)
    with _dbus_patched(fake_dbus):
        status = adapter.status()

    assert status.ekos_state == EkosRuntimeState.UNAVAILABLE
    assert "error" in status.details


def test_dbus_adapter_status_raw_status_in_details() -> None:
    """status() must preserve the raw Ekos status string in details['raw_status']."""
    fake_dbus, bus, cap_iface, foc_iface, align_iface = _make_fake_dbus(capture_status="Paused")
    adapter = DBusEkosAdapter(session_bus=bus)
    with _dbus_patched(fake_dbus):
        status = adapter.status()
    assert status.details.get("raw_status") == "Paused"


# ---------------------------------------------------------------------------
# Phase 3 round 2: sequence_exists from queue presence (Finding 2)
# ---------------------------------------------------------------------------


def test_dbus_adapter_status_sequence_exists_true_idle_with_loaded_queue() -> None:
    """sequence_exists must be True when Ekos is IDLE but the job queue is non-empty.

    Finding 2 (phase3_check_round1): 'idle with a loaded sequence' must be
    distinguishable from 'idle with no sequence'.  The previous implementation
    derived sequence_exists from state alone, so any IDLE report yielded False.
    """
    fake_dbus, bus, cap_iface, foc_iface, align_iface = _make_fake_dbus(
        capture_status="Idle",
        job_count=3,  # queue has 3 jobs → sequence exists
    )
    adapter = DBusEkosAdapter(session_bus=bus)
    with _dbus_patched(fake_dbus):
        status = adapter.status()
    assert status.ekos_state == EkosRuntimeState.IDLE
    assert status.sequence_exists is True, (
        "sequence_exists must be True when IDLE but job queue has entries"
    )


def test_dbus_adapter_status_sequence_exists_false_idle_empty_queue() -> None:
    """sequence_exists must be False when Ekos is IDLE with an empty job queue."""
    fake_dbus, bus, cap_iface, foc_iface, align_iface = _make_fake_dbus(
        capture_status="Idle",
        job_count=0,  # empty queue → no sequence
    )
    adapter = DBusEkosAdapter(session_bus=bus)
    with _dbus_patched(fake_dbus):
        status = adapter.status()
    assert status.ekos_state == EkosRuntimeState.IDLE
    assert status.sequence_exists is False, (
        "sequence_exists must be False when IDLE with an empty job queue"
    )

