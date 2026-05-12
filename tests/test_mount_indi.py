"""Tests for INDIMountBackend and AuthorshipTracker."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from kepler_node.agent.authorship import AuthorshipTracker
from kepler_node.agent.interfaces import DeviceActivityEvent, DeviceActivityEventType
from kepler_node.mount.indi import INDIMountBackend
from kepler_node.mount.protocols import MountPosition

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _proc(stdout: str = "", returncode: int = 0) -> MagicMock:
    proc = MagicMock(spec=subprocess.CompletedProcess)
    proc.stdout = stdout
    proc.stderr = ""
    proc.returncode = returncode
    return proc


def _make_backend() -> INDIMountBackend:
    return INDIMountBackend(
        host="localhost",
        port=7624,
        device_name="PMC Eight",
    )


def _event(
    event_type: DeviceActivityEventType,
    authored_by: str = "unknown",
    offset_seconds: float = 0.0,
) -> DeviceActivityEvent:
    return DeviceActivityEvent(
        event_type=event_type,
        observed_at=datetime.now(UTC),
        details={"authored_by": authored_by},
    )


# ---------------------------------------------------------------------------
# connect / disconnect
# ---------------------------------------------------------------------------


def test_connect_sets_connected_flag() -> None:
    backend = _make_backend()
    with patch("subprocess.run", return_value=_proc(returncode=0)):
        backend.connect()
    assert backend._connected is True


def test_disconnect_clears_connected_flag() -> None:
    backend = _make_backend()
    backend._connected = True
    with patch("subprocess.run", return_value=_proc(returncode=0)):
        backend.disconnect()
    assert backend._connected is False


def test_disconnect_clears_connected_even_on_setprop_error() -> None:
    backend = _make_backend()
    backend._connected = True
    with patch("subprocess.run", return_value=_proc(returncode=1)):
        backend.disconnect()
    assert backend._connected is False


# ---------------------------------------------------------------------------
# current_position
# ---------------------------------------------------------------------------


def test_current_position_parses_indi_output() -> None:
    backend = _make_backend()
    responses = [
        _proc(stdout="PMC Eight.EQUATORIAL_EOD_COORD.RA=13.498"),
        _proc(stdout="PMC Eight.EQUATORIAL_EOD_COORD.DEC=47.195"),
    ]
    with patch("subprocess.run", side_effect=responses):
        pos = backend.current_position()

    assert abs(pos.ra_hours - 13.498) < 0.001
    assert abs(pos.dec_deg - 47.195) < 0.001


def test_current_position_raises_on_unparseable_output() -> None:
    backend = _make_backend()
    with patch("subprocess.run", return_value=_proc(stdout="garbage")):
        with pytest.raises(RuntimeError, match="Could not parse mount position"):
            backend.current_position()


# ---------------------------------------------------------------------------
# slew_to: emits MOUNT_SLEW_STARTED authored event
# ---------------------------------------------------------------------------


def test_slew_to_emits_mount_slew_started_event() -> None:
    backend = _make_backend()
    target = MountPosition(ra_hours=13.498, dec_deg=47.195)

    with patch("subprocess.run", return_value=_proc(returncode=0)):
        backend.slew_to(target)

    events = list(backend.activity_events())
    assert len(events) == 1
    assert events[0].event_type == DeviceActivityEventType.MOUNT_SLEW_STARTED
    assert events[0].details.get("authored_by") == "kepler"
    assert events[0].details["ra_hours"] == str(target.ra_hours)
    assert events[0].details["dec_deg"] == str(target.dec_deg)


def test_slew_to_issues_track_and_coord_setprop_commands() -> None:
    backend = _make_backend()
    target = MountPosition(ra_hours=13.498, dec_deg=47.195)
    captured: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        captured.append(cmd)
        return _proc(returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        backend.slew_to(target)

    flat = [a for cmd in captured for a in cmd]
    assert any("ON_COORD_SET.TRACK=On" in a for a in flat)
    assert any("EQUATORIAL_EOD_COORD" in a for a in flat)


# ---------------------------------------------------------------------------
# sync_to: emits MOUNT_SYNC_APPLIED authored event
# ---------------------------------------------------------------------------


def test_sync_to_emits_mount_sync_applied_event() -> None:
    backend = _make_backend()
    target = MountPosition(ra_hours=13.498, dec_deg=47.195)

    with patch("subprocess.run", return_value=_proc(returncode=0)):
        backend.sync_to(target)

    events = list(backend.activity_events())
    assert len(events) == 1
    assert events[0].event_type == DeviceActivityEventType.MOUNT_SYNC_APPLIED
    assert events[0].details.get("authored_by") == "kepler"


def test_sync_to_issues_sync_setprop_command() -> None:
    backend = _make_backend()
    target = MountPosition(ra_hours=13.498, dec_deg=47.195)
    captured: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        captured.append(cmd)
        return _proc(returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        backend.sync_to(target)

    flat = [a for cmd in captured for a in cmd]
    assert any("ON_COORD_SET.SYNC=On" in a for a in flat)


# ---------------------------------------------------------------------------
# activity_events draining
# ---------------------------------------------------------------------------


def test_activity_events_drains_after_first_call() -> None:
    backend = _make_backend()
    target = MountPosition(ra_hours=0.0, dec_deg=0.0)

    with patch("subprocess.run", return_value=_proc(returncode=0)):
        backend.slew_to(target)
        backend.sync_to(target)

    first = list(backend.activity_events())
    assert len(first) == 2

    second = list(backend.activity_events())
    assert second == []


# ---------------------------------------------------------------------------
# poll_activity: observed INDI slew completion
# ---------------------------------------------------------------------------


def test_poll_activity_emits_slew_completed_when_position_matches_target() -> None:
    """poll_activity() emits MOUNT_SLEW_COMPLETED once position is within tolerance."""
    backend = _make_backend()
    target = MountPosition(ra_hours=13.498, dec_deg=47.195)

    with patch("subprocess.run", return_value=_proc(returncode=0)):
        backend.slew_to(target)

    # Drain the SLEW_STARTED event from slew_to()
    list(backend.activity_events())

    # Simulate indi_getprop returning the target position (slew complete).
    position_responses = [
        _proc(stdout=f"PMC Eight.EQUATORIAL_EOD_COORD.RA={target.ra_hours}"),
        _proc(stdout=f"PMC Eight.EQUATORIAL_EOD_COORD.DEC={target.dec_deg}"),
    ]
    with patch("subprocess.run", side_effect=position_responses):
        backend.poll_activity()

    events = list(backend.activity_events())
    assert len(events) == 1
    assert events[0].event_type == DeviceActivityEventType.MOUNT_SLEW_COMPLETED
    assert events[0].details.get("authored_by") == "kepler"


def test_poll_activity_does_not_emit_when_not_slewing() -> None:
    """poll_activity() is a no-op when no slew is in progress."""
    backend = _make_backend()

    with patch("subprocess.run", return_value=_proc(returncode=0)):
        backend.poll_activity()

    assert list(backend.activity_events()) == []


def test_poll_activity_does_not_emit_when_position_not_yet_reached() -> None:
    """poll_activity() does not emit SLEW_COMPLETED when position is still far from target."""
    backend = _make_backend()
    target = MountPosition(ra_hours=13.498, dec_deg=47.195)

    with patch("subprocess.run", return_value=_proc(returncode=0)):
        backend.slew_to(target)

    list(backend.activity_events())  # drain SLEW_STARTED

    # Current position is far from target (1 degree off).
    far_responses = [
        _proc(stdout="PMC Eight.EQUATORIAL_EOD_COORD.RA=12.498"),
        _proc(stdout="PMC Eight.EQUATORIAL_EOD_COORD.DEC=46.195"),
    ]
    with patch("subprocess.run", side_effect=far_responses):
        backend.poll_activity()

    assert list(backend.activity_events()) == []


def test_poll_activity_clears_slewing_to_after_completion() -> None:
    """_slewing_to is cleared once SLEW_COMPLETED is emitted."""
    backend = _make_backend()
    target = MountPosition(ra_hours=10.0, dec_deg=30.0)

    with patch("subprocess.run", return_value=_proc(returncode=0)):
        backend.slew_to(target)

    list(backend.activity_events())  # drain SLEW_STARTED

    position_responses = [
        _proc(stdout=f"PMC Eight.EQUATORIAL_EOD_COORD.RA={target.ra_hours}"),
        _proc(stdout=f"PMC Eight.EQUATORIAL_EOD_COORD.DEC={target.dec_deg}"),
    ]
    with patch("subprocess.run", side_effect=position_responses):
        backend.poll_activity()

    assert backend._slewing_to is None


def test_poll_activity_tolerates_indi_read_failure() -> None:
    """poll_activity() silently skips completion check if INDI read fails."""
    backend = _make_backend()
    target = MountPosition(ra_hours=5.0, dec_deg=20.0)

    with patch("subprocess.run", return_value=_proc(returncode=0)):
        backend.slew_to(target)

    list(backend.activity_events())  # drain SLEW_STARTED

    with patch("subprocess.run", side_effect=FileNotFoundError):
        backend.poll_activity()  # should not raise

    assert list(backend.activity_events()) == []
    assert backend._slewing_to == target  # still pending


# ---------------------------------------------------------------------------
# AuthorshipTracker
# ---------------------------------------------------------------------------


def test_authorship_tracker_records_and_matches_kepler_event() -> None:
    tracker = AuthorshipTracker(window_seconds=30)
    kepler_event = _event(DeviceActivityEventType.MOUNT_SLEW_STARTED, authored_by="kepler")

    tracker.record(kepler_event)

    observed = _event(DeviceActivityEventType.MOUNT_SLEW_STARTED, authored_by="external")
    assert tracker.is_authored(observed) is True


def test_authorship_tracker_reports_conflict_when_event_unrecognized() -> None:
    tracker = AuthorshipTracker(window_seconds=30)

    unrecognized = _event(DeviceActivityEventType.MOUNT_SLEW_STARTED)
    assert tracker.is_conflict(unrecognized, control_locked=True) is True


def test_authorship_tracker_no_conflict_when_control_not_locked() -> None:
    tracker = AuthorshipTracker(window_seconds=30)

    external_slew = _event(DeviceActivityEventType.MOUNT_SLEW_STARTED)
    assert tracker.is_conflict(external_slew, control_locked=False) is False


def test_authorship_tracker_no_conflict_for_non_eligible_event_types() -> None:
    tracker = AuthorshipTracker(window_seconds=30)

    completed = _event(DeviceActivityEventType.MOUNT_SLEW_COMPLETED)
    # MOUNT_SLEW_COMPLETED is not conflict-eligible (not in _CONFLICT_ELIGIBLE_TYPES)
    assert tracker.is_conflict(completed, control_locked=True) is False


def test_authorship_tracker_authored_event_clears_conflict() -> None:
    tracker = AuthorshipTracker(window_seconds=30)
    kepler_event = _event(DeviceActivityEventType.MOUNT_SLEW_STARTED, authored_by="kepler")
    tracker.record(kepler_event)

    observed = _event(DeviceActivityEventType.MOUNT_SLEW_STARTED)
    assert tracker.is_conflict(observed, control_locked=True) is False


def test_authorship_tracker_empty_window_means_all_eligible_events_conflict() -> None:
    tracker = AuthorshipTracker(window_seconds=30)

    capture_event = _event(DeviceActivityEventType.CAPTURE_STARTED)
    assert tracker.is_conflict(capture_event, control_locked=True) is True


def test_authorship_tracker_record_followed_by_slew_completed_for_indi_backend() -> None:
    """Round-trip: slew_to records authored event; tracker confirms it; no conflict."""
    tracker = AuthorshipTracker()
    backend = _make_backend()
    target = MountPosition(ra_hours=13.498, dec_deg=47.195)

    with patch("subprocess.run", return_value=_proc(returncode=0)):
        backend.slew_to(target)

    for event in backend.activity_events():
        tracker.record(event)

    observed = DeviceActivityEvent(
        event_type=DeviceActivityEventType.MOUNT_SLEW_STARTED,
        observed_at=datetime.now(UTC),
    )
    assert tracker.is_conflict(observed, control_locked=True) is False
