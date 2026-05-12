"""Tests for Gphoto2CameraBackend and CameraRemoteModeRequired."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kepler_node.agent.interfaces import DeviceActivityEventType
from kepler_node.camera.gphoto2 import CameraRemoteModeRequired, Gphoto2CameraBackend
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


def test_connect_raises_when_capturetarget_config_fails() -> None:
    backend = _make_backend()
    responses = [
        _proc(stdout="Model                          Port\n---\nFujifilm X-T5  usb:", returncode=0),
        _proc(stdout="", returncode=1),  # capturetarget fails
    ]
    with patch("subprocess.run", side_effect=responses):
        with pytest.raises(CameraRemoteModeRequired, match="capturetarget"):
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

    capturemode_calls = [args for args in call_args_list if any("capturemode" in a for a in args)]
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



# ---------------------------------------------------------------------------


def test_heartbeat_returns_true_when_batterylevel_readable() -> None:
    backend = _make_backend()
    with patch("subprocess.run", return_value=_proc(returncode=0)):
        assert backend.heartbeat() is True


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
            events_at_capture_time.extend(
                e.event_type for e in backend._pending_events
            )
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
