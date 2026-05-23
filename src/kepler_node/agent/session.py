"""Runtime session state models for Kepler Claw."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class WorkflowIntent(StrEnum):
    """High-level workflow intent that drives state transitions."""

    CALIBRATION = "calibration"
    TARGET_CENTERING = "target_centering"
    RECOVERY_VERIFICATION = "recovery_verification"
    CAPTURE = "capture"
    SUPERVISION = "supervision"


class ClawState(StrEnum):
    """State-machine states defined by the v1 handoff spec."""

    BOOT = "boot"
    DISCOVER = "discover"
    CONNECT = "connect"
    READY = "ready"
    CALIBRATE = "calibrate"
    TARGET_ACQUIRED = "target_acquired"
    TEST_CAPTURE = "test_capture"
    SOLVE = "solve"
    CORRECT = "correct"
    CENTER_VERIFY = "center_verify"
    CAPTURE = "capture"
    GUARD = "guard"
    RECOVER = "recover"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    # v1.1 supervisory states
    EKOS_WAIT = "ekos_wait"
    MONITORING = "monitoring"
    INTERVENING = "intervening"


class InterventionKind(StrEnum):
    """Types of bounded supervisory interventions Kepler may perform."""

    AUTOFOCUS = "autofocus"
    REVERIFY = "reverify"
    PAUSE_COOLING = "pause_cooling"
    PAUSE_TRACKING = "pause_tracking"
    AUTOFOCUS_ASSIST = "autofocus_assist"


class InterventionRecord(BaseModel):
    """Single intervention entry in the supervisory ledger."""

    kind: InterventionKind
    reason: str
    requested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    acknowledged: bool = False
    outcome: str | None = None
    retry_count: int = 0


class InterventionLedger(BaseModel):
    """Tracks bounded intervention history for a supervised session."""

    max_retries_per_kind: int = 2
    records: list[InterventionRecord] = Field(default_factory=list)
    active_kind: InterventionKind | None = None

    def open_intervention(self, kind: InterventionKind, reason: str) -> InterventionRecord:
        """Open a new intervention entry and mark it active."""
        retries = self.retries_for(kind)
        record = InterventionRecord(kind=kind, reason=reason, retry_count=retries)
        self.records.append(record)
        self.active_kind = kind
        return record

    def close_intervention(self, outcome: str) -> None:
        """Close the active intervention entry with the given outcome."""
        for r in reversed(self.records):
            if r.kind == self.active_kind and r.outcome is None:
                r.outcome = outcome
                r.acknowledged = True
                break
        self.active_kind = None

    def retries_for(self, kind: InterventionKind) -> int:
        """Return the number of past interventions of the given kind."""
        return sum(1 for r in self.records if r.kind == kind)

    def is_retry_exhausted(self, kind: InterventionKind) -> bool:
        """Return True when the retry budget for the given kind is used up."""
        return self.retries_for(kind) >= self.max_retries_per_kind

    @property
    def active_record(self) -> InterventionRecord | None:
        """Return the open intervention record, if any."""
        if self.active_kind is None:
            return None
        for r in reversed(self.records):
            if r.kind == self.active_kind and r.outcome is None:
                return r
        return None


class TerminalOutcome(StrEnum):
    """Terminal outcomes currently fixed by the v1 spec."""

    COMPLETED = "completed"
    STOPPED_BY_OPERATOR = "stopped_by_operator"
    RELEASED_CONTROL = "released_control"
    FAILED = "failed"


class ResumeContext(BaseModel):
    """Persisted pause metadata required for safe session resume."""

    resume_state: ClawState
    workflow_intent: WorkflowIntent
    pause_reason: str
    operator_action_required: str | None = None
    staged_target_id: str | None = None
    verification_prerequisites: dict[str, str] = Field(default_factory=dict)


class RuntimeSession(BaseModel):
    """Mutable runtime session state for the current Kepler workflow."""

    session_id: str | None = None
    state: ClawState = ClawState.BOOT
    workflow_intent: WorkflowIntent | None = None
    control_locked: bool = False
    resume_context: ResumeContext | None = None
    terminal_outcome: TerminalOutcome | None = None

    # Staged target for centering and recovery verification
    staged_target_ra_hours: float | None = None
    staged_target_dec_deg: float | None = None
    staged_target_id: str | None = None

    # Inline run parameters (set before session start)
    run_parameters: dict[str, Any] = Field(default_factory=dict)

    # Calibration state
    calibration_accepted: bool = False

    # Latest solve state (used by recovery decision logic)
    last_frame_path: str | None = None
    last_solve_ra_hours: float | None = None
    last_solve_dec_deg: float | None = None
    last_residual_arcmin: float | None = None
    last_solve_failure_category: str | None = None

    # Latest plain-language transition message (updated by ClawController on every transition)
    latest_message: str | None = None

    # Runtime retry counters (reset on fresh workflows; persisted into events)
    solve_attempts: int = 0
    calibration_loop_count: int = 0
    centering_loop_count: int = 0
    consecutive_bad_frames: int = 0
    reconnect_attempts: int = 0

    # v1.1 supervisory session fields
    intervention_ledger: InterventionLedger | None = None
    supervisory_next_action: str | None = None
    ekos_session_id: str | None = None

    @property
    def is_terminal(self) -> bool:
        """Return whether the current state is terminal."""

        return self.state in {ClawState.COMPLETED, ClawState.FAILED}

    def enter_calibrate(self) -> None:
        """Enter calibration flow, set workflow intent, and claim control lock."""

        self.state = ClawState.CALIBRATE
        self.workflow_intent = WorkflowIntent.CALIBRATION
        self.control_locked = True

    def enter_target_acquired(self) -> None:
        """Enter target-centering flow, set workflow intent, and claim control lock."""

        self.state = ClawState.TARGET_ACQUIRED
        self.workflow_intent = WorkflowIntent.TARGET_CENTERING
        self.control_locked = True

    def enter_capture(self) -> None:
        """Enter capture flow and mark Kepler as the control owner."""

        self.state = ClawState.CAPTURE
        self.workflow_intent = WorkflowIntent.CAPTURE
        self.control_locked = True

    def enter_recover(self) -> None:
        """Transition to the recover state and claim control lock."""

        self.state = ClawState.RECOVER
        self.control_locked = True

    def stage_target(
        self,
        *,
        ra_hours: float,
        dec_deg: float,
        target_id: str | None = None,
    ) -> None:
        """Accept and stage a target for centering, then enter target_acquired."""

        self.staged_target_ra_hours = ra_hours
        self.staged_target_dec_deg = dec_deg
        self.staged_target_id = target_id
        self.state = ClawState.TARGET_ACQUIRED
        self.workflow_intent = WorkflowIntent.TARGET_CENTERING
        self.control_locked = True

    def accept_calibration(self) -> None:
        """Mark calibration as accepted and reset its loop counters."""

        self.calibration_accepted = True
        self.calibration_loop_count = 0
        self.solve_attempts = 0

    def reset_verification_counters(self) -> None:
        """Reset per-workflow retry counters when starting a fresh loop."""

        self.solve_attempts = 0
        self.calibration_loop_count = 0
        self.centering_loop_count = 0
        self.consecutive_bad_frames = 0

    def pause(
        self,
        *,
        pause_reason: str,
        resume_state: ClawState,
        workflow_intent: WorkflowIntent,
        operator_action_required: str | None = None,
        staged_target_id: str | None = None,
        verification_prerequisites: dict[str, str] | None = None,
    ) -> None:
        """Persist pause metadata before transitioning to paused."""

        self.resume_context = ResumeContext(
            resume_state=resume_state,
            workflow_intent=workflow_intent,
            pause_reason=pause_reason,
            operator_action_required=operator_action_required,
            staged_target_id=staged_target_id,
            verification_prerequisites=verification_prerequisites or {},
        )
        self.state = ClawState.PAUSED
        self.workflow_intent = workflow_intent

    def release_control(self) -> None:
        """Terminate a paused session by explicitly releasing control."""

        if self.state != ClawState.PAUSED:
            raise ValueError("release_control is only valid from the paused state")

        self.control_locked = False
        self.resume_context = None
        self.workflow_intent = None
        self.intervention_ledger = None
        self.supervisory_next_action = None
        self.ekos_session_id = None
        self.state = ClawState.COMPLETED
        self.terminal_outcome = TerminalOutcome.RELEASED_CONTROL

    def stop(self) -> None:
        """Stop the active session and clear resumability."""

        self.resume_context = None
        self.control_locked = False
        self.workflow_intent = None
        self.state = ClawState.COMPLETED
        self.terminal_outcome = TerminalOutcome.STOPPED_BY_OPERATOR

    def fail(self) -> None:
        """Move the session into a failed terminal state."""

        self.resume_context = None
        self.control_locked = False
        self.workflow_intent = None
        self.intervention_ledger = None
        self.supervisory_next_action = None
        self.ekos_session_id = None
        self.state = ClawState.FAILED
        self.terminal_outcome = TerminalOutcome.FAILED

    def acknowledge_complete(self) -> None:
        """Acknowledge a completed session and return the node to ready.

        Valid only from COMPLETED.  Clears terminal metadata so the node
        can accept a new workflow without losing the stored session record.
        """
        if self.state != ClawState.COMPLETED:
            raise ValueError("acknowledge_complete is only valid from the completed state")

        self.state = ClawState.READY
        self.terminal_outcome = None
        self.workflow_intent = None
        self.session_id = None
        self.intervention_ledger = None
        self.supervisory_next_action = None
        self.ekos_session_id = None

    def clear_failure(self) -> None:
        """Clear a failed session after operator review and return the node to ready.

        Valid only from FAILED.  The API layer is responsible for verifying
        that active blocking conditions no longer require the failed state
        before calling this method; the session layer just performs the transition.
        """
        if self.state != ClawState.FAILED:
            raise ValueError("clear_failure is only valid from the failed state")

        self.state = ClawState.READY
        self.terminal_outcome = None
        self.workflow_intent = None
        self.session_id = None
        self.intervention_ledger = None
        self.supervisory_next_action = None
        self.ekos_session_id = None
