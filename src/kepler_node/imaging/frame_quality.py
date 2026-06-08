"""OpenCV-backed frame quality analyzer for Kepler guardrail logic.

Analyzes astrophoto frames delivered by EKOS to detect focus drift,
tracking errors, sensor heating (hot pixels), and sky transparency
changes — without needing to own the capture loop.
"""

from __future__ import annotations

from collections import deque
from enum import StrEnum
from typing import NamedTuple

import cv2
import numpy as np

try:
    import rawpy
except ImportError:  # pragma: no cover - optional at runtime until deps are synced
    rawpy = None

from kepler_node.imaging.protocols import QualityCheckResult, QualityClassification


class _StarMetrics(NamedTuple):
    hfr: float  # half-flux radius in (downsampled) pixels
    elongation: float  # major/minor axis ratio; 1.0 = circular


class FrameQualityAnalyzer:
    """Analyzes single astrophoto frames for focus, tracking, and sensor health.

    Uses OpenCV star detection on a downsampled copy for speed, then computes:
    - HFR (half-flux radius) as a focus quality proxy
    - Star elongation ratio as a tracking quality proxy
    - Hot pixel count as a sensor temperature health proxy

    Thresholds are configurable at construction time so tests can use tight
    synthetic values without touching the production defaults.
    """

    # Production defaults (full-resolution pixel units after scale-up)
    _DEFAULT_HFR_WARN = 4.0
    _DEFAULT_HFR_FAIL = 7.0
    _DEFAULT_ELONGATION_WARN = 1.5
    _DEFAULT_ELONGATION_FAIL = 2.5
    _DEFAULT_HOT_PIXEL_WARN = 30
    _DEFAULT_HOT_PIXEL_FAIL = 150
    _DEFAULT_MIN_STARS = 3
    _DEFAULT_SCALE = 0.25  # downsample factor for star detection pass

    def __init__(
        self,
        *,
        hfr_warn: float = _DEFAULT_HFR_WARN,
        hfr_fail: float = _DEFAULT_HFR_FAIL,
        elongation_warn: float = _DEFAULT_ELONGATION_WARN,
        elongation_fail: float = _DEFAULT_ELONGATION_FAIL,
        hot_pixel_warn: int = _DEFAULT_HOT_PIXEL_WARN,
        hot_pixel_fail: int = _DEFAULT_HOT_PIXEL_FAIL,
        min_stars_for_hfr: int = _DEFAULT_MIN_STARS,
        analysis_scale: float = _DEFAULT_SCALE,
    ) -> None:
        self._hfr_warn = hfr_warn
        self._hfr_fail = hfr_fail
        self._elongation_warn = elongation_warn
        self._elongation_fail = elongation_fail
        self._hot_pixel_warn = hot_pixel_warn
        self._hot_pixel_fail = hot_pixel_fail
        self._min_stars = min_stars_for_hfr
        self._scale = analysis_scale

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def analyze(self, image_path: object) -> QualityCheckResult:
        """Load and analyze a frame. Returns a QualityCheckResult.

        Accepts any path-like object. Returns FAIL if the image cannot be
        loaded rather than raising.
        """
        from pathlib import Path

        path = Path(str(image_path))
        gray = self._load_gray(path)
        if gray is None:
            return QualityCheckResult(
                overall=QualityClassification.FAIL,
                checks={"load": QualityClassification.FAIL},
                metrics={},
                summary=f"could not load image: {path.name}",
            )

        scale = self._scale
        small = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

        stars = self._detect_stars(small)
        hot_pixel_count = self._count_hot_pixels(gray)
        background_adu = float(np.median(gray))

        checks: dict[str, QualityClassification] = {}
        metrics: dict[str, float] = {
            "star_count": float(len(stars)),
            "hot_pixel_count": float(hot_pixel_count),
            "background_adu": background_adu,
        }

        # Focus quality via HFR (scale pixel measurements back to full-res)
        if len(stars) >= self._min_stars:
            hfr_scaled = float(np.mean([s.hfr for s in stars]))
            hfr_full = hfr_scaled / scale
            metrics["hfr_mean"] = hfr_full
            checks["focus"] = _classify_high_bad(hfr_full, self._hfr_warn, self._hfr_fail)
        else:
            metrics["hfr_mean"] = 0.0
            checks["focus"] = QualityClassification.WARN

        # Tracking quality via elongation
        if stars:
            elong = float(np.mean([s.elongation for s in stars]))
            metrics["elongation_mean"] = elong
            checks["tracking"] = _classify_high_bad(
                elong, self._elongation_warn, self._elongation_fail
            )
        else:
            checks["tracking"] = QualityClassification.WARN

        # Sensor health via hot pixel count
        checks["sensor_health"] = _classify_high_bad(
            float(hot_pixel_count), self._hot_pixel_warn, self._hot_pixel_fail
        )

        all_checks = list(checks.values())
        if QualityClassification.FAIL in all_checks:
            overall = QualityClassification.FAIL
        elif QualityClassification.WARN in all_checks:
            overall = QualityClassification.WARN
        else:
            overall = QualityClassification.PASS

        return QualityCheckResult(
            overall=overall,
            checks=checks,
            metrics=metrics,
            summary=_build_summary(checks, metrics),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _load_gray(path: object) -> np.ndarray | None:
        """Load image as uint8 grayscale. Returns None on any failure."""
        img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if img is not None:
            return img
        # Try 16-bit (TIFF from RAW converters)
        img16 = cv2.imread(str(path), cv2.IMREAD_ANYDEPTH | cv2.IMREAD_GRAYSCALE)
        if img16 is not None:
            return cv2.normalize(img16, None, 0, 255, cv2.NORM_MINMAX, dtype=cv2.CV_8U)
        suffix = path.suffix.lower() if hasattr(path, "suffix") else ""
        if suffix in {".raf", ".raw"} and rawpy is not None:
            try:
                with rawpy.imread(str(path)) as raw:
                    gray = raw.raw_image_visible.astype(np.float32)
                return cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX, dtype=cv2.CV_8U)
            except (rawpy.LibRawError, OSError, ValueError):
                return None
        return None

    def _detect_stars(self, gray: np.ndarray) -> list[_StarMetrics]:
        """Detect stars in a (typically downsampled) grayscale image."""
        bg = float(np.median(gray))
        bg_std = float(np.std(gray))

        # Threshold: bg + 5σ, capped to avoid saturated-star pollution
        thresh_val = min(bg + 5.0 * bg_std, 240.0)
        _, binary = cv2.threshold(gray.astype(np.float32), thresh_val, 255.0, cv2.THRESH_BINARY)
        binary = binary.astype(np.uint8)

        # Morphological open removes single-pixel noise hits
        kernel = np.ones((2, 2), np.uint8)
        binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)

        contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        stars: list[_StarMetrics] = []
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < 4 or area > 800:  # skip noise specks and bloated blobs
                continue

            M = cv2.moments(cnt)
            if M["m00"] == 0:
                continue
            cx = M["m10"] / M["m00"]
            cy = M["m01"] / M["m00"]

            hfr = _compute_hfr(gray, cx, cy, bg)

            elongation = 1.0
            if len(cnt) >= 5:
                try:
                    _, (ma, mb), _ = cv2.fitEllipse(cnt)
                    minor = min(ma, mb)
                    if minor > 1e-6:
                        elongation = max(ma, mb) / minor
                except cv2.error:
                    pass

            stars.append(_StarMetrics(hfr=hfr, elongation=elongation))

        return stars

    @staticmethod
    def _count_hot_pixels(gray: np.ndarray, *, detection_sigma: float = 5.0) -> int:
        """Count isolated bright pixels using morphological opening.

        Thresholds bright sources (stars + hot pixels), then applies a 3×3
        morphological open which preserves extended stellar blobs but erases
        isolated single-pixel spikes. Hot pixels = bright pixels eliminated
        by the opening.

        Subsamples large images (> 2 MP) to stay fast on the Pi.
        """
        sample = gray[::2, ::2] if gray.size > 2_000_000 else gray
        bg = float(np.median(sample))
        std = float(np.std(sample))
        if std < 0.5:  # essentially uniform image — no hot pixels
            return 0
        # Threshold bright sources; ensure minimum headroom above background
        thresh_val = float(min(max(bg + detection_sigma * std, bg + 20.0), 250.0))
        _, binary = cv2.threshold(sample, thresh_val, 255, cv2.THRESH_BINARY)
        binary = binary.astype(np.uint8)
        # Opening (erode then dilate) removes isolated pixels; stars survive
        kernel = np.ones((3, 3), np.uint8)
        opened = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
        # Hot pixels: caught by threshold but erased by opening
        return int(np.sum((binary > 0) & (opened == 0)))


