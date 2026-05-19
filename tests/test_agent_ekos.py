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
    assert isinstance(status, EkosSequenceStatus)
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
) -> tuple[ModuleType, MagicMock, MagicMock, MagicMock]:
    """Build a fake dbus module for sys.modules injection.

    Returns (fake_dbus_module, cap_iface, foc_iface, align_iface).
    """
    cap_iface = MagicMock()
    cap_iface.getSequenceQueueStatus.return_value = capture_status
    cap_iface.getCoolerTemperature.return_value = temperature
    if pause_raises:
        cap_iface.pause.side_effect = RuntimeError("DBus call failed")

    foc_iface = MagicMock()
    foc_iface.getAutoFocusPosition.return_value = focus_position

    align_iface = MagicMock()

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

