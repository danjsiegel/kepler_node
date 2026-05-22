"""Tests for the frame quality analyzer and session trend tracker.

Uses synthetic images built with numpy/OpenCV so no real FITS or RAW files
are required. Each test targets a specific quality axis (focus, tracking,
sensor health) or a trend detection scenario (drift, accumulation).
"""

from __future__ import annotations

import math
from pathlib import Path

import cv2
import numpy as np
import pytest

from kepler_node.imaging.frame_quality import (
    FrameQualityAnalyzer,
    FrameQualitySession,
    _compute_hfr,
)
from kepler_node.imaging.protocols import QualityCheckResult, QualityClassification

# ---------------------------------------------------------------------------
# Synthetic image helpers
# ---------------------------------------------------------------------------

_BG = 10  # background ADU for synthetic images


def _blank(shape: tuple[int, int] = (300, 300)) -> np.ndarray:
    return np.full(shape, _BG, dtype=np.uint8)


def _add_star(
    img: np.ndarray,
    cx: float,
    cy: float,
    *,
    sigma: float = 2.5,
    peak: int = 200,
    elongation: float = 1.0,
    angle_deg: float = 0.0,
) -> np.ndarray:
    """Paint a Gaussian star onto img in-place and return it."""
    h, w = img.shape
    ys, xs = np.indices((h, w), dtype=np.float32)
    angle_rad = math.radians(angle_deg)
    dx = (xs - cx) * math.cos(angle_rad) + (ys - cy) * math.sin(angle_rad)
    dy = -(xs - cx) * math.sin(angle_rad) + (ys - cy) * math.cos(angle_rad)
    sigma_x = sigma * math.sqrt(elongation)
    sigma_y = sigma
    gauss = np.exp(-(dx**2 / (2 * sigma_x**2) + dy**2 / (2 * sigma_y**2)))
    img_f = img.astype(np.float32) + peak * gauss
    return np.clip(img_f, 0, 255).astype(np.uint8)


def _star_field(
    n_stars: int = 8, shape: tuple[int, int] = (300, 300), sigma: float = 2.5
) -> np.ndarray:
    """Return a synthetic star field with n_stars round Gaussian stars."""
    rng = np.random.default_rng(42)
    img = _blank(shape)
    h, w = shape
    margin = 30
    for _ in range(n_stars):
        cx = float(rng.integers(margin, w - margin))
        cy = float(rng.integers(margin, h - margin))
        img = _add_star(img, cx, cy, sigma=sigma)
    return img


def _save(img: np.ndarray, path: Path) -> Path:
    cv2.imwrite(str(path), img)
    return path


def _tight_analyzer(**kwargs: object) -> FrameQualityAnalyzer:
    """Analyzer with tight thresholds suitable for synthetic test images."""
    defaults: dict[str, object] = dict(
        hfr_warn=5.0,
        hfr_fail=10.0,
        elongation_warn=1.4,
        elongation_fail=2.0,
        hot_pixel_warn=3,
        hot_pixel_fail=10,
        min_stars_for_hfr=2,
        analysis_scale=1.0,  # skip downsampling so synthetic pixels aren't shrunk
    )
    defaults.update(kwargs)
    return FrameQualityAnalyzer(**defaults)


# ---------------------------------------------------------------------------
# _compute_hfr unit tests
# ---------------------------------------------------------------------------


def test_compute_hfr_circular_star() -> None:
    """HFR for a tight Gaussian star should be small and positive."""
    img = _blank((100, 100))
    img = _add_star(img, 50, 50, sigma=2.0, peak=200)
    gray = img.astype(np.float32)
    hfr = _compute_hfr(gray, 50.0, 50.0, _BG)
    assert 0.5 < hfr < 6.0, f"Expected HFR ≈ sigma, got {hfr}"


