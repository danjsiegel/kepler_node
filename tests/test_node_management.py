"""Tests for LocalNodeManagementBackend and confirm_time_action."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kepler_node.agent.interfaces import (
    NetworkMode,
    NodeManagementBackend,
    TimeSource,
)
from kepler_node.agent.node_management import (
    _ACTIVE_MOTION_CAPTURE_STATES,
    LocalNodeManagementBackend,
    confirm_time_action,
)
from kepler_node.agent.session import ClawState, RuntimeSession

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_backend(tmp_path: Path) -> LocalNodeManagementBackend:
    return LocalNodeManagementBackend(
        data_root=tmp_path / "data",
        service_names=["indiserver", "gpsd"],
        storage_warning_threshold_bytes=1024,
    )


def _completed_proc(stdout: str = "", returncode: int = 0) -> MagicMock:
    proc = MagicMock(spec=subprocess.CompletedProcess)
    proc.stdout = stdout
    proc.stderr = ""
    proc.returncode = returncode
    return proc


# ---------------------------------------------------------------------------
# network_mode
# ---------------------------------------------------------------------------


def test_network_mode_returns_home_wifi_when_nmcli_shows_connected(
    tmp_path: Path,
) -> None:
    backend = _make_backend(tmp_path)
    nmcli_output = "wifi:connected:MyNetwork\n"
    with patch("subprocess.run", return_value=_completed_proc(nmcli_output)):
        assert backend.network_mode() == NetworkMode.HOME_WIFI_CLIENT


def test_network_mode_returns_field_hotspot_on_nmcli_failure(
    tmp_path: Path,
) -> None:
    backend = _make_backend(tmp_path)
    with patch("subprocess.run", side_effect=FileNotFoundError):
        assert backend.network_mode() == NetworkMode.FIELD_HOTSPOT


# ---------------------------------------------------------------------------
# service_health
# ---------------------------------------------------------------------------


def test_service_health_reports_healthy_when_systemctl_returns_zero(
    tmp_path: Path,
) -> None:
    backend = _make_backend(tmp_path)
    with patch(
        "subprocess.run",
        return_value=_completed_proc("active", returncode=0),
    ):
        results = backend.service_health()

    assert len(results) == 2
    assert all(r.healthy for r in results)
    assert results[0].name == "indiserver"


def test_service_health_reports_unhealthy_when_systemctl_returns_nonzero(
    tmp_path: Path,
) -> None:
    backend = _make_backend(tmp_path)
    with patch(
        "subprocess.run",
        return_value=_completed_proc("inactive", returncode=3),
    ):
        results = backend.service_health()

    assert all(not r.healthy for r in results)


def test_service_health_reports_unhealthy_on_file_not_found(
    tmp_path: Path,
) -> None:
    backend = _make_backend(tmp_path)
    with patch("subprocess.run", side_effect=FileNotFoundError):
        results = backend.service_health()
    assert all(not r.healthy for r in results)
    assert all("health check failed" in r.summary for r in results)


# ---------------------------------------------------------------------------
# time_status
# ---------------------------------------------------------------------------


def test_time_status_trusted_when_ntp_synchronized(tmp_path: Path) -> None:
    backend = _make_backend(tmp_path)
    timedatectl_out = "NTPSynchronized=yes\nLocalRTC=no\n"
    with patch("subprocess.run", return_value=_completed_proc(timedatectl_out)):
        status = backend.time_status()

    assert status.trusted is True
    assert status.source == TimeSource.NETWORK


def test_time_status_untrusted_when_not_synchronized(tmp_path: Path) -> None:
    backend = _make_backend(tmp_path)
    timedatectl_out = "NTPSynchronized=no\n"
    with patch("subprocess.run", return_value=_completed_proc(timedatectl_out)):
        status = backend.time_status()

    assert status.trusted is False
    assert status.source == TimeSource.UNTRUSTED


def test_time_status_untrusted_on_timedatectl_failure(tmp_path: Path) -> None:
    backend = _make_backend(tmp_path)
    with patch("subprocess.run", side_effect=FileNotFoundError):
        status = backend.time_status()
    assert status.trusted is False


# ---------------------------------------------------------------------------
# storage_status
# ---------------------------------------------------------------------------


def test_storage_status_creates_data_root_and_reports_writable(
    tmp_path: Path,
) -> None:
    backend = _make_backend(tmp_path)
    status = backend.storage_status()

    assert status.data_root == tmp_path / "data"
    assert (tmp_path / "data").is_dir()
    assert status.writable is True
    assert status.total_bytes > 0
    assert status.free_bytes >= 0


def test_storage_status_summary_ok_when_above_threshold(tmp_path: Path) -> None:
    backend = LocalNodeManagementBackend(
        data_root=tmp_path / "data",
        # Zero thresholds so real disk always has enough free space.
        storage_warning_threshold_bytes=0,
        storage_critical_threshold_bytes=0,
    )
    status = backend.storage_status()
    assert status.summary == "ok"


def test_storage_status_summary_low_when_below_warning_but_above_critical(
    tmp_path: Path,
) -> None:
    backend = LocalNodeManagementBackend(
        data_root=tmp_path / "data",
        # Absurdly large warning threshold; zero critical so real disk never hits it.
        storage_warning_threshold_bytes=10**18,
        storage_critical_threshold_bytes=0,
    )
    status = backend.storage_status()
    assert status.summary == "low free space"


def test_storage_status_summary_critically_low_when_below_critical_threshold(
    tmp_path: Path,
) -> None:
    backend = LocalNodeManagementBackend(
        data_root=tmp_path / "data",
        # Both thresholds absurdly large so real disk always appears critically low.
        storage_warning_threshold_bytes=10**18,
        storage_critical_threshold_bytes=10**18,
    )
    status = backend.storage_status()
    assert status.summary == "critically low free space"


# ---------------------------------------------------------------------------
# power_status
# ---------------------------------------------------------------------------


def test_power_status_healthy_when_vcgencmd_returns_zero_throttled(
    tmp_path: Path,
) -> None:
    backend = _make_backend(tmp_path)
    with patch(
        "subprocess.run",
        return_value=_completed_proc("throttled=0x0"),
    ):
        status = backend.power_status()

    assert status.healthy is True
    assert status.undervoltage_detected is False


def test_power_status_unhealthy_when_undervoltage_bit_set(tmp_path: Path) -> None:
    backend = _make_backend(tmp_path)
    # Bit 0 set = current undervoltage
    with patch(
        "subprocess.run",
        return_value=_completed_proc("throttled=0x1"),
    ):
        status = backend.power_status()

    assert status.healthy is False
    assert status.undervoltage_detected is True


def test_power_status_healthy_when_vcgencmd_not_found(tmp_path: Path) -> None:
    backend = _make_backend(tmp_path)
    with patch("subprocess.run", side_effect=FileNotFoundError):
        status = backend.power_status()
    # Non-Pi environments: treat as healthy rather than blocking.
    assert status.healthy is True
    assert status.undervoltage_detected is False


# ---------------------------------------------------------------------------
# confirm_time
# ---------------------------------------------------------------------------


def test_confirm_time_rejects_timestamp_before_2020(tmp_path: Path) -> None:
    backend = _make_backend(tmp_path)
    old_ts = datetime(2019, 12, 31, 23, 59, 59, tzinfo=UTC)
    status = backend.confirm_time(old_ts)

    assert status.trusted is False
    assert "2020-01-01" in status.summary


def test_confirm_time_returns_operator_confirmed_on_success(tmp_path: Path) -> None:
    backend = _make_backend(tmp_path)
    valid_ts = datetime(2026, 5, 11, 22, 0, 0, tzinfo=UTC)
    with patch("subprocess.run", return_value=_completed_proc(returncode=0)):
        status = backend.confirm_time(valid_ts)

    assert status.trusted is True
    assert status.source == TimeSource.OPERATOR_CONFIRMED


def test_time_status_remains_operator_confirmed_after_successful_confirm(
    tmp_path: Path,
) -> None:
    """After confirm_time() succeeds, time_status() must keep reporting OPERATOR_CONFIRMED."""
    backend = _make_backend(tmp_path)
    valid_ts = datetime(2026, 5, 11, 22, 0, 0, tzinfo=UTC)

    # Confirm the time (mocks date -s succeeding).
    with patch("subprocess.run", return_value=_completed_proc(returncode=0)):
        backend.confirm_time(valid_ts)

    # Now call time_status() with timedatectl reporting no NTP sync.
    timedatectl_no_ntp = "NTPSynchronized=no\n"
    with patch("subprocess.run", return_value=_completed_proc(timedatectl_no_ntp)):
        status = backend.time_status()

    assert status.trusted is True
    assert status.source == TimeSource.OPERATOR_CONFIRMED


def test_time_status_ntp_supersedes_operator_confirmed(tmp_path: Path) -> None:
    """NTP synchronization (stronger source) supersedes a previous operator confirm."""
    backend = _make_backend(tmp_path)
    valid_ts = datetime(2026, 5, 11, 22, 0, 0, tzinfo=UTC)

    with patch("subprocess.run", return_value=_completed_proc(returncode=0)):
        backend.confirm_time(valid_ts)

    timedatectl_synced = "NTPSynchronized=yes\n"
    with patch("subprocess.run", return_value=_completed_proc(timedatectl_synced)):
        status = backend.time_status()

    assert status.trusted is True
    assert status.source == TimeSource.NETWORK


def test_confirm_time_fails_closed_when_date_command_errors(tmp_path: Path) -> None:
    backend = _make_backend(tmp_path)
    valid_ts = datetime(2026, 5, 11, 22, 0, 0, tzinfo=UTC)
    with patch(
        "subprocess.run",
        return_value=_completed_proc(returncode=1),
    ):
        status = backend.confirm_time(valid_ts)

    assert status.trusted is False


# ---------------------------------------------------------------------------
# confirm_time_action gate
# ---------------------------------------------------------------------------


def test_confirm_time_action_passes_through_when_session_idle(
    tmp_path: Path,
) -> None:
    session = RuntimeSession(state=ClawState.READY)
    backend = _make_backend(tmp_path)
    valid_ts = datetime(2026, 5, 11, 22, 0, 0, tzinfo=UTC)

    with patch("subprocess.run", return_value=_completed_proc(returncode=0)):
        status = confirm_time_action(session=session, backend=backend, timestamp=valid_ts)

    assert status.trusted is True
    assert status.source == TimeSource.OPERATOR_CONFIRMED


def test_confirm_time_action_rejects_when_control_locked(tmp_path: Path) -> None:
    # CAPTURE is in _ACTIVE_MOTION_CAPTURE_STATES so it raises ValueError
    # regardless of control_locked; the API layer maps this to 409.
    session = RuntimeSession(state=ClawState.CAPTURE, control_locked=True)
    backend = _make_backend(tmp_path)
    valid_ts = datetime(2026, 5, 11, 22, 0, 0, tzinfo=UTC)

    with pytest.raises(ValueError, match="not safe during active motion or capture"):
        confirm_time_action(session=session, backend=backend, timestamp=valid_ts)


def test_confirm_time_action_allows_paused_session(tmp_path: Path) -> None:
    # A paused session may have control_locked=True from a prior capture, but
    # time confirmation is safe because no motion or capture is active.
    session = RuntimeSession(state=ClawState.PAUSED, control_locked=True)
    backend = _make_backend(tmp_path)
    valid_ts = datetime(2026, 5, 11, 22, 0, 0, tzinfo=UTC)

    with patch("subprocess.run", return_value=_completed_proc(returncode=0)):
        status = confirm_time_action(session=session, backend=backend, timestamp=valid_ts)

    assert status.trusted is True
    assert status.source == TimeSource.OPERATOR_CONFIRMED


def test_confirm_time_action_rejects_recover_state(tmp_path: Path) -> None:
    # RECOVER may involve mount motion; time confirmation must raise ValueError.
    session = RuntimeSession(state=ClawState.RECOVER, control_locked=False)
    backend = _make_backend(tmp_path)
    valid_ts = datetime(2026, 5, 11, 22, 0, 0, tzinfo=UTC)

    with pytest.raises(ValueError, match="not safe during active motion or capture"):
        confirm_time_action(session=session, backend=backend, timestamp=valid_ts)


@pytest.mark.parametrize("state", sorted(_ACTIVE_MOTION_CAPTURE_STATES, key=str))
def test_confirm_time_action_rejects_active_motion_states(state: ClawState, tmp_path: Path) -> None:
    session = RuntimeSession(state=state, control_locked=False)
    backend = _make_backend(tmp_path)
    valid_ts = datetime(2026, 5, 11, 22, 0, 0, tzinfo=UTC)

    with pytest.raises(ValueError, match="not safe during active motion or capture"):
        confirm_time_action(session=session, backend=backend, timestamp=valid_ts)


def test_confirm_time_action_satisfies_node_management_backend_protocol(
    tmp_path: Path,
) -> None:
    backend = _make_backend(tmp_path)
    # Structural check: LocalNodeManagementBackend satisfies NodeManagementBackend.
    _: NodeManagementBackend = backend  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# GPS time precedence
# ---------------------------------------------------------------------------

_GPS_TPV_FIX = '{"class":"TPV","mode":3,"time":"2026-05-13T22:00:00.000Z","lat":40.0,"lon":-74.0}'
_GPS_TPV_FIX_TZ_NAIVE = (
    '{"class":"TPV","mode":3,"time":"2026-05-13T22:00:00.000","lat":40.0,"lon":-74.0}'
)
_GPS_NO_FIX = '{"class":"TPV","mode":1}'
_GPS_VERSION_MSG = '{"class":"VERSION","release":"3.23"}'


def _completed_proc_lines(*lines: str, returncode: int = 0) -> MagicMock:
    proc = MagicMock(spec=subprocess.CompletedProcess)
    proc.stdout = "\n".join(lines) + "\n"
    proc.stderr = ""
    proc.returncode = returncode
    return proc


def test_time_status_gps_takes_precedence_over_ntp(tmp_path: Path) -> None:
    """GPS source wins over NTP when receiver has a valid fix (mode >= 2)."""
    backend = _make_backend(tmp_path)
    # First call: gpspipe returning a TPV fix; second call: timedatectl NTP synced.
    gpspipe_resp = _completed_proc_lines(_GPS_VERSION_MSG, _GPS_TPV_FIX)
    timedatectl_resp = _completed_proc("NTPSynchronized=yes\nRTCSynchronized=yes\n")

    with patch("subprocess.run", side_effect=[gpspipe_resp, timedatectl_resp]):
        status = backend.time_status()

    assert status.trusted is True
    assert status.source == TimeSource.GPS
    assert status.summary == "GPS fix active"


def test_time_status_gps_no_fix_falls_through_to_ntp(tmp_path: Path) -> None:
    """When GPS receiver has mode < 2 the backend falls through to NTP."""
    backend = _make_backend(tmp_path)
    gpspipe_resp = _completed_proc_lines(_GPS_NO_FIX)
    timedatectl_resp = _completed_proc("NTPSynchronized=yes\n")

    with patch("subprocess.run", side_effect=[gpspipe_resp, timedatectl_resp]):
        status = backend.time_status()

    assert status.trusted is True
    assert status.source == TimeSource.NETWORK


def test_time_status_rtc_fallback_when_ntp_unavailable(tmp_path: Path) -> None:
    """RTC is used when GPS has no fix and NTP is not synchronized."""
    backend = _make_backend(tmp_path)
    gpspipe_resp = _completed_proc_lines(_GPS_NO_FIX)
    timedatectl_resp = _completed_proc("NTPSynchronized=no\nRTCSynchronized=yes\n")

    with patch("subprocess.run", side_effect=[gpspipe_resp, timedatectl_resp]):
        status = backend.time_status()

    assert status.trusted is True
    assert status.source == TimeSource.RTC
    assert status.summary == "RTC synchronized"


def test_time_status_gps_fix_lost_falls_through_to_ntp(tmp_path: Path) -> None:
    """When gpspipe is unavailable (FileNotFoundError) NTP is used."""
    backend = _make_backend(tmp_path)
    # gpspipe not found, timedatectl reports NTP synced.
    with patch(
        "subprocess.run",
        side_effect=[FileNotFoundError, _completed_proc("NTPSynchronized=yes\n")],
    ):
        status = backend.time_status()

    assert status.trusted is True
    assert status.source == TimeSource.NETWORK


def test_time_status_gps_and_ntp_agree_no_mismatch(tmp_path: Path) -> None:
    """No mismatch flag when GPS and NTP agree within 5 s."""
    backend = _make_backend(tmp_path)
    # GPS TPV fix is at 2026-05-13T22:00:00Z; we mock now() to be 1 s later
    # so the delta is 1 s < 5 s — no mismatch should be reported.
    fake_now = datetime(2026, 5, 13, 22, 0, 1, tzinfo=UTC)
    gpspipe_resp = _completed_proc_lines(_GPS_TPV_FIX)
    timedatectl_resp = _completed_proc("NTPSynchronized=yes\n")

    with (
        patch("subprocess.run", side_effect=[gpspipe_resp, timedatectl_resp]),
        patch(
            "kepler_node.agent.node_management.datetime",
            wraps=datetime,
        ) as mock_dt,
    ):
        mock_dt.now.return_value = fake_now
        mock_dt.fromisoformat = datetime.fromisoformat
        status = backend.time_status()

    assert status.source == TimeSource.GPS
    assert status.gps_ntp_mismatch_seconds is None


def test_time_status_gps_surfaces_mismatch_when_disagreement_large(
    tmp_path: Path,
) -> None:
    """gps_ntp_mismatch_seconds is set when GPS and NTP disagree by >5 s.

    The GPS TPV message carries a time close to now; we force the delta by
    patching ``datetime.now`` inside the backend module so the divergence is
    controlled and deterministic.
    """

    backend = _make_backend(tmp_path)
    # GPS time is 2026-05-13T22:00:00Z; we pretend now() is 10 s ahead.
    fake_now = datetime(2026, 5, 13, 22, 0, 10, tzinfo=UTC)

    gpspipe_resp = _completed_proc_lines(_GPS_TPV_FIX)
    timedatectl_resp = _completed_proc("NTPSynchronized=yes\n")

    with (
        patch("subprocess.run", side_effect=[gpspipe_resp, timedatectl_resp]),
        patch(
            "kepler_node.agent.node_management.datetime",
            wraps=datetime,
        ) as mock_dt,
    ):
        mock_dt.now.return_value = fake_now
        mock_dt.fromisoformat = datetime.fromisoformat
        status = backend.time_status()

    assert status.source == TimeSource.GPS
    assert status.gps_ntp_mismatch_seconds is not None
    assert status.gps_ntp_mismatch_seconds > 5.0


def test_time_status_gps_tz_naive_timestamp_does_not_crash(tmp_path: Path) -> None:
    """TPV timestamp without a 'Z' suffix must not crash time_status().

    Some older gpsd versions or GPS drivers emit ISO 8601 timestamps without
    a trailing 'Z', e.g. '2026-05-13T22:00:00.000'.  Without the tzinfo guard
    the subsequent ``gps_time - now_utc`` subtraction would raise TypeError
    (offset-naive vs offset-aware).  The backend must treat the time as UTC
    and return a valid GPS TimeStatus.
    """
    backend = _make_backend(tmp_path)
    gpspipe_resp = _completed_proc_lines(_GPS_TPV_FIX_TZ_NAIVE)
    timedatectl_resp = _completed_proc("NTPSynchronized=yes\n")

    with patch("subprocess.run", side_effect=[gpspipe_resp, timedatectl_resp]):
        status = backend.time_status()

    assert status.source == TimeSource.GPS
    assert status.trusted is True
