"""Structured persistence models for Kepler v1 session storage."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from kepler_node.agent.session import ClawState, TerminalOutcome, WorkflowIntent

# ====================================================================== #
# Equipment Profile                                                        #
# ====================================================================== #


class EquipmentProfileHardwareMount(BaseModel):
    model: str | None = None
    driver_name: str | None = None
    serial_number: str | None = None


class FujiFocusCalibrationProfile(BaseModel):
    """Persisted Fuji focus calibration for one body+lens+posture combination."""

    schema_version: int = 1
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
    calibrated_at: datetime
    validation_source: str = "operator"
    notes: str = ""


class EquipmentProfileFocusCalibration(BaseModel):
    """Collection of persisted focus calibration profiles for the active camera."""

    schema_version: int = 1
    active_profile_id: str | None = None
    profiles: dict[str, FujiFocusCalibrationProfile] = Field(default_factory=dict)


class EquipmentProfileHardwareCamera(BaseModel):
    make: str | None = None
    model: str | None = None
    usb_power_supply_mode: str = "off"
    verification_shutter_mode: str = "electronic_preferred"
    fuji_focus_calibration: EquipmentProfileFocusCalibration | None = None


class EquipmentProfileHardwareLens(BaseModel):
    model: str | None = None
    is_zoom: bool = False
    default_focal_length_mm: float | None = None
    focal_length_source: str | None = None


class EquipmentProfileHardwareGps(BaseModel):
    enabled: bool = False
    provider: str = "gpsd"
    expected_receiver: str | None = None


class EquipmentProfileHardware(BaseModel):
    mount: EquipmentProfileHardwareMount = Field(default_factory=EquipmentProfileHardwareMount)
    camera: EquipmentProfileHardwareCamera = Field(default_factory=EquipmentProfileHardwareCamera)
    lens: EquipmentProfileHardwareLens = Field(default_factory=EquipmentProfileHardwareLens)
    gps: EquipmentProfileHardwareGps = Field(default_factory=EquipmentProfileHardwareGps)


class EquipmentProfileSiteDefaults(BaseModel):
    site_name: str | None = None
    latitude_deg: float | None = None
    longitude_deg: float | None = None
    elevation_m: float | None = None
    prefer_gps: bool = True


class EquipmentProfileSolvingHints(BaseModel):
    focal_length_assumption_mm: float | None = None
    pixel_scale_hint_arcsec_per_px: float | None = None


class EquipmentProfileBackendPreferences(BaseModel):
    camera_backend: str = "gphoto2"
    mount_backend: str = "indi"
    solver_backend: str = "astrometry_net"
    gps_backend: str = "gpsd"


class EquipmentProfile(BaseModel):
    """Canonical equipment profile persisted under data_root/profiles/."""

    schema_version: int = 1
    profile_id: str
    display_name: str
    is_default: bool = False
    hardware: EquipmentProfileHardware = Field(default_factory=EquipmentProfileHardware)
    site_defaults: EquipmentProfileSiteDefaults = Field(
        default_factory=EquipmentProfileSiteDefaults
    )
    solving_hints: EquipmentProfileSolvingHints = Field(
        default_factory=EquipmentProfileSolvingHints
    )
    backend_preferences: EquipmentProfileBackendPreferences = Field(
        default_factory=EquipmentProfileBackendPreferences
    )
    notes: str = ""
    created_at: datetime
    updated_at: datetime


# ====================================================================== #
# Install Manifest                                                         #
# ====================================================================== #


class InstallManifest(BaseModel):
    """Persisted install manifest written by bootstrap and updated by upgrade."""

    schema_version: int = 1
    kepler_version: str
    release_id: str
    release_channel: str = "stable"
    installed_at: datetime
    last_upgrade_at: datetime | None = None
    bootstrap_version: str = "1"
    bootstrap_profile: str | None = None
    os_id: str | None = None
    os_version: str | None = None
    architecture: str | None = None
    managed_services: list[str] = Field(default_factory=list)
    managed_packages: list[str] = Field(default_factory=list)
    schema_versions: dict[str, int] = Field(default_factory=dict)
    config_version: str = "1"
    last_upgrade_result: str | None = None


class ArtifactKind(StrEnum):
    """Canonical artifact kinds persisted in v1 metadata."""

    PREVIEW_PROXY = "preview_proxy"
    SOLVE_PROXY = "solve_proxy"
    SOLVE_OUTPUT = "solve_output"
    CALIBRATION_ARTIFACT = "calibration_artifact"


class ArtifactReference(BaseModel):
    """Typed artifact reference used in frame metadata and APIs."""

    artifact_kind: ArtifactKind
    relative_path: str
    source_frame_id: str | None = None
    created_at: datetime | None = None


class SessionScope(StrEnum):
    """Event stream scope."""

    SESSION = "session"
    NODE = "node"


class EventSeverity(StrEnum):
    """Operator-facing event severity."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class EventType(StrEnum):
    """Fixed v1 event taxonomy for session event streams."""

    STATE_TRANSITION = "state_transition"
    RECOVERY_ATTEMPT = "recovery_attempt"
    CORRECTIVE_ACTION = "corrective_action"
    WARNING = "warning"
    OPERATOR_ACTION_REQUIRED = "operator_action_required"
    QUALITY_ASSESSMENT = "quality_assessment"
    ARTIFACT_CREATED = "artifact_created"
    SESSION_OUTCOME = "session_outcome"
    PROVIDER_STATUS = "provider_status"
    EXTERNAL_CONTROL_CONFLICT = "external_control_conflict"


class SessionRecord(BaseModel):
    """Canonical session.json content for a managed capture session."""

    schema_version: int = 1
    session_id: str
    started_at: datetime
    updated_at: datetime
    state: ClawState
    target_source: str | None = None
    target_label: str | None = None
    ra_hours: float | None = None
    dec_deg: float | None = None
    equipment_profile_id: str | None = None
    operating_mode: str | None = None
    site_summary: dict[str, Any] = Field(default_factory=dict)
    time_source_summary: dict[str, Any] = Field(default_factory=dict)
    selected_inline_run_parameters: dict[str, Any] = Field(default_factory=dict)
    calibration_summary: dict[str, Any] = Field(default_factory=dict)
    mount_summary: dict[str, Any] = Field(default_factory=dict)
    terminal_outcome: TerminalOutcome | None = None


class EventRecord(BaseModel):
    """Canonical events.ndjson record."""

    timestamp: datetime
    session_scope: SessionScope
    session_id: str | None
    sequence: int
    event_type: EventType
    state: ClawState
    severity: EventSeverity
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class FrameRecord(BaseModel):
    """Canonical frame.json content for a single captured frame."""

    frame_id: str
    frame_role: str
    workflow_intent: WorkflowIntent | None = None
    capture_timestamp: datetime
    image_path: str
    raw_metadata: dict[str, Any] = Field(default_factory=dict)
    artifact_references: list[ArtifactReference] = Field(default_factory=list)
    solve_result_summary: dict[str, Any] = Field(default_factory=dict)
    quality_metrics: dict[str, Any] = Field(default_factory=dict)
    action_decision: str | None = None
    correction_context: dict[str, Any] = Field(default_factory=dict)
