"""Pure Kepler Fuji focus-assist and local Milky Way capture helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Protocol

import cv2
import numpy as np

from kepler_node.camera.protocols import CameraSettings, CaptureRequest, CaptureResult
from kepler_node.imaging.frame_quality import FrameQualityAnalyzer


class FocusAssistCamera(Protocol):
    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def capture(self, request: CaptureRequest) -> CaptureResult: ...

    def capture_preview(self, request: CaptureRequest) -> CaptureResult: ...

    def read_focus_position_raw(self) -> int: ...

    def set_focus_position_raw(self, raw_value: int) -> int: ...


@dataclass(slots=True)
class FocusAssistRequest:
    destination_dir: Path
    exposure_seconds: float
    iso: int
    aperture: float | None = None
    focus_min_raw: int = 45
    focus_max_raw: int = 1497
    coarse_step: int = 40
    fine_step: int = 10
    min_improvement_fraction: float = 0.05


@dataclass(slots=True)
class FocusAssistSample:
    raw_position: int
    image_path: Path
    star_count: int
    hfr_mean: float | None
    tenengrad: float
    metric_source: str
    summary: str


@dataclass(slots=True)
class FocusAssistResult:
    status: str
    started_raw: int
    best_raw: int
    final_raw: int
    coarse_samples: list[FocusAssistSample]
    fine_samples: list[FocusAssistSample]
    summary: str


@dataclass(slots=True)
class MilkyWaySequenceRequest:
    destination_dir: Path
    exposure_seconds: float
    iso: int
    aperture: float | None = None
    frame_count: int = 20
    inter_frame_delay_seconds: float = 1.0


def _center_crop(gray: np.ndarray, crop_fraction: float = 0.5) -> np.ndarray:
    height, width = gray.shape
    crop_width = max(32, int(width * crop_fraction))
    crop_height = max(32, int(height * crop_fraction))
    x0 = max(0, (width - crop_width) // 2)
    y0 = max(0, (height - crop_height) // 2)
    return gray[y0 : y0 + crop_height, x0 : x0 + crop_width]


def _tenengrad(gray: np.ndarray) -> float:
    grad_x = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    grad_y = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    return float(np.mean(grad_x * grad_x + grad_y * grad_y))


def score_focus_frame(
    image_path: Path,
    analyzer: FrameQualityAnalyzer | None = None,
) -> FocusAssistSample:
    analyzer = analyzer or FrameQualityAnalyzer()
    gray = analyzer._load_gray(image_path)
    if gray is None:
        raise RuntimeError(f"could not load focus frame: {image_path}")

    cropped = _center_crop(gray)
    small = cv2.resize(
        cropped,
        None,
        fx=analyzer._scale,
        fy=analyzer._scale,
        interpolation=cv2.INTER_AREA,
    )
    stars = analyzer._detect_stars(small)
    star_count = len(stars)
    tenengrad = _tenengrad(cropped)
    hfr_mean: float | None = None
    metric_source = "tenengrad"
    if star_count >= analyzer._min_stars:
        hfr_scaled = float(np.median([star.hfr for star in stars]))
        hfr_mean = hfr_scaled / analyzer._scale
        metric_source = "hfr"

    return FocusAssistSample(
        raw_position=0,
        image_path=image_path,
        star_count=star_count,
        hfr_mean=hfr_mean,
        tenengrad=tenengrad,
        metric_source=metric_source,
        summary=(
            f"stars={star_count} hfr={hfr_mean:.2f}" if hfr_mean is not None else f"stars={star_count} tenengrad={tenengrad:.2f}"
        ),
    )


def _sample_sort_key(sample: FocusAssistSample) -> tuple[float, float, float, float]:
    if sample.hfr_mean is not None:
        return (0.0, sample.hfr_mean, -float(sample.star_count), -sample.tenengrad)
    return (1.0, -sample.tenengrad, -float(sample.star_count), 0.0)


def _unique_positions(positions: list[int], minimum: int, maximum: int) -> list[int]:
    seen: set[int] = set()
    ordered: list[int] = []
    for position in positions:
        clamped = min(max(position, minimum), maximum)
        if clamped in seen:
            continue
        seen.add(clamped)
        ordered.append(clamped)
    return ordered


class FujiFocusAssistRunner:
    def __init__(
        self,
        camera: FocusAssistCamera,
        *,
        analyzer: FrameQualityAnalyzer | None = None,
    ) -> None:
        self._camera = camera
        self._analyzer = analyzer or FrameQualityAnalyzer()

    def run(self, request: FocusAssistRequest) -> FocusAssistResult:
        request.destination_dir.mkdir(parents=True, exist_ok=True)
        started_raw = self._camera.read_focus_position_raw()
        coarse_positions = _unique_positions(
            [
                started_raw - 2 * request.coarse_step,
                started_raw - request.coarse_step,
                started_raw,
                started_raw + request.coarse_step,
                started_raw + 2 * request.coarse_step,
            ],
            request.focus_min_raw,
            request.focus_max_raw,
        )
        coarse_samples = [self._capture_sample(raw, request, phase="coarse") for raw in coarse_positions]
        best_coarse = min(coarse_samples, key=_sample_sort_key)

        fine_samples: list[FocusAssistSample] = []
        if request.fine_step > 0:
            fine_positions = _unique_positions(
                [
                    best_coarse.raw_position - 2 * request.fine_step,
                    best_coarse.raw_position - request.fine_step,
                    best_coarse.raw_position,
                    best_coarse.raw_position + request.fine_step,
                    best_coarse.raw_position + 2 * request.fine_step,
                ],
                request.focus_min_raw,
                request.focus_max_raw,
            )
            fine_samples = [self._capture_sample(raw, request, phase="fine") for raw in fine_positions]
            best_sample = min(fine_samples, key=_sample_sort_key)
        else:
            best_sample = best_coarse

        final_raw = self._camera.set_focus_position_raw(best_sample.raw_position)
        baseline = next(sample for sample in coarse_samples if sample.raw_position == started_raw)
        improved = self._is_improved(baseline, best_sample, request.min_improvement_fraction)
        status = "success" if improved else "inconclusive"
        summary = (
            f"{status}: start={started_raw} best={best_sample.raw_position} final={final_raw}; "
            f"baseline={baseline.summary}; best={best_sample.summary}"
        )
        return FocusAssistResult(
            status=status,
            started_raw=started_raw,
            best_raw=best_sample.raw_position,
            final_raw=final_raw,
            coarse_samples=coarse_samples,
            fine_samples=fine_samples,
            summary=summary,
        )

    def _capture_sample(
        self,
        raw_position: int,
        request: FocusAssistRequest,
        *,
        phase: str,
    ) -> FocusAssistSample:
        settled = self._camera.set_focus_position_raw(raw_position)
        capture_request = CaptureRequest(
            exposure_seconds=request.exposure_seconds,
            settings=CameraSettings(iso=request.iso, aperture=request.aperture),
            destination_dir=request.destination_dir,
            frame_label=f"{phase}-raw-{settled}",
        )
        if hasattr(self._camera, "capture_preview"):
            result = self._camera.capture_preview(capture_request)
        else:
            result = self._camera.capture(capture_request)
        scored = score_focus_frame(result.image_path, self._analyzer)
        scored.raw_position = settled
        return scored

    @staticmethod
    def _is_improved(
        baseline: FocusAssistSample,
        candidate: FocusAssistSample,
        minimum_fraction: float,
    ) -> bool:
        if baseline.hfr_mean is not None and candidate.hfr_mean is not None:
            return candidate.hfr_mean <= baseline.hfr_mean * (1.0 - minimum_fraction)
        if baseline.hfr_mean is None and candidate.hfr_mean is not None:
            return True
        if baseline.tenengrad <= 0:
            return False
        return candidate.tenengrad >= baseline.tenengrad * (1.0 + minimum_fraction)


def run_milky_way_sequence(
    camera: FocusAssistCamera,
    request: MilkyWaySequenceRequest,
) -> list[CaptureResult]:
    request.destination_dir.mkdir(parents=True, exist_ok=True)
    settings = CameraSettings(iso=request.iso, aperture=request.aperture)
    frames: list[CaptureResult] = []
    for index in range(1, request.frame_count + 1):
        frames.append(
            camera.capture(
                CaptureRequest(
                    exposure_seconds=request.exposure_seconds,
                    settings=settings,
                    destination_dir=request.destination_dir,
                    frame_label=f"milky-way-{index:03d}",
                )
            )
        )
        if index < request.frame_count and request.inter_frame_delay_seconds > 0:
            time.sleep(request.inter_frame_delay_seconds)
    return frames