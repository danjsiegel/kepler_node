"""Runtime session state models for Kepler Claw."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class WorkflowIntent(StrEnum):
    """High-level workflow intent that drives state transitions."""

    CALIBRATION = "calibration"
    TARGET_CENTERING = "target_centering"
    RECOVERY_VERIFICATION = "recovery_verification"
    CAPTURE = "capture"


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

    @property
    def is_terminal(self) -> bool:
        """Return whether the current state is terminal."""

        return self.state in {ClawState.COMPLETED, ClawState.FAILED}

    def enter_calibrate(self) -> None:
        """Enter calibration flow and set its required workflow intent."""

        self.state = ClawState.CALIBRATE
        self.workflow_intent = WorkflowIntent.CALIBRATION

    def enter_target_acquired(self) -> None:
        """Enter target-centering flow and set its required workflow intent."""

        self.state = ClawState.TARGET_ACQUIRED
        self.workflow_intent = WorkflowIntent.TARGET_CENTERING

    def enter_capture(self) -> None:
        """Enter capture flow and mark Kepler as the control owner."""

        self.state = ClawState.CAPTURE
        self.workflow_intent = WorkflowIntent.CAPTURE
        self.control_locked = True

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
        self.state = ClawState.FAILED
        self.terminal_outcome = TerminalOutcome.FAILED