def test_compute_hfr_larger_star_has_larger_hfr() -> None:
    """A more diffuse star should have a larger HFR than a tight one."""
    img_tight = _add_star(_blank((100, 100)), 50, 50, sigma=1.5, peak=200)
    img_wide = _add_star(_blank((100, 100)), 50, 50, sigma=4.0, peak=200)
    hfr_tight = _compute_hfr(img_tight.astype(np.float32), 50.0, 50.0, _BG)
    hfr_wide = _compute_hfr(img_wide.astype(np.float32), 50.0, 50.0, _BG)
    assert hfr_tight < hfr_wide, f"tight HFR {hfr_tight} should be < wide HFR {hfr_wide}"


# ---------------------------------------------------------------------------
# FrameQualityAnalyzer — load failure
# ---------------------------------------------------------------------------


def test_analyze_missing_file_returns_fail() -> None:
    analyzer = _tight_analyzer()
    result = analyzer.analyze(Path("/nonexistent/path/frame.jpg"))
    assert result.overall == QualityClassification.FAIL
    assert "load" in result.checks


# ---------------------------------------------------------------------------
# FrameQualityAnalyzer — focus / HFR checks
# ---------------------------------------------------------------------------


def test_analyze_sharp_stars_pass_focus(tmp_path: Path) -> None:
    """Tight stars (small sigma) should yield a passing focus check."""
    img = _star_field(n_stars=8, sigma=2.0)
    path = _save(img, tmp_path / "sharp.jpg")
    analyzer = _tight_analyzer()
    result = analyzer.analyze(path)
    assert "focus" in result.checks
    assert result.checks["focus"] in (QualityClassification.PASS, QualityClassification.WARN)
    assert result.metrics.get("hfr_mean", 99) < 8.0


def test_analyze_bloated_stars_warn_or_fail_focus(tmp_path: Path) -> None:
    """Very diffuse stars (large sigma) should push HFR above the warn threshold."""
    img = _star_field(n_stars=8, sigma=6.0)
    path = _save(img, tmp_path / "bloated.jpg")
    analyzer = _tight_analyzer(hfr_warn=3.0, hfr_fail=6.0)
    result = analyzer.analyze(path)
    assert "focus" in result.checks
    hfr = result.metrics.get("hfr_mean", 0)
    assert hfr > 3.0, f"Expected HFR > 3 for bloated stars, got {hfr}"


def test_analyze_reports_hfr_in_metrics(tmp_path: Path) -> None:
    img = _star_field(n_stars=6, sigma=2.5)
    path = _save(img, tmp_path / "frame.jpg")
    result = _tight_analyzer().analyze(path)
    assert "hfr_mean" in result.metrics
    assert result.metrics["hfr_mean"] > 0


def test_analyze_star_count_in_metrics(tmp_path: Path) -> None:
    img = _star_field(n_stars=5, sigma=2.5)
    path = _save(img, tmp_path / "frame.jpg")
    result = _tight_analyzer().analyze(path)
    assert result.metrics.get("star_count", 0) >= 1


# ---------------------------------------------------------------------------
# FrameQualityAnalyzer — tracking / elongation checks
# ---------------------------------------------------------------------------


def test_round_stars_pass_tracking(tmp_path: Path) -> None:
    """Circular stars should result in an elongation near 1.0."""
    img = _star_field(n_stars=6, sigma=3.0)
    path = _save(img, tmp_path / "round.jpg")
    result = _tight_analyzer().analyze(path)
    elong = result.metrics.get("elongation_mean", 1.0)
    assert elong < 1.6, f"Expected near-circular elongation, got {elong}"


def test_elongated_stars_warn_or_fail_tracking(tmp_path: Path) -> None:
    """Stars stretched 3× along one axis should fail the tracking check."""
    img = _blank((300, 300))
    rng = np.random.default_rng(7)
    for _ in range(6):
        cx = float(rng.integers(50, 250))
        cy = float(rng.integers(50, 250))
        img = _add_star(img, cx, cy, sigma=2.0, peak=200, elongation=4.0, angle_deg=45.0)
    path = _save(img, tmp_path / "elongated.jpg")
    result = _tight_analyzer(elongation_warn=1.3, elongation_fail=1.8).analyze(path)
    assert result.checks.get("tracking") in (
        QualityClassification.WARN,
        QualityClassification.FAIL,
    )


