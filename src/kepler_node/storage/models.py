"""Structured persistence models for Kepler v1 session storage."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from kepler_node.agent.session import ClawState, TerminalOutcome, WorkflowIntent


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