# ---------------------------------------------------------------------------
# Module-level helpers (no state, no self)
# ---------------------------------------------------------------------------


def _compute_hfr(gray: np.ndarray, cx: float, cy: float, background: float) -> float:
    """Compute half-flux radius for a star centroid within a 40px window."""
    h, w = gray.shape
    r = 20
    x0, x1 = max(0, int(cx) - r), min(w, int(cx) + r + 1)
    y0, y1 = max(0, int(cy) - r), min(h, int(cy) + r + 1)

    patch = np.clip(gray[y0:y1, x0:x1].astype(np.float32) - background, 0, None)
    total_flux = float(patch.sum())
    if total_flux <= 0:
        return 0.0

    ys, xs = np.indices(patch.shape)
    dists = np.sqrt((xs + x0 - cx) ** 2 + (ys + y0 - cy) ** 2).ravel()
    order = np.argsort(dists)
    cum_flux = np.cumsum(patch.ravel()[order])

    idx = int(np.searchsorted(cum_flux, total_flux * 0.5))
    return float(dists[order[min(idx, len(dists) - 1)]])


def _classify_high_bad(
    value: float, warn_threshold: float, fail_threshold: float
) -> QualityClassification:
    """PASS when low, WARN when approaching threshold, FAIL when exceeded."""
    if value >= fail_threshold:
        return QualityClassification.FAIL
    if value >= warn_threshold:
        return QualityClassification.WARN
    return QualityClassification.PASS