# ---------------------------------------------------------------------------
# FrameQualityAnalyzer — sensor health / hot pixels
# ---------------------------------------------------------------------------


def test_clean_frame_passes_sensor_health(tmp_path: Path) -> None:
    img = _star_field(n_stars=6, sigma=2.5)
    path = _save(img, tmp_path / "clean.jpg")
    result = _tight_analyzer().analyze(path)
    assert result.checks.get("sensor_health") == QualityClassification.PASS


def test_hot_pixels_detected_and_counted(tmp_path: Path) -> None:
    """Isolated bright pixels significantly above their neighborhood are hot pixels."""
    img = _blank((200, 200))
    rng = np.random.default_rng(99)
    # Plant 20 hot pixels
    for _ in range(20):
        x = int(rng.integers(10, 190))
        y = int(rng.integers(10, 190))
        img[y, x] = 255
    path = _save(img, tmp_path / "hot.png")  # PNG: lossless, no JPEG spreading
    result = _tight_analyzer(hot_pixel_warn=5, hot_pixel_fail=15).analyze(path)
    assert result.metrics.get("hot_pixel_count", 0) >= 5


def test_many_hot_pixels_fails_sensor_health(tmp_path: Path) -> None:
    img = _blank((200, 200))
    rng = np.random.default_rng(11)
    for _ in range(30):
        x, y = int(rng.integers(5, 195)), int(rng.integers(5, 195))
        img[y, x] = 255
    path = _save(img, tmp_path / "many_hot.png")  # PNG: lossless, no JPEG spreading
    result = _tight_analyzer(hot_pixel_warn=5, hot_pixel_fail=15).analyze(path)
    assert result.checks.get("sensor_health") in (
        QualityClassification.WARN,
        QualityClassification.FAIL,
    )


# ---------------------------------------------------------------------------
# FrameQualityAnalyzer — overall classification
# ---------------------------------------------------------------------------


def test_overall_is_worst_check(tmp_path: Path) -> None:
    """overall should be FAIL if any single check is FAIL."""
    img = _blank((200, 200))
    rng = np.random.default_rng(22)
    # Add enough hot pixels to fail sensor_health
    for _ in range(25):
        x, y = int(rng.integers(5, 195)), int(rng.integers(5, 195))
        img[y, x] = 255
    # Add some round stars so focus/tracking pass
    img = _add_star(img, 100, 100, sigma=2.5)
    img = _add_star(img, 50, 150, sigma=2.5)
    path = _save(img, tmp_path / "hot_stars.png")  # PNG: lossless, no JPEG spreading
    result = _tight_analyzer(hot_pixel_warn=3, hot_pixel_fail=10).analyze(path)
    # At least one check should be degraded
    check_values = list(result.checks.values())
    worst = (
        QualityClassification.FAIL
        if QualityClassification.FAIL in check_values
        else (
            QualityClassification.WARN
            if QualityClassification.WARN in check_values
            else QualityClassification.PASS
        )
    )
    assert result.overall == worst


def test_summary_is_non_empty(tmp_path: Path) -> None:
    img = _star_field(n_stars=5)
    path = _save(img, tmp_path / "frame.jpg")
    result = _tight_analyzer().analyze(path)
    assert result.summary is not None
    assert len(result.summary) > 0


# ---------------------------------------------------------------------------
# FrameQualitySession — trend detection
# ---------------------------------------------------------------------------


