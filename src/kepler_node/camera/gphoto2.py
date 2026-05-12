"""Direct gphoto2-backed camera adapter for Kepler v1."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from typing import Iterable

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
            detect_result = self._run(["--auto-detect"])
        except FileNotFoundError as exc:
            raise RuntimeError(f"gphoto2 binary not found at '{self._gphoto2_bin}'") from exc

        if detect_result.returncode != 0 or not detect_result.stdout.strip():
            raise CameraRemoteModeRequired(
                "camera_remote_mode_required: no camera detected via gphoto2 --auto-detect; "
                "switch the camera into USB remote-control mode and retry connect"
            )

        # Verify that at least one basic config read succeeds as a remote-mode check.
        cap_result = self._run(["--get-config", "/main/settings/capturetarget"])
        if cap_result.returncode != 0:
            raise CameraRemoteModeRequired(
                "camera_remote_mode_required: cannot read capturetarget config; "
                "switch the camera into USB remote-control mode and retry connect"
            )

        # Enforce USB power supply mode (starter-rig default: off).
        usb_result = self._run(["--set-config", f"usbpowersupply={self._usb_power_supply_mode}"])
        if usb_result.returncode != 0:
            raise RuntimeError(
                f"connect failed: could not enforce usbpowersupply={self._usb_power_supply_mode} "
                f"({usb_result.stderr.strip()})"
            )

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
        """Return True if a cheap liveness read succeeds."""
        try:
            result = self._run(["--get-config", "batterylevel"], timeout=10)
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def apply_settings(self, settings: CameraSettings) -> CameraSettings:
        """Apply settings through gphoto2 and return the effective settings."""
        self._run(["--set-config", f"iso={settings.iso}"])
        if settings.aperture is not None:
            self._run(["--set-config", f"aperture={settings.aperture}"])
        if settings.shutter_behavior is not None:
            self._run(["--set-config", f"shutterspeed={settings.shutter_behavior}"])
        return settings

    def capture(self, request: CaptureRequest) -> CaptureResult:
        """Capture a single frame using the requested settings.

        Settings are applied immediately before the shutter fires so each
        per-operation capture reflects the current run-parameter intent.
        """
        request.destination_dir.mkdir(parents=True, exist_ok=True)

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
                "--capture-image-and-download",
                "--filename",
                str(request.destination_dir / f"{filename_stem}%C"),
            ],
            timeout=int(request.exposure_seconds) + 60,
        )

        captured_at = datetime.now(UTC)

        if capture_result.returncode != 0:
            raise RuntimeError(f"gphoto2 capture failed: {capture_result.stderr.strip()}")

        matches = sorted(request.destination_dir.glob(f"{filename_stem}*"))
        if not matches:
            raise RuntimeError("gphoto2 capture reported success but no output file was found")

        image_path = matches[0]

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
            metadata={"gphoto2_stdout": capture_result.stdout},
        )

    def activity_events(self) -> Iterable[DeviceActivityEvent]:
        """Yield and drain normalized device-activity events."""
        events, self._pending_events = self._pending_events, []
        yield from events
