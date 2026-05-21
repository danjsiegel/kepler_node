"""Tests for Gphoto2CameraBackend and CameraRemoteModeRequired."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kepler_node.agent.interfaces import DeviceActivityEventType
from kepler_node.camera.gphoto2 import (
    CameraAutocaptureModeBlocked,
    CameraRemoteModeRequired,
    Gphoto2CameraBackend,
)
from kepler_node.camera.protocols import (
    CameraSettings,
    CaptureRequest,
    ShutterPreference,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _proc(stdout: str = "", stderr: str = "", returncode: int = 0) -> MagicMock:
    proc = MagicMock(spec=subprocess.CompletedProcess)
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


def _make_backend() -> Gphoto2CameraBackend:
    return Gphoto2CameraBackend(
        gphoto2_bin="gphoto2",
        usb_power_supply_mode="off",
        verification_shutter_preference=ShutterPreference.ELECTRONIC_PREFERRED,
    )


# ---------------------------------------------------------------------------
# connect
# ---------------------------------------------------------------------------


def test_connect_raises_when_auto_detect_returns_empty(tmp_path: Path) -> None:
    backend = _make_backend()
    with patch("subprocess.run", return_value=_proc(stdout="", returncode=0)):
        with pytest.raises(CameraRemoteModeRequired, match="camera_remote_mode_required"):
            backend.connect()


def test_connect_raises_when_auto_detect_fails(tmp_path: Path) -> None:
    backend = _make_backend()
    with patch("subprocess.run", return_value=_proc(stdout="", returncode=1)):
        with pytest.raises(CameraRemoteModeRequired):
            backend.connect()


def test_connect_raises_when_remote_control_config_probes_all_fail() -> None:
    backend = _make_backend()
    responses = [
        _proc(stdout="Model                          Port\n---\nFujifilm X-T5  usb:", returncode=0),
        _proc(
            stdout="",
            stderr="/main/settings/capturetarget not found in configuration tree",
            returncode=1,
        ),
        _proc(stdout="", stderr="/main/actions/bulb not found in configuration tree", returncode=1),
        _proc(
            stdout="",
            stderr="/main/actions/autofocusdrive not found in configuration tree",
            returncode=1,
        ),
    ]
    with patch("subprocess.run", side_effect=responses):
        with pytest.raises(CameraRemoteModeRequired, match="supported remote-control config"):
            backend.connect()


def test_connect_raises_when_only_autofocusdrive_is_readable() -> None:
    backend = _make_backend()

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        if cmd[:2] == ["gphoto2", "--auto-detect"]:
            return _proc(
                stdout="Model                          Port\n---\nFujifilm X-T5  usb:\n",
                returncode=0,
            )
        if cmd[-1] == "/main/settings/capturetarget":
            return _proc(stderr="not found in configuration tree", returncode=1)
        if cmd[-1] == "/main/actions/bulb":
            return _proc(stderr="not found in configuration tree", returncode=1)
        if cmd[-1] == "/main/actions/autofocusdrive":
            return _proc(stdout="Label: Drive Fuji Autofocus\nCurrent: 2\nEND\n", returncode=0)
        return _proc(returncode=1)

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(CameraRemoteModeRequired):
            backend.connect()


def test_connect_raises_on_missing_gphoto2_binary() -> None:
    backend = Gphoto2CameraBackend(gphoto2_bin="/nonexistent/gphoto2")
    with patch("subprocess.run", side_effect=FileNotFoundError):
        with pytest.raises(RuntimeError, match="gphoto2 binary not found"):
            backend.connect()


def test_connect_succeeds_and_enforces_usb_power_mode() -> None:
    backend = _make_backend()
    detect_out = "Model                          Port\n---\nFujifilm X-T5  usb:\n"
    call_args_list: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        call_args_list.append(cmd)
        return _proc(stdout=detect_out, returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        backend.connect()

    assert backend._connected is True
    power_set_calls = [args for args in call_args_list if "usbpowersupply=off" in args]
    assert power_set_calls, "USB power supply mode was not enforced during connect"
    raw_calls = [args for args in call_args_list if any("imageformat=0" in a for a in args)]
    assert raw_calls, "RAW image format was not enforced during connect"
    delay_calls = [args for args in call_args_list if any("capturedelay=2" in a for a in args)]
    assert delay_calls, "Zero capture delay was not enforced during connect"


def test_connect_succeeds_when_fuji_style_bulb_probe_is_readable() -> None:
    backend = _make_backend()

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        if cmd[-1] == "/main/settings/capturetarget":
            return _proc(stderr="not found in configuration tree", returncode=1)
        if cmd[-1] == "/main/actions/bulb":
            return _proc(stdout="Label: Bulb Mode\nCurrent: 2\nEND\n", returncode=0)
        if any("usbpowersupply" in arg for arg in cmd):
            return _proc(stderr="usbpowersupply not found in configuration tree", returncode=1)
        return _proc(
            stdout="Model                          Port\n---\nFujifilm X-T5  usb:\n", returncode=0
        )

    with patch("subprocess.run", side_effect=fake_run):
        backend.connect()

    assert backend._connected is True


def test_connect_ignores_missing_usb_power_supply_config() -> None:
    backend = _make_backend()

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        if cmd[-1] == "/main/settings/capturetarget":
            return _proc(stdout="Label: Capture Target\nCurrent: Memory card\nEND\n", returncode=0)
        if any("usbpowersupply" in arg for arg in cmd):
            return _proc(stderr="usbpowersupply not found in configuration tree", returncode=1)
        return _proc(
            stdout="Model                          Port\n---\nFujifilm X-T5  usb:\n", returncode=0
        )

    with patch("subprocess.run", side_effect=fake_run):
        backend.connect()

    assert backend._connected is True


def test_connect_recovers_after_autocapture_reset() -> None:
    backend = _make_backend()
    mode = {"value": "Self-timer"}

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        if cmd[:2] == ["gphoto2", "--auto-detect"]:
            return _proc(
                stdout="Model                          Port\n---\nFuji Fujifilm X-T5             usb:003,014\n",
                returncode=0,
            )
        if cmd[-1] == "/main/settings/capturetarget":
            return _proc(stderr="not found in configuration tree", returncode=1)
        if cmd[-1] == "/main/actions/bulb":
            return _proc(stdout="Label: Bulb Mode\nCurrent: 2\nEND\n", returncode=0)
        if cmd[-1] == "/main/capturesettings/capturemode":
            return _proc(
                stdout=f"Label: Still Capture Mode\nCurrent: {mode['value']}\nEND\n",
                returncode=0,
            )
        if cmd[:2] == ["gphoto2", "--reset"]:
            mode["value"] = "Unknown value 0010"
            return _proc(returncode=0)
        return _proc(returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        backend.connect()

    assert backend._connected is True


def test_connect_applies_verification_shutter_preference_when_config_map_provided() -> None:
    detect_out = "Model                          Port\n---\nFujifilm X-T5  usb:\n"
    backend = Gphoto2CameraBackend(
        verification_shutter_preference=ShutterPreference.ELECTRONIC_PREFERRED,
        shutter_preference_config_map={
            ShutterPreference.ELECTRONIC_PREFERRED: "capturemode=0",
            ShutterPreference.MECHANICAL_REQUIRED: "capturemode=1",
        },
    )
    call_args_list: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        call_args_list.append(cmd)
        return _proc(stdout=detect_out, returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        backend.connect()

    shutter_calls = [args for args in call_args_list if "capturemode=0" in args]
    assert shutter_calls, "Verification shutter preference was not applied during connect"


def test_connect_skips_shutter_preference_when_no_config_map() -> None:
    # Default _make_backend() has no shutter_preference_config_map; no capturemode call.
    backend = _make_backend()
    detect_out = "Model                          Port\n---\nFujifilm X-T5  usb:\n"
    call_args_list: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        call_args_list.append(cmd)
        return _proc(stdout=detect_out, returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        backend.connect()

    capturemode_calls = [
        args
        for args in call_args_list
        if "--set-config" in args and any("capturemode" in a for a in args)
    ]
    assert not capturemode_calls, "Unexpected capturemode config call without config map"


def test_connect_raises_when_usb_power_supply_set_config_fails() -> None:
    backend = _make_backend()
    detect_out = "Model                          Port\n---\nFujifilm X-T5  usb:\n"

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        if "--set-config" in cmd and any("usbpowersupply" in a for a in cmd):
            return _proc(returncode=1, stderr="config write failed")
        return _proc(stdout=detect_out, returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(RuntimeError, match="usbpowersupply"):
            backend.connect()

    assert backend._connected is False


def test_connect_raises_when_shutter_preference_set_config_fails() -> None:
    backend = Gphoto2CameraBackend(
        verification_shutter_preference=ShutterPreference.ELECTRONIC_PREFERRED,
        shutter_preference_config_map={
            ShutterPreference.ELECTRONIC_PREFERRED: "capturemode=0",
        },
    )
    detect_out = "Model                          Port\n---\nFujifilm X-T5  usb:\n"

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        if "--set-config" in cmd and any("capturemode" in a for a in cmd):
            return _proc(returncode=1, stderr="config write failed")
        return _proc(stdout=detect_out, returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(RuntimeError, match="shutter preference"):
            backend.connect()

    assert backend._connected is False


def test_connect_drains_pending_transfer_before_config_writes() -> None:
    """Connect-time drain runs --wait-event-and-download before the first set-config.

    If a previous session crashed mid-capture the Fuji body holds a pending
    object that blocks all PTP writes with 0xa002.  The drain must run before
    any --set-config call so the camera is in a clean state.
    """
    backend = _make_backend()
    detect_out = "Model                          Port\n---\nFujifilm X-T5  usb:\n"
    call_order: list[str] = []

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        joined = " ".join(cmd)
        if "--wait-event-and-download" in joined:
            call_order.append("drain")
        elif "--set-config" in joined:
            call_order.append(f"set-config:{[a for a in cmd if '=' in a][0]}")
        return _proc(stdout=detect_out, returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        backend.connect()

    drain_idx = next(i for i, c in enumerate(call_order) if c == "drain")
    set_idx = next(i for i, c in enumerate(call_order) if c.startswith("set-config:"))
    assert drain_idx < set_idx, "drain must run before first --set-config"


# ---------------------------------------------------------------------------


def test_heartbeat_returns_true_when_batterylevel_readable() -> None:
    backend = _make_backend()
    with patch("subprocess.run", return_value=_proc(returncode=0)):
        assert backend.heartbeat() is True


def test_heartbeat_returns_true_when_fuji_status_node_is_readable() -> None:
    backend = _make_backend()

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        if cmd[-1] == "batterylevel":
            return _proc(stderr="not found in configuration tree", returncode=1)
        if cmd[-1] == "/main/status/batterylevel":
            return _proc(stderr="not found in configuration tree", returncode=1)
        if cmd[-1] == "/main/status/cameramodel":
            return _proc(stdout="Label: Camera Model\nCurrent: X-T5\nEND\n", returncode=0)
        return _proc(stderr="unexpected call", returncode=1)

    with patch("subprocess.run", side_effect=fake_run):
        assert backend.heartbeat() is True


def test_diagnostic_status_reports_card_reader_mode() -> None:
    backend = _make_backend()

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        if cmd[:2] == ["gphoto2", "--auto-detect"]:
            return _proc(
                stdout="Model                          Port\n---\nFuji Fujifilm X-T5             usb:003,012\n",
                returncode=0,
            )
        if cmd[-1] in {"/main/settings/capturetarget", "/main/actions/bulb"}:
            return _proc(stderr="not found in configuration tree", returncode=1)
        if cmd[-1] == "/main/status/cameramodel":
            return _proc(stdout="Label: Camera Model\nCurrent: X-T5\nEND\n", returncode=0)
        return _proc(returncode=1)

    with patch("subprocess.run", side_effect=fake_run):
        status = backend.diagnostic_status()

    assert status["status"] == "card_reader_mode"
    assert status["ready"] is False


def test_diagnostic_status_reports_autocapture_mode() -> None:
    backend = _make_backend()

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        if cmd[:2] == ["gphoto2", "--auto-detect"]:
            return _proc(
                stdout="Model                          Port\n---\nFuji Fujifilm X-T5             usb:003,012\n",
                returncode=0,
            )
        if cmd[-1] == "/main/settings/capturetarget":
            return _proc(stderr="not found in configuration tree", returncode=1)
        if cmd[-1] == "/main/actions/bulb":
            return _proc(stdout="Label: Bulb Mode\nCurrent: 2\nEND\n", returncode=0)
        if cmd[-1] == "/main/capturesettings/capturemode":
            return _proc(
                stdout="Label: Still Capture Mode\nCurrent: Self-timer\nEND\n",
                returncode=0,
            )
        if cmd[-1] == "/main/capturesettings/capturedelay":
            return _proc(
                stdout="Label: Capture Delay\nCurrent: 2.000s\nEND\n",
                returncode=0,
            )
        return _proc(returncode=1)

    with patch("subprocess.run", side_effect=fake_run):
        status = backend.diagnostic_status()

    assert status["status"] == "autocapture_mode"
    assert status["ready"] is False
    assert status["capture_mode"] == "Self-timer"
    assert status["capture_delay"] == "2.000s"


def test_diagnostic_status_treats_self_timer_with_zero_delay_as_remote_ready() -> None:
    backend = _make_backend()

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        if cmd[:2] == ["gphoto2", "--auto-detect"]:
            return _proc(
                stdout="Model                          Port\n---\nFuji Fujifilm X-T5             usb:003,021\n",
                returncode=0,
            )
        if cmd[-1] == "/main/settings/capturetarget":
            return _proc(stderr="not found in configuration tree", returncode=1)
        if cmd[-1] == "/main/actions/bulb":
            return _proc(stdout="Label: Bulb Mode\nCurrent: 2\nEND\n", returncode=0)
        if cmd[-1] == "/main/capturesettings/capturemode":
            return _proc(
                stdout="Label: Still Capture Mode\nCurrent: Self-timer\nEND\n",
                returncode=0,
            )
        if cmd[-1] == "/main/capturesettings/capturedelay":
            return _proc(
                stdout="Label: Capture Delay\nCurrent: 0.000s\nEND\n",
                returncode=0,
            )
        return _proc(returncode=1)

    with patch("subprocess.run", side_effect=fake_run):
        status = backend.diagnostic_status()

    assert status["status"] == "remote_control_ready"
    assert status["ready"] is True
    assert status["capture_mode"] == "Self-timer"
    assert status["capture_delay"] == "0.000s"


def test_heartbeat_returns_false_when_gphoto2_fails() -> None:
    backend = _make_backend()
    with patch("subprocess.run", return_value=_proc(returncode=1)):
        assert backend.heartbeat() is False


def test_heartbeat_returns_false_on_timeout() -> None:
    backend = _make_backend()
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("gphoto2", 10)):
        assert backend.heartbeat() is False


# ---------------------------------------------------------------------------
# apply_settings
# ---------------------------------------------------------------------------


def test_apply_settings_issues_iso_command() -> None:
    backend = _make_backend()
    settings = CameraSettings(iso=800)
    call_args_list: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        call_args_list.append(cmd)
        return _proc(returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        result = backend.apply_settings(settings)

    iso_calls = [args for args in call_args_list if any("iso=800" in a for a in args)]
    assert iso_calls, "ISO was not applied through gphoto2"
    assert result.iso == 800


def test_apply_settings_includes_optional_aperture_and_shutter() -> None:
    backend = _make_backend()
    settings = CameraSettings(iso=400, aperture=5.6, shutter_behavior="1/125")
    captured: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        captured.append(cmd)
        return _proc(returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        backend.apply_settings(settings)

    flat = [a for cmd in captured for a in cmd]
    assert any("aperture=5.6" in a for a in flat)
    assert any("shutterspeed=1/125" in a for a in flat)


def test_apply_settings_can_set_white_balance_from_extras() -> None:
    backend = _make_backend()
    settings = CameraSettings(iso=400, extras={"white_balance": 9})
    captured: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        captured.append(cmd)
        return _proc(returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        backend.apply_settings(settings)

    flat = [a for cmd in captured for a in cmd]
    assert any("whitebalance=9" in a for a in flat)


def test_apply_settings_falls_back_to_fuji_f_number_control() -> None:
    backend = _make_backend()
    settings = CameraSettings(iso=400, aperture=5.6)
    captured: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        captured.append(cmd)
        if any("aperture=5.6" in arg for arg in cmd):
            return _proc(stderr="aperture not found in configuration tree", returncode=1)
        return _proc(returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        backend.apply_settings(settings)

    flat = [a for cmd in captured for a in cmd]
    assert any("aperture=5.6" in a for a in flat)
    assert any("f-number=f/5.6" in a for a in flat)


# ---------------------------------------------------------------------------
# capture
# ---------------------------------------------------------------------------


def test_capture_started_event_is_queued_before_subprocess_returns(
    tmp_path: Path,
) -> None:
    """CAPTURE_STARTED must be in _pending_events before --capture-image-and-download returns."""
    backend = _make_backend()
    request = CaptureRequest(
        exposure_seconds=5.0,
        settings=CameraSettings(iso=400),
        destination_dir=tmp_path / "frames",
        frame_label="in-flight",
    )
    fake_image = tmp_path / "frames" / "in-flight.RAF"
    events_at_capture_time: list[DeviceActivityEventType] = []

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        if "--capture-image-and-download" in cmd:
            # Inspect pending events while the capture is "in flight"
            events_at_capture_time.extend(e.event_type for e in backend._pending_events)
            fake_image.parent.mkdir(parents=True, exist_ok=True)
            fake_image.touch()
        return _proc(returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        backend.capture(request)

    assert DeviceActivityEventType.CAPTURE_STARTED in events_at_capture_time, (
        "CAPTURE_STARTED must be queued before --capture-image-and-download returns"
    )


def test_capture_emits_started_and_completed_activity_events(
    tmp_path: Path,
) -> None:
    backend = _make_backend()
    request = CaptureRequest(
        exposure_seconds=5.0,
        settings=CameraSettings(iso=400),
        destination_dir=tmp_path / "frames",
        shutter_preference=ShutterPreference.ELECTRONIC_PREFERRED,
        frame_label="frame-0001",
    )

    fake_image = tmp_path / "frames" / "frame-0001.RAF"

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        if "--capture-image-and-download" in cmd:
            fake_image.parent.mkdir(parents=True, exist_ok=True)
            fake_image.touch()
        return _proc(returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        result = backend.capture(request)

    assert result.image_path == fake_image

    events = list(backend.activity_events())
    types = [e.event_type for e in events]
    assert DeviceActivityEventType.CAPTURE_STARTED in types
    assert DeviceActivityEventType.CAPTURE_COMPLETED in types


def test_capture_prefers_raf_when_jpg_and_raf_are_downloaded(tmp_path: Path) -> None:
    backend = _make_backend()
    request = CaptureRequest(
        exposure_seconds=5.0,
        settings=CameraSettings(iso=400),
        destination_dir=tmp_path / "frames",
        frame_label="frame-both",
    )
    fake_jpg = tmp_path / "frames" / "frame-bothjpg"
    fake_raf = tmp_path / "frames" / "frame-bothraf"

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        if "--capture-image-and-download" in cmd:
            fake_jpg.parent.mkdir(parents=True, exist_ok=True)
            fake_jpg.touch()
            fake_raf.touch()
        return _proc(returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        result = backend.capture(request)

    assert result.image_path == fake_raf


def test_capture_uses_force_overwrite_for_repeated_frame_labels(tmp_path: Path) -> None:
    backend = _make_backend()
    request = CaptureRequest(
        exposure_seconds=5.0,
        settings=CameraSettings(iso=400),
        destination_dir=tmp_path / "frames",
        frame_label="frame-repeat",
    )
    seen_capture_cmds: list[list[str]] = []
    fake_image = tmp_path / "frames" / "frame-repeatraf"

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        if "--capture-image-and-download" in cmd:
            seen_capture_cmds.append(cmd)
            fake_image.parent.mkdir(parents=True, exist_ok=True)
            fake_image.touch()
        return _proc(returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        backend.capture(request)

    assert seen_capture_cmds
    assert "--force-overwrite" in seen_capture_cmds[0]


def test_capture_applies_shutter_preference_when_config_map_provided(
    tmp_path: Path,
) -> None:
    backend = Gphoto2CameraBackend(
        shutter_preference_config_map={
            ShutterPreference.ELECTRONIC_PREFERRED: "capturemode=0",
            ShutterPreference.MECHANICAL_REQUIRED: "capturemode=1",
        },
    )
    request = CaptureRequest(
        exposure_seconds=2.0,
        settings=CameraSettings(iso=800),
        destination_dir=tmp_path / "frames",
        shutter_preference=ShutterPreference.MECHANICAL_REQUIRED,
        frame_label="verify-frame",
    )
    fake_image = tmp_path / "frames" / "verify-frame.RAF"
    call_args_list: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        call_args_list.append(cmd)
        if "--capture-image-and-download" in cmd:
            fake_image.parent.mkdir(parents=True, exist_ok=True)
            fake_image.touch()
        return _proc(returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        backend.capture(request)

    capturemode_calls = [args for args in call_args_list if "capturemode=1" in args]
    assert capturemode_calls, "Shutter preference config was not applied during capture"


def test_capture_skips_shutter_preference_for_operator_selected(
    tmp_path: Path,
) -> None:
    backend = Gphoto2CameraBackend(
        shutter_preference_config_map={
            ShutterPreference.ELECTRONIC_PREFERRED: "capturemode=0",
        },
    )
    request = CaptureRequest(
        exposure_seconds=2.0,
        settings=CameraSettings(iso=800),
        destination_dir=tmp_path / "frames",
        shutter_preference=ShutterPreference.OPERATOR_SELECTED,
        frame_label="op-frame",
    )
    fake_image = tmp_path / "frames" / "op-frame.RAF"
    call_args_list: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        call_args_list.append(cmd)
        if "--capture-image-and-download" in cmd:
            fake_image.parent.mkdir(parents=True, exist_ok=True)
            fake_image.touch()
        return _proc(returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        backend.capture(request)

    capturemode_calls = [args for args in call_args_list if any("capturemode" in a for a in args)]
    assert not capturemode_calls, "capturemode config should not be applied for OPERATOR_SELECTED"


def test_capture_raises_on_gphoto2_failure(tmp_path: Path) -> None:
    backend = _make_backend()
    request = CaptureRequest(
        exposure_seconds=5.0,
        settings=CameraSettings(iso=400),
        destination_dir=tmp_path / "frames",
        frame_label="frame-err",
    )

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        if "--capture-image-and-download" in cmd:
            return _proc(returncode=1, stderr="USB communication failed")
        return _proc(returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(RuntimeError, match="gphoto2 capture failed"):
            backend.capture(request)


def test_capture_recovers_pending_transfer_after_gphoto2_failure(tmp_path: Path) -> None:
    """Fuji body stuck with 'Transfer image to PC' drains automatically.

    When the capture command fails (e.g. after a stuck AF transaction), the
    camera body may hold a completed image object in a pending-transfer state.
    The backend should drain it via --wait-event-and-download and return the
    image rather than raising.
    """
    backend = _make_backend()
    request = CaptureRequest(
        exposure_seconds=1.0,
        settings=CameraSettings(iso=400),
        destination_dir=tmp_path / "frames",
        frame_label="drain-test",
    )
    pending_raf = tmp_path / "frames" / "drain-testraf"

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        if "--capture-image-and-download" in cmd:
            return _proc(returncode=1, stderr="Perhaps no auto-focus?")
        if "--wait-event-and-download" in " ".join(cmd):
            pending_raf.parent.mkdir(parents=True, exist_ok=True)
            pending_raf.touch()
            return _proc(returncode=0, stdout="Saving file as drain-testraf\n")
        return _proc(returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        result = backend.capture(request)

    assert result.image_path == pending_raf


def test_capture_succeeds_when_files_exist_despite_nonfatal_download_stderr(tmp_path: Path) -> None:
    backend = _make_backend()
    request = CaptureRequest(
        exposure_seconds=5.0,
        settings=CameraSettings(iso=400),
        destination_dir=tmp_path / "frames",
        frame_label="frame-partial-download",
    )
    fake_jpg = tmp_path / "frames" / "frame-partial-downloadjpg"
    fake_raf = tmp_path / "frames" / "frame-partial-downloadraf"

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        if "--capture-image-and-download" in cmd:
            fake_jpg.parent.mkdir(parents=True, exist_ok=True)
            fake_jpg.touch()
            fake_raf.touch()
            return _proc(
                returncode=0,
                stdout=(
                    "New file is in location /store_10000001/DSCF0010.jpg on the camera\n"
                    "Saving file as /tmp/frame-partial-downloadjpg\n"
                    "New file is in location /store_10000001/DSCF0009.raf on the camera\n"
                    "Saving file as /tmp/frame-partial-downloadraf\n"
                ),
                stderr=(
                    "\n*** Error ***              \n"
                    "PTP Access Denied\n"
                    "ERROR: Could not get image.\n"
                ),
            )
        return _proc(returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        result = backend.capture(request)

    assert result.image_path == fake_raf
    assert "PTP Access Denied" in result.metadata["gphoto2_stderr"]


def test_capture_raises_when_camera_is_in_autocapture_mode(tmp_path: Path) -> None:
    backend = _make_backend()
    request = CaptureRequest(
        exposure_seconds=5.0,
        settings=CameraSettings(iso=400),
        destination_dir=tmp_path / "frames",
        frame_label="frame-auto-mode",
    )
    call_args_list: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        call_args_list.append(cmd)
        if cmd[:2] == ["gphoto2", "--auto-detect"]:
            return _proc(
                stdout="Model                          Port\n---\nFujifilm X-T5  usb:\n",
                returncode=0,
            )
        if cmd[-1] == "/main/settings/capturetarget":
            return _proc(stderr="not found in configuration tree", returncode=1)
        if cmd[-1] == "/main/actions/bulb":
            return _proc(stdout="Label: Bulb Mode\nCurrent: 2\nEND\n", returncode=0)
        if cmd[-1] == "/main/capturesettings/capturemode":
            return _proc(
                stdout="Label: Still Capture Mode\nCurrent: Self-timer\nEND\n", returncode=0
            )
        if cmd[-1] == "/main/capturesettings/capturedelay":
            return _proc(stdout="Label: Capture Delay\nCurrent: 2.000s\nEND\n", returncode=0)
        if cmd[:2] == ["gphoto2", "--reset"]:
            return _proc(returncode=0)
        return _proc(returncode=0)

    with (
        patch("subprocess.run", side_effect=fake_run),
        patch.object(backend, "_find_usb_sysfs_device", return_value=None),
    ):
        with pytest.raises(CameraAutocaptureModeBlocked, match="attempted recovery: gphoto2 reset"):
            backend.capture(request)

    assert not any("--capture-image-and-download" in cmd for cmd in call_args_list)


def test_capture_recovers_after_autocapture_reset(tmp_path: Path) -> None:
    backend = _make_backend()
    request = CaptureRequest(
        exposure_seconds=5.0,
        settings=CameraSettings(iso=400),
        destination_dir=tmp_path / "frames",
        frame_label="frame-auto-recovered",
    )
    fake_image = tmp_path / "frames" / "frame-auto-recovered.RAF"
    mode = {"value": "Self-timer"}
    delay = {"value": "2.000s"}

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        if cmd[:2] == ["gphoto2", "--auto-detect"]:
            return _proc(
                stdout="Model                          Port\n---\nFuji Fujifilm X-T5             usb:003,014\n",
                returncode=0,
            )
        if cmd[-1] == "/main/settings/capturetarget":
            return _proc(stderr="not found in configuration tree", returncode=1)
        if cmd[-1] == "/main/actions/bulb":
            return _proc(stdout="Label: Bulb Mode\nCurrent: 2\nEND\n", returncode=0)
        if cmd[-1] == "/main/capturesettings/capturemode":
            return _proc(
                stdout=f"Label: Still Capture Mode\nCurrent: {mode['value']}\nEND\n",
                returncode=0,
            )
        if cmd[-1] == "/main/capturesettings/capturedelay":
            return _proc(
                stdout=f"Label: Capture Delay\nCurrent: {delay['value']}\nEND\n",
                returncode=0,
            )
        if cmd[:2] == ["gphoto2", "--reset"]:
            mode["value"] = "Unknown value 0010"
            delay["value"] = "0.000s"
            return _proc(returncode=0)
        if "--capture-image-and-download" in cmd:
            fake_image.parent.mkdir(parents=True, exist_ok=True)
            fake_image.touch()
            return _proc(returncode=0)
        return _proc(returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        result = backend.capture(request)

    assert result.image_path == fake_image


def test_capture_allows_self_timer_label_when_capture_delay_zero(tmp_path: Path) -> None:
    backend = _make_backend()
    request = CaptureRequest(
        exposure_seconds=5.0,
        settings=CameraSettings(iso=400),
        destination_dir=tmp_path / "frames",
        frame_label="frame-self-timer-zero-delay",
    )
    fake_image = tmp_path / "frames" / "frame-self-timer-zero-delay.RAF"

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        if cmd[:2] == ["gphoto2", "--auto-detect"]:
            return _proc(
                stdout="Model                          Port\n---\nFuji Fujifilm X-T5             usb:003,021\n",
                returncode=0,
            )
        if cmd[-1] == "/main/settings/capturetarget":
            return _proc(stderr="not found in configuration tree", returncode=1)
        if cmd[-1] == "/main/actions/bulb":
            return _proc(stdout="Label: Bulb Mode\nCurrent: 2\nEND\n", returncode=0)
        if cmd[-1] == "/main/capturesettings/capturemode":
            return _proc(
                stdout="Label: Still Capture Mode\nCurrent: Self-timer\nEND\n", returncode=0
            )
        if cmd[-1] == "/main/capturesettings/capturedelay":
            return _proc(stdout="Label: Capture Delay\nCurrent: 0.000s\nEND\n", returncode=0)
        if "--capture-image-and-download" in cmd:
            fake_image.parent.mkdir(parents=True, exist_ok=True)
            fake_image.touch()
            return _proc(returncode=0)
        return _proc(returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        result = backend.capture(request)

    assert result.image_path == fake_image


def test_capture_raises_when_gphoto_reports_error_in_stderr_with_zero_exit(tmp_path: Path) -> None:
    backend = _make_backend()
    request = CaptureRequest(
        exposure_seconds=5.0,
        settings=CameraSettings(iso=400),
        destination_dir=tmp_path / "frames",
        frame_label="frame-fuji-err",
    )

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        if "--capture-image-and-download" in cmd:
            return _proc(
                returncode=0,
                stderr=(
                    "\n*** Error ***\n"
                    "Fuji Capture failed: Perhaps no auto-focus?\n"
                    "ERROR: Could not capture image.\n"
                    "ERROR: Could not capture.\n"
                ),
            )
        return _proc(returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(RuntimeError, match="Perhaps no auto-focus"):
            backend.capture(request)


# ---------------------------------------------------------------------------
# activity_events draining
# ---------------------------------------------------------------------------


def test_activity_events_drains_after_first_call(tmp_path: Path) -> None:
    backend = _make_backend()
    request = CaptureRequest(
        exposure_seconds=1.0,
        settings=CameraSettings(iso=400),
        destination_dir=tmp_path / "frames",
        frame_label="drain-test",
    )
    fake_image = tmp_path / "frames" / "drain-test.RAF"

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        if "--capture-image-and-download" in cmd:
            fake_image.parent.mkdir(parents=True, exist_ok=True)
            fake_image.touch()
        return _proc(returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        backend.capture(request)

    first_drain = list(backend.activity_events())
    assert len(first_drain) == 2  # started + completed

    second_drain = list(backend.activity_events())
    assert second_drain == []