def _make_result(
    hfr: float, hot_pixels: float, overall: QualityClassification = QualityClassification.PASS
) -> QualityCheckResult:
    return QualityCheckResult(
        overall=overall,
        checks={"focus": overall},
        metrics={"hfr_mean": hfr, "hot_pixel_count": hot_pixels},
    )


def _make_result_with_stars(
    hfr: float,
    hot_pixels: float,
    star_count: float,
    overall: QualityClassification = QualityClassification.PASS,
) -> QualityCheckResult:
    return QualityCheckResult(
        overall=overall,
        checks={"focus": overall},
        metrics={"hfr_mean": hfr, "hot_pixel_count": hot_pixels, "star_count": star_count},
    )


def test_session_continue_with_few_frames() -> None:
    session = FrameQualitySession()
    session.add(_make_result(2.0, 5.0))
    session.add(_make_result(2.1, 5.0))
    assert session.recommendation() == FrameQualitySession.Recommendation.CONTINUE


def test_session_triggers_autofocus_on_hfr_drift() -> None:
    """25%+ HFR increase over the window should recommend autofocus."""
    session = FrameQualitySession(window_size=6, hfr_drift_fraction=0.20)
    for hfr in [2.0, 2.0, 2.0, 2.5, 2.6, 2.7]:  # ~35% drift
        session.add(_make_result(hfr, 5.0))
    assert session.recommendation() == FrameQualitySession.Recommendation.TRIGGER_AUTOFOCUS


def test_session_stable_hfr_does_not_trigger_autofocus() -> None:
    session = FrameQualitySession(window_size=6, hfr_drift_fraction=0.25)
    for hfr in [2.0, 2.05, 2.1, 2.05, 2.0, 2.08]:  # noise, no trend
        session.add(_make_result(hfr, 5.0))
    assert session.recommendation() != FrameQualitySession.Recommendation.TRIGGER_AUTOFOCUS


def test_session_triggers_sensor_pause_on_hot_pixel_accumulation() -> None:
    """Hot pixel count rising by > accumulation threshold → pause sensor."""
    session = FrameQualitySession(window_size=6, hot_pixel_accumulation=30)
    for hot in [5.0, 6.0, 7.0, 25.0, 40.0, 55.0]:  # sensor heating up
        session.add(_make_result(2.0, hot))
    assert session.recommendation() == FrameQualitySession.Recommendation.PAUSE_SENSOR


def test_session_stable_hot_pixels_no_pause() -> None:
    session = FrameQualitySession(window_size=6, hot_pixel_accumulation=30)
    for hot in [5.0, 6.0, 5.0, 7.0, 6.0, 5.0]:
        session.add(_make_result(2.0, hot))
    assert session.recommendation() != FrameQualitySession.Recommendation.PAUSE_SENSOR


def test_session_triggers_weather_pause_on_star_count_drop() -> None:
    """Significant star-count drop (clouds/fog) should recommend PAUSE_WEATHER."""
    session = FrameQualitySession(
        window_size=6,
        min_stars_for_weather_check=5,
        weather_star_drop_fraction=0.5,
    )
    # Early frames: ~20 stars. Recent frames: ~5 stars (75% drop, exceeds 50% threshold).
    for stars in [20.0, 22.0, 21.0, 8.0, 6.0, 5.0]:
        session.add(_make_result_with_stars(2.0, 5.0, stars))
    assert session.recommendation() == FrameQualitySession.Recommendation.PAUSE_WEATHER


def test_session_stable_star_count_no_weather_pause() -> None:
    """Stable star count should not trigger PAUSE_WEATHER."""
    session = FrameQualitySession(
        window_size=6,
        min_stars_for_weather_check=5,
        weather_star_drop_fraction=0.5,
    )
    for stars in [18.0, 20.0, 19.0, 21.0, 18.0, 20.0]:
        session.add(_make_result_with_stars(2.0, 5.0, stars))
    assert session.recommendation() != FrameQualitySession.Recommendation.PAUSE_WEATHER


