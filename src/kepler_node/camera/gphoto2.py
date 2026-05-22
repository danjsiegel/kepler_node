"""Direct gphoto2-backed camera adapter for Kepler v1."""

from __future__ import annotations

import re
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from kepler_node.agent.interfaces import DeviceActivityEvent, DeviceActivityEventType
from kepler_node.camera.protocols import (
    CameraSettings,
    CaptureRequest,
    CaptureResult,
    ShutterPreference,
)


class CameraRemoteModeRequired(RuntimeError):
    """Raised when the camera is not in the required USB remote-control mode.

    Blocking condition: ``camera_remote_mode_required``.
    Operator action: switch the camera into USB remote-control mode and retry connect.
    """


class CameraAutocaptureModeBlocked(RuntimeError):
    """Raised when the camera is in a body-side auto/self-timer capture posture.

    Blocking condition: ``camera_autocapture_mode_blocking``.
    Operator action: exit self-timer/autocapture mode on the camera body and retry.
    """


_FUJI_AUTOCAPTURE_OPERATOR_HINT = (
    "Set Drive Mode to Single Shot (not Self-timer), keep USB TETHER SHOOTING AUTO/PC SHOOT AUTO enabled, "
    "and leave shutter/ISO/aperture in tether-compatible positions such as A or T/command control; "
    "replug USB if the camera remains stuck in Self-timer"
)

_RAW_IMAGEFORMAT_ASSIGNMENTS = (
    "/main/imgsettings/imageformat=0",
    "imageformat=0",
)

_ZERO_CAPTURE_DELAY_ASSIGNMENTS = (
    "/main/capturesettings/capturedelay=2",
    "capturedelay=2",
)


