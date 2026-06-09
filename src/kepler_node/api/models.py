"""Pydantic models for the Kepler Node local API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class BlockerCondition(BaseModel):
    """Structured blocker or degraded condition returned by read endpoints."""

    name: str
    severity: str
    summary: str
    operator_action_required: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    """GET /api/v1/health response."""

    status: str
    summary: str
    updated_at: datetime
    services: list[dict[str, Any]] = Field(default_factory=list)


class NodeStatusResponse(BaseModel):
    """GET /api/v1/node/status response."""

    state: str
    workflow_intent: str | None
    control_locked: bool
    network_mode: str
    time_certainty: dict[str, Any]
    power_integrity: dict[str, Any]
    detected_devices: dict[str, Any]
    build_summary: str = "kepler-node v1"
    active_equipment_profile: dict[str, Any] | None = None
    planner_mode: str | None = None
    planner_connection_details: dict[str, Any] | None = None
    install_manifest: dict[str, Any] | None = None


class ReadinessResponse(BaseModel):
    """GET /api/v1/readiness response."""

    ready: bool
    calibrated: bool
    time_trusted: bool
    blockers: list[BlockerCondition]
    degraded: list[BlockerCondition]
    storage_summary: dict[str, Any]
    power_summary: dict[str, Any]
    external_control_summary: dict[str, Any] | None = None
    supervision_ready: bool = False
    supervision_blockers: list[BlockerCondition] = Field(default_factory=list)


class SessionStateResponse(BaseModel):
    """GET /api/v1/session/current/state — lightweight polling view."""

    session_id: str | None
    state: str
    workflow_intent: str | None
    control_locked: bool
    latest_message: str
    blockers: list[BlockerCondition] = Field(default_factory=list)
    degraded: list[BlockerCondition] = Field(default_factory=list)
    pause_summary: dict[str, Any] | None = None
    # v1.1 supervisory fields
    supervisory_next_action: str | None = None
    active_owner: str | None = None
    intervention_summary: dict[str, Any] | None = None


class SessionSummaryResponse(BaseModel):
    """GET /api/v1/session/current — full managed session summary."""

    session_id: str | None
    state: str
    workflow_intent: str | None
    control_locked: bool
    target_summary: dict[str, Any] | None
    run_parameters: dict[str, Any]
    timing_summary: dict[str, Any]
    quality_summary: dict[str, Any]
    blockers: list[BlockerCondition] = Field(default_factory=list)
    degraded: list[BlockerCondition] = Field(default_factory=list)
    terminal_outcome: str | None = None


class ActionResponse(BaseModel):
    """Response body for all state-changing session action endpoints."""

    state: str
    workflow_intent: str | None
    control_locked: bool
    message: str
    blockers: list[BlockerCondition] = Field(default_factory=list)
    degraded: list[BlockerCondition] = Field(default_factory=list)


class FrameSummary(BaseModel):
    """Single frame entry in GET /api/v1/session/current/frames."""

    frame_id: str
    capture_timestamp: datetime
    acceptance_summary: str
    solve_summary: dict[str, Any] = Field(default_factory=dict)
    quality_summary: dict[str, Any] = Field(default_factory=dict)


class FrameListResponse(BaseModel):
    """GET /api/v1/session/current/frames response."""

    frames: list[FrameSummary]
    next_before_frame_id: str | None = None


class ArtifactSummary(BaseModel):
    """Single artifact entry in GET /api/v1/session/current/artifacts."""

    artifact_kind: str
    relative_path: str
    frame_id: str | None = None
    created_at: str | None = None


class ArtifactListResponse(BaseModel):
    """GET /api/v1/session/current/artifacts response."""

    artifacts: list[ArtifactSummary]


class OutcomeSummary(BaseModel):
    """GET /api/v1/session/current/outcome response when session is terminal."""

    session_id: str
    state: str
    terminal_outcome: str
    stop_reason: str | None = None
    failure_explanation: str | None = None


class EventSummary(BaseModel):
    """Single event entry returned by GET /api/v1/session/current/events."""

    sequence: int
    timestamp: datetime
    event_type: str
    state: str
    severity: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class EventListResponse(BaseModel):
    """GET /api/v1/session/current/events response."""

    events: list[EventSummary]
    next_before_sequence: int | None = None


class TimeConfirmRequest(BaseModel):
    """POST /api/v1/time/confirm request body."""

    confirmed_at: datetime = Field(
        description="Operator-confirmed RFC 3339 timestamp to apply to the node wall clock."
    )


class TimeConfirmResponse(BaseModel):
    """POST /api/v1/time/confirm response body."""

    trusted: bool
    source: str
    summary: str
    applied: bool


# ------------------------------------------------------------------ #
# Equipment profile API models                                         #
# ------------------------------------------------------------------ #


class EquipmentProfileSummary(BaseModel):
    """Single entry in GET /api/v1/equipment/profiles list."""

    profile_id: str
    display_name: str
    is_default: bool
    hardware_summary: dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime


class EquipmentProfileListResponse(BaseModel):
    """GET /api/v1/equipment/profiles response."""

    profiles: list[EquipmentProfileSummary]
    active_profile_id: str | None = None


class EquipmentProfileResponse(BaseModel):
    """GET /api/v1/equipment/profiles/{profile_id} response — full document."""

    profile: dict[str, Any]
    is_active: bool = False


# ------------------------------------------------------------------ #
# Target intake API models                                             #
# ------------------------------------------------------------------ #


class TargetRequest(BaseModel):
    """POST /api/v1/target request body."""

    target_label: str
    ra_hours: float
    dec_deg: float
    target_source: str = "manual"
    run_parameters: dict[str, Any] = Field(default_factory=dict)


class TargetCurrentResponse(BaseModel):
    """GET /api/v1/target/current response."""

    target_label: str | None
    ra_hours: float | None
    dec_deg: float | None
    target_source: str | None
    run_parameters: dict[str, Any]
    active_equipment_profile_id: str | None


class SessionStartRequest(BaseModel):
    """POST /api/v1/session/start — no required body in v1; run parameters may be in staged target."""

    pass


# ------------------------------------------------------------------ #
# Supervisory session API models (v1.1)                                #
# ------------------------------------------------------------------ #


class PlannerModeResponse(BaseModel):
    """GET /api/v1/planner-mode response."""

    planner_mode: str | None
    connection_details: dict[str, Any] | None = None


class InterventionStateResponse(BaseModel):
    """GET /api/v1/session/current/intervention response."""

    active_kind: str | None


class WidefieldRecommendationResponse(BaseModel):
    """GET /api/v1/widefield/recommendations response."""

    focal_length_mm: float
    aperture: float | None = None
    crop_factor: float
    pixel_pitch_um: float
    classic_500_seconds: float
    crop_500_seconds: float
    npf_seconds: float | None = None
    recommended_seconds: float
    focus_exposure_seconds: float
    focus_iso: int
    capture_iso_min: int
    capture_iso_max: int
    notes: list[str] = Field(default_factory=list)
    lens_model: str | None = None


class WidefieldConditionRequestBody(BaseModel):
    destination_dir: str | None = None
    sample_exposure_seconds: float = 2.0
    sample_iso: int = 3200
    focal_length_mm: float | None = None
    aperture: float | None = None


class WidefieldConditionEvaluationResponse(BaseModel):
    image_path: str
    sample_exposure_seconds: float
    sample_iso: int
    focal_length_mm: float
    aperture: float | None = None
    star_count: int
    background_adu: float
    highlight_fraction: float
    trailing_ceiling_seconds: float
    recommended_exposure_seconds: float
    recommended_iso: int
    status: str
    summary: str
    notes: list[str] = Field(default_factory=list)
    lens_model: str | None = None


class FocusAssistRequestBody(BaseModel):
    """POST /api/v1/widefield/focus-assist request body."""

    destination_dir: str | None = None
    exposure_seconds: float = 3.0
    iso: int = 3200
    aperture: float | None = None
    focus_min_raw: int = 45
    focus_max_raw: int = 1497
    coarse_step: int = 40
    fine_step: int = 10
    min_improvement_fraction: float = 0.05


class FocusAssistSampleResponse(BaseModel):
    raw_position: int
    image_path: str
    star_count: int
    hfr_mean: float | None = None
    tenengrad: float
    metric_source: str
    summary: str


class FocusAssistActionResponse(BaseModel):
    status: str
    started_raw: int
    best_raw: int
    final_raw: int
    summary: str
    coarse_samples: list[FocusAssistSampleResponse] = Field(default_factory=list)
    fine_samples: list[FocusAssistSampleResponse] = Field(default_factory=list)
    active_reason: str | None
    active_since: datetime | None
    retry_count: int
    recent_records: list[dict[str, Any]]
    intervention_window: str