def test_session_no_weather_pause_when_early_stars_below_threshold() -> None:
    """PAUSE_WEATHER should not trigger when early star count was below threshold."""
    session = FrameQualitySession(
        window_size=6,
        min_stars_for_weather_check=10,  # threshold higher than early stars
        weather_star_drop_fraction=0.5,
    )
    for stars in [3.0, 2.0, 3.0, 0.0, 0.0, 0.0]:  # always low — could be a lens setup
        session.add(_make_result_with_stars(2.0, 5.0, stars))
    assert session.recommendation() != FrameQualitySession.Recommendation.PAUSE_WEATHER


def test_session_warn_on_latest_fail() -> None:
    session = FrameQualitySession(window_size=6)
    for _ in range(5):
        session.add(_make_result(2.0, 5.0))
    session.add(_make_result(2.0, 5.0, overall=QualityClassification.FAIL))
    assert session.recommendation() == FrameQualitySession.Recommendation.WARN


def test_session_frame_count_property() -> None:
    session = FrameQualitySession(window_size=5)
    assert session.frame_count == 0
    session.add(_make_result(2.0, 5.0))
    session.add(_make_result(2.1, 5.0))
    assert session.frame_count == 2


# ---------------------------------------------------------------------------
# FrameWatcher — basic async behavior
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_watcher_detects_new_frame(tmp_path: Path) -> None:
    """FrameWatcher should yield a result when a new image lands."""
    from kepler_node.imaging.watcher import FrameWatcher

    output_dir = tmp_path / "ekos-output"
    output_dir.mkdir()

    analyzer = _tight_analyzer()
    watcher = FrameWatcher(output_dir, analyzer, poll_interval_seconds=0.05)

    results: list[tuple[Path, object]] = []

    async def _collect() -> None:
        async for path, result in watcher.watch():
            results.append((path, result))
            watcher.stop()

    import asyncio

    task = asyncio.create_task(_collect())
    # Drop a frame after the watcher has started
    await asyncio.sleep(0.1)
    img = _star_field(n_stars=5)
    _save(img, output_dir / "frame001.jpg")
    await asyncio.wait_for(task, timeout=3.0)

    assert len(results) == 1
    assert results[0][0].name == "frame001.jpg"


@pytest.mark.anyio
async def test_watcher_ignores_preexisting_files(tmp_path: Path) -> None:
    """Files present before watching starts should not be yielded."""
    from kepler_node.imaging.watcher import FrameWatcher

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    # Pre-existing file
    img = _star_field(n_stars=4)
    _save(img, output_dir / "old_frame.jpg")

    analyzer = _tight_analyzer()
    watcher = FrameWatcher(output_dir, analyzer, poll_interval_seconds=0.05)
    results: list[tuple[Path, object]] = []

    import asyncio

    async def _collect() -> None:
        async for path, result in watcher.watch():
            results.append((path, result))
            watcher.stop()

    task = asyncio.create_task(_collect())
    await asyncio.sleep(0.1)
    _save(img, output_dir / "new_frame.jpg")
    await asyncio.wait_for(task, timeout=3.0)

    assert all(p.name == "new_frame.jpg" for p, _ in results)


