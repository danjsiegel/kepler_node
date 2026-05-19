"""Fuji X-series body-specific behaviour contract tests.

These tests document and protect the Fuji-specific recovery paths in
Gphoto2CameraBackend that differ from generic gphoto2 behaviour:

  - Pending-transfer drain (0xa002 recovery)
  - Connect-time drain before any PTP writes
  - Capture failure → drain → recover file sequence
  - Heartbeat probe ordering for Fuji status nodes

All tests use subprocess.run mocks so the live camera is never required.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from kepler_node.camera.gphoto2 import Gphoto2CameraBackend
from kepler_node.camera.protocols import CameraSettings, CaptureRequest, ShutterPreference


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _proc(stdout: str = "", stderr: str = "", returncode: int = 0) -> MagicMock:
    m = MagicMock()
    m.stdout = stdout
    m.stderr = stderr
    m.returncode = returncode
    return m


def _fuji_detect_out() -> str:
    return "Model                          Port\n---\nFuji Fujifilm X-T5             usb:003,022\n"


def _bulb_out() -> str:
    return "Label: Bulb Mode\nReadonly: 0\nType: TOGGLE\nCurrent: 2\nEND\n"


def _make_backend() -> Gphoto2CameraBackend:
    return Gphoto2CameraBackend(
        gphoto2_bin="gphoto2",
        usb_power_supply_mode="off",
        verification_shutter_preference=ShutterPreference.ELECTRONIC_PREFERRED,
    )


# ---------------------------------------------------------------------------
# _drain_pending_transfer
# ---------------------------------------------------------------------------


def test_drain_pending_transfer_runs_wait_event_and_download(tmp_path: Path) -> None:
    """drain issues --wait-event-and-download with the correct duration."""
    backend = _make_backend()
    captured: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        captured.append(cmd)
        return _proc()

    with patch("subprocess.run", side_effect=fake_run):
        backend._drain_pending_transfer(tmp_path, "drain-stem", wait_seconds=5)

    drain_cmds = [c for c in captured if any("--wait-event-and-download" in a for a in c)]
    assert len(drain_cmds) == 1
    drain_cmd = drain_cmds[0]
    assert any("5s" in a for a in drain_cmd), "wait duration must match wait_seconds argument"
    assert "--force-overwrite" in drain_cmd


def test_drain_pending_transfer_returns_files_in_destination(tmp_path: Path) -> None:
    """drain returns any files matching the stem written by gphoto2."""
    backend = _make_backend()
    dest = tmp_path / "drain-out"
    dest.mkdir()

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        if any("--wait-event-and-download" in a for a in cmd):
            (dest / "drain-stemraf").write_bytes(b"RAF")
        return _proc()

    with patch("subprocess.run", side_effect=fake_run):
        result = backend._drain_pending_transfer(dest, "drain-stem")

    assert len(result) == 1
    assert result[0].name == "drain-stemraf"


def test_drain_pending_transfer_returns_empty_when_nothing_pending(tmp_path: Path) -> None:
    """drain returns an empty list when no pending transfer exists."""
    backend = _make_backend()

    with patch("subprocess.run", return_value=_proc(returncode=0)):
        result = backend._drain_pending_transfer(tmp_path, "drain-stem", wait_seconds=2)

    assert result == []


def test_drain_pending_transfer_uses_filename_stem_pattern(tmp_path: Path) -> None:
    """gphoto2 --filename must use the stem%C pattern so extensions are auto-selected."""
    backend = _make_backend()
    captured: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        captured.append(cmd)
        return _proc()

    with patch("subprocess.run", side_effect=fake_run):
        backend._drain_pending_transfer(tmp_path, "my-stem", wait_seconds=3)

    drain_cmd = next(c for c in captured if any("--wait-event-and-download" in a for a in c))
    filename_arg = drain_cmd[drain_cmd.index("--filename") + 1]
    assert filename_arg.endswith("my-stem%C"), (
        f"--filename must end with stem%%C, got: {filename_arg}"
    )


# ---------------------------------------------------------------------------
# _drain_pending_transfer_on_connect
# ---------------------------------------------------------------------------


def test_drain_on_connect_uses_temporary_directory() -> None:
    """Connect-time drain writes to a temp dir so downloaded junk is discarded."""
    backend = _make_backend()
    temp_dirs_used: list[str] = []

    original_drain = backend._drain_pending_transfer

    def recording_drain(dest: Path, stem: str, **kwargs: object) -> list[Path]:
        temp_dirs_used.append(str(dest))
        return original_drain(dest, stem, **kwargs)

    with patch.object(backend, "_drain_pending_transfer", side_effect=recording_drain):
        with patch("subprocess.run", return_value=_proc()):
            backend._drain_pending_transfer_on_connect()

    assert len(temp_dirs_used) == 1
    # tempfile.TemporaryDirectory removes the dir after the context exits
    assert not Path(temp_dirs_used[0]).exists(), "temp dir must be cleaned up after connect drain"


def test_drain_on_connect_uses_short_wait_seconds() -> None:
    """Connect-time drain uses a short timeout to avoid blocking connect."""
    backend = _make_backend()
    drain_kwargs: list[dict] = []

    def capturing_drain(dest: Path, stem: str, **kwargs: object) -> list[Path]:
        drain_kwargs.append(dict(kwargs))
        return []

    with patch.object(backend, "_drain_pending_transfer", side_effect=capturing_drain):
        backend._drain_pending_transfer_on_connect()

    assert drain_kwargs, "drain must be called"
    assert drain_kwargs[0].get("wait_seconds", 10) <= 3, (
        "connect-time drain must use a short wait (<=3s)"
    )


# ---------------------------------------------------------------------------
# Connect-time drain ordering
# ---------------------------------------------------------------------------


def test_drain_runs_before_set_config_during_connect() -> None:
    """Drain clears pending 0xa002 state before any PTP write during connect.

    If gphoto2 --set-config is called before draining, a stuck Fuji body
    returns 0xa002 for every write, aborting the connect.
    """
    backend = _make_backend()
    call_order: list[str] = []

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        joined = " ".join(cmd)
        if any("--wait-event-and-download" in a for a in cmd):
            call_order.append("drain")
        elif "--set-config" in joined:
            call_order.append("set-config")
        return _proc(stdout=_fuji_detect_out())

    with patch("subprocess.run", side_effect=fake_run):
        backend.connect()

    drain_indices = [i for i, c in enumerate(call_order) if c == "drain"]
    set_indices = [i for i, c in enumerate(call_order) if c == "set-config"]

    assert drain_indices, "drain must run at connect time"
    assert set_indices, "set-config must run at connect time"
    assert drain_indices[0] < set_indices[0], (
        "drain must precede the first --set-config call"
    )


# ---------------------------------------------------------------------------
# Capture failure → drain recovery
# ---------------------------------------------------------------------------


def test_capture_drain_recovery_succeeds_when_drain_finds_file(tmp_path: Path) -> None:
    """After a capture failure, drain is attempted; if it finds a file capture succeeds."""
    backend = _make_backend()
    dest = tmp_path / "frames"
    dest.mkdir()

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        joined = " ".join(cmd)
        if "--capture-image-and-download" in joined:
            # Fuji returns a generic capture error but the image is pending
            return _proc(
                stderr="*** Error ***\nFuji Capture failed: Perhaps no auto-focus?\nERROR: Could not capture image.\nERROR: Could not capture.\n",
                returncode=1,
            )
        if any("--wait-event-and-download" in a for a in cmd):
            # Drain retrieves the image that was captured despite the error
            stem = [a for a in cmd if "%" in a][0].split("/")[-1].replace("%C", "")
            (dest / f"{stem}raf").write_bytes(b"RAF")
            return _proc()
        return _proc(stdout=_fuji_detect_out())

    request = CaptureRequest(
        exposure_seconds=1.0,
        settings=CameraSettings(iso=400),
        destination_dir=dest,
        frame_label="recovery-test",
    )

    with patch("subprocess.run", side_effect=fake_run):
        result = backend.capture(request)

    assert result.image_path.exists()
    assert "recovery-test" in result.image_path.name


def test_capture_raises_when_drain_also_finds_nothing(tmp_path: Path) -> None:
    """Capture re-raises the original error when drain finds no file."""
    backend = _make_backend()
    dest = tmp_path / "frames"
    dest.mkdir()

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        return _proc(
            stderr="*** Error ***\nERROR: Could not capture image.\n",
            returncode=1,
        )

    request = CaptureRequest(
        exposure_seconds=1.0,
        settings=CameraSettings(iso=400),
        destination_dir=dest,
        frame_label="no-file",
    )

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(RuntimeError, match="gphoto2 capture failed"):
            backend.capture(request)


def test_capture_drain_only_runs_when_no_file_found(tmp_path: Path) -> None:
    """Drain is NOT invoked when capture succeeds and a file is present."""
    backend = _make_backend()
    dest = tmp_path / "frames"
    dest.mkdir()
    drain_calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        joined = " ".join(cmd)
        if any("--wait-event-and-download" in a for a in cmd):
            drain_calls.append(cmd)
        if "--capture-image-and-download" in joined:
            stem = [a for a in cmd if "%" in a][0].split("/")[-1].replace("%C", "")
            (dest / f"{stem}raf").write_bytes(b"RAF")
        return _proc()

    request = CaptureRequest(
        exposure_seconds=1.0,
        settings=CameraSettings(iso=400),
        destination_dir=dest,
        frame_label="clean-capture",
    )

    with patch("subprocess.run", side_effect=fake_run):
        backend.capture(request)

    assert drain_calls == [], "drain must not run when capture succeeds normally"


# ---------------------------------------------------------------------------
# Fuji heartbeat probe ordering
# ---------------------------------------------------------------------------


def test_heartbeat_fuji_bulb_probe_used_when_batterylevel_absent() -> None:
    """Fuji bodies expose /main/actions/bulb; heartbeat must probe it as fallback."""
    backend = _make_backend()

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        key = cmd[-1] if cmd else ""
        if key in ("batterylevel", "/main/status/batterylevel"):
            return _proc(returncode=1)
        if key in ("/main/status/cameramodel", "/main/status/manufacturer"):
            return _proc(returncode=1)
        if key == "/main/actions/bulb":
            return _proc(stdout=_bulb_out(), returncode=0)
        return _proc(returncode=1)

    with patch("subprocess.run", side_effect=fake_run):
        assert backend.heartbeat() is True


def test_heartbeat_false_when_all_fuji_probes_fail() -> None:
    """heartbeat returns False when every probe returns non-zero."""
    backend = _make_backend()

    with patch("subprocess.run", return_value=_proc(returncode=1)):
        assert backend.heartbeat() is False


def test_heartbeat_false_on_file_not_found() -> None:
    """heartbeat returns False if gphoto2 binary is missing (prevents crash)."""
    backend = _make_backend()

    with patch("subprocess.run", side_effect=FileNotFoundError("no gphoto2")):
        assert backend.heartbeat() is False