class Gphoto2CameraBackend:
    """gphoto2 process-backed camera adapter.

    Uses explicit per-operation ``gphoto2`` invocations rather than a persistent
    tethered session.  Connect enforces starter-rig required settings; capture
    enforces settings per operation.
    """

    def __init__(
        self,
        *,
        gphoto2_bin: str = "gphoto2",
        usb_power_supply_mode: str = "off",
        verification_shutter_preference: ShutterPreference = ShutterPreference.ELECTRONIC_PREFERRED,
        shutter_preference_config_map: dict[ShutterPreference, str] | None = None,
    ) -> None:
        self._gphoto2_bin = gphoto2_bin
        self._usb_power_supply_mode = usb_power_supply_mode
        self._verification_shutter_preference = verification_shutter_preference
        self._shutter_preference_config_map = shutter_preference_config_map
        self._connected = False
        self._pending_events: list[DeviceActivityEvent] = []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run(
        self,
        args: list[str],
        timeout: int = 30,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self._gphoto2_bin, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def _config_read_succeeds(self, config_path: str) -> bool:
        result = self._run(["--get-config", config_path])
        return result.returncode == 0

    def _config_current_value(self, config_path: str) -> str | None:
        result = self._run(["--get-config", config_path])
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            if line.startswith("Current:"):
                return line.split(":", 1)[1].strip()
        return None

    def _first_readable_config(self, config_paths: tuple[str, ...]) -> str | None:
        for config_path in config_paths:
            if self._config_read_succeeds(config_path):
                return config_path
        return None

    def _set_config_first_supported(
        self,
        config_assignments: tuple[str, ...],
    ) -> subprocess.CompletedProcess[str]:
        last_result: subprocess.CompletedProcess[str] | None = None
        for assignment in config_assignments:
            result = self._run(["--set-config", assignment])
            last_result = result
            if result.returncode == 0:
                return result
            if not self._config_missing(result):
                return result
        assert last_result is not None
        return last_result

    def _auto_detected_cameras(self) -> list[str]:
        detect_result = self._run(["--auto-detect"])
        if detect_result.returncode != 0:
            return []
        return [
            line.rstrip()
            for line in detect_result.stdout.splitlines()
            if line.strip() and not line.startswith("Model") and not line.startswith("-")
        ]

    @staticmethod
    def _config_missing(result: subprocess.CompletedProcess[str]) -> bool:
        message = f"{result.stdout}\n{result.stderr}".lower()
        return "not found in configuration tree" in message

    def _has_remote_control_surface(self) -> bool:
        # Different camera families expose different config trees. Canon-style
        # capturetarget is not universal; Fuji tethered-control posture exposes
        # /main/actions/bulb, while card-reader/PTP posture exposes only status
        # nodes and autofocusdrive.
        return (
            self._first_readable_config(
                (
                    "/main/settings/capturetarget",
                    "/main/actions/bulb",
                )
            )
            is not None
        )

    @staticmethod
    def _has_gphoto_capture_failure(stderr: str) -> bool:
        lowered = stderr.lower()
        return "capture failed" in lowered or "error: could not capture" in lowered

    @staticmethod
    def _preferred_capture_match(matches: list[Path]) -> Path:
        def score(path: Path) -> tuple[int, str]:
            name = path.name.lower()
            if name.endswith("raf") or name.endswith("raw"):
                return (0, name)
            if name.endswith("jpg") or name.endswith("jpeg"):
                return (1, name)
            return (2, name)

        return sorted(matches, key=score)[0]

    @staticmethod
    def _is_autocapture_mode(capture_mode: str | None) -> bool:
        if capture_mode is None:
            return False
        lowered = capture_mode.lower()
        return "self-timer" in lowered or "self timer" in lowered

    @staticmethod
    def _capture_delay_is_armed(capture_delay: str | None) -> bool:
        if capture_delay is None:
            return False
        normalized = capture_delay.strip().lower()
        return normalized not in {"0", "0.0", "0.000", "0.000s", "off"}

    @staticmethod
    def _camera_usb_bus_dev(camera_label: str) -> tuple[str, str] | None:
        if "usb:" not in camera_label:
            return None
        suffix = camera_label.rsplit("usb:", 1)[1].strip()
        if "," not in suffix:
            return None
        bus, dev = suffix.split(",", 1)
        try:
            return str(int(bus)), str(int(dev))
        except ValueError:
            return None

    def _find_usb_sysfs_device(self, camera_label: str | None) -> Path | None:
        if not camera_label:
            return None
        location = self._camera_usb_bus_dev(camera_label)
        if location is None:
            return None
        busnum, devnum = location
        sysfs_root = Path("/sys/bus/usb/devices")
        if not sysfs_root.exists():
            return None
        for candidate in sysfs_root.iterdir():
            if not candidate.is_dir():
                continue
            try:
                cand_busnum = (candidate / "busnum").read_text(encoding="utf-8").strip()
                cand_devnum = (candidate / "devnum").read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if cand_busnum == busnum and cand_devnum == devnum:
                return candidate
        return None

    @staticmethod
    def _usb_reauthorize(device_path: Path) -> bool:
        authorized = device_path / "authorized"
        if not authorized.exists():
            return False
        try:
            authorized.write_text("0", encoding="utf-8")
            time.sleep(1)
            authorized.write_text("1", encoding="utf-8")
            time.sleep(2)
            return True
        except OSError:
            return False

    def _attempt_autocapture_recovery(self, diagnostic: dict[str, Any]) -> dict[str, Any]:
        recovery_steps: list[str] = []

        reset_result = self._run(["--reset"], timeout=20)
        recovery_steps.append(
            "gphoto2 reset"
            if reset_result.returncode == 0
            else f"gphoto2 reset failed: {reset_result.stderr.strip() or reset_result.stdout.strip() or 'unknown error'}"
        )

        reprobe = self.diagnostic_status()
        reprobe["recovery_steps"] = recovery_steps
        if reprobe.get("status") != "autocapture_mode":
            return reprobe

        usb_device = self._find_usb_sysfs_device(
            str(diagnostic.get("camera") or reprobe.get("camera") or "")
        )
        if usb_device is None:
            recovery_steps.append("usb re-enumeration unavailable")
            reprobe["recovery_steps"] = recovery_steps
            return reprobe

        if self._usb_reauthorize(usb_device):
            recovery_steps.append(f"usb re-enumeration via {usb_device.name}")
        else:
            recovery_steps.append(f"usb re-enumeration failed via {usb_device.name}")

        reprobe = self.diagnostic_status()
        reprobe["recovery_steps"] = recovery_steps
        return reprobe

    def _drain_pending_transfer(
        self,
        destination_dir: Path,
        filename_stem: str,
        *,
        wait_seconds: int = 10,
    ) -> list[Path]:
        """Attempt to drain any pending image transfer from the camera body.

        After a failed capture the Fuji body may hold an image object that was
        created during the aborted operation (e.g. a half-open AF-triggered
        capture transaction).  The camera UI shows "Transfer image to PC" in
        this state and all subsequent PTP writes fail with 0xa002 until the
        object is downloaded or the session is reset.

        Runs ``gphoto2 --wait-event-and-download`` to drain the pending object
        and returns any files that landed.  Returns an empty list when nothing
        was pending.
        """
        self._run(
            [
                "--force-overwrite",
                f"--wait-event-and-download={wait_seconds}s",
                "--filename",
                str(destination_dir / f"{filename_stem}%C"),
            ],
            timeout=wait_seconds + 15,
        )
        return sorted(destination_dir.glob(f"{filename_stem}*"))

    def _drain_pending_transfer_on_connect(self) -> None:
        """Drain any pending image transfer from the camera body at connect time.

        When a previous session left the Fuji body in a half-open PTP capture
        transaction (e.g. after a Kepler crash mid-capture), all subsequent
        PTP writes fail with 0xa002 until the pending object is downloaded.
        This method runs a short ``--wait-event-and-download`` before the
        first config writes so the camera is in a clean state.  Downloaded
        files are written to a temp directory and discarded.
        """
        with tempfile.TemporaryDirectory(prefix="kepler-drain-") as tmpdir:
            self._drain_pending_transfer(
                Path(tmpdir),
                "pending-drain",
                wait_seconds=2,
            )

    @staticmethod
    def _parse_list_files_output(output: str) -> list[tuple[str, int, str]]:
        current_folder: str | None = None
        entries: list[tuple[str, int, str]] = []
        folder_re = re.compile(r"^There is \d+ file(?:s)? in folder '([^']+)'\.$")
        empty_folder_re = re.compile(r"^There is no file in folder '([^']+)'\.$")
        file_re = re.compile(r"^#(\d+)\s+(\S+)")

        for raw_line in output.splitlines():
            line = raw_line.strip()
            folder_match = folder_re.match(line)
            if folder_match:
                current_folder = folder_match.group(1)
                continue

            if empty_folder_re.match(line):
                current_folder = None
                continue

            if current_folder is None:
                continue

            file_match = file_re.match(line)
            if file_match:
                entries.append((current_folder, int(file_match.group(1)), file_match.group(2)))

        return entries

    @staticmethod
    def _recovery_output_path(destination_dir: Path, source_name: str) -> Path:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        safe_name = Path(source_name).name
        candidate = destination_dir / f"recovered-{timestamp}-{safe_name}"
        counter = 1
        while candidate.exists():
            candidate = destination_dir / f"recovered-{timestamp}-{counter}-{safe_name}"
            counter += 1
        return candidate

    def recover_stuck_camera_files(self, destination_dir: Path) -> list[Path]:
        """Recover and clear any stale image objects still queued on the camera.

        This is the production recovery path for the live Fuji failure mode we
        observed: a late RAW can remain in camera-side RAM even after the INDI
        driver is restarted.  The routine first drains any pending transfer
        events, then explicitly lists the camera stores, downloads every listed
        file into the recovery directory, and deletes the camera-side copy so
        subsequent PTP sessions start cleanly.
        """
        destination_dir.mkdir(parents=True, exist_ok=True)
        recovered = self._drain_pending_transfer(
            destination_dir,
            "recovered-pending-",
            wait_seconds=2,
        )

        list_result = self._run(["--list-files"], timeout=30)
        if list_result.returncode != 0:
            detail = list_result.stderr.strip() or list_result.stdout.strip() or "unknown error"
            raise RuntimeError(f"gphoto2 list-files failed during camera recovery: {detail}")

        entries = self._parse_list_files_output(list_result.stdout)
        if not entries:
            return sorted(recovered)

        grouped: dict[str, list[tuple[int, str]]] = {}
        for folder, index, filename in entries:
            grouped.setdefault(folder, []).append((index, filename))

        for folder, folder_entries in grouped.items():
            for index, filename in sorted(folder_entries, key=lambda item: item[0], reverse=True):
                output_path = self._recovery_output_path(destination_dir, filename)
                get_result = self._run(
                    [
                        "--folder",
                        folder,
                        "--get-file",
                        str(index),
                        "--filename",
                        str(output_path),
                        "--force-overwrite",
                    ],
                    timeout=120,
                )
                if get_result.returncode != 0 or not output_path.exists():
                    detail = get_result.stderr.strip() or get_result.stdout.strip() or "unknown error"
                    raise RuntimeError(
                        f"gphoto2 get-file failed during camera recovery for {folder}#{index} ({filename}): {detail}"
                    )

                delete_result = self._run(
                    ["--folder", folder, "--delete-file", str(index)],
                    timeout=30,
                )
                if delete_result.returncode != 0:
                    detail = (
                        delete_result.stderr.strip()
                        or delete_result.stdout.strip()
                        or "unknown error"
                    )
                    raise RuntimeError(
                        f"gphoto2 delete-file failed during camera recovery for {folder}#{index} ({filename}): {detail}"
                    )

                recovered.append(output_path)

        return sorted(recovered)

    def _normalize_capture_setup(self) -> None:
        imageformat_result = self._set_config_first_supported(_RAW_IMAGEFORMAT_ASSIGNMENTS)
        if imageformat_result.returncode != 0 and not self._config_missing(imageformat_result):
            raise RuntimeError(
                f"connect failed: could not enforce RAW image format ({imageformat_result.stderr.strip()})"
            )

        delay_result = self._set_config_first_supported(_ZERO_CAPTURE_DELAY_ASSIGNMENTS)
        if delay_result.returncode != 0 and not self._config_missing(delay_result):
            raise RuntimeError(
                f"connect failed: could not enforce zero capture delay ({delay_result.stderr.strip()})"
            )

    def diagnostic_status(self) -> dict[str, Any]:
        """Return a coarse camera USB posture summary for UI/readiness use."""
        try:
            detected = self._auto_detected_cameras()
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return {
                "status": "gphoto_unavailable",
                "connected": False,
                "ready": False,
                "summary": f"gphoto2 unavailable: {exc}",
            }

        if not detected:
            return {
                "status": "disconnected",
                "connected": False,
                "ready": False,
                "summary": "No USB camera detected via gphoto2",
            }

        remote_probe = self._first_readable_config(
            (
                "/main/settings/capturetarget",
                "/main/actions/bulb",
            )
        )
        if remote_probe is not None:
            capture_mode = self._config_current_value("/main/capturesettings/capturemode")
            capture_delay = self._config_current_value("/main/capturesettings/capturedelay")
            if self._is_autocapture_mode(capture_mode) and self._capture_delay_is_armed(
                capture_delay
            ):
                return {
                    "status": "autocapture_mode",
                    "connected": True,
                    "ready": False,
                    "summary": (
                        "Camera is in Still Capture Mode 'Self-timer'; "
                        "exit self-timer/autocapture mode on the body before capture"
                    ),
                    "camera": detected[0],
                    "capture_mode": capture_mode,
                    "capture_delay": capture_delay,
                    "operator_hint": _FUJI_AUTOCAPTURE_OPERATOR_HINT,
                }
            return {
                "status": "remote_control_ready",
                "connected": True,
                "ready": True,
                "summary": f"Remote-control surface available via {remote_probe}",
                "camera": detected[0],
                "capture_mode": capture_mode,
                "capture_delay": capture_delay,
            }

        status_probe = self._first_readable_config(
            (
                "/main/status/cameramodel",
                "/main/status/batterylevel",
                "/main/actions/autofocusdrive",
            )
        )
        if status_probe is not None:
            return {
                "status": "card_reader_mode",
                "connected": True,
                "ready": False,
                "summary": (
                    "Camera is detected but only exposing status/card-reader controls; "
                    "switch the body to USB tether/remote-control mode"
                ),
                "camera": detected[0],
            }

        return {
            "status": "detected_unknown_mode",
            "connected": True,
            "ready": False,
            "summary": "Camera is detected but no supported remote-control surface is available",
            "camera": detected[0],
        }

    # ------------------------------------------------------------------
    # CameraBackend implementation
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Connect the camera and enforce required starter-rig settings.

        Raises ``CameraRemoteModeRequired`` with blocking condition
        ``camera_remote_mode_required`` when the camera is not in the
        expected USB remote-control mode.
        """
        try:
            detected = self._auto_detected_cameras()
        except FileNotFoundError as exc:
            raise RuntimeError(f"gphoto2 binary not found at '{self._gphoto2_bin}'") from exc

        if not detected:
            raise CameraRemoteModeRequired(
                "camera_remote_mode_required: no camera detected via gphoto2 --auto-detect; "
                "switch the camera into USB remote-control mode and retry connect"
            )

        # Verify that at least one remote-control oriented config read succeeds.
        if not self._has_remote_control_surface():
            raise CameraRemoteModeRequired(
                "camera_remote_mode_required: cannot read any supported remote-control config; "
                "switch the camera into USB remote-control mode and retry connect"
            )

        # Clear any pending transfer left by a previous crashed session before
        # the first config writes (a stuck pending transfer blocks all PTP writes
        # with 0xa002).  No-op when nothing is pending; completes in <1s.
        self._drain_pending_transfer_on_connect()

        diagnostic = self.diagnostic_status()
        if diagnostic.get("status") == "autocapture_mode":
            recovered = self._attempt_autocapture_recovery(diagnostic)
            if recovered.get("status") != "remote_control_ready":
                recovery_notes = "; ".join(
                    str(step) for step in recovered.get("recovery_steps", [])
                )
                detail = recovered.get(
                    "summary",
                    "Camera is in a blocked auto-capture mode",
                )
                if recovery_notes:
                    detail = f"{detail}; attempted recovery: {recovery_notes}"
                operator_hint = recovered.get("operator_hint") or diagnostic.get("operator_hint")
                if operator_hint:
                    detail = f"{detail}. {operator_hint}"
                raise CameraAutocaptureModeBlocked(f"camera_autocapture_mode_blocking: {detail}")

        # Enforce USB power supply mode when the camera exposes that control.
        usb_result = self._run(["--set-config", f"usbpowersupply={self._usb_power_supply_mode}"])
        if usb_result.returncode != 0 and not self._config_missing(usb_result):
            raise RuntimeError(
                f"connect failed: could not enforce usbpowersupply={self._usb_power_supply_mode} "
                f"({usb_result.stderr.strip()})"
            )

        self._normalize_capture_setup()

        # Apply verification shutter preference when a config map is provided.
        if self._shutter_preference_config_map is not None:
            cfg = self._shutter_preference_config_map.get(self._verification_shutter_preference)
            if cfg is not None:
                shutter_result = self._run(["--set-config", cfg])
                if shutter_result.returncode != 0:
                    raise RuntimeError(
                        f"connect failed: could not apply verification shutter preference "
                        f"'{cfg}' ({shutter_result.stderr.strip()})"
                    )

        self._connected = True

    def disconnect(self) -> None:
        """Disconnect the camera backend."""
        self._connected = False

    def heartbeat(self) -> bool:
        """Return True if a cheap liveness read succeeds.

        ``batterylevel`` is not universally exposed. Fuji bodies often answer
        lightweight status/action nodes such as ``/main/status/cameramodel`` or
        ``/main/actions/bulb`` instead.
        """
        try:
            probe = self._first_readable_config(
                (
                    "batterylevel",
                    "/main/status/batterylevel",
                    "/main/status/cameramodel",
                    "/main/status/manufacturer",
                    "/main/actions/bulb",
                )
            )
            return probe is not None
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def apply_settings(self, settings: CameraSettings) -> CameraSettings:
        """Apply settings through gphoto2 and return the effective settings."""
        self._run(["--set-config", f"iso={settings.iso}"])
        if settings.aperture is not None:
            self._set_config_first_supported(
                (
                    f"aperture={settings.aperture}",
                    f"f-number=f/{settings.aperture:g}",
                )
            )
        if settings.shutter_behavior is not None:
            self._run(["--set-config", f"shutterspeed={settings.shutter_behavior}"])
        white_balance = settings.extras.get("white_balance")
        if white_balance is not None:
            self._set_config_first_supported(
                (
                    f"/main/imgsettings/whitebalance={white_balance}",
                    f"whitebalance={white_balance}",
                )
            )
        return settings

    def capture(self, request: CaptureRequest) -> CaptureResult:
        """Capture a single frame using the requested settings.

        Settings are applied immediately before the shutter fires so each
        per-operation capture reflects the current run-parameter intent.
        """
        request.destination_dir.mkdir(parents=True, exist_ok=True)

        diagnostic = self.diagnostic_status()
        if diagnostic.get("status") == "autocapture_mode":
            recovered = self._attempt_autocapture_recovery(diagnostic)
            if recovered.get("status") == "remote_control_ready":
                diagnostic = recovered
            else:
                recovery_notes = "; ".join(
                    str(step) for step in recovered.get("recovery_steps", [])
                )
                summary = recovered.get(
                    "summary",
                    "Camera is in a blocked auto-capture mode",
                )
                detail = summary
                if recovery_notes:
                    detail = f"{detail}; attempted recovery: {recovery_notes}"
                operator_hint = recovered.get("operator_hint") or diagnostic.get("operator_hint")
                if operator_hint:
                    detail = f"{detail}. {operator_hint}"
                raise CameraAutocaptureModeBlocked(f"camera_autocapture_mode_blocking: {detail}")

        if diagnostic.get("status") == "autocapture_mode":
            raise CameraAutocaptureModeBlocked(
                "camera_autocapture_mode_blocking: "
                f"{diagnostic.get('summary', 'Camera is in a blocked auto-capture mode')}"
            )

        # Honor per-request shutter preference for non-operator-selected frames.
        if (
            self._shutter_preference_config_map is not None
            and request.shutter_preference != ShutterPreference.OPERATOR_SELECTED
        ):
            cfg = self._shutter_preference_config_map.get(request.shutter_preference)
            if cfg is not None:
                self._run(["--set-config", cfg])

        self.apply_settings(request.settings)

        filename_stem = request.frame_label or f"frame-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"

        # Record authorship before the shutter fires so in-flight captures are
        # correctly attributed to Kepler (not mistaken for foreign activity).
        capture_started_at = datetime.now(UTC)
        self._pending_events.append(
            DeviceActivityEvent(
                event_type=DeviceActivityEventType.CAPTURE_STARTED,
                observed_at=capture_started_at,
            )
        )

        capture_result = self._run(
            [
                "--force-overwrite",
                "--capture-image-and-download",
                "--filename",
                str(request.destination_dir / f"{filename_stem}%C"),
            ],
            timeout=int(request.exposure_seconds) + 60,
        )

        captured_at = datetime.now(UTC)
        matches = sorted(request.destination_dir.glob(f"{filename_stem}*"))

        if (
            capture_result.returncode != 0
            or self._has_gphoto_capture_failure(capture_result.stderr)
        ) and not matches:
            # The Fuji body may hold a pending image transfer from the failed
            # operation (camera UI shows "Transfer image to PC"; all PTP writes
            # block with 0xa002 until the object is drained).  Attempt to drain
            # it before surfacing the error to the caller.
            matches = self._drain_pending_transfer(request.destination_dir, filename_stem)
            if not matches:
                detail = (
                    capture_result.stderr.strip()
                    or capture_result.stdout.strip()
                    or "unknown error"
                )
                raise RuntimeError(f"gphoto2 capture failed: {detail}")

        if not matches:
            raise RuntimeError("gphoto2 capture reported success but no output file was found")

        image_path = self._preferred_capture_match(matches)

        self._pending_events.append(
            DeviceActivityEvent(
                event_type=DeviceActivityEventType.CAPTURE_COMPLETED,
                observed_at=datetime.now(UTC),
                details={"image_path": str(image_path)},
            )
        )

        return CaptureResult(
            image_path=image_path,
            captured_at=captured_at,
            metadata={
                "gphoto2_stdout": capture_result.stdout,
                "gphoto2_stderr": capture_result.stderr,
            },
        )

    def activity_events(self) -> Iterable[DeviceActivityEvent]:
        """Yield and drain normalized device-activity events."""
        events, self._pending_events = self._pending_events, []
        yield from events
