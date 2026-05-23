"""Camera adapter contracts for Kepler v1."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from kepler_node.agent.interfaces import DeviceActivityEvent


class CameraSettings(BaseModel):
    """Camera settings that the active backend can enforce safely."""

    iso: int
    aperture: float | None = None
    shutter_behavior: str | None = None
    extras: dict[str, Any] = Field(default_factory=dict)


class ShutterPreference(StrEnum):
    """Requested shutter posture for a capture operation."""

    OPERATOR_SELECTED = "operator_selected"
    ELECTRONIC_PREFERRED = "electronic_preferred"
    MECHANICAL_REQUIRED = "mechanical_required"


class CaptureRequest(BaseModel):
    """Requested capture parameters for a single frame."""

    exposure_seconds: float
    settings: CameraSettings
    destination_dir: Path
    shutter_preference: ShutterPreference = ShutterPreference.OPERATOR_SELECTED
    frame_label: str | None = None


class CaptureResult(BaseModel):
    """Result of a completed capture operation."""

    image_path: Path
    captured_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class FocusCalibrationResult(BaseModel):
    """Bounded raw focus range discovered for one calibrated posture."""

    profile_id: str
    camera_model: str | None = None
    lens_model: str | None = None
    focal_length_mm: float | None = None
    focus_mode: str | None = None
    raw_min: int
    raw_max: int
    normalized_max: int = 10_000
    settle_tolerance: int = 8
    safety_margin: int = 32
    calibrated_at: datetime = Field(default_factory=datetime.utcnow)
    validation_source: str = "operator"
    notes: str = ""


class CameraBackend(Protocol):
    """Camera backend contract used by orchestration."""

    def connect(self) -> None:
        """Connect the camera backend."""

    def disconnect(self) -> None:
        """Disconnect the camera backend."""

    def heartbeat(self) -> bool:
        """Return whether the backend still looks alive."""

    def capture(self, request: CaptureRequest) -> CaptureResult:
        """Capture a single frame using the requested settings."""

    def apply_settings(self, settings: CameraSettings) -> CameraSettings:
        """Apply settings and return the effective settings in use."""

    def activity_events(self) -> Iterable[DeviceActivityEvent]:
        """Yield normalized observed device activity for conflict detection."""


@runtime_checkable
class FocusCalibrationCapable(Protocol):
    """Optional camera capability for bounded focus calibration."""

    def calibrate_focus_range(self) -> FocusCalibrationResult:
        """Discover and return a calibrated raw focus working range."""
