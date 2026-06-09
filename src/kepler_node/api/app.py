"""FastAPI application builder for the Kepler Node local API.

Usage::

    from kepler_node.api.app import build_app
    app = build_app(controller=my_controller)

The ``controller`` argument must be a ``ClawController`` instance.  In
production the CLI creates it from the active adapters and settings;
in tests a ``ClawController`` with fake adapters is injected instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket as _socket
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query

from kepler_node.agent.claw import ClawController
from kepler_node.agent.interfaces import (
    PowerStatus,
    ReadinessCondition,
    StorageStatus,
    TimeSource,
    TimeStatus,
)
from kepler_node.agent.node_management import confirm_time_action
from kepler_node.agent.session import ClawState, RuntimeSession, TerminalOutcome
from kepler_node.api.models import (
    ActionResponse,
    ArtifactListResponse,
    ArtifactSummary,
    BlockerCondition,
    EquipmentProfileListResponse,
    EquipmentProfileResponse,
    EquipmentProfileSummary,
    EventListResponse,
    EventSummary,
    FrameListResponse,
    FrameSummary,
    HealthResponse,
    FocusAssistActionResponse,
    FocusAssistRequestBody,
    FocusAssistSampleResponse,
    InterventionStateResponse,
    NodeStatusResponse,
    OutcomeSummary,
    PlannerModeResponse,
    ReadinessResponse,
    SessionStateResponse,
    SessionSummaryResponse,
    TargetCurrentResponse,
    TargetRequest,
    TimeConfirmRequest,
    TimeConfirmResponse,
    WidefieldConditionEvaluationResponse,
    WidefieldConditionRequestBody,
    WidefieldRecommendationResponse,
)
from kepler_node.camera.fuji_focus_assist import (
    evaluate_widefield_conditions,
    FocusAssistRequest,
    FujiFocusAssistRunner,
    recommend_widefield_settings,
)

_logger = logging.getLogger(__name__)

# How often the lifespan background task pings the camera to prevent
# auto-power-off.  Must be shorter than the Fuji body's ~5-minute idle timer.
_KEEPALIVE_INTERVAL_SECONDS = 90

# Poll interval for the Ekos frame-landing watcher.
_WATCHER_POLL_INTERVAL_SECONDS = 2.0

# Poll interval for the Ekos read-only observation loop (focus, temperature,
# sequence status).  5 seconds provides timely event population without hammering
# the DBus interface.
_EKOS_OBSERVATION_INTERVAL_SECONDS = 5.0

# Poll interval for the mount read-only observation loop.  Kept equal to the
# Ekos interval so external mount motion is noticed at roughly the same cadence
# as Ekos capture-state changes.
_MOUNT_OBSERVATION_INTERVAL_SECONDS = 5.0

# Human-readable labels for operator-facing time-source warnings.
_TIME_SOURCE_LABEL: dict[TimeSource, str] = {
    TimeSource.GPS: "GPS",
    TimeSource.NETWORK: "NTP/network",
    TimeSource.RTC: "RTC",
    TimeSource.OPERATOR_CONFIRMED: "operator-confirmed",
    TimeSource.UNTRUSTED: "untrusted",
}

# States that indicate no active managed session
_PRE_SESSION_STATES = {
    ClawState.BOOT,
    ClawState.DISCOVER,
    ClawState.CONNECT,
    ClawState.READY,
}


def _is_pre_session_posture(session: RuntimeSession) -> bool:
    """Return True when no managed session has actually started.

    A paused controller with ``session_id is None`` is a pre-session pause
    (for example connect blocked during startup), not an active managed
    imaging session.
    """

    return session.session_id is None and (
        session.state in _PRE_SESSION_STATES or session.state == ClawState.PAUSED
    )


def _to_blocker(c: ReadinessCondition) -> BlockerCondition:
    return BlockerCondition(
        name=c.name,
        severity=c.severity,
        summary=c.summary,
        operator_action_required=c.operator_action_required,
    )


# States where mount and camera have not yet been connected
_PRE_CONNECT_STATES = {ClawState.BOOT, ClawState.DISCOVER, ClawState.CONNECT}


def _get_detected_devices(controller: ClawController) -> dict[str, dict[str, bool | str]]:
    """Derive mount/camera summary from session state and profile readiness.

    The v1 camera and mount protocols do not expose device identity, so the
    API distinguishes between three coarse operator-facing states:

    - ``not_initialized``: no active equipment profile has been selected yet
    - ``pending_connect``: a profile exists, but the controller has not yet
      advanced past the connect stage
    - ``connected``: the controller progressed past CONNECT
    """
    connected = controller.session.state not in _PRE_CONNECT_STATES
    if connected:
        status = "connected"
    elif controller.active_equipment_profile is None:
        status = "not_initialized"
    else:
        status = "pending_connect"

    return {
        "mount": {"connected": connected, "status": status},
        "camera": {"connected": connected, "status": status},
    }


def _get_camera_diagnostic(controller: ClawController) -> dict[str, Any] | None:
    broker_owns_camera_path = getattr(controller, "_broker_owns_camera_path", None)
    if callable(broker_owns_camera_path) and broker_owns_camera_path():
        return None

    diagnostic_status = getattr(controller.camera, "diagnostic_status", None)
    if not callable(diagnostic_status):
        return None
    try:
        diagnostic = diagnostic_status()
    except Exception as exc:
        return {
            "status": "diagnostic_failed",
            "connected": False,
            "ready": False,
            "summary": f"Camera diagnostic probe failed: {exc}",
        }
    return diagnostic if diagnostic is not None else None


def _node_host() -> str:
    """Return the node's primary network address for operator connection details.

    Tries to discover the outbound interface IP first, then falls back to the
    system hostname so the operator always has something actionable.
    """
    try:
        with _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM) as s:
            s.settimeout(0)
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except Exception:
        return _socket.gethostname()


def _get_degraded(
    controller: ClawController,
    *,
    time_st: TimeStatus | None = None,
    camera_diag: dict[str, Any] | None = None,
) -> list[BlockerCondition]:
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
    if time_st is None:
        time_st = controller.node.time_status()
    # GPS vs NTP disagreement: surface when both sources are available and differ >5 s.
    if time_st.gps_ntp_mismatch_seconds is not None:
        degraded.append(
            BlockerCondition(
                name="time_source_mismatch",
                severity="degraded",
                summary=(
                    f"GPS and network time disagree by "
                    f"{time_st.gps_ntp_mismatch_seconds:.1f}s; using GPS (valid fix active)"
                ),
            )
        )
    elif time_st.trusted and time_st.source not in {TimeSource.NETWORK, TimeSource.GPS}:
        # Weaker-than-preferred time source (RTC or operator_confirmed).
        degraded.append(
            BlockerCondition(
                name="time_source_weaker",
                severity="degraded",
                summary=f"Time source is {_TIME_SOURCE_LABEL.get(time_st.source, time_st.source.value)}; NTP or GPS preferred",
            )
        )

    if camera_diag is None:
        camera_diag = _get_camera_diagnostic(controller)
    if camera_diag is not None and camera_diag.get("status") == "disconnected":
        degraded.append(
            BlockerCondition(
                name="camera_disconnected",
                severity="degraded",
                summary=camera_diag.get("summary", "No USB camera detected"),
            )
        )
    return degraded


def _get_hardware_blockers(
    *,
    time_st: TimeStatus,
    storage_st: StorageStatus,
    power_st: PowerStatus,
    camera_diag: dict[str, Any] | None,
) -> list[BlockerCondition]:
    blockers: list[BlockerCondition] = []

    if not time_st.trusted:
        blockers.append(
            BlockerCondition(
                name="time_uncertain",
                severity="blocking",
                summary="Time is not trusted; cannot start calibration or capture",
                operator_action_required="Confirm time or wait for NTP synchronization",
                details={"time_source": time_st.source, "time_summary": time_st.summary},
            )
        )

    if "critically" in storage_st.summary or not storage_st.writable:
        blockers.append(
            BlockerCondition(
                name="storage_critically_low",
                severity="blocking",
                summary=storage_st.summary,
                operator_action_required="Free disk space or verify storage mount before continuing",
                details={"free_bytes": str(storage_st.free_bytes)},
            )
        )

    if not power_st.healthy:
        blockers.append(
            BlockerCondition(
                name="power_integrity_warning",
                severity="blocking",
                summary=power_st.summary,
                operator_action_required="Check power supply and USB connections",
                details={"undervoltage": str(power_st.undervoltage_detected)},
            )
        )

    if camera_diag is None:
        return blockers

    camera_status = camera_diag.get("status")
    if camera_status == "diagnostic_failed":
        blockers.append(
            BlockerCondition(
                name="camera_diagnostic_failed",
                severity="blocking",
                summary=camera_diag.get("summary", "Camera diagnostic probe failed"),
                operator_action_required="Check camera USB connection and retry",
            )
        )
    elif camera_status in {"card_reader_mode", "detected_unknown_mode"}:
        blockers.append(
            BlockerCondition(
                name="camera_remote_mode_required",
                severity="blocking",
                summary=camera_diag.get(
                    "summary",
                    "Camera is not in a supported USB remote-control mode",
                ),
                operator_action_required="Switch camera to USB tether/remote-control mode and retry",
                details={"camera_status": str(camera_status)},
            )
        )
    elif camera_status == "autocapture_mode":
        blockers.append(
            BlockerCondition(
                name="camera_autocapture_mode_blocking",
                severity="blocking",
                summary=camera_diag.get(
                    "summary",
                    "Camera is in a blocked self-timer/autocapture mode",
                ),
                operator_action_required=(
                    camera_diag.get("operator_hint")
                    or "Exit self-timer/autocapture mode on the camera body and retry"
                ),
                details={
                    "camera_status": str(camera_status),
                    "capture_mode": str(camera_diag.get("capture_mode")),
                    "capture_delay": str(camera_diag.get("capture_delay")),
                },
            )
        )

    return blockers


def _get_session_blockers(session: RuntimeSession) -> list[BlockerCondition]:
    """Return session-state blockers that apply after any action response.

    These mirror the equivalent logic in ``GET /api/v1/readiness`` so thin
    clients that only inspect action responses still see the current
    session-level blocking condition (active session or uncleared terminal).
    """
    if _is_pre_session_posture(session):
        return []

    if session.is_terminal:
        action = "acknowledge-complete" if session.state == ClawState.COMPLETED else "clear-failure"
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
            controller.session.workflow_intent.value if controller.session.workflow_intent else None
        ),
        control_locked=controller.session.control_locked,
        message=message,
        blockers=hw_blockers + session_blockers,
        degraded=_get_degraded(controller),
    )


async def _camera_keepalive_loop(controller: ClawController) -> None:
    """Background coroutine: call camera_keepalive() every _KEEPALIVE_INTERVAL_SECONDS.

    Runs for the lifetime of the FastAPI app.  Exceptions from keepalive are
    logged but never propagate so the loop cannot bring down the server.
    The interval sleep comes *first* so the node has time to finish startup
    before the first probe fires.
    """
    while True:
        await asyncio.sleep(_KEEPALIVE_INTERVAL_SECONDS)
        try:
            controller.camera_keepalive()
        except Exception:
            _logger.exception("camera keepalive loop raised unexpectedly")


async def _ekos_observation_loop(controller: ClawController) -> None:
    """Background coroutine: poll Ekos device state every _EKOS_OBSERVATION_INTERVAL_SECONDS.

    Calls ``controller.poll_ekos_observation()`` to populate focus, temperature,
    and sequence-status events in the Ekos adapter queue.  Those events are
    drained by ``_check_conflicts()`` when the controller performs conflict
    detection, completing the read-only observation seam.

    Exceptions from individual polls are absorbed by the adapter; this loop
    never propagates so it cannot bring down the server.  The interval sleep
    comes first so the node has time to finish startup before the first probe.
    """
    while True:
        await asyncio.sleep(_EKOS_OBSERVATION_INTERVAL_SECONDS)
        try:
            controller.poll_ekos_observation()
        except Exception:
            _logger.exception("ekos observation loop raised unexpectedly")


async def _mount_observation_loop(controller: ClawController) -> None:
    """Background coroutine: poll mount device state every _MOUNT_OBSERVATION_INTERVAL_SECONDS.

    Calls ``controller.poll_mount_observation()`` to populate mount activity
    events (including externally-authored slews) in the mount adapter queue.
    Events are drained by ``_check_conflicts()`` during conflict detection,
    making mount observation continuous and symmetric with Ekos observation.

    Exceptions from individual polls are absorbed; this loop never propagates.
    The interval sleep comes first so the node has time to finish startup.
    """
    while True:
        await asyncio.sleep(_MOUNT_OBSERVATION_INTERVAL_SECONDS)
        try:
            controller.poll_mount_observation()
        except Exception:
            _logger.exception("mount observation loop raised unexpectedly")


async def _frame_watcher_loop(controller: ClawController, output_dir: Path) -> None:
    """Background coroutine: watch *output_dir* for newly landed Ekos frames.

    Consumes the ``FrameWatcher.watch()`` async generator.  Each landed frame
    is forwarded to ``controller.observe_landed_frame()`` so the intervention
    policy can react.  A ``FrameQualitySession`` is created per managed session:
    when the controller's ``session_id`` changes the watcher's rolling quality
    state is reset so the new session starts with a clean baseline.
    """
    from kepler_node.imaging.frame_quality import FrameQualitySession
    from kepler_node.imaging.watcher import FrameWatcher

    quality_session = FrameQualitySession()
    watcher = FrameWatcher(
        output_dir, session=quality_session, poll_interval_seconds=_WATCHER_POLL_INTERVAL_SECONDS
    )
    _logger.info("frame watcher started on %s", output_dir)
    last_session_id: str | None = controller.session.session_id
    try:
        async for path, result in watcher.watch():
            # Reset rolling quality state whenever the managed session changes so
            # each new session starts with a fresh baseline rather than inheriting
            # history from the previous session.
            current_session_id = controller.session.session_id
            if current_session_id != last_session_id:
                quality_session = FrameQualitySession()
                # Re-attribute the current frame to the new session so the
                # first post-switch frame is counted in the new session's
                # rolling baseline rather than being silently lost.
                quality_session.add(result)
                watcher.set_session(quality_session)
                last_session_id = current_session_id
            try:
                controller.observe_landed_frame(path, result, quality_session)
            except Exception:
                _logger.exception("frame watcher: observe_landed_frame raised for %s", path.name)
    except asyncio.CancelledError:
        watcher.stop()
        raise


def build_app(*, controller: ClawController, ekos_output_dir: Path | None = None) -> FastAPI:
    """Build and return a FastAPI application bound to *controller*.

    All routes close over the controller instance so no global state or
    request-scoped dependency injection is needed for the v1 single-node
    deployment model.

    Args:
        controller:      The ``ClawController`` instance to bind.
        ekos_output_dir: Optional path to the Ekos output directory.  When
                         provided, a background ``_frame_watcher_loop`` task is
                         started during the lifespan to ingest newly landed frames.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ARG001
        tasks = [
            asyncio.create_task(_camera_keepalive_loop(controller)),
            asyncio.create_task(_ekos_observation_loop(controller)),
            asyncio.create_task(_mount_observation_loop(controller)),
        ]
        if ekos_output_dir is not None:
            tasks.append(asyncio.create_task(_frame_watcher_loop(controller, ekos_output_dir)))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(
        title="Kepler Node API",
        version="1.0.0",
        description="Local control API for the Kepler autonomous imaging node.",
        lifespan=lifespan,
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
                {
                    "name": s.name,
                    "status": "healthy" if s.healthy else "degraded",
                    "summary": s.summary,
                }
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

        # Install manifest summary
        manifest = controller.store.read_install_manifest()
        install_manifest_summary: dict[str, Any] | None = None
        if manifest is not None:
            install_manifest_summary = {
                "kepler_version": manifest.kepler_version,
                "release_id": manifest.release_id,
                "bootstrap_profile": manifest.bootstrap_profile,
                "installed_at": manifest.installed_at.isoformat(),
                "last_upgrade_at": (
                    manifest.last_upgrade_at.isoformat() if manifest.last_upgrade_at else None
                ),
                "last_upgrade_result": manifest.last_upgrade_result,
            }

        # Active equipment profile summary
        profile = controller.active_equipment_profile
        profile_summary: dict[str, Any] | None = None
        if profile is not None:
            profile_summary = {
                "profile_id": profile.profile_id,
                "display_name": profile.display_name,
                "lens_is_zoom": profile.hardware.lens.is_zoom,
                "focal_length_mm": (
                    profile.solving_hints.focal_length_assumption_mm
                    or profile.hardware.lens.default_focal_length_mm
                ),
            }

        # Service reachability for planner connection details
        planner_services = controller.node.service_health()
        planner_service_map = {s.name: s.healthy for s in planner_services}

        # Planner mode derived from bootstrap profile
        planner_mode: str | None = None
        planner_connection: dict[str, Any] | None = None
        if manifest is not None and manifest.bootstrap_profile:
            planner_mode = manifest.bootstrap_profile
            node_host = _node_host()
            indi_reachable = planner_service_map.get("indiserver")
            kepler_reachable = planner_service_map.get("kepler-node")
            xrdp_reachable = planner_service_map.get("xrdp")
            if planner_mode == "headless-node":
                planner_connection = {
                    "mode": "remote_kstars_ekos",
                    "host": node_host,
                    "summary": (
                        f"Connect KStars/Ekos remotely: set INDI server host to "
                        f"{node_host} and port 7624"
                    ),
                    "indi_port": 7624,
                    "indi_reachable": indi_reachable,
                    "kepler_reachable": kepler_reachable,
                }
            elif planner_mode == "field-fallback":
                planner_connection = {
                    "mode": "on_node_kstars_ekos",
                    "host": node_host,
                    "summary": (
                        f"Launch KStars/Ekos on this node via xRDP remote desktop: "
                        f"connect to {node_host} on port 3389 (RDP)"
                    ),
                    "rdp_port": 3389,
                    "indi_reachable": indi_reachable,
                    "kepler_reachable": kepler_reachable,
                    "xrdp_reachable": xrdp_reachable,
                }

        build_summary = "kepler-node v1"
        if manifest is not None:
            build_summary = f"kepler-node {manifest.kepler_version}"

        detected_devices = _get_detected_devices(controller)
        camera_diag = _get_camera_diagnostic(controller)
        if camera_diag is not None:
            detected_devices["camera"] = {
                **detected_devices["camera"],
                "status": camera_diag.get("status", detected_devices["camera"]["status"]),
                "summary": camera_diag.get("summary"),
                "usb_connected": bool(camera_diag.get("connected")),
                "ready": bool(camera_diag.get("ready")),
            }

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
            detected_devices=detected_devices,
            build_summary=build_summary,
            active_equipment_profile=profile_summary,
            planner_mode=planner_mode,
            planner_connection_details=planner_connection,
            install_manifest=install_manifest_summary,
        )

    # ------------------------------------------------------------------ #
    # GET /api/v1/readiness                                                #
    # ------------------------------------------------------------------ #

    @app.get("/api/v1/readiness", response_model=ReadinessResponse)
    def get_readiness() -> ReadinessResponse:
        """Readiness status for calibration and session start."""
        session = controller.session
        time_status = controller.node.time_status()
        storage_status = controller.node.storage_status()
        power_status = controller.node.power_status()
        camera_diag = _get_camera_diagnostic(controller)
        hw_blockers = _get_hardware_blockers(
            time_st=time_status,
            storage_st=storage_status,
            power_st=power_status,
            camera_diag=camera_diag,
        )

        session_blockers = _get_session_blockers(session)
        external_control_summary: dict | None = None

        if not _is_pre_session_posture(session):
            external_control_summary = {
                "state": session.state,
                "control_locked": session.control_locked,
                "session_id": session.session_id,
                "workflow_intent": (
                    session.workflow_intent.value if session.workflow_intent else None
                ),
            }

        all_blockers = [_to_blocker(b) for b in hw_blockers] + session_blockers

        # supervision_ready must mirror the attach gate: no generic blockers,
        # READY state, active profile, AND Ekos/broker reachable.
        supervision_blockers = (
            controller.get_supervision_blockers() if session.state == ClawState.READY else []
        )
        supervision_ready = (
            len(all_blockers) == 0
            and session.state == ClawState.READY
            and controller.active_equipment_profile is not None
            and len(supervision_blockers) == 0
        )
        return ReadinessResponse(
            ready=len(all_blockers) == 0,
            calibrated=session.calibration_accepted,
            time_trusted=time_status.trusted,
            blockers=all_blockers,
            degraded=_get_degraded(controller, time_st=time_status, camera_diag=camera_diag),
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
            supervision_ready=supervision_ready,
            supervision_blockers=[_to_blocker(b) for b in supervision_blockers],
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

    @app.post("/api/v1/focus-calibrate", response_model=ActionResponse)
    def post_focus_calibrate() -> ActionResponse:
        """Run an explicit pre-session Fuji focus calibration and return to ready."""

        try:
            result = controller.run_focus_calibration()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _action_resp(controller, result.message, next_state=result.next_state)

    @app.get("/api/v1/widefield/recommendations", response_model=WidefieldRecommendationResponse)
    def get_widefield_recommendations(
        focal_length_mm: Annotated[float | None, Query()] = None,
        aperture: Annotated[float | None, Query()] = None,
    ) -> WidefieldRecommendationResponse:
        profile = controller.active_equipment_profile
        resolved_focal_length = focal_length_mm
        resolved_aperture = aperture
        lens_model = None
        if profile is not None:
            lens_model = profile.hardware.lens.model
            if resolved_focal_length is None:
                resolved_focal_length = (
                    profile.solving_hints.focal_length_assumption_mm
                    or profile.hardware.lens.default_focal_length_mm
                )
        if resolved_focal_length is None:
            raise HTTPException(status_code=422, detail="focal_length_mm is required when no active profile provides one")

        recommendation = recommend_widefield_settings(
            focal_length_mm=resolved_focal_length,
            aperture=resolved_aperture,
        )
        return WidefieldRecommendationResponse(
            **recommendation.__dict__,
            lens_model=lens_model,
        )

    @app.post("/api/v1/widefield/evaluate", response_model=WidefieldConditionEvaluationResponse)
    def post_widefield_evaluate(
        body: WidefieldConditionRequestBody,
    ) -> WidefieldConditionEvaluationResponse:
        profile = controller.active_equipment_profile
        resolved_focal_length = body.focal_length_mm
        resolved_aperture = body.aperture
        lens_model = None
        if profile is not None:
            lens_model = profile.hardware.lens.model
            if resolved_focal_length is None:
                resolved_focal_length = (
                    profile.solving_hints.focal_length_assumption_mm
                    or profile.hardware.lens.default_focal_length_mm
                )
        if resolved_focal_length is None:
            raise HTTPException(status_code=422, detail="focal_length_mm is required when no active profile provides one")

        destination_dir = (
            Path(body.destination_dir)
            if body.destination_dir
            else controller.store.data_root / "widefield-eval" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        )

        with controller._camera_io_lock:
            try:
                controller.camera.connect()
                result = evaluate_widefield_conditions(
                    controller.camera,
                    destination_dir=destination_dir,
                    sample_exposure_seconds=body.sample_exposure_seconds,
                    sample_iso=body.sample_iso,
                    focal_length_mm=resolved_focal_length,
                    aperture=resolved_aperture,
                )
            except RuntimeError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            finally:
                with contextlib.suppress(Exception):
                    controller.camera.disconnect()

        return WidefieldConditionEvaluationResponse(
            image_path=str(result.image_path),
            sample_exposure_seconds=result.sample_exposure_seconds,
            sample_iso=result.sample_iso,
            focal_length_mm=result.focal_length_mm,
            aperture=result.aperture,
            star_count=result.star_count,
            background_adu=result.background_adu,
            highlight_fraction=result.highlight_fraction,
            trailing_ceiling_seconds=result.trailing_ceiling_seconds,
            recommended_exposure_seconds=result.recommended_exposure_seconds,
            recommended_iso=result.recommended_iso,
            status=result.status,
            summary=result.summary,
            notes=result.notes,
            lens_model=lens_model,
        )

    @app.post("/api/v1/widefield/focus-assist", response_model=FocusAssistActionResponse)
    def post_focus_assist(body: FocusAssistRequestBody) -> FocusAssistActionResponse:
        if not hasattr(controller.camera, "read_focus_position_raw") or not hasattr(controller.camera, "set_focus_position_raw"):
            raise HTTPException(status_code=422, detail="active camera backend does not support Fuji focus assist")

        destination_dir = (
            Path(body.destination_dir)
            if body.destination_dir
            else controller.store.data_root / "focus-assist" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        )
        runner = FujiFocusAssistRunner(controller.camera)
        request = FocusAssistRequest(
            destination_dir=destination_dir,
            exposure_seconds=body.exposure_seconds,
            iso=body.iso,
            aperture=body.aperture,
            focus_min_raw=body.focus_min_raw,
            focus_max_raw=body.focus_max_raw,
            coarse_step=body.coarse_step,
            fine_step=body.fine_step,
            min_improvement_fraction=body.min_improvement_fraction,
        )

        with controller._camera_io_lock:
            try:
                controller.camera.connect()
                result = runner.run(request)
            except RuntimeError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            finally:
                with contextlib.suppress(Exception):
                    controller.camera.disconnect()

        return FocusAssistActionResponse(
            status=result.status,
            started_raw=result.started_raw,
            best_raw=result.best_raw,
            final_raw=result.final_raw,
            summary=result.summary,
            coarse_samples=[
                FocusAssistSampleResponse(
                    raw_position=sample.raw_position,
                    image_path=str(sample.image_path),
                    star_count=sample.star_count,
                    hfr_mean=sample.hfr_mean,
                    tenengrad=sample.tenengrad,
                    metric_source=sample.metric_source,
                    summary=sample.summary,
                )
                for sample in result.coarse_samples
            ],
            fine_samples=[
                FocusAssistSampleResponse(
                    raw_position=sample.raw_position,
                    image_path=str(sample.image_path),
                    star_count=sample.star_count,
                    hfr_mean=sample.hfr_mean,
                    tenengrad=sample.tenengrad,
                    metric_source=sample.metric_source,
                    summary=sample.summary,
                )
                for sample in result.fine_samples
            ],
        )

    # ------------------------------------------------------------------ #
    # GET /api/v1/session/current                                          #
    # ------------------------------------------------------------------ #

    @app.get("/api/v1/session/current", response_model=SessionSummaryResponse | None)
    def get_session_current() -> SessionSummaryResponse | None:
        """Full managed-session summary; null when no session is active."""
        session = controller.session
        if _is_pre_session_posture(session):
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
            workflow_intent=(session.workflow_intent.value if session.workflow_intent else None),
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
            terminal_outcome=(session.terminal_outcome.value if session.terminal_outcome else None),
        )

    # ------------------------------------------------------------------ #
    # GET /api/v1/session/current/state                                    #
    # ------------------------------------------------------------------ #

    @app.get("/api/v1/session/current/state", response_model=SessionStateResponse | None)
    def get_session_state() -> SessionStateResponse | None:
        """Lightweight polling view; null when no session is active."""
        session = controller.session
        if _is_pre_session_posture(session):
            return None

        blockers = controller.check_readiness()
        pause: dict[str, Any] | None = None
        if session.resume_context is not None:
            pause = {
                "pause_reason": session.resume_context.pause_reason,
                "resume_state": session.resume_context.resume_state,
                "operator_action_required": session.resume_context.operator_action_required,
            }

        # Supervisory fields (v1.1)
        intervention_summary: dict[str, Any] | None = None
        if session.intervention_ledger is not None:
            ledger = session.intervention_ledger
            active = ledger.active_record
            intervention_summary = {
                "active_kind": ledger.active_kind,
                "total_records": len(ledger.records),
                "active_record": {
                    "kind": active.kind,
                    "reason": active.reason,
                    "retry_count": active.retry_count,
                } if active else None,
            }

        try:
            canonical = controller.canonical_state()
            active_owner: str | None = canonical.active_owner.value if canonical.active_owner else None
        except Exception:
            active_owner = None

        return SessionStateResponse(
            session_id=session.session_id,
            state=session.state,
            workflow_intent=(session.workflow_intent.value if session.workflow_intent else None),
            control_locked=session.control_locked,
            latest_message=session.latest_message or f"state: {session.state}",
            blockers=[_to_blocker(b) for b in blockers],
            degraded=_get_degraded(controller),
            pause_summary=pause,
            supervisory_next_action=session.supervisory_next_action,
            active_owner=active_owner,
            intervention_summary=intervention_summary,
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
    # POST /api/v1/camera/recover                                         #
    # ------------------------------------------------------------------ #

    @app.post("/api/v1/camera/recover", response_model=ActionResponse)
    def post_camera_recover() -> ActionResponse:
        """Run the bounded manual camera recovery sequence."""
        try:
            result = controller.recover_camera_session()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _action_resp(controller, result.message, next_state=result.next_state)

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
    # GET /api/v1/planner-mode                                             #
    # ------------------------------------------------------------------ #

    @app.get("/api/v1/planner-mode", response_model=PlannerModeResponse)
    def get_planner_mode() -> PlannerModeResponse:
        """Return the active planner mode from the install manifest."""
        planner_mode: str | None = None
        try:
            manifest = controller.store.read_install_manifest()
            if manifest is not None:
                planner_mode = manifest.bootstrap_profile
        except Exception:
            pass
        return PlannerModeResponse(planner_mode=planner_mode)

    # ------------------------------------------------------------------ #
    # POST /api/v1/session/attach                                          #
    # ------------------------------------------------------------------ #

    @app.post("/api/v1/session/attach", response_model=ActionResponse)
    def post_session_attach() -> ActionResponse:
        """Attach supervision to an Ekos-managed session. READY → EKOS_WAIT.

        409 when not in ready state or a session is already active.
        422 when readiness blockers, missing equipment profile, or Ekos/broker
        unavailability prevent the attach.
        """
        session = controller.session
        if session.state != ClawState.READY:
            raise HTTPException(
                status_code=409,
                detail=f"attach is only valid from ready state (current: {session.state})",
            )
        try:
            result = controller.attach_session()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _action_resp(controller, result.message)

    # ------------------------------------------------------------------ #
    # GET /api/v1/session/current/intervention                             #
    # ------------------------------------------------------------------ #

    @app.get("/api/v1/session/current/intervention", response_model=InterventionStateResponse | None)
    def get_session_intervention() -> InterventionStateResponse | None:
        """Return the current intervention state for a supervised session.

        Returns null when no session is active or the session is not supervised.
        """

        session = controller.session
        if session.intervention_ledger is None:
            return None

        ledger = session.intervention_ledger
        active = ledger.active_record
        recent_records = [
            {
                "kind": r.kind,
                "reason": r.reason,
                "requested_at": r.requested_at.isoformat(),
                "outcome": r.outcome,
                "retry_count": r.retry_count,
                "acknowledged": r.acknowledged,
            }
            for r in ledger.records[-10:]
        ]
        iw = controller._intervention_window
        return InterventionStateResponse(
            active_kind=active.kind if active else None,
            active_reason=active.reason if active else None,
            active_since=active.requested_at if active else None,
            retry_count=active.retry_count if active else 0,
            recent_records=recent_records,
            intervention_window=iw.value if hasattr(iw, "value") else str(iw),
        )



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
            failure_explanation = (
                session.latest_message or "Session failed (no further detail available)"
            )

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

    # ------------------------------------------------------------------ #
    # Equipment profile endpoints                                          #
    # ------------------------------------------------------------------ #

    @app.get("/api/v1/equipment/profiles", response_model=EquipmentProfileListResponse)
    def get_equipment_profiles() -> EquipmentProfileListResponse:
        """List all stored equipment profiles."""
        profiles = controller.store.list_profiles()
        active_id = (
            controller.active_equipment_profile.profile_id
            if controller.active_equipment_profile
            else None
        )
        summaries = [
            EquipmentProfileSummary(
                profile_id=p.profile_id,
                display_name=p.display_name,
                is_default=p.is_default,
                hardware_summary={
                    "mount_model": p.hardware.mount.model,
                    "camera_make": p.hardware.camera.make,
                    "camera_model": p.hardware.camera.model,
                    "lens_model": p.hardware.lens.model,
                    "lens_is_zoom": p.hardware.lens.is_zoom,
                },
                updated_at=p.updated_at,
            )
            for p in profiles
        ]
        return EquipmentProfileListResponse(profiles=summaries, active_profile_id=active_id)

    @app.get(
        "/api/v1/equipment/profiles/{profile_id}",
        response_model=EquipmentProfileResponse,
    )
    def get_equipment_profile(profile_id: str) -> EquipmentProfileResponse:
        """Full equipment profile document.  404 when not found."""
        profile = controller.store.read_profile(profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail=f"Profile {profile_id!r} not found")
        active_id = (
            controller.active_equipment_profile.profile_id
            if controller.active_equipment_profile
            else None
        )
        return EquipmentProfileResponse(
            profile=profile.model_dump(mode="json"),
            is_active=(profile_id == active_id),
        )

    @app.post(
        "/api/v1/equipment/profiles", response_model=EquipmentProfileResponse, status_code=201
    )
    def post_equipment_profile(body: dict[str, Any]) -> EquipmentProfileResponse:
        """Create a new equipment profile.  409 on duplicate profile_id, 422 on validation."""
        from datetime import UTC, datetime

        from kepler_node.storage.models import EquipmentProfile

        if "profile_id" not in body or not body["profile_id"]:
            import re

            slug = re.sub(r"[^a-z0-9]+", "-", body.get("display_name", "profile").lower()).strip(
                "-"
            )
            body["profile_id"] = slug or "profile"

        if controller.store.read_profile(body["profile_id"]) is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Profile {body['profile_id']!r} already exists",
            )

        now = datetime.now(UTC)
        body.setdefault("created_at", now.isoformat())
        body.setdefault("updated_at", now.isoformat())

        try:
            profile = EquipmentProfile.model_validate(body)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        controller.store.write_profile(profile)
        return EquipmentProfileResponse(profile=profile.model_dump(mode="json"), is_active=False)

    @app.put(
        "/api/v1/equipment/profiles/{profile_id}",
        response_model=EquipmentProfileResponse,
    )
    def put_equipment_profile(profile_id: str, body: dict[str, Any]) -> EquipmentProfileResponse:
        """Replace an equipment profile.  404 when not found, 409 during active session."""
        from kepler_node.storage.models import EquipmentProfile

        existing = controller.store.read_profile(profile_id)
        if existing is None:
            raise HTTPException(status_code=404, detail=f"Profile {profile_id!r} not found")

        if "profile_id" in body and body["profile_id"] != profile_id:
            raise HTTPException(
                status_code=422,
                detail="profile_id in body must match path value",
            )

        # Block edit of active profile while session is in progress
        active_id = (
            controller.active_equipment_profile.profile_id
            if controller.active_equipment_profile
            else None
        )
        if profile_id == active_id and not _is_pre_session_posture(controller.session):
            raise HTTPException(
                status_code=409,
                detail="Cannot edit active profile while a managed session is in progress",
            )

        body["profile_id"] = profile_id
        from datetime import UTC, datetime

        body["updated_at"] = datetime.now(UTC).isoformat()
        # Preserve original created_at if not provided in body
        if "created_at" not in body:
            body["created_at"] = existing.created_at.isoformat()

        try:
            profile = EquipmentProfile.model_validate(body)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        controller.store.write_profile(profile)

        # If this is the active profile, refresh in-memory state and clear
        # stale calibration so readiness blockers reflect the new configuration
        # immediately (spec line 1631).
        if profile_id == active_id:
            controller.set_active_equipment_profile(profile, auto_focus_calibration=True)
            controller.session.calibration_accepted = False

        return EquipmentProfileResponse(
            profile=profile.model_dump(mode="json"),
            is_active=(profile_id == active_id),
        )

    @app.post(
        "/api/v1/equipment/profiles/{profile_id}/select",
        response_model=ActionResponse,
    )
    def post_equipment_profile_select(profile_id: str) -> ActionResponse:
        """Select a profile as active.  409 during active managed session, 404 when not found."""
        if not _is_pre_session_posture(controller.session):
            raise HTTPException(
                status_code=409,
                detail="Cannot change active profile while a managed session is in progress",
            )

        profile = controller.store.read_profile(profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail=f"Profile {profile_id!r} not found")

        auto_calibration = controller.set_active_equipment_profile(
            profile, auto_focus_calibration=True
        )
        message = f"Active profile set to {profile.display_name!r}"
        if auto_calibration is not None:
            message = f"{message}; {auto_calibration.message}"
        return _action_resp(controller, message)

    # ------------------------------------------------------------------ #
    # Target intake endpoints                                              #
    # ------------------------------------------------------------------ #

    @app.get("/api/v1/target/current", response_model=TargetCurrentResponse | None)
    def get_target_current() -> TargetCurrentResponse | None:
        """Return the staged target, or null when no target is staged."""
        session = controller.session
        if session.staged_target_ra_hours is None:
            return None
        return TargetCurrentResponse(
            target_label=controller._staged_target_label,
            ra_hours=session.staged_target_ra_hours,
            dec_deg=session.staged_target_dec_deg,
            target_source=controller._staged_target_source,
            run_parameters=controller._staged_run_parameters,
            active_equipment_profile_id=(
                controller.active_equipment_profile.profile_id
                if controller.active_equipment_profile
                else None
            ),
        )

    @app.post("/api/v1/target", response_model=TargetCurrentResponse)
    def post_target(body: TargetRequest) -> TargetCurrentResponse:
        """Stage a target for the next session.  Replaces any previously staged target.

        Not valid once active centering or capture has begun.
        """
        _active_centering_or_capture = {
            ClawState.TARGET_ACQUIRED,
            ClawState.TEST_CAPTURE,
            ClawState.SOLVE,
            ClawState.CORRECT,
            ClawState.CENTER_VERIFY,
            ClawState.CAPTURE,
            ClawState.GUARD,
            ClawState.RECOVER,
        }
        if controller.session.state in _active_centering_or_capture:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Cannot stage target while session is in state {controller.session.state!r}"
                ),
            )

        controller.stage_target_intake(
            target_label=body.target_label,
            ra_hours=body.ra_hours,
            dec_deg=body.dec_deg,
            target_source=body.target_source,
            run_parameters=body.run_parameters,
        )

        return TargetCurrentResponse(
            target_label=controller._staged_target_label,
            ra_hours=controller.session.staged_target_ra_hours,
            dec_deg=controller.session.staged_target_dec_deg,
            target_source=controller._staged_target_source,
            run_parameters=controller._staged_run_parameters,
            active_equipment_profile_id=(
                controller.active_equipment_profile.profile_id
                if controller.active_equipment_profile
                else None
            ),
        )

    @app.delete("/api/v1/target/current", response_model=ActionResponse)
    def delete_target_current() -> ActionResponse:
        """Clear the staged target.  409 during active centering/capture, 200 when none staged."""
        _active_centering_or_capture = {
            ClawState.TARGET_ACQUIRED,
            ClawState.TEST_CAPTURE,
            ClawState.SOLVE,
            ClawState.CORRECT,
            ClawState.CENTER_VERIFY,
            ClawState.CAPTURE,
            ClawState.GUARD,
            ClawState.RECOVER,
        }
        if controller.session.state in _active_centering_or_capture:
            raise HTTPException(
                status_code=409,
                detail="Cannot clear target during active centering or capture",
            )
        controller.clear_staged_target()
        return _action_resp(controller, "Staged target cleared")

    # ------------------------------------------------------------------ #
    # POST /api/v1/session/start                                           #
    # ------------------------------------------------------------------ #

    @app.post("/api/v1/session/start", response_model=ActionResponse)
    def post_session_start() -> ActionResponse:
        """Start a managed session from a staged target.

        Valid only from ``ready`` with staged target + run parameters, trusted
        time, and no readiness blockers.  409 for wrong state, 422 for
        readiness or run-parameter failures.
        """
        try:
            result = controller.start_session()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        return _action_resp(controller, result.message)

    return app
