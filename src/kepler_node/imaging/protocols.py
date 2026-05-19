"""Imaging and solver contracts for Kepler v1."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field


class SolveFailureCategory(StrEnum):
    """Accepted v1 solve failure categories."""

    NO_STARS_DETECTED = "no_stars_detected"
    INSUFFICIENT_CONFIDENCE = "insufficient_confidence"
    TIMEOUT = "timeout"
    INDEX_MISSING_OR_NO_MATCH = "index_missing_or_no_match"
    BAD_INPUT_FRAME = "bad_input_frame"
    SOLVER_UNAVAILABLE = "solver_unavailable"


class QualityClassification(StrEnum):
    """Allowed v1 quality classifications for checks and overall status."""

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


class SolveResult(BaseModel):
    """Normalized solve result payload for orchestration decisions."""

    success: bool
    solved_at: datetime | None = None
    solved_ra_hours: float | None = None
    solved_dec_deg: float | None = None
    residual_arcmin: float | None = None
    confidence_summary: str | None = None
    failure_category: SolveFailureCategory | None = None
    provider_details: dict[str, Any] = Field(default_factory=dict)


class QualityCheckResult(BaseModel):
    """Normalized quality-classification output for a frame."""

    overall: QualityClassification
    checks: dict[str, QualityClassification] = Field(default_factory=dict)
    metrics: dict[str, float] = Field(default_factory=dict)
    summary: str | None = None


class QualityAnalyzer(Protocol):
    """Frame quality analyzer contract used by Kepler guardrail logic."""

    def analyze(self, image_path: Path) -> QualityCheckResult:
        """Analyze a single frame and return a quality classification."""


class SolverBackend(Protocol):
    """Solver contract used by Kepler orchestration."""

    def solve(
        self,
        image_path: Path,
        *,
        expected_ra_hours: float | None = None,
        expected_dec_deg: float | None = None,
        blind: bool = False,
    ) -> SolveResult:
        """Solve a frame and normalize the result for orchestration logic."""
