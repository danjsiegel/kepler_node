from kepler_node.agent import ClawState, RuntimeSession, TerminalOutcome, WorkflowIntent


def test_runtime_session_sets_required_workflow_intents() -> None:
    session = RuntimeSession()

    session.enter_calibrate()
    assert session.state == ClawState.CALIBRATE
    assert session.workflow_intent == WorkflowIntent.CALIBRATION
    assert session.control_locked is True

    session.enter_target_acquired()
    assert session.state == ClawState.TARGET_ACQUIRED
    assert session.workflow_intent == WorkflowIntent.TARGET_CENTERING
    assert session.control_locked is True

    session.enter_capture()
    assert session.state == ClawState.CAPTURE
    assert session.workflow_intent == WorkflowIntent.CAPTURE
    assert session.control_locked is True


def test_pause_persists_resume_context_before_terminal_actions_clear_it() -> None:
    session = RuntimeSession(session_id="session-123")
    session.enter_capture()

    session.pause(
        pause_reason="external control conflict",
        resume_state=ClawState.TARGET_ACQUIRED,
        workflow_intent=WorkflowIntent.TARGET_CENTERING,
        operator_action_required="Resolve external control",
        staged_target_id="m51",
    )

    assert session.state == ClawState.PAUSED
    assert session.resume_context is not None
    assert session.resume_context.resume_state == ClawState.TARGET_ACQUIRED
    assert session.resume_context.workflow_intent == WorkflowIntent.TARGET_CENTERING

    session.release_control()

    assert session.state == ClawState.COMPLETED
    assert session.terminal_outcome == TerminalOutcome.RELEASED_CONTROL
    assert session.resume_context is None
    assert session.control_locked is False
    assert session.workflow_intent is None


def test_stop_and_fail_clear_resume_context_and_workflow_intent() -> None:
    stopped_session = RuntimeSession(session_id="session-stop")
    stopped_session.enter_capture()
    stopped_session.pause(
        pause_reason="operator requested stop",
        resume_state=ClawState.CAPTURE,
        workflow_intent=WorkflowIntent.CAPTURE,
    )

    stopped_session.stop()

    assert stopped_session.state == ClawState.COMPLETED
    assert stopped_session.terminal_outcome == TerminalOutcome.STOPPED_BY_OPERATOR
    assert stopped_session.resume_context is None
    assert stopped_session.control_locked is False
    assert stopped_session.workflow_intent is None

    failed_session = RuntimeSession(session_id="session-fail")
    failed_session.enter_capture()
    failed_session.pause(
        pause_reason="storage failure",
        resume_state=ClawState.CAPTURE,
        workflow_intent=WorkflowIntent.CAPTURE,
    )

    failed_session.fail()

    assert failed_session.state == ClawState.FAILED
    assert failed_session.terminal_outcome == TerminalOutcome.FAILED
    assert failed_session.resume_context is None
    assert failed_session.control_locked is False
    assert failed_session.workflow_intent is None


def test_release_control_requires_paused_state() -> None:
    session = RuntimeSession()

    try:
        session.release_control()
    except ValueError as exc:
        assert str(exc) == "release_control is only valid from the paused state"
    else:
        raise AssertionError("release_control should reject non-paused sessions")