@pytest.mark.anyio
async def test_watcher_retries_failed_analysis_on_next_poll(tmp_path: Path) -> None:
    """A file whose analysis fails on the first poll must be retried on the next.

    Previously the watcher marked all current-snapshot files as seen before
    attempting analysis, so a transient failure (partial write, locked file)
    permanently dropped the frame.  After the fix, only successfully-analyzed
    files are added to the seen set.
    """
    import asyncio
    from unittest.mock import MagicMock

    from kepler_node.imaging.protocols import QualityClassification
    from kepler_node.imaging.watcher import FrameWatcher

    output_dir = tmp_path / "retry_out"
    output_dir.mkdir()

    call_count = [0]
    good_result = MagicMock()
    good_result.overall = QualityClassification.PASS
    good_result.summary = "ok"
    good_result.checks = {}

    def _flaky_analyze(path):
        call_count[0] += 1
        if call_count[0] == 1:
            raise OSError("simulated partial write")
        return good_result

    analyzer = MagicMock()
    analyzer.analyze.side_effect = _flaky_analyze

    watcher = FrameWatcher(output_dir, analyzer, poll_interval_seconds=0.05)
    results: list[tuple] = []

    async def _collect() -> None:
        async for path, result in watcher.watch():
            results.append((path, result))
            watcher.stop()

    task = asyncio.create_task(_collect())
    # Drop the frame after the watcher has taken its initial snapshot
    await asyncio.sleep(0.1)
    img = _star_field(n_stars=5)
    _save(img, output_dir / "partial.jpg")
    await asyncio.wait_for(task, timeout=3.0)

    assert call_count[0] == 2, (
        f"Expected analyze() called twice (fail then succeed), got {call_count[0]}"
    )
    assert len(results) == 1
    assert results[0][0].name == "partial.jpg"


@pytest.mark.anyio
async def test_watcher_retries_load_fail_result_on_next_poll(tmp_path: Path) -> None:
    """A file that returns a load-FAIL result must not be yielded and must be retried.

    The default FrameQualityAnalyzer returns a QualityCheckResult with
    checks["load"] == FAIL when cv2.imread cannot open the file (e.g. partial
    write or corrupt header). That result must *not* be yielded, not added to the
    quality session, not trigger the callback, and must leave the file unseen so
    the next poll cycle retries it after it becomes valid.
    """
    import asyncio

    from kepler_node.imaging.frame_quality import FrameQualityAnalyzer, FrameQualitySession
    from kepler_node.imaging.protocols import QualityClassification
    from kepler_node.imaging.watcher import FrameWatcher

    output_dir = tmp_path / "retry_load_out"
    output_dir.mkdir()

    analyzer = FrameQualityAnalyzer()
    session = FrameQualitySession()
    callback_calls: list[tuple[Path, object]] = []
    watcher = FrameWatcher(
        output_dir,
        analyzer,
        session=session,
        poll_interval_seconds=0.05,
        on_new_frame=lambda p, r: callback_calls.append((p, r)),
    )
    results: list[tuple[Path, object]] = []

    async def _collect() -> None:
        async for path, result in watcher.watch():
            results.append((path, result))
            watcher.stop()

    task = asyncio.create_task(_collect())
    # Wait for the watcher to take its initial snapshot (no files yet)
    await asyncio.sleep(0.1)

    # Write an unreadable / corrupt file — cv2.imread will return None
    bad_file = output_dir / "landing.jpg"
    bad_file.write_bytes(b"not-an-image")

    # Give the watcher several poll cycles to try and fail the load
    await asyncio.sleep(0.3)

    # Nothing must have been yielded, session must be pristine, callback silent
    assert len(results) == 0, f"Load-FAIL must not be yielded; got {len(results)} results"
    assert session.frame_count == 0, (
        f"Load-FAIL must not mutate the quality session; frame_count={session.frame_count}"
    )
    assert len(callback_calls) == 0, (
        f"Load-FAIL must not fire on_new_frame callback; got {len(callback_calls)} calls"
    )

    # Now overwrite with a valid image — watcher must retry and yield
    img = _star_field(n_stars=5)
    _save(img, bad_file)

    await asyncio.wait_for(task, timeout=3.0)

    assert len(results) == 1, (
        f"Expected one result after the file became readable; got {len(results)}"
    )
    assert results[0][0].name == "landing.jpg"
    yielded_result = results[0][1]
    assert yielded_result.checks.get("load") != QualityClassification.FAIL, (
        "Yielded result must not be a load-FAIL"
    )
    assert session.frame_count == 1, "Valid frame must be added to the session"
    assert len(callback_calls) == 1, "Valid frame must fire the callback"