def _build_summary(checks: dict[str, QualityClassification], metrics: dict[str, float]) -> str:
    issues = [k for k, v in checks.items() if v != QualityClassification.PASS]
    stars = int(metrics.get("star_count", 0))
    hfr = metrics.get("hfr_mean", 0.0)
    if not issues:
        return f"pass — {stars} stars, HFR {hfr:.1f}px"
    parts: list[str] = []
    for k in issues:
        cls = checks[k].value
        if k == "focus":
            parts.append(f"focus {cls} HFR={hfr:.1f}px")
        elif k == "tracking":
            parts.append(f"tracking {cls} elong={metrics.get('elongation_mean', 0):.2f}")
        elif k == "sensor_health":
            parts.append(f"sensor_health {cls} hot_px={int(metrics.get('hot_pixel_count', 0))}")
        else:
            parts.append(f"{k} {cls}")
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# Session-level quality trend tracker
# ---------------------------------------------------------------------------


class FrameQualitySession:
    """Tracks frame quality across a session to detect drift and recommend action.

    Maintains a rolling window of QualityCheckResult values. After enough
    frames accumulate, detects:
    - HFR drift (focus softening over time → suggest autofocus)
    - Hot pixel accumulation (sensor heating → suggest sensor break)
    - Sudden quality collapse (clouds, tracking loss)

    Usage::

        session = FrameQualitySession()
        for path in new_frames:
            result = analyzer.analyze(path)
            session.add(result)
            rec = session.recommendation()
            if rec == FrameQualitySession.Recommendation.TRIGGER_AUTOFOCUS:
                ekos.run_autofocus()
    """

    class Recommendation(StrEnum):
        CONTINUE = "continue"
        WARN = "warn"
        TRIGGER_AUTOFOCUS = "trigger_autofocus"
        PAUSE_SENSOR = "pause_sensor"  # let sensor cool
        PAUSE_WEATHER = "pause_weather"  # clouds / transparency

    def __init__(
        self,
        *,
        window_size: int = 10,
        hfr_drift_fraction: float = 0.25,  # 25% HFR increase triggers autofocus
        hot_pixel_accumulation: int = 50,  # delta count over window triggers sensor pause
        min_stars_for_weather_check: int = 5,  # minimum early stars to trust as transparency proxy
        weather_star_drop_fraction: float = 0.5,  # 50% drop in star count triggers weather pause
    ) -> None:
        self._window: deque[QualityCheckResult] = deque(maxlen=window_size)
        self._hfr_drift_fraction = hfr_drift_fraction
        self._hot_pixel_accumulation = hot_pixel_accumulation
        self._min_stars_for_weather_check = min_stars_for_weather_check
        self._weather_star_drop_fraction = weather_star_drop_fraction

    def add(self, result: QualityCheckResult) -> None:
        """Record a new frame's quality result."""
        self._window.append(result)

    def recommendation(self) -> Recommendation:
        """Return an action recommendation based on accumulated frame history."""
        if len(self._window) < 3:
            return self.Recommendation.CONTINUE

        frames = list(self._window)

        # Hot pixel accumulation: sensor temperature rising
        hot_counts = [r.metrics.get("hot_pixel_count", 0.0) for r in frames]
        recent_hot = float(np.mean(hot_counts[-3:]))
        early_hot = float(np.mean(hot_counts[:3]))
        if recent_hot > early_hot + self._hot_pixel_accumulation:
            return self.Recommendation.PAUSE_SENSOR

        # Transparency: significant star-count drop suggests clouds or fog
        star_counts = [r.metrics.get("star_count", 0.0) for r in frames]
        recent_stars = float(np.mean(star_counts[-3:]))
        early_stars = float(np.mean(star_counts[:3]))
        if early_stars >= self._min_stars_for_weather_check and recent_stars < early_stars * (
            1.0 - self._weather_star_drop_fraction
        ):
            return self.Recommendation.PAUSE_WEATHER

        # HFR drift: focus softening
        hfrs = [r.metrics["hfr_mean"] for r in frames if r.metrics.get("hfr_mean", 0) > 0]
        if len(hfrs) >= 3:
            recent_hfr = float(np.mean(hfrs[-3:]))
            early_hfr = float(np.mean(hfrs[:3]))
            if early_hfr > 0 and recent_hfr > early_hfr * (1 + self._hfr_drift_fraction):
                return self.Recommendation.TRIGGER_AUTOFOCUS

        # Latest frame failed hard
        latest = frames[-1]
        if latest.overall == QualityClassification.FAIL:
            return self.Recommendation.WARN

        return self.Recommendation.CONTINUE

    @property
    def frame_count(self) -> int:
        return len(self._window)
