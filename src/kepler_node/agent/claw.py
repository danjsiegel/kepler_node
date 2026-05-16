"""Kepler Claw state-machine controller for v1."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from kepler_node.agent.authorship import AuthorshipTracker
from kepler_node.agent.interfaces import (
    DeviceActivityEvent,
    DeviceActivityEventType,
    NodeManagementBackend,
    ReadinessCondition,
)
from kepler_node.agent.session import ClawState, RuntimeSession, WorkflowIntent
from kepler_node.camera.protocols import (
    CameraBackend,
    CameraSettings,
    CaptureRequest,
    ShutterPreference,
)
from kepler_node.imaging.protocols import SolveFailureCategory, SolverBackend
from kepler_node.mount.protocols import MountBackend, MountPosition
from kepler_node.storage.filesystem import FilesystemSessionStore
from kepler_node.storage.models import (
    EquipmentProfile,
    EventRecord,
    EventSeverity,
    EventType,
    SessionRecord,
    SessionScope,
)

# Reconnect backoff ladder (spec line 1125: ~5s, 15s, 30s per attempt)
_RECONNECT_BACKOFF_SECONDS = [5.0, 15.0, 30.0]


# Solve failure categories that are solver-specific (re-evaluate existing frame)
_SOLVER_SPECIFIC_FAILURES = {
    SolveFailureCategory.TIMEOUT,
    SolveFailureCategory.INDEX_MISSING_OR_NO_MATCH,
}

# Solve failure categories that indicate the frame itself is suspect (fresh frame needed)
_FRAME_SUSPECT_FAILURES = {
    SolveFailureCategory.NO_STARS_DETECTED,
    SolveFailureCategory.BAD_INPUT_FRAME,
}


class TransitionResult(BaseModel):
    """Outcome of a single state-machine transition."""

    previous_state: ClawState
    next_state: ClawState
    message: str = ""
    blockers: list[ReadinessCondition] = Field(default_factory=list)
    degraded: list[ReadinessCondition] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class ClawController:
    """Orchestrates the Kepler Claw state machine against live adapters.

    Each public method corresponds to a state-machine action that the outer
    driver (API layer, CLI, or test) calls.  The controller mutates the
    session in place, emits structured events to storage when a session_id
    is active, and returns a TransitionResult describing the outcome.
    """

    # v1 bounded retry policy (per spec lines 1123-1133)
    MAX_SOLVE_ATTEMPTS = 3
    MAX_CALIBRATION_LOOPS = 5
    MAX_CENTERING_LOOPS = 3
    MAX_RECONNECT_ATTEMPTS = 3
    MAX_CONSECUTIVE_BAD_FRAMES = 3

    # v1 centering tolerances (per spec lines 1038-1040)
    CALIBRATION_TOLERANCE_ARCMIN = 60.0  # 1.0 degree
    CENTERING_TOLERANCE_ARCMIN = 15.0
    RECOVERY_TOLERANCE_ARCMIN = 15.0

    # v1 settle delays in seconds (per spec lines 1029-1030)
    SETTLE_AFTER_SLEW_SECONDS = 5.0
    SETTLE_AFTER_CORRECTION_SECONDS = 2.5

    @staticmethod
    def _camera_operator_blocker(exc: Exception) -> ReadinessCondition | None:
        msg = str(exc)
        if "camera_autocapture_mode_blocking" in msg:
            return ReadinessCondition(
                name="camera_autocapture_mode_blocking",
                severity="blocking",
                summary=msg.split(": ", 1)[1] if ": " in msg else msg,
                operator_action_required=(
                    "Set Drive Mode to Single Shot (not Self-timer), keep USB tether mode enabled, and leave shutter/ISO/aperture in tether-compatible positions such as A or command control"
                ),
            )
        if "camera_remote_mode_required" in msg:
            return ReadinessCondition(
                name="camera_remote_mode_required",
                severity="blocking",
                summary=msg,
                operator_action_required=(
                    "Switch camera to USB remote-control mode and retry"
                ),
            )
        return None

    def __init__(
        self,
        *,
        session: RuntimeSession,
        node_backend: NodeManagementBackend,
        mount_backend: MountBackend,
        camera_backend: CameraBackend,
        solver_backend: SolverBackend,
        store: FilesystemSessionStore,
        authorship_tracker: AuthorshipTracker,
        verification_dir: Path,
        test_exposure_seconds: float = 5.0,
    ) -> None:
        self.session = session
        self.node = node_backend
        self.mount = mount_backend
        self.camera = camera_backend
        self.solver = solver_backend
        self.store = store
        self.authorship = authorship_tracker
        self.verification_dir = verification_dir
        self.test_exposure_seconds = test_exposure_seconds
        self._event_sequence: int = 0
        self._node_event_sequence: int = 0
        self._node_events: list[EventRecord] = []
        self._session_started_at: datetime = datetime.now(UTC)
        # Active equipment profile set by discover() or /equipment/profiles/{id}/select
        self.active_equipment_profile: EquipmentProfile | None = None
        # Staged target metadata (label and source tracked separately from session RA/Dec)
        self._staged_target_label: str | None = None
        self._staged_target_source: str | None = None
        self._staged_run_parameters: dict[str, Any] = {}

    @property
    def node_events(self) -> list[EventRecord]:
        """In-memory buffer of node-scoped pre-session diagnostic events."""
        return self._node_events

    # ------------------------------------------------------------------ #
    # Pre-session: boot -> discover -> connect -> ready                    #
    # ------------------------------------------------------------------ #

    def boot(self) -> TransitionResult:
        """Transition BOOT -> DISCOVER.

        Validates that the Claw process itself can start.  Moves to
        DISCOVER immediately unless the core environment is fundamentally
        broken.
        """
        prev = self.session.state
        self.session.state = ClawState.DISCOVER
        return self._make_transition(prev, ClawState.DISCOVER, "boot complete, entering discover")

    def discover(self) -> TransitionResult:
        """Inspect services and environment -> CONNECT or PAUSED.

        Checks service health and network mode.  Unhealthy services are
        surfaced as degraded conditions but do not block the transition to
        CONNECT unless the situation is fundamentally unsafe.
        """
        prev = self.session.state
        degraded: list[ReadinessCondition] = []

        # Service health check
        try:
            healths = self.node.service_health()
            for svc in healths:
                if not svc.healthy:
                    degraded.append(
                        ReadinessCondition(
                            name=f"service_unhealthy_{svc.name}",
                            severity="degraded",
                            summary=f"Service {svc.name!r} is not healthy: {svc.summary}",
                            operator_action_required=f"Check that {svc.name} is running",
                            details=svc.details,
                        )
                    )
        except Exception as exc:
            degraded.append(
                ReadinessCondition(
                    name="service_health_check_failed",
                    severity="degraded",
                    summary=f"Service health check failed: {exc}",
                )
            )

        # Auto-select the default equipment profile (spec lines 581-583)
        try:
            stored_profiles = self.store.list_profiles()
            if not stored_profiles:
                degraded.append(
                    ReadinessCondition(
                        name="no_default_equipment_profile",
                        severity="degraded",
                        summary=(
                            "No equipment profiles are configured; "
                            "target intake and session start will be unavailable"
                        ),
                        operator_action_required=(
                            "POST /api/v1/equipment/profiles to create a profile, "
                            "then POST /api/v1/equipment/profiles/{id}/select"
                        ),
                    )
                )
            else:
                defaults = [p for p in stored_profiles if p.is_default]
                if len(defaults) == 1:
                    self.active_equipment_profile = defaults[0]
                elif len(defaults) > 1:
                    degraded.append(
                        ReadinessCondition(
                            name="multiple_default_profiles",
                            severity="degraded",
                            summary=(
                                f"{len(defaults)} profiles are marked as default; "
                                "select one explicitly"
                            ),
                            operator_action_required=(
                                "POST /api/v1/equipment/profiles/{id}/select to choose the active profile"
                            ),
                        )
                    )
                else:
                    degraded.append(
                        ReadinessCondition(
                            name="no_default_equipment_profile",
                            severity="degraded",
                            summary=(
                                "No profile is marked as default; "
                                "target intake and session start will be unavailable"
                            ),
                            operator_action_required=(
                                "Mark a profile as default or POST /api/v1/equipment/profiles/{id}/select"
                            ),
                        )
                    )
        except Exception as exc:
            degraded.append(
                ReadinessCondition(
                    name="profile_load_failed",
                    severity="degraded",
                    summary=f"Could not load equipment profiles: {exc}",
                )
            )

        _profile_pause_conditions = {
            "multiple_default_profiles",
            "no_default_equipment_profile",
        }
        degraded_names = {c.name for c in degraded}
        if degraded_names & _profile_pause_conditions:
            self.session.state = ClawState.PAUSED
            result = self._make_transition(
                prev,
                ClawState.PAUSED,
                "discover paused: operator profile resolution required",
            )
        else:
            self.session.state = ClawState.CONNECT
            result = self._make_transition(
                prev,
                ClawState.CONNECT,
                "discover complete" if not degraded else "discover complete with degraded services",
            )
        result.degraded = degraded
        return result

    def connect(self) -> TransitionResult:
        """Connect mount and camera -> READY, PAUSED, or RECOVER.

        Attempts to connect both adapters.  Any blocking connect failure
        produces a ReadinessCondition and pauses the session.
        """
        prev = self.session.state
        blockers: list[ReadinessCondition] = []

        # Connect mount
        try:
            self.mount.connect()
        except Exception as exc:
            blockers.append(
                ReadinessCondition(
                    name="mount_connect_failed",
                    severity="blocking",
                    summary=f"Mount connection failed: {exc}",
                    operator_action_required="Check mount power and INDI server, then retry",
                )
            )

        # Connect camera
        try:
            self.camera.connect()
        except RuntimeError as exc:
            msg = str(exc)
            if "camera_autocapture_mode_blocking" in msg:
                blockers.append(
                    ReadinessCondition(
                        name="camera_autocapture_mode_blocking",
                        severity="blocking",
                        summary=msg,
                        operator_action_required=(
                            "Set Drive Dial to S (Single Shot), keep USB tether mode enabled, and replug USB if needed"
                        ),
                    )
                )
            elif "camera_remote_mode_required" in msg or "remote" in msg.lower():
                blockers.append(
                    ReadinessCondition(
                        name="camera_remote_mode_required",
                        severity="blocking",
                        summary=msg,
                        operator_action_required=(
                            "Switch camera to USB remote-control mode and retry connect"
                        ),
                    )
                )
            else:
                blockers.append(
                    ReadinessCondition(
                        name="camera_connect_failed",
                        severity="blocking",
                        summary=f"Camera connection failed: {exc}",
                        operator_action_required="Check camera USB connection and retry",
                    )
                )
        except Exception as exc:
            blockers.append(
                ReadinessCondition(
                    name="camera_connect_failed",
                    severity="blocking",
                    summary=f"Camera connection failed: {exc}",
                    operator_action_required="Check camera USB connection and retry",
                )
            )

        if blockers:
            self.session.pause(
                pause_reason="connect_blocked",
                resume_state=ClawState.CONNECT,
                workflow_intent=self.session.workflow_intent or WorkflowIntent.CALIBRATION,
                operator_action_required=blockers[0].operator_action_required,
            )
            self._emit_event(
                EventType.OPERATOR_ACTION_REQUIRED,
                f"connect blocked: {blockers[0].name}",
                EventSeverity.ERROR,
                details={"blockers": [b.name for b in blockers]},
            )
            result = self._make_transition(
                prev, ClawState.PAUSED, "connect blocked by adapter failure"
            )
            result.blockers = blockers
            return result

        # Successful connect: clear reconnect counter and go to READY
        self.session.reconnect_attempts = 0
        self.session.state = ClawState.READY
        return self._make_transition(prev, ClawState.READY, "all adapters connected")

    def check_readiness(self) -> list[ReadinessCondition]:
        """Return the current list of blocking readiness conditions.

        Checks time trust, storage, and power integrity.  This is called
        before calibrate or session start to surface operator-facing blockers.
        """
        blockers: list[ReadinessCondition] = []

        time_st = self.node.time_status()
        if not time_st.trusted:
            blockers.append(
                ReadinessCondition(
                    name="time_uncertain",
                    severity="blocking",
                    summary="Time is not trusted; cannot start calibration or capture",
                    operator_action_required="Confirm time or wait for NTP synchronization",
                    details={"time_source": time_st.source, "time_summary": time_st.summary},
                )
            )

        storage_st = self.node.storage_status()
        if "critically" in storage_st.summary or not storage_st.writable:
            blockers.append(
                ReadinessCondition(
                    name="storage_critically_low",
                    severity="blocking",
                    summary=storage_st.summary,
                    operator_action_required="Free disk space or verify storage mount before continuing",
                    details={"free_bytes": str(storage_st.free_bytes)},
                )
            )

        power_st = self.node.power_status()
        if not power_st.healthy:
            blockers.append(
                ReadinessCondition(
                    name="power_integrity_warning",
                    severity="blocking",
                    summary=power_st.summary,
                    operator_action_required="Check power supply and USB connections",
                    details={"undervoltage": str(power_st.undervoltage_detected)},
                )
            )

        diagnostic_status = getattr(self.camera, "diagnostic_status", None)
        if callable(diagnostic_status):
            try:
                camera_diag = diagnostic_status()
            except Exception as exc:
                blockers.append(
                    ReadinessCondition(
                        name="camera_diagnostic_failed",
                        severity="blocking",
                        summary=f"Camera diagnostic probe failed: {exc}",
                        operator_action_required="Check camera USB connection and retry",
                    )
                )
            else:
                if camera_diag and camera_diag.get("status") in {"card_reader_mode", "detected_unknown_mode"}:
                    blockers.append(
                        ReadinessCondition(
                            name="camera_remote_mode_required",
                            severity="blocking",
                            summary=camera_diag.get(
                                "summary",
                                "Camera is not in a supported USB remote-control mode",
                            ),
                            operator_action_required=(
                                "Switch camera to USB tether/remote-control mode and retry"
                            ),
                            details={"camera_status": str(camera_diag.get("status"))},
                        )
                    )
                if camera_diag and camera_diag.get("status") == "autocapture_mode":
                    blockers.append(
                        ReadinessCondition(
                            name="camera_autocapture_mode_blocking",
                            severity="blocking",
                            summary=camera_diag.get(
                                "summary",
                                "Camera is in a blocked self-timer/autocapture mode",
                            ),
                            operator_action_required=(
                                camera_diag.get(
                                    "operator_hint",
                                    "Set Drive Mode to Single Shot (not Self-timer), keep USB tether mode enabled, and leave shutter/ISO/aperture in tether-compatible positions such as A or command control",
                                )
                            ),
                            details={"camera_status": str(camera_diag.get("status"))},
                        )
                    )

        # Zoom-lens gate: refuse calibration/centering until focal length is trusted
        profile = self.active_equipment_profile
        if profile is not None and profile.hardware.lens.is_zoom:
            fl = (
                profile.solving_hints.focal_length_assumption_mm
                or profile.hardware.lens.default_focal_length_mm
            )
            if fl is None:
                blockers.append(
                    ReadinessCondition(
                        name="focal_length_assumption_required",
                        severity="blocking",
                        summary=(
                            "Zoom lens is active but no trusted focal-length assumption is set; "
                            "update the active equipment profile with a focal-length assumption"
                        ),
                        operator_action_required=(
                            "Set solving_hints.focal_length_assumption_mm or "
                            "hardware.lens.default_focal_length_mm in the active equipment profile"
                        ),
                    )
                )

        return blockers

    # ------------------------------------------------------------------ #
    # Target intake and session start                                      #
    # ------------------------------------------------------------------ #

    def stage_target_intake(
        self,
        *,
        target_label: str,
        ra_hours: float,
        dec_deg: float,
        target_source: str = "manual",
        run_parameters: dict[str, Any] | None = None,
    ) -> None:
        """Stage a target for session start.

        Stores coordinates and metadata on the controller without advancing
        the state machine.  Replaces any previously staged target.  Only
        valid before active centering or capture begins.
        """
        self._staged_target_label = target_label
        self._staged_target_source = target_source
        self._staged_run_parameters = run_parameters or {}
        self.session.staged_target_ra_hours = ra_hours
        self.session.staged_target_dec_deg = dec_deg
        self.session.staged_target_id = None

    def clear_staged_target(self) -> None:
        """Remove the currently staged target.  Safe when no session is active."""
        self._staged_target_label = None
        self._staged_target_source = None
        self._staged_run_parameters = {}
        self.session.staged_target_ra_hours = None
        self.session.staged_target_dec_deg = None
        self.session.staged_target_id = None

    def start_session(self) -> TransitionResult:
        """Create a managed session and enter target centering.

        Valid only from ``ready`` when a staged target and inline run
        parameters are present, time is trusted, and no readiness blockers
        exist.  On success, creates a ``session_id``, persists the initial
        ``SessionRecord``, copies the staged run parameters onto the session,
        and enters ``run_target_centering()``.

        Raises ``ValueError`` for wrong state (→ 409).
        Raises ``RuntimeError`` for validation or readiness failures (→ 422).
        """
        if self.session.state != ClawState.READY:
            raise ValueError(
                f"session/start is only valid from ready state (current: {self.session.state})"
            )
        if (
            self.session.staged_target_ra_hours is None
            or self.session.staged_target_dec_deg is None
        ):
            raise RuntimeError("session/start requires a staged target; POST /api/v1/target first")
        if not self._staged_run_parameters:
            raise RuntimeError(
                "session/start requires inline run parameters "
                "(exposure_seconds, camera_settings, stop_condition)"
            )
        required_run_param_keys = {"exposure_seconds", "camera_settings", "stop_condition"}
        missing = required_run_param_keys - set(self._staged_run_parameters.keys())
        if missing:
            raise RuntimeError(
                f"session/start run parameters missing required fields: {sorted(missing)}"
            )

        if self.active_equipment_profile is None:
            raise RuntimeError(
                "session/start requires an active equipment profile; "
                "POST /api/v1/equipment/profiles/{id}/select first"
            )

        blockers = self.check_readiness()
        if blockers:
            raise RuntimeError(f"session/start blocked: {blockers[0].name} — {blockers[0].summary}")

        time_st = self.node.time_status()
        if not time_st.trusted:
            raise RuntimeError("session/start requires trusted time; confirm time first")

        # Create session with canonical id format (spec line 1260)
        now = datetime.now(UTC)
        session_id = f"session-{now.strftime('%Y%m%dT%H%M%SZ')}-{os.urandom(3).hex()}"
        self.session.session_id = session_id
        self.session.run_parameters = dict(self._staged_run_parameters)
        self._session_started_at = now

        # Persist initial session record
        record = SessionRecord(
            session_id=session_id,
            started_at=now,
            updated_at=now,
            state=ClawState.TARGET_ACQUIRED,
            target_source=self._staged_target_source,
            target_label=self._staged_target_label,
            ra_hours=self.session.staged_target_ra_hours,
            dec_deg=self.session.staged_target_dec_deg,
            equipment_profile_id=self.active_equipment_profile.profile_id,
            operating_mode="managed",
            selected_inline_run_parameters=dict(self._staged_run_parameters),
        )
        self.store.write_session_record(record)

        return self.run_target_centering()

    # ------------------------------------------------------------------ #
    # Shared verification loop: calibrate / test_capture / solve /        #
    # center_verify / correct                                              #
    # ------------------------------------------------------------------ #

    def run_calibrate(self) -> TransitionResult:
        """Enter calibration -> run the shared verification loop.

        Gates on readiness blockers before starting.  On entry, resets
        verification counters and sets workflow_intent to CALIBRATION.
        """
        prev = self.session.state
        blockers = self.check_readiness()
        if blockers:
            self.session.pause(
                pause_reason=f"readiness_blockers: {blockers[0].name}",
                resume_state=ClawState.CALIBRATE,
                workflow_intent=WorkflowIntent.CALIBRATION,
                operator_action_required=blockers[0].operator_action_required,
            )
            self._emit_event(
                EventType.OPERATOR_ACTION_REQUIRED,
                f"calibrate blocked: {blockers[0].name}",
                EventSeverity.ERROR,
                details={"blockers": [b.name for b in blockers]},
            )
            result = self._make_transition(prev, ClawState.PAUSED, "calibrate blocked by readiness")
            result.blockers = blockers
            return result

        self.session.enter_calibrate()
        self.session.reset_verification_counters()
        self._make_transition(prev, ClawState.CALIBRATE, "entering calibration")

        return self.run_verification_loop()

    def run_target_centering(self) -> TransitionResult:
        """From TARGET_ACQUIRED run the centering verification loop.

        Expects staged_target_ra_hours and staged_target_dec_deg to already
        be set on the session.  Gates on calibration acceptance and readiness
        before starting the first centering verification frame (spec lines 802-805).
        """
        prev = self.session.state

        # Gate 1: calibration must be accepted; spec line 803 requires routing
        # back to CALIBRATE if calibration is still required.
        if not self.session.calibration_accepted:
            return self.run_calibrate()

        # Gate 2: readiness check before the first centering frame (spec line 805).
        blockers = self.check_readiness()
        if blockers:
            self.session.pause(
                pause_reason=f"readiness_blockers: {blockers[0].name}",
                resume_state=ClawState.TARGET_ACQUIRED,
                workflow_intent=WorkflowIntent.TARGET_CENTERING,
                operator_action_required=blockers[0].operator_action_required,
            )
            self._emit_event(
                EventType.OPERATOR_ACTION_REQUIRED,
                f"target centering blocked: {blockers[0].name}",
                EventSeverity.ERROR,
                details={"blockers": [b.name for b in blockers]},
            )
            result = self._make_transition(
                prev, ClawState.PAUSED, "target centering blocked by readiness"
            )
            result.blockers = blockers
            return result

        self.session.workflow_intent = WorkflowIntent.TARGET_CENTERING
        self.session.centering_loop_count = 0
        self.session.solve_attempts = 0
        self._make_transition(prev, ClawState.TARGET_ACQUIRED, "beginning target centering")
        return self.run_verification_loop()

    def run_verification_loop(self) -> TransitionResult:
        """Execute the test_capture -> solve -> center_verify -> correct loop.

        Loops until the session exits to a non-loop state:
        READY, TARGET_ACQUIRED, CAPTURE, PAUSED, FAILED, or RECOVER.
        The loop is driven by workflow_intent set on the session.
        """
        while True:
            tc = self._do_test_capture()
            if tc.next_state != ClawState.SOLVE:
                return tc

            sv = self._do_solve()
            if sv.next_state != ClawState.CENTER_VERIFY:
                return sv

            cv = self._do_center_verify()
            if cv.next_state == ClawState.CORRECT:
                cr = self._do_correct()
                if cr.next_state != ClawState.TEST_CAPTURE:
                    return cr
                # loop: correct returned TEST_CAPTURE, continue for next iteration
                continue

            return cv

    # ------------------------------------------------------------------ #
    # Capture, guard, recover                                              #
    # ------------------------------------------------------------------ #

    def capture_one_frame(self, *, request: CaptureRequest) -> TransitionResult:
        """Capture one production frame and transition to GUARD.

        Checks for external control conflicts before each exposure.
        On write or capture failure, applies a single bounded retry per
        the v1 storage write policy (spec lines 1130-1133).
        """
        prev = self.session.state

        # Conflict check before starting a new exposure
        conflict = self._check_conflicts()
        if conflict:
            return self._make_transition(
                prev, ClawState.PAUSED, "external control conflict detected"
            )

        self.session.state = ClawState.CAPTURE
        self._emit_event(EventType.STATE_TRANSITION, "capture: starting frame", EventSeverity.INFO)

        try:
            self.authorship.record(
                DeviceActivityEvent(
                    event_type=DeviceActivityEventType.CAPTURE_STARTED,
                    observed_at=datetime.now(UTC),
                    details={"frame_label": request.frame_label or "production"},
                )
            )
            result = self.camera.capture(request)
        except Exception as exc:
            blocker = self._camera_operator_blocker(exc)
            if blocker is not None:
                self.session.pause(
                    pause_reason=blocker.name,
                    resume_state=ClawState.CAPTURE,
                    workflow_intent=WorkflowIntent.CAPTURE,
                    operator_action_required=blocker.operator_action_required,
                )
                self._emit_event(
                    EventType.OPERATOR_ACTION_REQUIRED,
                    f"capture blocked: {blocker.name}",
                    EventSeverity.ERROR,
                    details={"blockers": [blocker.name], "error": str(exc)},
                )
                result_transition = self._make_transition(
                    prev,
                    ClawState.PAUSED,
                    f"capture blocked: {blocker.name}",
                )
                result_transition.blockers = [blocker]
                return result_transition

            # Single bounded write retry per spec lines 1130-1133.
            # Re-probe storage: if still writable and trusted, retry once on the
            # same canonical path.  Disk-full, permission, or unavailable storage
            # are terminal failures that skip the retry.
            storage_st = self.node.storage_status()
            storage_ok = storage_st.writable and "critically" not in storage_st.summary
            retry_exception: Exception | None = None
            if storage_ok:
                try:
                    result = self.camera.capture(request)
                except Exception as retry_exc:
                    retry_exception = retry_exc
            else:
                retry_exception = exc

            if retry_exception is not None:
                if not storage_ok:
                    # Hard-stop: storage is unavailable or unsafe (spec lines 1156-1163,
                    # 892-915).  This is a data-integrity failure, not an operator stop.
                    self._emit_event(
                        EventType.WARNING,
                        f"terminal write failure: storage unavailable after capture failure: {retry_exception}",
                        EventSeverity.ERROR,
                        details={
                            "failure_kind": "terminal_write_failure",
                            "error": str(retry_exception),
                        },
                    )
                    self.session.fail()
                    try:
                        self._persist_terminal_outcome(
                            "session failed: terminal storage failure during capture"
                        )
                    finally:
                        self._release_session_resources()
                    return self._make_transition(
                        prev,
                        ClawState.FAILED,
                        f"terminal write failure: {retry_exception}",
                        details={
                            "failure_kind": "terminal_write_failure",
                            "error": str(retry_exception),
                        },
                    )
                self.session.enter_recover()
                return self._make_transition(
                    ClawState.CAPTURE,
                    ClawState.RECOVER,
                    f"capture failed after write retry: {retry_exception}",
                    details={"failure_kind": "capture_error", "error": str(retry_exception)},
                )

        self.session.last_frame_path = str(result.image_path)
        self.session.state = ClawState.GUARD
        return self._make_transition(
            ClawState.CAPTURE,
            ClawState.GUARD,
            "frame captured, entering guard",
            details={"image_path": str(result.image_path)},
        )

    def evaluate_guard(
        self,
        *,
        quality_overall: str,
        quality_details: dict[str, Any] | None = None,
        frames_remaining: int | None = None,
    ) -> TransitionResult:
        """Evaluate session health after a frame and decide the next state.

        Implements the consecutive-bad-frame policy (spec lines 1126, 1348):
        - warn on the first bad frame
        - recover on the second
        - pause on the third if the issue persists
        Also checks for storage and power blocking conditions before
        allowing the next frame.
        """
        prev = self.session.state
        details: dict[str, Any] = {"quality": quality_overall}
        if quality_details:
            details["quality_details"] = quality_details

        self._emit_event(
            EventType.QUALITY_ASSESSMENT,
            f"guard: quality={quality_overall}",
            EventSeverity.INFO if quality_overall == "pass" else EventSeverity.WARNING,
            details=details,
        )

        # Session completion condition
        if frames_remaining is not None and frames_remaining <= 0:
            self.session.stop()
            self.session.terminal_outcome = None  # stop() sets STOPPED_BY_OPERATOR, override
            from kepler_node.agent.session import TerminalOutcome

            self.session.terminal_outcome = TerminalOutcome.COMPLETED
            self.session.state = ClawState.COMPLETED
            try:
                self._persist_terminal_outcome("capture run complete")
            finally:
                self._release_session_resources()
            return self._make_transition(prev, ClawState.COMPLETED, "capture run complete")

        # All blocking conditions (storage, time, power) prevent the next frame.
        # Spec line 860: guard exits to capture only when no blocking device,
        # storage, time, or power condition is present.
        blockers = self.check_readiness()
        if blockers:
            first = blockers[0]
            self.session.pause(
                pause_reason=first.name,
                resume_state=ClawState.CAPTURE,
                workflow_intent=WorkflowIntent.CAPTURE,
                operator_action_required=first.operator_action_required,
            )
            self._emit_event(
                EventType.OPERATOR_ACTION_REQUIRED,
                f"guard blocked before next frame: {first.name}",
                EventSeverity.ERROR,
                details={"blockers": [b.name for b in blockers]},
            )
            result = self._make_transition(prev, ClawState.PAUSED, f"guard blocked: {first.name}")
            result.blockers = blockers
            return result

        # Bad frame policy
        if quality_overall == "fail":
            self.session.consecutive_bad_frames += 1
            bad = self.session.consecutive_bad_frames

            if bad >= self.MAX_CONSECUTIVE_BAD_FRAMES:
                self.session.pause(
                    pause_reason=f"consecutive_bad_frames={bad}",
                    resume_state=ClawState.CAPTURE,
                    workflow_intent=WorkflowIntent.CAPTURE,
                    operator_action_required=(
                        "Review recent frames; check focus, tracking, and sky conditions"
                    ),
                )
                return self._make_transition(
                    prev,
                    ClawState.PAUSED,
                    f"paused after {bad} consecutive bad frames",
                )
            if bad >= 2:
                self.session.enter_recover()
                return self._make_transition(
                    prev,
                    ClawState.RECOVER,
                    f"recovering after {bad} consecutive bad frames",
                )

            # First bad frame: warn and continue
            self._emit_event(
                EventType.WARNING,
                f"bad frame #{bad}: continuing with caution",
                EventSeverity.WARNING,
                details=details,
            )

        elif quality_overall == "pass":
            self.session.consecutive_bad_frames = 0

        # Conflict check between frames
        conflict = self._check_conflicts()
        if conflict:
            return self._make_transition(
                prev, ClawState.PAUSED, "external control conflict detected between frames"
            )

        self.session.state = ClawState.CAPTURE
        return self._make_transition(prev, ClawState.CAPTURE, "guard passed, continuing capture")

    def recover(
        self,
        *,
        reason: str,
        failure_category: str | None = None,
        from_capture: bool = False,
        mount_disconnected: bool = False,
    ) -> TransitionResult:
        """Apply bounded recovery and return the target next state.

        Decision rules (spec lines 1135-1140):
        - solver-specific failures → re-evaluate in SOLVE (if attempt budget allows)
        - frame-suspect failures → fresh TEST_CAPTURE
        - mount disconnect → CONNECT then re-verify via test_capture path (not direct CAPTURE)
        - retries exhausted → PAUSED
        - unsafe condition → FAILED
        """
        prev = self.session.state
        self.session.enter_recover()
        self._emit_event(
            EventType.RECOVERY_ATTEMPT,
            f"recover: {reason}",
            EventSeverity.WARNING,
            details={"reason": reason, "failure_category": failure_category or ""},
        )

        # Mount disconnect: attempt reconnect with backoff (spec line 1125)
        if mount_disconnected:
            # Spec line 1052: mount reconnect during active centering or calibration
            # requires explicit operator confirmation rather than auto-reconnect.
            active_workflow = self.session.workflow_intent
            if active_workflow in (WorkflowIntent.CALIBRATION, WorkflowIntent.TARGET_CENTERING):
                self.session.pause(
                    pause_reason="mount_disconnected_during_active_workflow",
                    resume_state=ClawState.CONNECT,
                    workflow_intent=active_workflow,
                    operator_action_required=(
                        f"Mount disconnected during active {active_workflow.value}. "
                        "Check mount hardware and INDI server, then resume to reconnect "
                        "and restart the workflow."
                    ),
                )
                self._emit_event(
                    EventType.OPERATOR_ACTION_REQUIRED,
                    f"mount disconnected during {active_workflow.value}: operator confirmation required",
                    EventSeverity.ERROR,
                    details={"workflow_intent": active_workflow.value, "reason": reason},
                )
                return self._make_transition(
                    prev,
                    ClawState.PAUSED,
                    f"mount disconnected during {active_workflow.value}, operator confirmation required",
                )

            self.session.reconnect_attempts += 1
            if self.session.reconnect_attempts > self.MAX_RECONNECT_ATTEMPTS:
                self.session.pause(
                    pause_reason=f"mount_reconnect_exhausted attempts={self.session.reconnect_attempts}",
                    resume_state=ClawState.CONNECT,
                    workflow_intent=self.session.workflow_intent or WorkflowIntent.CALIBRATION,
                    operator_action_required="Check mount hardware and INDI server, then resume",
                )
                return self._make_transition(
                    prev, ClawState.PAUSED, "mount reconnect attempts exhausted"
                )

            # Backoff before reconnect attempt (spec line 1125: ~5s/15s/30s)
            attempt_index = self.session.reconnect_attempts - 1
            if attempt_index < len(_RECONNECT_BACKOFF_SECONDS):
                time.sleep(_RECONNECT_BACKOFF_SECONDS[attempt_index])

            # After reconnect, invalidate calibration and require re-verification
            self.session.calibration_accepted = False
            self.session.state = ClawState.CONNECT
            return self._make_transition(
                prev,
                ClawState.CONNECT,
                f"mount disconnected, attempting reconnect (attempt {self.session.reconnect_attempts})",
                details={"reconnect_attempt": self.session.reconnect_attempts},
            )

        # Solver-specific failure: re-evaluate existing frame if budget allows
        fcat = failure_category or ""
        is_solver_specific = fcat in {f.value for f in _SOLVER_SPECIFIC_FAILURES}
        is_frame_suspect = fcat in {f.value for f in _FRAME_SUSPECT_FAILURES}

        if (
            is_solver_specific
            and self.session.last_frame_path
            and self.session.solve_attempts < self.MAX_SOLVE_ATTEMPTS
        ):
            # Try blind solve on retry if hint-based already failed.
            # If recovering from active capture, keep the verification intent so
            # center_verify can route back to CAPTURE on success (not FAILED).
            if from_capture:
                self.session.workflow_intent = WorkflowIntent.RECOVERY_VERIFICATION
            self.session.state = ClawState.SOLVE
            return self._make_transition(
                prev,
                ClawState.SOLVE,
                f"re-evaluating existing frame after {failure_category}",
                details={"failure_category": fcat},
            )

        # Frame-suspect failure or capture error: take a fresh frame.
        # Same RECOVERY_VERIFICATION guard applies so the shared loop exits to
        # CAPTURE instead of failing when the intent is still CAPTURE.
        if is_frame_suspect or (not is_solver_specific):
            if from_capture:
                self.session.workflow_intent = WorkflowIntent.RECOVERY_VERIFICATION
            self.session.state = ClawState.TEST_CAPTURE
            return self._make_transition(
                prev,
                ClawState.TEST_CAPTURE,
                f"taking fresh verification frame after {failure_category or reason}",
                details={"failure_category": fcat},
            )

        # Fallback: fresh capture
        if from_capture:
            self.session.workflow_intent = WorkflowIntent.RECOVERY_VERIFICATION
        self.session.state = ClawState.TEST_CAPTURE
        return self._make_transition(
            prev, ClawState.TEST_CAPTURE, f"recover: fresh frame after {reason}"
        )

    def resume(self) -> TransitionResult:
        """Resume a paused session, returning it to its stored resume state.

        Valid only from PAUSED.  Reads resume_context, transitions to
        resume_state, restores workflow_intent, and clears resume_context.
        The session remains control_locked if it was before; this is not a
        release-control operation (use session.release_control() for that).
        """
        prev = self.session.state
        if self.session.state != ClawState.PAUSED:
            raise ValueError("resume is only valid from the PAUSED state")

        ctx = self.session.resume_context
        if ctx is None:
            self.session.fail()
            return self._make_transition(
                prev, ClawState.FAILED, "resume called with no resume_context"
            )

        resume_state = ctx.resume_state
        resume_intent = ctx.workflow_intent
        self.session.state = resume_state
        self.session.workflow_intent = resume_intent
        self.session.resume_context = None
        return self._make_transition(
            prev,
            resume_state,
            f"resumed from paused to {resume_state}",
            details={"pause_reason": ctx.pause_reason, "resume_state": resume_state},
        )

    def check_and_handle_conflicts(self) -> bool:
        """Check for external control conflicts and pause if one is found.

        Returns True if a conflict was detected and the session was paused.
        """
        return self._check_conflicts()

    # ------------------------------------------------------------------ #
    # Private loop steps                                                   #
    # ------------------------------------------------------------------ #

    def _do_test_capture(self) -> TransitionResult:
        """Capture a short verification frame.

        Returns a TransitionResult with next_state SOLVE on success, or
        RECOVER/PAUSED on failure.
        """
        prev = self.session.state
        self.session.state = ClawState.TEST_CAPTURE
        self._emit_event(EventType.STATE_TRANSITION, "test_capture: capturing", EventSeverity.INFO)

        self.verification_dir.mkdir(parents=True, exist_ok=True)
        request = CaptureRequest(
            exposure_seconds=self.test_exposure_seconds,
            settings=CameraSettings(iso=800),
            destination_dir=self.verification_dir,
            shutter_preference=ShutterPreference.ELECTRONIC_PREFERRED,
            frame_label="verification",
        )

        try:
            self.authorship.record(
                DeviceActivityEvent(
                    event_type=DeviceActivityEventType.CAPTURE_STARTED,
                    observed_at=datetime.now(UTC),
                    details={"frame_label": "verification"},
                )
            )
            capture_result = self.camera.capture(request)
            self.session.last_frame_path = str(capture_result.image_path)
            self.session.solve_attempts = 0  # reset for fresh frame
        except Exception as exc:
            blocker = self._camera_operator_blocker(exc)
            if blocker is not None:
                self.session.pause(
                    pause_reason=blocker.name,
                    resume_state=ClawState.TEST_CAPTURE,
                    workflow_intent=self.session.workflow_intent or WorkflowIntent.CALIBRATION,
                    operator_action_required=blocker.operator_action_required,
                )
                result = self._make_transition(
                    prev,
                    ClawState.PAUSED,
                    f"test capture blocked: {blocker.name}",
                )
                result.blockers = [blocker]
                return result
            self.session.enter_recover()
            return self._make_transition(
                prev,
                ClawState.RECOVER,
                f"test capture failed: {exc}",
                details={"failure_kind": "capture_error", "error": str(exc)},
            )

        return TransitionResult(
            previous_state=ClawState.TEST_CAPTURE,
            next_state=ClawState.SOLVE,
            message="test capture complete",
        )

    def _do_solve(self) -> TransitionResult:
        """Solve the latest verification frame.

        Enforces per-frame solve attempt budget (MAX_SOLVE_ATTEMPTS).
        Uses blind solve on the second attempt when the first failure looks
        hint-related (per spec line 312).
        """
        prev = self.session.state
        self.session.state = ClawState.SOLVE
        self._emit_event(EventType.STATE_TRANSITION, "solve: solving frame", EventSeverity.INFO)

        if not self.session.last_frame_path:
            self.session.enter_recover()
            return self._make_transition(prev, ClawState.RECOVER, "solve: no frame available")

        self.session.solve_attempts += 1

        # Blind solve is justified after one hint-related failure
        last_cat = self.session.last_solve_failure_category or ""
        use_blind = self.session.solve_attempts > 1 and last_cat in {
            f.value for f in _SOLVER_SPECIFIC_FAILURES
        }

        solve_result = self.solver.solve(
            Path(self.session.last_frame_path),
            expected_ra_hours=self.session.staged_target_ra_hours,
            expected_dec_deg=self.session.staged_target_dec_deg,
            blind=use_blind,
        )
        self.session.last_solve_failure_category = (
            solve_result.failure_category.value if solve_result.failure_category else None
        )

        if not solve_result.success:
            fcat = (
                solve_result.failure_category.value if solve_result.failure_category else "unknown"
            )
            if self.session.solve_attempts >= self.MAX_SOLVE_ATTEMPTS:
                intent = self.session.workflow_intent or WorkflowIntent.CALIBRATION
                self.session.pause(
                    pause_reason=f"solve_retries_exhausted: {fcat}",
                    resume_state=ClawState.TEST_CAPTURE,
                    workflow_intent=intent,
                    operator_action_required=(
                        "Check sky conditions, equipment alignment, and solver index coverage"
                    ),
                )
                self._emit_event(
                    EventType.OPERATOR_ACTION_REQUIRED,
                    f"solve retries exhausted after {self.session.solve_attempts} attempts",
                    EventSeverity.ERROR,
                    details={"failure_category": fcat, "attempts": self.session.solve_attempts},
                )
                return self._make_transition(
                    prev, ClawState.PAUSED, f"solve retries exhausted: {fcat}"
                )

            self.session.enter_recover()
            return self._make_transition(
                prev,
                ClawState.RECOVER,
                f"solve failed: {fcat}",
                details={"failure_category": fcat, "attempts": self.session.solve_attempts},
            )

        # Success: record solve result
        self.session.last_solve_ra_hours = solve_result.solved_ra_hours
        self.session.last_solve_dec_deg = solve_result.solved_dec_deg
        self.session.last_residual_arcmin = solve_result.residual_arcmin
        self.session.last_solve_failure_category = None
        self.session.solve_attempts = 0

        return TransitionResult(
            previous_state=ClawState.SOLVE,
            next_state=ClawState.CENTER_VERIFY,
            message="solve succeeded",
            details={
                "ra_hours": solve_result.solved_ra_hours,
                "dec_deg": solve_result.solved_dec_deg,
                "residual_arcmin": solve_result.residual_arcmin,
            },
        )

    def _do_center_verify(self) -> TransitionResult:
        """Evaluate the solve residual against the tolerance for current intent.

        Exits to CORRECT (more correction needed), or the appropriate
        completion state (READY, TARGET_ACQUIRED, CAPTURE), or PAUSED when
        retries are exhausted.
        """
        prev = self.session.state
        self.session.state = ClawState.CENTER_VERIFY
        self._emit_event(
            EventType.STATE_TRANSITION, "center_verify: evaluating residual", EventSeverity.INFO
        )

        intent = self.session.workflow_intent
        residual = self.session.last_residual_arcmin or 0.0

        # Validate intent (spec line 1041: capture intent must not enter center_verify)
        if intent == WorkflowIntent.CAPTURE:
            self.session.fail()
            return self._make_transition(
                prev,
                ClawState.FAILED,
                "center_verify entered with CAPTURE intent; this is a programming error",
            )

        if intent == WorkflowIntent.CALIBRATION:
            tolerance = self.CALIBRATION_TOLERANCE_ARCMIN
            max_loops = self.MAX_CALIBRATION_LOOPS
            loop_count = self.session.calibration_loop_count
        else:
            # TARGET_CENTERING or RECOVERY_VERIFICATION
            tolerance = (
                self.CENTERING_TOLERANCE_ARCMIN
                if intent == WorkflowIntent.TARGET_CENTERING
                else self.RECOVERY_TOLERANCE_ARCMIN
            )
            max_loops = self.MAX_CENTERING_LOOPS
            loop_count = self.session.centering_loop_count

        details = {
            "residual_arcmin": residual,
            "tolerance_arcmin": tolerance,
            "loop_count": loop_count,
            "intent": intent,
        }

        if residual <= tolerance:
            return self._handle_verify_success(prev, intent, details)

        if loop_count >= max_loops:
            resume_target = (
                ClawState.CALIBRATE
                if intent == WorkflowIntent.CALIBRATION
                else ClawState.TARGET_ACQUIRED
            )
            self.session.pause(
                pause_reason=(
                    f"centering_retries_exhausted loops={loop_count} residual={residual:.1f}arcmin"
                ),
                resume_state=resume_target,
                workflow_intent=intent,
                operator_action_required=(
                    "Check mount alignment, pointing model, and sky conditions before retrying"
                ),
            )
            self._emit_event(
                EventType.OPERATOR_ACTION_REQUIRED,
                f"centering retries exhausted after {loop_count} loops",
                EventSeverity.ERROR,
                details=details,
            )
            return self._make_transition(
                prev, ClawState.PAUSED, f"centering retries exhausted after {loop_count} loops"
            )

        # Correction needed
        return TransitionResult(
            previous_state=ClawState.CENTER_VERIFY,
            next_state=ClawState.CORRECT,
            message=f"residual {residual:.1f} arcmin exceeds tolerance {tolerance:.1f}, correcting",
            details=details,
        )

    def _handle_verify_success(
        self, prev: ClawState, intent: WorkflowIntent, details: dict[str, Any]
    ) -> TransitionResult:
        """Transition to the correct completion state after verify success."""
        if intent == WorkflowIntent.CALIBRATION:
            self.session.accept_calibration()
            self._emit_event(
                EventType.STATE_TRANSITION,
                "calibration accepted",
                EventSeverity.INFO,
                details=details,
            )
            if self.session.staged_target_ra_hours is not None:
                self.session.enter_target_acquired()
                return self._make_transition(
                    prev,
                    ClawState.TARGET_ACQUIRED,
                    "calibration accepted, advancing to target centering",
                    details=details,
                )
            self.session.state = ClawState.READY
            return self._make_transition(
                prev, ClawState.READY, "calibration accepted, returning to ready", details=details
            )

        if intent == WorkflowIntent.TARGET_CENTERING:
            self.session.enter_capture()
            return self._make_transition(
                prev, ClawState.CAPTURE, "target centered, starting capture", details=details
            )

        if intent == WorkflowIntent.RECOVERY_VERIFICATION:
            self.session.state = ClawState.CAPTURE
            self.session.workflow_intent = WorkflowIntent.CAPTURE
            return self._make_transition(
                prev,
                ClawState.CAPTURE,
                "recovery verified, resuming capture",
                details=details,
            )

        self.session.fail()
        return self._make_transition(
            prev, ClawState.FAILED, "unexpected workflow_intent in center_verify"
        )

    def _do_correct(self) -> TransitionResult:
        """Apply one corrective action appropriate to the current workflow_intent.

        Calibration: sync to solved position.
        Target centering / recovery: physical slew to staged target.
        Returns TEST_CAPTURE on success, or RECOVER / PAUSED on failure.
        """
        prev = self.session.state
        self.session.state = ClawState.CORRECT
        self._emit_event(
            EventType.CORRECTIVE_ACTION, "correct: applying correction", EventSeverity.INFO
        )

        intent = self.session.workflow_intent

        if intent == WorkflowIntent.CALIBRATION:
            self.session.calibration_loop_count += 1
            target_ra = self.session.last_solve_ra_hours
            target_dec = self.session.last_solve_dec_deg
        else:
            self.session.centering_loop_count += 1
            target_ra = self.session.staged_target_ra_hours
            target_dec = self.session.staged_target_dec_deg

        if target_ra is None or target_dec is None:
            self.session.pause(
                pause_reason="correct_no_target_position",
                resume_state=ClawState.CALIBRATE
                if intent == WorkflowIntent.CALIBRATION
                else ClawState.TARGET_ACQUIRED,
                workflow_intent=intent or WorkflowIntent.CALIBRATION,
                operator_action_required="No target position available for correction",
            )
            return self._make_transition(
                prev, ClawState.PAUSED, "no target position for correction"
            )

        position = MountPosition(ra_hours=target_ra, dec_deg=target_dec)

        try:
            if intent == WorkflowIntent.CALIBRATION:
                self.authorship.record(
                    DeviceActivityEvent(
                        event_type=DeviceActivityEventType.MOUNT_SYNC_APPLIED,
                        observed_at=datetime.now(UTC),
                        details={
                            "ra_hours": str(target_ra),
                            "dec_deg": str(target_dec),
                            "action": "sync",
                        },
                    )
                )
                self.mount.sync_to(position)
                settle = self.SETTLE_AFTER_CORRECTION_SECONDS
            else:
                self.authorship.record(
                    DeviceActivityEvent(
                        event_type=DeviceActivityEventType.MOUNT_SLEW_STARTED,
                        observed_at=datetime.now(UTC),
                        details={
                            "ra_hours": str(target_ra),
                            "dec_deg": str(target_dec),
                            "action": "slew",
                        },
                    )
                )
                self.mount.slew_to(position)
                settle = self.SETTLE_AFTER_SLEW_SECONDS
        except Exception as exc:
            self.session.enter_recover()
            return self._make_transition(
                prev,
                ClawState.RECOVER,
                f"correction command failed: {exc}",
                details={"error": str(exc)},
            )

        self._emit_event(
            EventType.CORRECTIVE_ACTION,
            f"correction applied ({intent.value}), settling {settle}s",
            EventSeverity.INFO,
            details={"intent": intent.value, "ra_hours": target_ra, "dec_deg": target_dec},
        )
        time.sleep(settle)

        return TransitionResult(
            previous_state=ClawState.CORRECT,
            next_state=ClawState.TEST_CAPTURE,
            message="correction applied, returning to test_capture",
        )

    # ------------------------------------------------------------------ #
    # Conflict detection                                                   #
    # ------------------------------------------------------------------ #

    def _check_conflicts(self) -> bool:
        """Check mount and camera activity for external control conflicts.

        If a conflict is detected and control_locked is True, pauses the
        session and emits an EXTERNAL_CONTROL_CONFLICT event.
        Returns True if a conflict was found and the session is now paused.
        """
        if not self.session.control_locked:
            return False

        for event in self.mount.activity_events():
            if self.authorship.is_conflict(event, control_locked=True):
                self._pause_on_conflict("mount", str(event.event_type))
                return True

        for event in self.camera.activity_events():
            if self.authorship.is_conflict(event, control_locked=True):
                self._pause_on_conflict("camera", str(event.event_type))
                return True

        return False

    def _pause_on_conflict(self, device: str, event_type: str) -> None:
        prev = self.session.state
        intent = self.session.workflow_intent or WorkflowIntent.CAPTURE

        # Choose a resume state that forms a consistent, safe state/intent pair.
        # Spec line 742: safe resume targets are ready, target_acquired, test_capture,
        # and capture.  Never pair target_acquired with a capture-phase intent.
        _SAFE_RESUME_STATES = {
            ClawState.READY,
            ClawState.TARGET_ACQUIRED,
            ClawState.TEST_CAPTURE,
            ClawState.CAPTURE,
        }
        if prev in _SAFE_RESUME_STATES:
            resume_state = prev
        elif intent in (WorkflowIntent.CAPTURE, WorkflowIntent.RECOVERY_VERIFICATION):
            resume_state = ClawState.CAPTURE
        else:
            resume_state = ClawState.TARGET_ACQUIRED

        self.session.pause(
            pause_reason="external_control_conflict",
            resume_state=resume_state,
            workflow_intent=intent,
            operator_action_required=(
                f"External {device} activity detected ({event_type}). "
                "Confirm Kepler should resume or release control."
            ),
        )
        self._emit_event(
            EventType.EXTERNAL_CONTROL_CONFLICT,
            f"external control conflict on {device}: {event_type}",
            EventSeverity.ERROR,
            details={"device": device, "event_type": event_type},
        )
        self._make_transition(prev, ClawState.PAUSED, "external control conflict")

    # ------------------------------------------------------------------ #
    # Event emission and transition helpers                                #
    # ------------------------------------------------------------------ #

    def stop(self) -> TransitionResult:
        """Operator-requested session stop.  Emits SESSION_OUTCOME and persists."""
        prev = self.session.state
        self.session.stop()
        try:
            self._persist_terminal_outcome("session stopped by operator")
        finally:
            self._release_session_resources()
        return self._make_transition(prev, ClawState.COMPLETED, "session stopped by operator")

    def pause(self) -> TransitionResult:
        """Pause the active managed session through the canonical transition path."""
        if self.session.state == ClawState.PAUSED:
            return TransitionResult(
                previous_state=ClawState.PAUSED,
                next_state=ClawState.PAUSED,
                message="session already paused",
                details={
                    "workflow_intent": (
                        self.session.workflow_intent.value if self.session.workflow_intent else None
                    ),
                    "control_locked": self.session.control_locked,
                },
            )
        if self.session.state in {
            ClawState.BOOT,
            ClawState.DISCOVER,
            ClawState.CONNECT,
            ClawState.READY,
        }:
            raise ValueError("No active managed session to pause")
        if self.session.is_terminal:
            raise ValueError("Cannot pause a terminal session")

        workflow_intent = self.session.workflow_intent or WorkflowIntent.CALIBRATION
        prev = self.session.state
        self.session.pause(
            pause_reason="operator pause request",
            resume_state=prev,
            workflow_intent=workflow_intent,
        )
        return self._make_transition(prev, ClawState.PAUSED, "session paused by operator")

    def fail(self, *, reason: str = "unrecoverable failure") -> TransitionResult:
        """Transition the session to FAILED.  Emits SESSION_OUTCOME and persists."""
        prev = self.session.state
        self.session.fail()
        try:
            self._persist_terminal_outcome(f"session failed: {reason}")
        finally:
            self._release_session_resources()
        return self._make_transition(prev, ClawState.FAILED, f"session failed: {reason}")

    def release_control(self) -> TransitionResult:
        """Release operator control from PAUSED.  Emits SESSION_OUTCOME and persists."""
        prev = self.session.state
        self.session.release_control()
        try:
            self._persist_terminal_outcome("control released by operator")
        finally:
            self._release_session_resources()
        return self._make_transition(prev, ClawState.COMPLETED, "control released by operator")

    def acknowledge_complete(self) -> TransitionResult:
        """Acknowledge a completed session and return the node to ready.

        Valid only from COMPLETED.  Emits a STATE_TRANSITION event and
        resets the in-memory session so the node can accept a new workflow.

        The transition event is emitted *before* session_id is cleared so
        that it is written to the session's events.ndjson rather than the
        node-level buffer (spec lines 911-916).
        """
        if self.session.state != ClawState.COMPLETED:
            raise ValueError("acknowledge_complete is only valid from the completed state")
        prev = self.session.state
        # Emit while session_id is still set, then clear in-memory state.
        result = self._make_transition(
            prev, ClawState.READY, "completed session acknowledged; node returned to ready"
        )
        self.session.acknowledge_complete()
        return result

    def begin_calibrate(self) -> TransitionResult:
        """Transition to CALIBRATE state without running the full verification loop.

        Used by the API endpoint so the caller can trigger calibration as a fast
        state-change action rather than a synchronous long-running operation.

        Raises RuntimeError when readiness blockers exist (so the API can map to 422).
        Valid only from READY or TARGET_ACQUIRED.
        """
        valid_states = {ClawState.READY, ClawState.TARGET_ACQUIRED}
        if self.session.state not in valid_states:
            raise ValueError(
                f"begin_calibrate is only valid from ready or target_acquired, "
                f"current state: {self.session.state}"
            )
        blockers = self.check_readiness()
        if blockers:
            raise RuntimeError(f"calibrate blocked: {blockers[0].name}")
        prev = self.session.state
        self.session.enter_calibrate()
        self.session.reset_verification_counters()
        return self._make_transition(prev, ClawState.CALIBRATE, "entering calibration")

    def clear_failure(self) -> TransitionResult:
        """Clear a failed session after operator review and return the node to ready.

        Valid only from FAILED.  Fails with ValueError when active blocking
        conditions (storage_critically_low, power_integrity_warning) still
        require the node to remain in the failed state so the API layer can
        map those to 422.

        The transition event is emitted *before* session_id is cleared so
        that it is written to the session's events.ndjson rather than the
        node-level buffer (spec lines 912-916).
        """
        if self.session.state != ClawState.FAILED:
            raise ValueError("clear_failure is only valid from the failed state")
        blockers = self.check_readiness()
        hardware_blocks = [
            b for b in blockers if b.name in {"storage_critically_low", "power_integrity_warning"}
        ]
        if hardware_blocks:
            names = ", ".join(b.name for b in hardware_blocks)
            raise RuntimeError(
                f"cannot clear failure while active hardware conditions require failed: {names}"
            )
        prev = self.session.state
        # Emit while session_id is still set, then clear in-memory state.
        result = self._make_transition(
            prev, ClawState.READY, "failed session cleared; node returned to ready"
        )
        self.session.clear_failure()
        return result

    def _release_session_resources(self) -> None:
        """Release camera-session resources and clean up temporary verification artifacts.

        Called on all terminal paths.  Each step is best-effort so resource
        errors do not block each other or the terminal transition (spec lines
        901-906: release camera-session resources; clear temp verification
        artifacts that should not remain live).
        """
        try:
            self.camera.disconnect()
        except Exception:
            pass

        if self.verification_dir.exists():
            for artifact in self.verification_dir.iterdir():
                try:
                    artifact.unlink(missing_ok=True)
                except Exception:
                    pass

    def _persist_terminal_outcome(self, message: str) -> None:
        """Emit a SESSION_OUTCOME event and update session.json at terminal transitions.

        Loads the existing session.json when present so that all previously
        written metadata (target label, run parameters, equipment profile, etc.)
        is preserved.  Only ``state``, ``updated_at``, and ``terminal_outcome``
        are updated.  When no session.json exists yet, builds the record from
        the current session state so no metadata is lost.
        """
        outcome = self.session.terminal_outcome
        self._emit_event(
            EventType.SESSION_OUTCOME,
            message,
            EventSeverity.INFO,
            details={"terminal_outcome": outcome.value if outcome else "unknown"},
        )
        if not self.session.session_id:
            return
        existing = self.store.read_session_record(self.session.session_id)
        if existing is not None:
            record = existing.model_copy(
                update={
                    "updated_at": datetime.now(UTC),
                    "state": self.session.state,
                    "terminal_outcome": outcome,
                }
            )
        else:
            record = SessionRecord(
                session_id=self.session.session_id,
                started_at=self._session_started_at,
                updated_at=datetime.now(UTC),
                state=self.session.state,
                terminal_outcome=outcome,
                target_label=self.session.staged_target_id,
                ra_hours=self.session.staged_target_ra_hours,
                dec_deg=self.session.staged_target_dec_deg,
                selected_inline_run_parameters=self.session.run_parameters,
                calibration_summary={
                    "calibration_accepted": self.session.calibration_accepted,
                    "calibration_loop_count": self.session.calibration_loop_count,
                },
            )
        self.store.write_session_record(record)

    def _make_transition(
        self,
        prev: ClawState,
        next_state: ClawState,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> TransitionResult:
        """Emit a STATE_TRANSITION event and return a TransitionResult.

        Always injects ``workflow_intent`` and ``control_locked`` from the
        current session into ``details`` so API/UI consumers can build
        state-changing responses (spec line 1594) without re-reading
        controller state.  Caller-supplied keys take priority.
        """
        runtime_ctx: dict[str, Any] = {
            "workflow_intent": (
                self.session.workflow_intent.value if self.session.workflow_intent else None
            ),
            "control_locked": self.session.control_locked,
        }
        merged = {**runtime_ctx, **(details or {})}
        self._emit_event(
            EventType.STATE_TRANSITION,
            message,
            EventSeverity.INFO,
            details=merged,
            prev_state=prev,
        )
        self.session.latest_message = message
        return TransitionResult(
            previous_state=prev,
            next_state=next_state,
            message=message,
            details=merged,
        )

    def _emit_state_event(self, state: ClawState, message: str) -> None:
        self._emit_event(EventType.STATE_TRANSITION, message, EventSeverity.INFO)

    def _emit_event(
        self,
        event_type: EventType,
        message: str,
        severity: EventSeverity,
        *,
        details: dict[str, Any] | None = None,
        prev_state: ClawState | None = None,
    ) -> None:
        """Append a structured event to storage (session) or node buffer (pre-session).

        When no session_id is active, events are collected in the in-memory
        ``node_events`` buffer with ``session_scope=node`` so the operator
        can observe pre-session calibration progress (spec lines 212, 1276-1277).
        """
        if not self.session.session_id:
            self._node_event_sequence += 1
            record = EventRecord(
                timestamp=datetime.now(UTC),
                session_scope=SessionScope.NODE,
                session_id=None,
                sequence=self._node_event_sequence,
                event_type=event_type,
                state=self.session.state,
                severity=severity,
                message=message,
                details=details or {},
            )
            self._node_events.append(record)
            return

        self._event_sequence += 1
        record = EventRecord(
            timestamp=datetime.now(UTC),
            session_scope=SessionScope.SESSION,
            session_id=self.session.session_id,
            sequence=self._event_sequence,
            event_type=event_type,
            state=self.session.state,
            severity=severity,
            message=message,
            details=details or {},
        )

        try:
            self.store.append_event(self.session.session_id, record)
        except FileNotFoundError:
            # Session directory not found — lazily initialize it so no event
            # is silently dropped once a managed session_id is active.
            self._ensure_session_directory()
            self.store.append_event(self.session.session_id, record)

    def _ensure_session_directory(self) -> None:
        """Create the session directory if it does not yet exist.

        Called lazily from ``_emit_event()`` the first time an event cannot be
        appended because the session directory is missing.  Writes a minimal
        ``session.json`` so that subsequent ``append_event()`` calls succeed.
        Any failure here propagates to the caller — silent drops are not
        acceptable once a managed session_id is active.
        """
        if not self.session.session_id:
            return
        init_record = SessionRecord(
            session_id=self.session.session_id,
            started_at=self._session_started_at,
            updated_at=datetime.now(UTC),
            state=self.session.state,
            target_label=self.session.staged_target_id,
            ra_hours=self.session.staged_target_ra_hours,
            dec_deg=self.session.staged_target_dec_deg,
        )
        self.store.write_session_record(init_record)
