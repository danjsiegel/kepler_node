from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import re

import cv2
import numpy as np
import pytest

from kepler_node.camera import fuji_focus_assist
from kepler_node.camera.fuji_focus_assist import (
    FocusAssistRequest,
    FocusAssistSample,
    FujiFocusAssistRunner,
    MilkyWaySequenceRequest,
    run_milky_way_sequence,
)
from kepler_node.camera.protocols import CameraSettings, CaptureRequest, CaptureResult


class FakeFocusCamera:
    def __init__(self, *, start_raw: int = 400, best_raw: int = 440) -> None:
        self.current_raw = start_raw
        self.best_raw = best_raw
        self.capture_calls: list[CaptureRequest] = []

    def connect(self) -> None:
        return None

    def disconnect(self) -> None:
        return None

    def read_focus_position_raw(self) -> int:
        return self.current_raw

    def set_focus_position_raw(self, raw_value: int) -> int:
        self.current_raw = raw_value
        return self.current_raw

    def capture(self, request: CaptureRequest) -> CaptureResult:
        self.capture_calls.append(request)
        request.destination_dir.mkdir(parents=True, exist_ok=True)
        image_path = request.destination_dir / f"{request.frame_label}.png"
        img = np.zeros((800, 1200), dtype=np.uint8)
        radius = max(2, min(18, abs(self.current_raw - self.best_raw) // 20 + 2))
        for center in ((300, 300), (600, 500), (900, 320), (700, 650), (450, 550)):
            cv2.circle(img, center, radius, 255, -1)
        cv2.imwrite(str(image_path), img)
        return CaptureResult(image_path=image_path, captured_at=datetime.now(UTC))

    def capture_preview(self, request: CaptureRequest) -> CaptureResult:
        return self.capture(request)


def test_focus_assist_runner_moves_to_better_focus_position(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    camera = FakeFocusCamera(start_raw=400, best_raw=440)
    runner = FujiFocusAssistRunner(camera)

    def fake_score(image_path: Path, analyzer: object | None = None) -> FocusAssistSample:
        match = re.search(r"raw-(-?\d+)", image_path.stem)
        assert match is not None
        raw_position = int(match.group(1))
        distance = abs(raw_position - camera.best_raw)
        if distance <= 10:
            return FocusAssistSample(
                raw_position=raw_position,
                image_path=image_path,
                star_count=12,
                hfr_mean=1.5,
                tenengrad=5000.0,
                metric_source="hfr",
                summary="stars=12 hfr=1.50",
            )
        return FocusAssistSample(
            raw_position=raw_position,
            image_path=image_path,
            star_count=8,
            hfr_mean=1.5 + distance / 100.0,
            tenengrad=5000.0 - distance,
            metric_source="hfr",
            summary=f"stars=8 hfr={1.5 + distance / 100.0:.2f}",
        )

    monkeypatch.setattr(fuji_focus_assist, "score_focus_frame", fake_score)

    result = runner.run(
        FocusAssistRequest(
            destination_dir=tmp_path,
            exposure_seconds=1.0,
            iso=3200,
            focus_min_raw=45,
            focus_max_raw=1497,
            coarse_step=40,
            fine_step=10,
        )
    )

    assert result.status == "success"
    assert abs(result.best_raw - 440) <= 10
    assert abs(result.final_raw - 440) <= 10
    assert result.coarse_samples
    assert result.fine_samples


def test_milky_way_sequence_captures_requested_frame_count(tmp_path: Path) -> None:
    camera = FakeFocusCamera()

    frames = run_milky_way_sequence(
        camera,
        MilkyWaySequenceRequest(
            destination_dir=tmp_path,
            exposure_seconds=8.0,
            iso=1600,
            aperture=2.8,
            frame_count=3,
        ),
    )

    assert len(frames) == 3
    assert len(camera.capture_calls) == 3
    assert frames[0].image_path.exists()