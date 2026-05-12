"""Camera integration surfaces."""

from kepler_node.camera.gphoto2 import CameraRemoteModeRequired, Gphoto2CameraBackend
from kepler_node.camera.protocols import (
    CameraBackend,
    CameraSettings,
    CaptureRequest,
    CaptureResult,
    ShutterPreference,
)

__all__ = [
    "CameraBackend",
    "CameraRemoteModeRequired",
    "CameraSettings",
    "CaptureRequest",
    "CaptureResult",
    "Gphoto2CameraBackend",
    "ShutterPreference",
]
