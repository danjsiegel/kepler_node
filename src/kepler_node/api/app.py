"""FastAPI application builder for the Kepler Node local API (Phase 4).

Usage::

    from kepler_node.api.app import build_app
    app = build_app(controller=my_controller)

The ``controller`` argument must be a ``ClawController`` instance.  In
production the CLI creates it from the active adapters and settings;
in tests a ``ClawController`` with fake adapters is injected instead.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query

from kepler_node.agent.claw import ClawController
from kepler_node.agent.interfaces import ReadinessCondition
from kepler_node.agent.node_management import confirm_time_action
from kepler_node.agent.session import ClawState, RuntimeSession, TerminalOutcome
from kepler_node.api.models import (
    ActionResponse,
    ArtifactListResponse,
    ArtifactSummary,
    BlockerCondition,
    EventListResponse,
    EventSummary,
    FrameListResponse,
    FrameSummary,
    HealthResponse,
    NodeStatusResponse,
    OutcomeSummary,
    ReadinessResponse,
    SessionStateResponse,
    SessionSummaryResponse,
    TimeConfirmRequest,
    TimeConfirmResponse,
)

# States that indicate no active managed session
_PRE_SESSION_STATES = {
    ClawState.BOOT,
    ClawState.DISCOVER,
    ClawState.CONNECT,
    ClawState.READY,
}


def _to_blocker(c: ReadinessCondition) -> BlockerCondition:
    return BlockerCondition(
        name=c.name,
        severity=c.severity,
        summary=c.summary,
        operator_action_required=c.operator_action_required,
    )


# States where mount and camera have not yet been connected
_PRE_CONNECT_STATES = {ClawState.BOOT, ClawState.DISCOVER, ClawState.CONNECT}


def _get_detected_devices(controller: ClawController) -> dict[str, dict[str, bool]]:
    """Derive mount/camera connection summary from session state.

    The v1 camera and mount protocols do not expose device identity, so
    connection state is inferred from ``ClawState``: once the node advances
    past CONNECT both adapters were successfully connected.
    """
    connected = controller.session.state not in _PRE_CONNECT_STATES
    return {
        "mount": {"connected": connected},
        "camera": {"connected": connected},
    }


def _get_degraded(controller: ClawController) -> list[BlockerCondition]:
    """Derive non-blocking degraded conditions from node backend state."""
    degraded: list[BlockerCondition] = []
    storage = controller.node.storage_status()
    free_gb = storage.free_bytes / (1024**3)
    # warn if below 20 GiB but not yet critically low
    if 0 < free_gb < 20 and storage.writable and "critically" not in storage.summary:
        degraded.append(
            BlockerCondition(
                name="low_storage_warning",
                severity="degraded",
                summary=f"Storage is below 20 GiB ({free_gb:.1f} GiB free)",
            )
        )
    time_st = controller.node.time_status()
    if time_st.trusted and time_st.source not in {"ntp", "gps"}:
        degraded.append(
            BlockerCondition(
                name="time_source_mismatch",
                severity="degraded",
                summary=f"Time source is {time_st.source!r}; NTP or GPS preferred",
            )
        )
    return degraded


def _get_session_blockers(session: RuntimeSession) -> list[BlockerCondition]:
    """Return session-state blockers that apply after any action response.

    These mirror the equivalent logic in ``GET /api/v1/readiness`` so thin
    clients that only inspect action responses still see the current
    session-level blocking condition (active session or uncleared terminal).
    """
    if session.state in _PRE_SESSION_STATES:
        return []

    if session.is_terminal:
        action = (
            "acknowledge-complete"
            if session.state == ClawState.COMPLETED
            else "clear-failure"
        )
        return [
            BlockerCondition(
                name="terminal_session_uncleared",
                severity="blocking",
                summary=(
                    f"Session is in terminal state '{session.state}'; "
                    f"call {action} before starting a new session"
                ),
                operator_action_required=f"POST /api/v1/session/{action}",
            )
        ]

    return [
        BlockerCondition(
            name="active_session",
            severity="blocking",
            summary=(
                f"A managed session is active (state: {session.state}); "
                "stop or release control before starting a new session"
            ),
            operator_action_required=(
                "POST /api/v1/session/stop or /api/v1/session/release-control"
            ),
        )
    ]


def _action_resp(
    controller: ClawController,
    message: str,
    *,
    next_state: ClawState | None = None,
) -> ActionResponse:
    """Build a standard action response from current controller/session state.

    Includes both hardware blockers (from ``check_readiness()``) and
    session-state blockers so thin clients see the full blocking picture
    without having to poll ``GET /api/v1/readiness`` separately.
    """
    state = next_state or controller.session.state
    hw_blockers = [_to_blocker(b) for b in controller.check_readiness()]
    session_blockers = _get_session_blockers(controller.session)
    return ActionResponse(
        state=state,
        workflow_intent=(
            controller.session.workflow_intent.value
            if controller.session.workflow_intent
            else None
        ),
        control_locked=controller.session.control_locked,
        message=message,
        blockers=hw_blockers + session_blockers,
        degraded=_get_degraded(controller),
    )


def build_app(*, controller: ClawController) -> FastAPI:
    """Build and return a FastAPI application bound to *controller*.

    All routes close over the controller instance so no global state or
    request-scoped dependency injection is needed for the v1 single-node
    deployment model.
    """
    app = FastAPI(
        title="Kepler Node API",
        version="1.0.0",
        description="Local control API for the Kepler autonomous imaging node.",
    )

    # ------------------------------------------------------------------ #
    # GET /api/v1/health                                                   #
    # ------------------------------------------------------------------ #

    @app.get("/api/v1/health", response_model=HealthResponse)
    def get_health() -> HealthResponse:
        """Overall node health and service summary."""
        services = controller.node.service_health()
        if any(not s.healthy for s in services):
            overall = "degraded"
        else:
            overall = "healthy"

        return HealthResponse(
            status=overall,
            summary=f"Node is {overall}",
            updated_at=datetime.now(UTC),
            services=[
                {"name": s.name, "status": "healthy" if s.healthy else "degraded", "summary": s.summary}
                for s in services
            ],
        )

    # ------------------------------------------------------------------ #
    # GET /api/v1/node/status                                              #
    # ------------------------------------------------------------------ #

    @app.get("/api/v1/node/status", response_model=NodeStatusResponse)
    def get_node_status() -> NodeStatusResponse:
        """Current Claw state, network mode, device summary, time, and power."""
        time_status = controller.node.time_status()
        power_status = controller.node.power_status()
        network_mode = controller.node.network_mode()

        return NodeStatusResponse(
            state=controller.session.state,
            workflow_intent=(
                controller.session.workflow_intent.value
                if controller.session.workflow_intent
                else None
            ),
            control_locked=controller.session.control_locked,
            network_mode=network_mode,
            time_certainty={
                "trusted": time_status.trusted,
                "source": time_status.source,
                "summary": time_status.summary,
            },
            power_integrity={
                "healthy": power_status.healthy,
                "undervoltage_detected": power_status.undervoltage_detected,
                "summary": power_status.summary,
            },
            detected_devices=_get_detected_devices(controller),
        )

    # ------------------------------------------------------------------ #
    # GET /api/v1/readiness                                                #
    # ------------------------------------------------------------------ #

    @app.get("/api/v1/readiness", response_model=ReadinessResponse)
    def get_readiness() -> ReadinessResponse:
        """Readiness status for calibration and session start."""
        hw_blockers = controller.check_readiness()
        session = controller.session
        time_status = controller.node.time_status()
        storage_status = controller.node.storage_status()
        power_status = controller.node.power_status()

        session_blockers = _get_session_blockers(session)
        external_control_summary: dict | None = None

        if session.state not in _PRE_SESSION_STATES:
            external_control_summary = {
                "state": session.state,
                "control_locked": session.control_locked,
                "session_id": session.session_id,
                "workflow_intent": (
                    session.workflow_intent.value if session.workflow_intent else None
                ),
            }

        all_blockers = [_to_blocker(b) for b in hw_blockers] + session_blockers
        return ReadinessResponse(
            ready=len(all_blockers) == 0,
            calibrated=session.calibration_accepted,
            time_trusted=time_status.trusted,
            blockers=all_blockers,
            degraded=_get_degraded(controller),
            storage_summary={
                "free_bytes": storage_status.free_bytes,
                "total_bytes": storage_status.total_bytes,
                "writable": storage_status.writable,
                "summary": storage_status.summary,
            },
            power_summary={
                "healthy": power_status.healthy,
                "summary": power_status.summary,
            },
            external_control_summary=external_control_summary,
        )

    # ------------------------------------------------------------------ #
    # POST /api/v1/time/confirm                                           #
    # ------------------------------------------------------------------ #

    @app.post("/api/v1/time/confirm", response_model=TimeConfirmResponse)
    def post_time_confirm(body: TimeConfirmRequest) -> TimeConfirmResponse:
        """Apply an operator-confirmed timestamp to the node wall clock.

        Valid only when the node is not in active motion or capture.
        Fails closed: if the clock set fails, time remains untrusted.
        """
        try:
            result = confirm_time_action(
                session=controller.session,
                backend=controller.node,
                timestamp=body.confirmed_at,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        applied = result.trusted
        return TimeConfirmResponse(
            trusted=result.trusted,
            source=result.source.value if hasattr(result.source, "value") else str(result.source),
            summary=result.summary,
            applied=applied,
        )

    # ------------------------------------------------------------------ #
    # POST /api/v1/calibrate                                               #
    # ------------------------------------------------------------------ #

    @app.post("/api/v1/calibrate", response_model=ActionResponse)
    def post_calibrate() -> ActionResponse:
        """Enter calibration.  Valid from ready or target_acquired; 409 otherwise.

        Returns 422 when readiness blockers still exist.
        On success, transitions to the calibrate state and returns the new state.
        """
        try:
            result = controller.begin_calibrate()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _action_resp(controller, result.message)

    # ------------------------------------------------------------------ #
    # GET /api/v1/session/current                                          #
    # ------------------------------------------------------------------ #

    @app.get("/api/v1/session/current", response_model=SessionSummaryResponse | None)
    def get_session_current() -> SessionSummaryResponse | None:
        """Full managed-session summary; null when no session is active."""
        session = controller.session
        if session.session_id is None and session.state in _PRE_SESSION_STATES:
            return None

        blockers = controller.check_readiness()
        target: dict[str, Any] | None = None
        if session.staged_target_ra_hours is not None:
            target = {
                "target_id": session.staged_target_id,
                "ra_hours": session.staged_target_ra_hours,
                "dec_deg": session.staged_target_dec_deg,
            }

        return SessionSummaryResponse(
            session_id=session.session_id,
            state=session.state,
            workflow_intent=(
                session.workflow_intent.value if session.workflow_intent else None
            ),
            control_locked=session.control_locked,
            target_summary=target,
            run_parameters=session.run_parameters,
            timing_summary={},
            quality_summary={
                "consecutive_bad_frames": session.consecutive_bad_frames,
                "last_residual_arcmin": session.last_residual_arcmin,
            },
            blockers=[_to_blocker(b) for b in blockers],
            degraded=_get_degraded(controller),
            terminal_outcome=(
                session.terminal_outcome.value if session.terminal_outcome else None
            ),
        )

    # ------------------------------------------------------------------ #
    # GET /api/v1/session/current/state                                    #
    # ------------------------------------------------------------------ #

    @app.get("/api/v1/session/current/state", response_model=SessionStateResponse | None)
    def get_session_state() -> SessionStateResponse | None:
        """Lightweight polling view; null when no session is active."""
        session = controller.session
        if session.session_id is None and session.state in _PRE_SESSION_STATES:
            return None

        blockers = controller.check_readiness()
        pause: dict[str, Any] | None = None
        if session.resume_context is not None:
            pause = {
                "pause_reason": session.resume_context.pause_reason,
                "resume_state": session.resume_context.resume_state,
                "operator_action_required": session.resume_context.operator_action_required,
            }

        return SessionStateResponse(
            session_id=session.session_id,
            state=session.state,
            workflow_intent=(
                session.workflow_intent.value if session.workflow_intent else None
            ),
            control_locked=session.control_locked,
            latest_message=session.latest_message or f"state: {session.state}",
            blockers=[_to_blocker(b) for b in blockers],
            degraded=_get_degraded(controller),
            pause_summary=pause,
        )

    # ------------------------------------------------------------------ #
    # POST /api/v1/session/stop                                            #
    # ------------------------------------------------------------------ #

    @app.post("/api/v1/session/stop", response_model=ActionResponse)
    def post_session_stop() -> ActionResponse:
        """Stop the active session and clear resumability."""
        session = controller.session
        if session.session_id is None:
            raise HTTPException(status_code=409, detail="No active managed session to stop")
        if session.state in _PRE_SESSION_STATES:
            raise HTTPException(status_code=409, detail="No active managed session to stop")
        if session.is_terminal:
            raise HTTPException(status_code=409, detail="Session is already terminal")

        result = controller.stop()
        return _action_resp(controller, result.message)

    # ------------------------------------------------------------------ #
    # POST /api/v1/session/pause                                           #
    # ------------------------------------------------------------------ #

    @app.post("/api/v1/session/pause", response_model=ActionResponse)
    def post_session_pause() -> ActionResponse:
        """Pause the active session (idempotent if already paused)."""
        session = controller.session
        if session.session_id is None:
            raise HTTPException(status_code=409, detail="No active managed session to pause")
        try:
            result = controller.pause()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _action_resp(controller, result.message, next_state=result.next_state)

    # ------------------------------------------------------------------ #
    # POST /api/v1/session/resume                                          #
    # ------------------------------------------------------------------ #

    @app.post("/api/v1/session/resume", response_model=ActionResponse)
    def post_session_resume() -> ActionResponse:
        """Resume a paused session.  409 when not paused or resume_context missing."""
        session = controller.session
        if session.state != ClawState.PAUSED:
            raise HTTPException(status_code=409, detail="Session is not paused")
        if session.resume_context is None:
            raise HTTPException(status_code=409, detail="No resume context available")

        result = controller.resume()
        return _action_resp(controller, result.message)

    # ------------------------------------------------------------------ #
    # POST /api/v1/session/release-control                                 #
    # ------------------------------------------------------------------ #

    @app.post("/api/v1/session/release-control", response_model=ActionResponse)
    def post_session_release_control() -> ActionResponse:
        """Release control from PAUSED and transition to COMPLETED."""
        session = controller.session
        if session.state != ClawState.PAUSED:
            raise HTTPException(
                status_code=409,
                detail="release-control is only valid from the paused state",
            )
        if session.session_id is None:
            raise HTTPException(
                status_code=409,
                detail="release-control requires an active managed session",
            )

        result = controller.release_control()
        return _action_resp(controller, result.message)

    # ------------------------------------------------------------------ #
    # POST /api/v1/session/acknowledge-complete                            #
    # ------------------------------------------------------------------ #

    @app.post("/api/v1/session/acknowledge-complete", response_model=ActionResponse)
    def post_session_acknowledge_complete() -> ActionResponse:
        """Acknowledge a completed session and return the node to ready."""
        if controller.session.state != ClawState.COMPLETED:
            raise HTTPException(
                status_code=409,
                detail="acknowledge-complete is only valid from the completed state",
            )

        result = controller.acknowledge_complete()
        return _action_resp(controller, result.message)

    # ------------------------------------------------------------------ #
    # POST /api/v1/session/clear-failure                                   #
    # ------------------------------------------------------------------ #

    @app.post("/api/v1/session/clear-failure", response_model=ActionResponse)
    def post_session_clear_failure() -> ActionResponse:
        """Clear a failed session after operator review.  422 when hardware blocks remain."""
        if controller.session.state != ClawState.FAILED:
            raise HTTPException(
                status_code=409,
                detail="clear-failure is only valid from the failed state",
            )

        try:
            result = controller.clear_failure()
        except RuntimeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        return _action_resp(controller, result.message)

    # ------------------------------------------------------------------ #
    # GET /api/v1/session/current/frames                                   #
    # ------------------------------------------------------------------ #

    @app.get("/api/v1/session/current/frames", response_model=FrameListResponse)
    def get_session_frames(
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        before_frame_id: Annotated[str | None, Query()] = None,
    ) -> FrameListResponse:
        """Newest-first frame list for the current session."""
        session_id = controller.session.session_id
        if session_id is None:
            return FrameListResponse(frames=[], next_before_frame_id=None)

        try:
            records, next_cursor = controller.store.list_frames(
                session_id, limit=limit, before_frame_id=before_frame_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except FileNotFoundError:
            return FrameListResponse(frames=[], next_before_frame_id=None)

        frames = [
            FrameSummary(
                frame_id=r.frame_id,
                capture_timestamp=r.capture_timestamp,
                acceptance_summary=r.action_decision or "pending",
                solve_summary=r.solve_result_summary,
                quality_summary=r.quality_metrics,
            )
            for r in records
        ]
        return FrameListResponse(frames=frames, next_before_frame_id=next_cursor)

    # ------------------------------------------------------------------ #
    # GET /api/v1/session/current/artifacts                                #
    # ------------------------------------------------------------------ #

    @app.get("/api/v1/session/current/artifacts", response_model=ArtifactListResponse)
    def get_session_artifacts() -> ArtifactListResponse:
        """Typed artifact summaries for the current session."""
        session_id = controller.session.session_id
        if session_id is None:
            return ArtifactListResponse(artifacts=[])

        try:
            raw = controller.store.list_artifacts(session_id)
        except FileNotFoundError:
            return ArtifactListResponse(artifacts=[])

        return ArtifactListResponse(
            artifacts=[
                ArtifactSummary(
                    artifact_kind=a["artifact_kind"],
                    relative_path=a["relative_path"],
                    frame_id=a.get("frame_id"),
                    created_at=a.get("created_at"),
                )
                for a in raw
            ]
        )

    # ------------------------------------------------------------------ #
    # GET /api/v1/session/current/outcome                                  #
    # ------------------------------------------------------------------ #

    @app.get("/api/v1/session/current/outcome", response_model=OutcomeSummary | None)
    def get_session_outcome() -> OutcomeSummary | None:
        """Terminal outcome summary; null when session is not yet terminal.

        ``stop_reason`` is populated for operator-initiated stops and control
        releases.  ``failure_explanation`` is populated for failed sessions
        and carries the last transition message so the operator can review
        why the session failed before clearing it.
        """
        session = controller.session
        if not session.is_terminal or session.terminal_outcome is None:
            return None

        stop_reason: str | None = None
        failure_explanation: str | None = None

        if session.terminal_outcome in {
            TerminalOutcome.STOPPED_BY_OPERATOR,
            TerminalOutcome.RELEASED_CONTROL,
        }:
            stop_reason = session.terminal_outcome.value
        elif session.terminal_outcome == TerminalOutcome.FAILED:
            failure_explanation = session.latest_message or "Session failed (no further detail available)"

        return OutcomeSummary(
            session_id=session.session_id or "unknown",
            state=session.state,
            terminal_outcome=session.terminal_outcome.value,
            stop_reason=stop_reason,
            failure_explanation=failure_explanation,
        )

    # ------------------------------------------------------------------ #
    # GET /api/v1/session/current/events                                   #
    # ------------------------------------------------------------------ #

    @app.get("/api/v1/session/current/events", response_model=EventListResponse)
    def get_session_events(
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        before_sequence: Annotated[int | None, Query()] = None,
    ) -> EventListResponse:
        """Newest-first event stream for the current session."""
        session_id = controller.session.session_id
        if session_id is None:
            return EventListResponse(events=[], next_before_sequence=None)

        try:
            records, next_cursor = controller.store.list_events(
                session_id, limit=limit, before_sequence=before_sequence
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except FileNotFoundError:
            return EventListResponse(events=[], next_before_sequence=None)

        events = [
            EventSummary(
                sequence=r.sequence,
                timestamp=r.timestamp,
                event_type=r.event_type,
                state=r.state,
                severity=r.severity,
                message=r.message,
                details=r.details,
            )
            for r in records
        ]
        return EventListResponse(events=events, next_before_sequence=next_cursor)

    return app
