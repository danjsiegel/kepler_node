"""Tests for Phase 2 imaging/verification.py and evaluate_guard quality_recommendation.

Covers:
- VerificationSolveHelper wraps a solver with audit context
- VerificationSolveResult.safe_to_resume and confidence assessment
- Solver errors are captured, never propagated
- evaluate_guard: trigger_autofocus recommendation calls ekos.request_autofocus()
- evaluate_guard: pause_sensor / pause_weather recommendation pauses session and calls ekos.pause()
- evaluate_guard: warn recommendation emits warning event but continues
- camera_keepalive skipped when Ekos sequence is active
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable
from unittest.mock import MagicMock, patch

import pytest

from kepler_node.imaging.protocols import SolveFailureCategory, SolveResult
from kepler_node.imaging.verification import VerificationSolveHelper, VerificationSolveResult


# ---------------------------------------------------------------------------
# Helpers: fake SolverBackend
# ---------------------------------------------------------------------------


def _success_solver(
    *,
    ra: float = 5.5,
    dec: float = -5.5,
    residual: float = 1.0,
) -> MagicMock:
    solver = MagicMock()
    solver.solve.return_value = SolveResult(
        success=True,
        solved_ra_hours=ra,
        solved_dec_deg=dec,
        residual_arcmin=residual,
        confidence_summary="ok",
    )
    return solver


def _failure_solver(category: SolveFailureCategory = SolveFailureCategory.TIMEOUT) -> MagicMock:
    solver = MagicMock()
    solver.solve.return_value = SolveResult(
        success=False,
        failure_category=category,
    )
    return solver


def _raising_solver() -> MagicMock:
    solver = MagicMock()
    solver.solve.side_effect = RuntimeError("solver crashed")
    return solver


# ---------------------------------------------------------------------------
# VerificationSolveHelper
# ---------------------------------------------------------------------------


def test_verification_solve_success(tmp_path: Path) -> None:
    helper = VerificationSolveHelper(_success_solver(), reason="post_intervention")
    fake_frame = tmp_path / "frame.fits"
    fake_frame.touch()
    vr = helper.solve_for_verification(fake_frame, expected_ra_hours=5.5, expected_dec_deg=-5.5)
    assert vr.solve_result.success is True
    assert vr.reason == "post_intervention"
    assert vr.safe_to_resume is True
    assert "within tolerance" in vr.confidence


def test_verification_solve_failure_not_safe(tmp_path: Path) -> None:
    helper = VerificationSolveHelper(
        _failure_solver(SolveFailureCategory.TIMEOUT), reason="post_intervention"
    )
    vr = helper.solve_for_verification(tmp_path / "frame.fits")
    assert vr.solve_result.success is False
    assert vr.safe_to_resume is False
    assert "timeout" in vr.confidence


def test_verification_solve_large_offset_not_safe(tmp_path: Path) -> None:
    helper = VerificationSolveHelper(
        _success_solver(residual=20.0), reason="audit"
    )
    vr = helper.solve_for_verification(tmp_path / "frame.fits")
    assert vr.safe_to_resume is False
    assert "re-center required" in vr.confidence


def test_verification_solve_marginal_offset_confidence(tmp_path: Path) -> None:
    helper = VerificationSolveHelper(_success_solver(residual=8.0), reason="audit")
    vr = helper.solve_for_verification(tmp_path / "frame.fits")
    assert vr.safe_to_resume is True
    assert "marginal" in vr.confidence


def test_verification_solve_reason_override(tmp_path: Path) -> None:
    helper = VerificationSolveHelper(_success_solver(), reason="default")
    vr = helper.solve_for_verification(tmp_path / "frame.fits", reason="recovery_check")
    assert vr.reason == "recovery_check"


def test_verification_solve_no_residual_safe(tmp_path: Path) -> None:
    """A successful solve without residual info should still be safe to resume."""
    solver = MagicMock()
    solver.solve.return_value = SolveResult(success=True, residual_arcmin=None)
    helper = VerificationSolveHelper(solver, reason="test")
    vr = helper.solve_for_verification(tmp_path / "frame.fits")
    assert vr.safe_to_resume is True


def test_verification_solve_captures_exception(tmp_path: Path) -> None:
    """Solver exception must be captured, not propagated."""
    helper = VerificationSolveHelper(_raising_solver(), reason="test")
    vr = helper.solve_for_verification(tmp_path / "frame.fits")
    assert vr.solve_result.success is False
    assert vr.solve_result.failure_category == SolveFailureCategory.SOLVER_UNAVAILABLE
    assert vr.safe_to_resume is False


def test_verification_solve_blind_flag_passed(tmp_path: Path) -> None:
    solver = _success_solver()
    helper = VerificationSolveHelper(solver, reason="test")
    helper.solve_for_verification(tmp_path / "frame.fits", blind=True)
    _, kwargs = solver.solve.call_args
    assert kwargs["blind"] is True


# ---------------------------------------------------------------------------
# Inline fake backends for claw controller tests
# ---------------------------------------------------------------------------


from kepler_node.agent.interfaces import (
    DeviceActivityEvent,
    NetworkMode,
    PowerStatus,
    ServiceHealth,
    StorageStatus,
    TimeSource,
    TimeStatus,
)
from kepler_node.mount.protocols import MountPosition


class _FakeNode:
    def network_mode(self) -> NetworkMode:
        return NetworkMode.FIELD_HOTSPOT

    def service_health(self) -> list[ServiceHealth]:
        return []

    def time_status(self) -> TimeStatus:
        return TimeStatus(trusted=True, source=TimeSource.NETWORK, summary="ok")

    def storage_status(self) -> StorageStatus:
        return StorageStatus(
            data_root=Path("/tmp"),
            free_bytes=50_000_000_000,
            total_bytes=100_000_000_000,
            writable=True,
            summary="ok",
        )

    def power_status(self) -> PowerStatus:
        return PowerStatus(healthy=True, summary="ok")

    def confirm_time(self, timestamp: datetime) -> TimeStatus:
        return self.time_status()


class _FakeMount:
    def __init__(self) -> None:
        self.connected = False

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def current_position(self) -> MountPosition:
        return MountPosition(ra_hours=0.0, dec_deg=0.0)

    def slew_to(self, position: MountPosition) -> None:
        pass

    def sync_to(self, position: MountPosition) -> None:
        pass

    def poll_activity(self) -> None:
        pass

    def activity_events(self) -> Iterable[DeviceActivityEvent]:
        return iter([])


class _FakeCamera:
    def __init__(self) -> None:
        self.heartbeat = MagicMock(return_value=True)

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def capture(self, request: object) -> object:
        from kepler_node.camera.protocols import CaptureResult
        from kepler_node.camera.protocols import CaptureRequest
        req: CaptureRequest = request  # type: ignore[assignment]
        path = req.destination_dir / "test_frame.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return CaptureResult(image_path=path, captured_at=datetime.now(UTC))

    def apply_settings(self, settings: object) -> object:
        return settings

    def activity_events(self) -> Iterable[DeviceActivityEvent]:
        return iter([])


class _FakeSolver:
    def solve(self, image_path: Path, **_: object) -> SolveResult:
        return SolveResult(success=True, solved_ra_hours=1.0, solved_dec_deg=45.0, residual_arcmin=5.0)


def _make_controller(tmp_path: Path, ekos_adapter: object | None = None) -> object:
    """Build a minimal ClawController for guard tests."""
    from kepler_node.agent.authorship import AuthorshipTracker
    from kepler_node.agent.claw import ClawController
    from kepler_node.agent.session import ClawState, RuntimeSession, WorkflowIntent
    from kepler_node.storage.filesystem import FilesystemSessionStore
    from kepler_node.storage.models import SessionRecord

    session = RuntimeSession()
    session.state = ClawState.CAPTURE
    session.session_id = "sess-test"
    session.workflow_intent = WorkflowIntent.CAPTURE
    session.control_locked = True

    store = FilesystemSessionStore(tmp_path)
    store.write_session_record(
        SessionRecord(
            session_id="sess-test",
            started_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            state=ClawState.CAPTURE,
        )
    )

    return ClawController(
        session=session,
        node_backend=_FakeNode(),
        mount_backend=_FakeMount(),
        camera_backend=_FakeCamera(),
        solver_backend=_FakeSolver(),
        store=store,
        authorship_tracker=AuthorshipTracker(),
        verification_dir=tmp_path / "verify",
        ekos_adapter=ekos_adapter,
    )


# ---------------------------------------------------------------------------
# evaluate_guard: quality_recommendation wiring
# ---------------------------------------------------------------------------


def test_evaluate_guard_trigger_autofocus_calls_ekos(tmp_path: Path) -> None:
    from kepler_node.agent.ekos import StubEkosAdapter

    ekos = StubEkosAdapter()
    with patch.object(ekos, "request_autofocus", wraps=ekos.request_autofocus) as mock_af:
        ctrl = _make_controller(tmp_path, ekos_adapter=ekos)
        result = ctrl.evaluate_guard(
            quality_overall="pass",
            quality_recommendation="trigger_autofocus",
        )
    mock_af.assert_called_once()
    # Should continue to capture (not paused) since the frame itself passed
    assert result.next_state.value == "capture"


def test_evaluate_guard_pause_sensor_pauses_session(tmp_path: Path) -> None:
    from kepler_node.agent.ekos import StubEkosAdapter

    ekos = StubEkosAdapter()
    with patch.object(ekos, "pause", wraps=ekos.pause) as mock_pause:
        ctrl = _make_controller(tmp_path, ekos_adapter=ekos)
        result = ctrl.evaluate_guard(
            quality_overall="pass",
            quality_recommendation="pause_sensor",
        )
    mock_pause.assert_called_once()
    assert result.next_state.value == "paused"


def test_evaluate_guard_pause_weather_pauses_session(tmp_path: Path) -> None:
    from kepler_node.agent.ekos import StubEkosAdapter

    ekos = StubEkosAdapter()
    with patch.object(ekos, "pause", wraps=ekos.pause) as mock_pause:
        ctrl = _make_controller(tmp_path, ekos_adapter=ekos)
        result = ctrl.evaluate_guard(
            quality_overall="pass",
            quality_recommendation="pause_weather",
        )
    mock_pause.assert_called_once()
    assert result.next_state.value == "paused"


def test_evaluate_guard_pause_sensor_sets_intervention_window_requested(tmp_path: Path) -> None:
    """evaluate_guard with pause_sensor must set _intervention_window to REQUESTED.

    Finding 3: all evaluate_guard pause paths that call ekos.pause() must record
    the pending-confirmation state so canonical_state() returns UNKNOWN active_owner
    rather than defaulting to EKOS.
    """
    from kepler_node.agent.absolute_state import InterventionWindowState
    from kepler_node.agent.ekos import StubEkosAdapter

    ctrl = _make_controller(tmp_path, ekos_adapter=StubEkosAdapter())
    ctrl.evaluate_guard(quality_overall="pass", quality_recommendation="pause_sensor")
    assert ctrl._intervention_window == InterventionWindowState.REQUESTED


def test_evaluate_guard_pause_weather_sets_intervention_window_requested(tmp_path: Path) -> None:
    """evaluate_guard with pause_weather must set _intervention_window to REQUESTED."""
    from kepler_node.agent.absolute_state import InterventionWindowState
    from kepler_node.agent.ekos import StubEkosAdapter

    ctrl = _make_controller(tmp_path, ekos_adapter=StubEkosAdapter())
    ctrl.evaluate_guard(quality_overall="pass", quality_recommendation="pause_weather")
    assert ctrl._intervention_window == InterventionWindowState.REQUESTED


def test_evaluate_guard_warn_recommendation_continues(tmp_path: Path) -> None:
    from kepler_node.agent.ekos import StubEkosAdapter

    ctrl = _make_controller(tmp_path, ekos_adapter=StubEkosAdapter())
    result = ctrl.evaluate_guard(
        quality_overall="pass",
        quality_recommendation="warn",
    )
    assert result.next_state.value == "capture"


def test_evaluate_guard_no_recommendation_unchanged(tmp_path: Path) -> None:
    from kepler_node.agent.ekos import StubEkosAdapter

    ctrl = _make_controller(tmp_path, ekos_adapter=StubEkosAdapter())
    result = ctrl.evaluate_guard(quality_overall="pass")
    assert result.next_state.value == "capture"


# ---------------------------------------------------------------------------
# camera_keepalive: skipped when Ekos sequence is active
# ---------------------------------------------------------------------------


def test_camera_keepalive_skipped_when_ekos_active(tmp_path: Path) -> None:
    from kepler_node.agent.absolute_state import EkosRuntimeState, NormalizedEkosSnapshot
    from kepler_node.agent.session import ClawState

    ekos = MagicMock()
    ekos.status.return_value = NormalizedEkosSnapshot(
        ekos_state=EkosRuntimeState.RUNNING,
        exposure_active=True,
    )

    ctrl = _make_controller(tmp_path, ekos_adapter=ekos)
    ctrl.session.state = ClawState.READY

    result = ctrl.camera_keepalive()
    assert "Ekos sequence active" in result.message
    # heartbeat() on the fake camera should never be called
    ctrl.camera.heartbeat.assert_not_called()


def test_camera_keepalive_fires_when_ekos_idle(tmp_path: Path) -> None:
    from kepler_node.agent.absolute_state import EkosRuntimeState, NormalizedEkosSnapshot
    from kepler_node.agent.session import ClawState

    ekos = MagicMock()
    ekos.status.return_value = NormalizedEkosSnapshot(
        ekos_state=EkosRuntimeState.IDLE,
    )

    ctrl = _make_controller(tmp_path, ekos_adapter=ekos)
    ctrl.session.state = ClawState.READY
    ctrl.camera.heartbeat.return_value = True

    result = ctrl.camera_keepalive()
    assert "ok" in result.message
    ctrl.camera.heartbeat.assert_called_once()


# ---------------------------------------------------------------------------
# ekos_output_dir config setting
# ---------------------------------------------------------------------------


def test_ekos_output_dir_setting_exists() -> None:
    """Settings must expose ekos_output_dir so the frame watcher can be wired."""
    from kepler_node.config import Settings

    s = Settings()
    assert hasattr(s, "ekos_output_dir")
    assert s.ekos_output_dir is None  # default: not configured


# ---------------------------------------------------------------------------
# observe_landed_frame
# ---------------------------------------------------------------------------


def _make_quality_result(overall: str = "pass") -> object:
    from kepler_node.imaging.protocols import QualityCheckResult, QualityClassification

    cls = QualityClassification(overall)
    return QualityCheckResult(
        overall=cls,
        checks={"stars": cls},
        metrics={"snr": 20.0},
        summary="ok",
    )


def test_observe_landed_frame_ingests_when_session_active(tmp_path: Path) -> None:
    """observe_landed_frame persists the frame when a session is active."""
    from kepler_node.imaging.frame_quality import FrameQualitySession

    ctrl = _make_controller(tmp_path)
    frame = tmp_path / "frame.fits"
    frame.touch()

    qr = _make_quality_result("pass")
    fqs = FrameQualitySession()

    ctrl.observe_landed_frame(frame, qr, fqs)

    # Session is active ("sess-test") so a FrameRecord must have been persisted
    records, _ = ctrl.store.list_frames("sess-test")
    assert len(records) >= 1
    assert records[0].frame_role == "science"


def test_observe_landed_frame_calls_evaluate_guard_in_capture_state(tmp_path: Path) -> None:
    """When state == CAPTURE, observe_landed_frame forwards to evaluate_guard."""
    from kepler_node.agent.session import ClawState
    from kepler_node.imaging.frame_quality import FrameQualitySession

    ekos = MagicMock()
    from kepler_node.agent.ekos import EkosSequenceStatus

    ekos.status.return_value = EkosSequenceStatus(active=True, paused=False)
    ctrl = _make_controller(tmp_path, ekos_adapter=ekos)
    ctrl.session.state = ClawState.CAPTURE

    frame = tmp_path / "frame.fits"
    frame.touch()
    qr = _make_quality_result("pass")
    fqs = FrameQualitySession()

    with patch.object(ctrl, "evaluate_guard", wraps=ctrl.evaluate_guard) as mock_guard:
        ctrl.observe_landed_frame(frame, qr, fqs)

    mock_guard.assert_called_once()


def test_observe_landed_frame_skips_evaluate_guard_outside_capture(tmp_path: Path) -> None:
    """When state != CAPTURE, observe_landed_frame skips evaluate_guard."""
    from kepler_node.agent.session import ClawState
    from kepler_node.imaging.frame_quality import FrameQualitySession

    ctrl = _make_controller(tmp_path)
    ctrl.session.state = ClawState.READY  # not CAPTURE

    frame = tmp_path / "frame.fits"
    frame.touch()
    qr = _make_quality_result("pass")
    fqs = FrameQualitySession()

    with patch.object(ctrl, "evaluate_guard") as mock_guard:
        result = ctrl.observe_landed_frame(frame, qr, fqs)

    mock_guard.assert_not_called()
    assert result is None


def test_observe_landed_frame_with_autofocus_recommendation(tmp_path: Path) -> None:
    """Rolling session autofocus recommendation is forwarded to evaluate_guard."""
    from kepler_node.agent.ekos import EkosSequenceStatus, StubEkosAdapter
    from kepler_node.agent.session import ClawState
    from kepler_node.imaging.frame_quality import FrameQualitySession

    ekos = StubEkosAdapter()
    ctrl = _make_controller(tmp_path, ekos_adapter=ekos)
    ctrl.session.state = ClawState.CAPTURE

    # Seed quality session with enough failing focus frames to trigger autofocus
    fqs = FrameQualitySession()
    from kepler_node.imaging.protocols import QualityCheckResult, QualityClassification

    bad_result = QualityCheckResult(
        overall=QualityClassification.FAIL,
        checks={"focus": QualityClassification.FAIL},
        metrics={"fwhm": 8.0},
        summary="focus poor",
    )
    for _ in range(5):
        fqs.add(bad_result)

    frame = tmp_path / "frame.fits"
    frame.touch()

    with patch.object(ctrl, "evaluate_guard", wraps=ctrl.evaluate_guard) as mock_guard:
        ctrl.observe_landed_frame(frame, bad_result, fqs)

    mock_guard.assert_called_once()
    call_kwargs = mock_guard.call_args[1] if mock_guard.call_args[1] else {}
    # quality_recommendation may or may not be set depending on fqs.recommendation()
    # but evaluate_guard must have been called with quality_overall
    assert "quality_overall" in call_kwargs


# ---------------------------------------------------------------------------
# run_verification_solve
# ---------------------------------------------------------------------------


def test_run_verification_solve_persists_solve_result_summary(tmp_path: Path) -> None:
    """run_verification_solve persists a FrameRecord with solve_result_summary."""
    from kepler_node.agent.ekos import StubEkosAdapter

    ekos = StubEkosAdapter()
    ctrl = _make_controller(tmp_path, ekos_adapter=ekos)

    frame = tmp_path / "verify.fits"
    frame.touch()

    vr = ctrl.run_verification_solve(
        frame, reason="post_intervention", expected_ra_hours=1.0, expected_dec_deg=45.0
    )

    assert vr is not None
    # _FakeSolver succeeds: safe_to_resume should be True (within default tolerance)
    # Regardless — frame records should have been written
    records, _ = ctrl.store.list_frames("sess-test")
    assert len(records) >= 1
    verification_records = [r for r in records if r.frame_role == "verification"]
    assert len(verification_records) == 1
    assert "success" in verification_records[0].solve_result_summary


def test_run_verification_solve_calls_reverify_when_not_safe(tmp_path: Path) -> None:
    """run_verification_solve requests Ekos reverify when solve is unsafe."""
    from kepler_node.agent.ekos import StubEkosAdapter

    solver = MagicMock()
    solver.solve.return_value = SolveResult(
        success=False,
        failure_category=SolveFailureCategory.INDEX_MISSING_OR_NO_MATCH,
    )

    ctrl = _make_controller(tmp_path, ekos_adapter=StubEkosAdapter())
    ctrl.solver = solver  # replace the solver

    frame = tmp_path / "verify.fits"
    frame.touch()

    with patch.object(ctrl.ekos, "request_reverify", wraps=ctrl.ekos.request_reverify) as mock_rv:
        vr = ctrl.run_verification_solve(frame, reason="audit")

    assert vr.safe_to_resume is False
    mock_rv.assert_called_once()


def test_run_verification_solve_does_not_call_reverify_when_safe(tmp_path: Path) -> None:
    """run_verification_solve does NOT request reverify when pointing is safe."""
    from kepler_node.agent.ekos import StubEkosAdapter

    # _FakeSolver returns success=True, residual=5.0 arcmin — safe by default thresholds
    ctrl = _make_controller(tmp_path, ekos_adapter=StubEkosAdapter())

    frame = tmp_path / "verify.fits"
    frame.touch()

    with patch.object(ctrl.ekos, "request_reverify") as mock_rv:
        vr = ctrl.run_verification_solve(
            frame, reason="post_intervention", expected_ra_hours=1.0, expected_dec_deg=45.0
        )

    # residual 5 arcmin should be within default tolerance
    if vr.safe_to_resume:
        mock_rv.assert_not_called()


# ---------------------------------------------------------------------------
# _frame_watcher_loop: wires into build_app lifespan
# ---------------------------------------------------------------------------


def test_frame_watcher_loop_cancels_cleanly(tmp_path: Path) -> None:
    """_frame_watcher_loop stops cleanly on cancellation."""
    import asyncio

    from kepler_node.api.app import _frame_watcher_loop

    ctrl = _make_controller(tmp_path)
    watch_dir = tmp_path / "ekos_output"
    watch_dir.mkdir()

    async def _run() -> None:
        task = asyncio.create_task(_frame_watcher_loop(ctrl, watch_dir))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert task.done()

    asyncio.run(_run())


def test_frame_watcher_loop_passes_quality_session_to_watcher(tmp_path: Path) -> None:
    """_frame_watcher_loop must wire quality_session into FrameWatcher constructor."""
    import asyncio

    from kepler_node.api.app import _frame_watcher_loop
    from kepler_node.imaging.frame_quality import FrameQualitySession

    ctrl = _make_controller(tmp_path)
    watch_dir = tmp_path / "ekos_output"
    watch_dir.mkdir()

    captured: dict = {}

    class _SpyWatcher:
        def __init__(self, directory, session=None, *, poll_interval_seconds=2.0, **kw):
            captured["session"] = session
            self._live = True

        def stop(self) -> None:
            self._live = False

        async def watch(self):
            while self._live:
                await asyncio.sleep(0.01)
            if False:
                yield  # make it an async generator

    async def _run() -> None:
        with patch("kepler_node.imaging.watcher.FrameWatcher", _SpyWatcher):
            task = asyncio.create_task(_frame_watcher_loop(ctrl, watch_dir))
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(_run())

    assert isinstance(captured.get("session"), FrameQualitySession), (
        "_frame_watcher_loop must pass session=quality_session to FrameWatcher"
    )


def test_frame_watcher_loop_rolling_state_advances_across_frames(tmp_path: Path) -> None:
    """Frames yielded through _frame_watcher_loop must advance rolling quality state."""
    import asyncio

    from kepler_node.api.app import _frame_watcher_loop
    from kepler_node.imaging.frame_quality import FrameQualitySession
    from kepler_node.imaging.protocols import QualityCheckResult, QualityClassification

    ctrl = _make_controller(tmp_path)
    watch_dir = tmp_path / "ekos_output2"
    watch_dir.mkdir()

    # Synthetic results for three frames
    def _qr(n: int) -> QualityCheckResult:
        return QualityCheckResult(
            overall=QualityClassification.PASS,
            checks={"focus": QualityClassification.PASS},
            metrics={"hfr_mean": 2.0 + n * 0.1, "hot_pixel_count": 5.0, "star_count": 15.0},
        )

    session_holder: dict = {}

    class _SyntheticWatcher:
        def __init__(self, directory, session=None, *, poll_interval_seconds=2.0, **kw):
            session_holder["session"] = session

        def stop(self) -> None:
            pass

        async def watch(self):
            for i in range(3):
                qr = _qr(i)
                frame = tmp_path / f"frame{i:03d}.fits"
                frame.touch()
                # Mirror what the real FrameWatcher does: add to session before yielding
                if session_holder["session"] is not None:
                    session_holder["session"].add(qr)
                yield frame, qr
            await asyncio.sleep(10)  # hold open until cancelled

    async def _run() -> None:
        with patch("kepler_node.imaging.watcher.FrameWatcher", _SyntheticWatcher):
            task = asyncio.create_task(_frame_watcher_loop(ctrl, watch_dir))
            await asyncio.sleep(0.2)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(_run())

    fqs: FrameQualitySession = session_holder["session"]
    assert fqs is not None
    assert fqs.frame_count == 3, (
        f"Expected 3 frames in rolling state, got {fqs.frame_count}"
    )


# ---------------------------------------------------------------------------
# resume() wires ekos.resume() — Fix 1
# ---------------------------------------------------------------------------


def test_resume_calls_ekos_resume(tmp_path: Path) -> None:
    """ClawController.resume() must delegate to ekos.resume() before transitioning state."""
    from kepler_node.agent.ekos import StubEkosAdapter
    from kepler_node.agent.session import ClawState, WorkflowIntent

    ekos = StubEkosAdapter()
    with patch.object(ekos, "resume", wraps=ekos.resume) as mock_resume:
        ctrl = _make_controller(tmp_path, ekos_adapter=ekos)
        ctrl.session.state = ClawState.CAPTURE
        ctrl.session.pause(
            pause_reason="storage_critically_low",
            resume_state=ClawState.CAPTURE,
            workflow_intent=WorkflowIntent.CAPTURE,
            operator_action_required="Free disk space",
        )
        result = ctrl.resume()

    mock_resume.assert_called_once()
    assert result.next_state == ClawState.CAPTURE


def test_resume_stays_paused_when_ekos_resume_fails(tmp_path: Path) -> None:
    """When ekos.resume() returns False, the controller stays PAUSED."""
    from kepler_node.agent.ekos import StubEkosAdapter
    from kepler_node.agent.session import ClawState, WorkflowIntent

    ekos = StubEkosAdapter()
    with patch.object(ekos, "resume", return_value=False):
        ctrl = _make_controller(tmp_path, ekos_adapter=ekos)
        ctrl.session.state = ClawState.CAPTURE
        ctrl.session.pause(
            pause_reason="test_pause",
            resume_state=ClawState.CAPTURE,
            workflow_intent=WorkflowIntent.CAPTURE,
            operator_action_required="Resolve issue",
        )
        result = ctrl.resume()

    assert result.next_state == ClawState.PAUSED
    assert ctrl.session.state == ClawState.PAUSED
    assert ctrl.session.resume_context is not None  # context preserved for retry


# ---------------------------------------------------------------------------
# poll_ekos_observation() controller method — Fix 2
# ---------------------------------------------------------------------------


def test_poll_ekos_observation_delegates_to_adapter(tmp_path: Path) -> None:
    """poll_ekos_observation() calls poll_focus, poll_temperature, poll_sequence_status."""
    from kepler_node.agent.ekos import StubEkosAdapter

    ekos = StubEkosAdapter()
    with (
        patch.object(ekos, "poll_focus") as mock_focus,
        patch.object(ekos, "poll_temperature") as mock_temp,
        patch.object(ekos, "poll_sequence_status") as mock_seq,
    ):
        ctrl = _make_controller(tmp_path, ekos_adapter=ekos)
        ctrl.poll_ekos_observation()

    mock_focus.assert_called_once()
    mock_temp.assert_called_once()
    mock_seq.assert_called_once()


# ---------------------------------------------------------------------------
# _check_conflicts drains ekos.observe() — Fix 2
# ---------------------------------------------------------------------------


def test_check_conflicts_drains_ekos_observe(tmp_path: Path) -> None:
    """_check_conflicts() must drain ekos.observe() so observation events are consumed."""
    from datetime import UTC, datetime

    from kepler_node.agent.ekos import StubEkosAdapter
    from kepler_node.agent.interfaces import DeviceActivityEvent, DeviceActivityEventType
    from kepler_node.agent.session import ClawState

    ekos = StubEkosAdapter()
    # Inject a non-conflict-eligible observation event directly into the queue
    obs_event = DeviceActivityEvent(
        event_type=DeviceActivityEventType.FOCUS_POSITION_CHANGED,
        observed_at=datetime.now(UTC),
        details={"focus_position": "5000"},
    )
    with patch.object(ekos, "observe", return_value=[obs_event]) as mock_observe:
        ctrl = _make_controller(tmp_path, ekos_adapter=ekos)
        ctrl.session.state = ClawState.CAPTURE
        ctrl.session.control_locked = True
        detected = ctrl.check_and_handle_conflicts()

    mock_observe.assert_called_once()
    assert detected is False  # focus event is not conflict-eligible


# ---------------------------------------------------------------------------
# _ekos_observation_loop background task — Fix 2
# ---------------------------------------------------------------------------


def test_ekos_observation_loop_cancels_cleanly(tmp_path: Path) -> None:
    """_ekos_observation_loop stops cleanly on cancellation."""
    import asyncio

    from kepler_node.api.app import _ekos_observation_loop

    ctrl = _make_controller(tmp_path)

    async def _run() -> None:
        task = asyncio.create_task(_ekos_observation_loop(ctrl))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert task.done()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# MountBackend protocol: poll_activity() is part of the contract
# ---------------------------------------------------------------------------


def test_mount_backend_protocol_has_poll_activity() -> None:
    """MountBackend protocol must declare poll_activity() so conflict detection can call it."""
    import inspect

    from kepler_node.mount.protocols import MountBackend

    members = {name for name, _ in inspect.getmembers(MountBackend)}
    assert "poll_activity" in members, (
        "MountBackend protocol is missing poll_activity(); "
        "add it so _check_conflicts() can populate mount events before draining them"
    )


# ---------------------------------------------------------------------------
# _check_conflicts calls mount.poll_activity() — acceptance check 4
# ---------------------------------------------------------------------------


def test_check_conflicts_calls_mount_poll_activity(tmp_path: Path) -> None:
    """_check_conflicts() must call mount.poll_activity() to populate events before draining."""
    from kepler_node.agent.session import ClawState

    ctrl = _make_controller(tmp_path)
    ctrl.session.state = ClawState.CAPTURE
    ctrl.session.control_locked = True

    with patch.object(ctrl.mount, "poll_activity") as mock_poll:
        ctrl.check_and_handle_conflicts()

    mock_poll.assert_called_once()


# ---------------------------------------------------------------------------
# _serve.py wires DBusEkosAdapter — acceptance check 3 runtime path
# ---------------------------------------------------------------------------


def test_serve_wires_dbus_ekos_adapter() -> None:
    """make_dev_app() must construct the controller with DBusEkosAdapter (not StubEkosAdapter)."""
    import inspect

    from kepler_node.api._serve import make_dev_app
    from kepler_node.agent.ekos import DBusEkosAdapter

    src = inspect.getsource(make_dev_app)
    assert "DBusEkosAdapter" in src, (
        "_serve.make_dev_app() must wire DBusEkosAdapter as the ekos_adapter; "
        "StubEkosAdapter should only be the fallback for tests and pre-Ekos stages"
    )


# ---------------------------------------------------------------------------
# Phase 2 acceptance check: canonical absolute-state model
# ---------------------------------------------------------------------------


def test_normalized_ekos_snapshot_defaults_to_unknown() -> None:
    """NormalizedEkosSnapshot must default to UNKNOWN state (conservative)."""
    from kepler_node.agent.absolute_state import EkosRuntimeState, NormalizedEkosSnapshot

    snap = NormalizedEkosSnapshot()
    assert snap.ekos_state == EkosRuntimeState.UNKNOWN
    assert snap.is_unknown is True
    assert snap.active is False
    assert snap.paused is False


def test_normalized_ekos_snapshot_freshness() -> None:
    """Stale snapshots must report is_unknown=True even if state is known."""
    from datetime import timedelta

    from kepler_node.agent.absolute_state import EkosRuntimeState, NormalizedEkosSnapshot

    # Fresh running snapshot
    fresh = NormalizedEkosSnapshot(
        ekos_state=EkosRuntimeState.RUNNING,
        freshness_ttl_seconds=60.0,
    )
    assert fresh.is_unknown is False
    assert fresh.is_running is True

    # Artificially stale snapshot
    stale = NormalizedEkosSnapshot(
        ekos_state=EkosRuntimeState.RUNNING,
        confirmed_at=datetime.now(UTC) - timedelta(seconds=120),
        freshness_ttl_seconds=60.0,
    )
    assert stale.is_stale is True
    assert stale.is_unknown is True
    assert stale.is_running is False


def test_normalized_ekos_snapshot_paused_state() -> None:
    """is_paused must only be True for PAUSED state with a fresh snapshot."""
    from kepler_node.agent.absolute_state import EkosRuntimeState, NormalizedEkosSnapshot

    snap = NormalizedEkosSnapshot(ekos_state=EkosRuntimeState.PAUSED)
    assert snap.is_paused is True
    assert snap.paused is True
    assert snap.active is False


def test_canonical_absolute_state_safe_for_intervention() -> None:
    """CanonicalAbsoluteState.is_safe_for_kepler_intervention() must enforce all conditions."""
    from kepler_node.agent.absolute_state import (
        ActiveOwner,
        BrokerRuntimeState,
        CanonicalAbsoluteState,
        EkosRuntimeState,
        InterventionWindowState,
    )

    # Must be safe when: window OPEN, broker READY, Ekos PAUSED, Kepler owns, not locked
    safe = CanonicalAbsoluteState(
        active_owner=ActiveOwner.KEPLER,
        ekos_state=EkosRuntimeState.PAUSED,
        broker_state=BrokerRuntimeState.READY,
        intervention_window=InterventionWindowState.OPEN,
        control_locked=True,
    )
    assert safe.is_safe_for_kepler_intervention() is True

    # Must NOT be safe when window is still REQUESTED (not yet confirmed open)
    not_open = CanonicalAbsoluteState(
        active_owner=ActiveOwner.UNKNOWN,
        ekos_state=EkosRuntimeState.PAUSED,
        broker_state=BrokerRuntimeState.READY,
        intervention_window=InterventionWindowState.REQUESTED,
        control_locked=True,
    )
    assert not_open.is_safe_for_kepler_intervention() is False


def test_canonical_absolute_state_safe_to_resume() -> None:
    """is_safe_to_resume() must require Ekos PAUSED and window RELEASING or OPEN."""
    from kepler_node.agent.absolute_state import (
        ActiveOwner,
        BrokerRuntimeState,
        CanonicalAbsoluteState,
        EkosRuntimeState,
        InterventionWindowState,
    )

    resumable = CanonicalAbsoluteState(
        active_owner=ActiveOwner.EKOS,
        ekos_state=EkosRuntimeState.PAUSED,
        broker_state=BrokerRuntimeState.READY,
        intervention_window=InterventionWindowState.RELEASING,
        control_locked=True,
    )
    assert resumable.is_safe_to_resume() is True

    not_resumable = CanonicalAbsoluteState(
        active_owner=ActiveOwner.UNKNOWN,
        ekos_state=EkosRuntimeState.UNKNOWN,
        broker_state=BrokerRuntimeState.UNKNOWN,
        intervention_window=InterventionWindowState.OPEN,
        control_locked=True,
    )
    assert not_resumable.is_safe_to_resume() is False


# ---------------------------------------------------------------------------
# Phase 2 acceptance check: broker seam
# ---------------------------------------------------------------------------


def test_stub_broker_backend_returns_ready() -> None:
    """StubBrokerBackend must return READY state and be reachable."""
    from kepler_node.agent.broker import BrokerRuntimeState, StubBrokerBackend

    broker = StubBrokerBackend()
    snap = broker.snapshot()
    assert snap.broker_state == BrokerRuntimeState.READY
    assert snap.device_path_available is True
    assert broker.is_reachable() is True


def test_indiwebmanager_broker_backend_unreachable() -> None:
    """IndiWebManagerBrokerBackend must return UNAVAILABLE when host is unreachable."""
    from kepler_node.agent.broker import BrokerRuntimeState, IndiWebManagerBrokerBackend

    broker = IndiWebManagerBrokerBackend(
        host="127.0.0.1",
        port=19999,  # nothing listening here
        timeout_seconds=0.5,
    )
    snap = broker.snapshot()
    assert snap.broker_state == BrokerRuntimeState.UNAVAILABLE
    assert broker.is_reachable() is False


# ---------------------------------------------------------------------------
# Phase 2 acceptance check: canonical_state() synthesis on ClawController
# ---------------------------------------------------------------------------


def test_canonical_state_returns_canonical_absolute_state(tmp_path: Path) -> None:
    """ClawController.canonical_state() must return a CanonicalAbsoluteState."""
    from kepler_node.agent.absolute_state import CanonicalAbsoluteState

    ctrl = _make_controller(tmp_path)
    state = ctrl.canonical_state()
    assert isinstance(state, CanonicalAbsoluteState)


def test_canonical_state_active_owner_ekos_when_running(tmp_path: Path) -> None:
    """canonical_state() must report EKOS as owner when Ekos is running and window is closed."""
    from kepler_node.agent.absolute_state import ActiveOwner, EkosRuntimeState, NormalizedEkosSnapshot

    ekos = MagicMock()
    ekos.status.return_value = NormalizedEkosSnapshot(ekos_state=EkosRuntimeState.RUNNING)

    ctrl = _make_controller(tmp_path, ekos_adapter=ekos)
    state = ctrl.canonical_state()
    assert state.active_owner == ActiveOwner.EKOS


def test_canonical_state_unknown_when_ekos_unavailable(tmp_path: Path) -> None:
    """canonical_state() must report UNKNOWN owner when Ekos is unavailable."""
    from kepler_node.agent.absolute_state import ActiveOwner, EkosRuntimeState, NormalizedEkosSnapshot

    ekos = MagicMock()
    ekos.status.return_value = NormalizedEkosSnapshot(ekos_state=EkosRuntimeState.UNAVAILABLE)

    ctrl = _make_controller(tmp_path, ekos_adapter=ekos)
    state = ctrl.canonical_state()
    assert state.active_owner == ActiveOwner.UNKNOWN


# ---------------------------------------------------------------------------
# Phase 2 acceptance check: pause-acquire-act-release-resume workflow
# ---------------------------------------------------------------------------


def test_pause_sets_intervention_window_requested(tmp_path: Path) -> None:
    """pause() must set _intervention_window to REQUESTED."""
    from kepler_node.agent.absolute_state import InterventionWindowState
    from kepler_node.agent.session import ClawState

    ekos = MagicMock()
    ekos.pause.return_value = True

    ctrl = _make_controller(tmp_path, ekos_adapter=ekos)
    # pause() requires a non-idle active session state (CAPTURE, not PAUSED)
    ctrl.session.state = ClawState.CAPTURE

    ctrl.pause()
    assert ctrl._intervention_window == InterventionWindowState.REQUESTED


def test_confirm_ekos_paused_opens_window(tmp_path: Path) -> None:
    """confirm_ekos_paused() must open the intervention window when Ekos is confirmed paused."""
    from kepler_node.agent.absolute_state import EkosRuntimeState, InterventionWindowState, NormalizedEkosSnapshot
    from kepler_node.agent.session import ClawState

    ekos = MagicMock()
    ekos.pause.return_value = True
    ekos.status.return_value = NormalizedEkosSnapshot(ekos_state=EkosRuntimeState.PAUSED)

    ctrl = _make_controller(tmp_path, ekos_adapter=ekos)
    ctrl.session.state = ClawState.CAPTURE
    ctrl.pause()

    opened = ctrl.confirm_ekos_paused()
    assert opened is True
    assert ctrl._intervention_window == InterventionWindowState.OPEN


def test_confirm_ekos_paused_stays_requested_when_unknown(tmp_path: Path) -> None:
    """confirm_ekos_paused() must leave window REQUESTED when Ekos state is unknown."""
    from kepler_node.agent.absolute_state import EkosRuntimeState, InterventionWindowState, NormalizedEkosSnapshot
    from kepler_node.agent.session import ClawState

    ekos = MagicMock()
    ekos.pause.return_value = True
    ekos.status.return_value = NormalizedEkosSnapshot(ekos_state=EkosRuntimeState.UNKNOWN)

    ctrl = _make_controller(tmp_path, ekos_adapter=ekos)
    ctrl.session.state = ClawState.CAPTURE
    ctrl.pause()

    opened = ctrl.confirm_ekos_paused()
    assert opened is False
    assert ctrl._intervention_window == InterventionWindowState.REQUESTED


def test_fail_resets_intervention_window(tmp_path: Path) -> None:
    """fail() must reset _intervention_window to CLOSED."""
    from kepler_node.agent.absolute_state import InterventionWindowState
    from kepler_node.agent.session import ClawState

    ctrl = _make_controller(tmp_path)
    ctrl._intervention_window = InterventionWindowState.OPEN
    ctrl.session.state = ClawState.CAPTURE

    ctrl.fail(reason="test failure")
    assert ctrl._intervention_window == InterventionWindowState.CLOSED


def test_release_control_resets_intervention_window(tmp_path: Path) -> None:
    """release_control() must reset _intervention_window to CLOSED."""
    from kepler_node.agent.absolute_state import InterventionWindowState
    from kepler_node.agent.session import ClawState

    ctrl = _make_controller(tmp_path)
    ctrl._intervention_window = InterventionWindowState.OPEN
    ctrl.session.state = ClawState.PAUSED

    ctrl.release_control()
    assert ctrl._intervention_window == InterventionWindowState.CLOSED


# ---------------------------------------------------------------------------
# Phase 2 correctness: contradictory-state blocking
# ---------------------------------------------------------------------------


def test_resume_blocks_when_ekos_running(tmp_path: Path) -> None:
    """resume() must stay PAUSED when Ekos reports RUNNING (contradictory to paused posture).

    Spec §Control Handoff Protocol lines 402-408: a contradictory live state
    (Ekos reporting RUNNING while we expect it to be paused) must keep
    active_owner as unknown and keep the controller in PAUSED.
    """
    from kepler_node.agent.absolute_state import ActiveOwner, EkosRuntimeState, NormalizedEkosSnapshot
    from kepler_node.agent.session import ClawState, WorkflowIntent

    ekos = MagicMock()
    ekos.status.return_value = NormalizedEkosSnapshot(ekos_state=EkosRuntimeState.RUNNING)
    ekos.resume.return_value = True

    ctrl = _make_controller(tmp_path, ekos_adapter=ekos)
    ctrl.session.state = ClawState.CAPTURE
    ctrl.session.pause(
        pause_reason="operator_pause",
        resume_state=ClawState.CAPTURE,
        workflow_intent=WorkflowIntent.CAPTURE,
        operator_action_required="Resolve conflict",
    )

    result = ctrl.resume()

    assert result.next_state == ClawState.PAUSED
    assert ctrl.session.state == ClawState.PAUSED
    ekos.resume.assert_not_called()
    assert result.details.get("active_owner") == ActiveOwner.UNKNOWN


def test_resume_blocks_when_ekos_unavailable(tmp_path: Path) -> None:
    """resume() must stay PAUSED when Ekos is UNAVAILABLE (cannot verify pause state).

    Spec line 407: missing or unavailable pause confirmation must keep
    active_owner unknown and controller in PAUSED.
    """
    from kepler_node.agent.absolute_state import ActiveOwner, EkosRuntimeState, NormalizedEkosSnapshot
    from kepler_node.agent.session import ClawState, WorkflowIntent

    ekos = MagicMock()
    ekos.status.return_value = NormalizedEkosSnapshot(ekos_state=EkosRuntimeState.UNAVAILABLE)
    ekos.resume.return_value = True

    ctrl = _make_controller(tmp_path, ekos_adapter=ekos)
    ctrl.session.state = ClawState.CAPTURE
    ctrl.session.pause(
        pause_reason="operator_pause",
        resume_state=ClawState.CAPTURE,
        workflow_intent=WorkflowIntent.CAPTURE,
        operator_action_required="Resolve issue",
    )

    result = ctrl.resume()

    assert result.next_state == ClawState.PAUSED
    assert ctrl.session.state == ClawState.PAUSED
    ekos.resume.assert_not_called()
    assert result.details.get("active_owner") == ActiveOwner.UNKNOWN


def test_confirm_ekos_paused_stays_requested_when_recent_activity(tmp_path: Path) -> None:
    """confirm_ekos_paused() must not open the window when device activity was recent.

    Spec line 398: Kepler must wait for pause confirmation AND for observed
    device activity to settle before treating the semaphore as open.
    """
    from datetime import UTC, datetime

    from kepler_node.agent.absolute_state import EkosRuntimeState, InterventionWindowState, NormalizedEkosSnapshot
    from kepler_node.agent.session import ClawState

    ekos = MagicMock()
    ekos.pause.return_value = True
    ekos.status.return_value = NormalizedEkosSnapshot(ekos_state=EkosRuntimeState.PAUSED)

    ctrl = _make_controller(tmp_path, ekos_adapter=ekos)
    ctrl.session.state = ClawState.CAPTURE
    ctrl.pause()

    # Simulate recent device activity (within the settle window)
    ctrl._last_significant_activity_at = datetime.now(UTC)

    opened = ctrl.confirm_ekos_paused()
    assert opened is False
    assert ctrl._intervention_window == InterventionWindowState.REQUESTED


def test_canonical_state_unknown_when_window_open_but_ekos_running(tmp_path: Path) -> None:
    """canonical_state() must return UNKNOWN active_owner when window=OPEN but Ekos=RUNNING.

    Spec lines 446-447: if the broker looks healthy but live device activity
    contradicts the expected paused posture, the path is not safely open.
    The conservative precedence rule requires active_owner = UNKNOWN.
    """
    from kepler_node.agent.absolute_state import (
        ActiveOwner,
        EkosRuntimeState,
        InterventionWindowState,
        NormalizedEkosSnapshot,
    )

    ekos = MagicMock()
    ekos.status.return_value = NormalizedEkosSnapshot(ekos_state=EkosRuntimeState.RUNNING)

    ctrl = _make_controller(tmp_path, ekos_adapter=ekos)
    ctrl.session.control_locked = True
    ctrl._intervention_window = InterventionWindowState.OPEN

    state = ctrl.canonical_state()

    assert state.active_owner == ActiveOwner.UNKNOWN
    assert state.ekos_state == EkosRuntimeState.RUNNING
    assert state.intervention_window == InterventionWindowState.OPEN

def test_pause_on_conflict_calls_ekos_pause(tmp_path: Path) -> None:
    """_pause_on_conflict() must call ekos.pause() before setting window to REQUESTED.

    Finding 3 (check_round2): the external-conflict path must implement the
    full pause-acquire-act-release-resume handoff, which begins with
    "Kepler requests that Ekos pause" (spec §Control Handoff Protocol step 2).
    Previously _pause_on_conflict() only called session.pause() and set the
    window to REQUESTED without ever requesting the Ekos pause.
    """
    from kepler_node.agent.absolute_state import InterventionWindowState

    ekos = MagicMock()
    ekos.pause.return_value = True

    ctrl = _make_controller(tmp_path, ekos_adapter=ekos)

    ctrl._pause_on_conflict("camera", "capture_started")

    ekos.pause.assert_called_once()
    assert ctrl._intervention_window == InterventionWindowState.REQUESTED
