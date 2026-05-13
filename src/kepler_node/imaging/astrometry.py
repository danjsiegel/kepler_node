"""Local astrometry.net solver adapter for Kepler v1."""

from __future__ import annotations

import math
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from kepler_node.imaging.protocols import SolveFailureCategory, SolveResult


class AstrometryNetSolverBackend:
    """Subprocess-backed local astrometry.net solver.

    Normalizes ``solve-field`` output into the v1 ``SolveResult`` payload,
    mapping all accepted failure categories. Blind-solve retry policy and
    confidence degradation decisions remain in Claw.
    """

    def __init__(
        self,
        *,
        solve_field_bin: str = "solve-field",
        hinted_timeout_seconds: float = 15.0,
        blind_timeout_seconds: float = 45.0,
        index_path: Path | None = None,
    ) -> None:
        self._solve_field_bin = solve_field_bin
        self._hinted_timeout_seconds = hinted_timeout_seconds
        self._blind_timeout_seconds = blind_timeout_seconds
        self._index_path = index_path

    def solve(
        self,
        image_path: Path,
        *,
        expected_ra_hours: float | None = None,
        expected_dec_deg: float | None = None,
        blind: bool = False,
    ) -> SolveResult:
        """Run solve-field and normalize the result for orchestration logic."""
        if not image_path.exists():
            return SolveResult(
                success=False,
                failure_category=SolveFailureCategory.BAD_INPUT_FRAME,
                confidence_summary="input image file not found",
                provider_details={"image_path": str(image_path)},
            )

        timeout = self._blind_timeout_seconds if blind else self._hinted_timeout_seconds

        with tempfile.TemporaryDirectory() as tmp_dir:
            args = self._build_args(
                image_path,
                tmp_dir=tmp_dir,
                timeout=timeout,
                expected_ra_hours=expected_ra_hours,
                expected_dec_deg=expected_dec_deg,
                blind=blind,
            )

            try:
                result = subprocess.run(
                    args,
                    capture_output=True,
                    text=True,
                    timeout=timeout + 10,
                )
            except subprocess.TimeoutExpired:
                return SolveResult(
                    success=False,
                    failure_category=SolveFailureCategory.TIMEOUT,
                    confidence_summary="solve-field process timed out",
                    provider_details={"timeout_seconds": str(timeout)},
                )
            except FileNotFoundError:
                return SolveResult(
                    success=False,
                    failure_category=SolveFailureCategory.SOLVER_UNAVAILABLE,
                    confidence_summary="solve-field binary not found",
                    provider_details={"binary": self._solve_field_bin},
                )

            return self._parse_result(
                result,
                expected_ra_hours=expected_ra_hours,
                expected_dec_deg=expected_dec_deg,
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_args(
        self,
        image_path: Path,
        *,
        tmp_dir: str,
        timeout: float,
        expected_ra_hours: float | None,
        expected_dec_deg: float | None,
        blind: bool,
    ) -> list[str]:
        args = [
            self._solve_field_bin,
            "--no-plots",
            "--overwrite",
            "--dir",
            tmp_dir,
            "--new-fits",
            "none",
            "--temp-axy",
            "--time-limit",
            str(int(timeout)),
        ]
        if not blind and expected_ra_hours is not None and expected_dec_deg is not None:
            ra_deg = expected_ra_hours * 15.0
            args += [
                "--ra",
                str(ra_deg),
                "--dec",
                str(expected_dec_deg),
                "--radius",
                "15",
            ]
        if self._index_path is not None:
            args += ["--index-path", str(self._index_path)]
        args.append(str(image_path))
        return args

    def _parse_result(
        self,
        result: subprocess.CompletedProcess[str],
        *,
        expected_ra_hours: float | None,
        expected_dec_deg: float | None,
    ) -> SolveResult:
        output = result.stdout + result.stderr

        if "Field not solved" in output or result.returncode != 0:
            category = self._classify_failure(output)
            return SolveResult(
                success=False,
                failure_category=category,
                confidence_summary=f"solve failed: {category}",
                provider_details={"returncode": str(result.returncode)},
            )

        solved_ra_deg, solved_dec_deg = self._extract_field_center(output)
        if solved_ra_deg is None:
            return SolveResult(
                success=False,
                failure_category=SolveFailureCategory.INSUFFICIENT_CONFIDENCE,
                confidence_summary="field center not parseable from solver output",
                provider_details={"output_tail": output[-500:]},
            )

        solved_ra_hours = solved_ra_deg / 15.0
        residual_arcmin = self._compute_residual(
            solved_ra_deg=solved_ra_deg,
            solved_dec_deg=solved_dec_deg,
            expected_ra_hours=expected_ra_hours,
            expected_dec_deg=expected_dec_deg,
        )

        return SolveResult(
            success=True,
            solved_at=datetime.now(UTC),
            solved_ra_hours=solved_ra_hours,
            solved_dec_deg=solved_dec_deg,
            residual_arcmin=residual_arcmin,
            confidence_summary="solved",
            provider_details={"returncode": str(result.returncode)},
        )

    @staticmethod
    def _extract_field_center(output: str) -> tuple[float | None, float | None]:
        """Parse RA/Dec from solve-field stdout line."""
        for line in output.splitlines():
            if "Field center: (RA,Dec) = (" in line:
                try:
                    # Use rfind to locate the last parenthesised coordinate pair.
                    start = line.rfind("(") + 1
                    end = line.rfind(")")
                    inner = line[start:end]
                    ra_str, dec_str = inner.split(",", 1)
                    return float(ra_str.strip()), float(dec_str.strip())
                except (IndexError, ValueError):
                    pass
        return None, None

    @staticmethod
    def _compute_residual(
        *,
        solved_ra_deg: float,
        solved_dec_deg: float,
        expected_ra_hours: float | None,
        expected_dec_deg: float | None,
    ) -> float | None:
        """Compute total residual offset in arcminutes from expected target."""
        if expected_ra_hours is None or expected_dec_deg is None:
            return None
        expected_ra_deg = expected_ra_hours * 15.0
        cos_dec = math.cos(math.radians(expected_dec_deg))
        delta_ra_deg = (solved_ra_deg - expected_ra_deg) * cos_dec
        delta_dec_deg = solved_dec_deg - expected_dec_deg
        return math.sqrt(delta_ra_deg**2 + delta_dec_deg**2) * 60.0

    @staticmethod
    def _classify_failure(output: str) -> SolveFailureCategory:
        """Map solve-field output text to the accepted v1 failure category."""
        lower = output.lower()
        if any(tok in lower for tok in ("no sources", "no stars", "0 sources")):
            return SolveFailureCategory.NO_STARS_DETECTED
        if any(tok in lower for tok in ("time limit", "timed out")):
            return SolveFailureCategory.TIMEOUT
        if any(tok in lower for tok in ("no index", "did not match", "no match")):
            return SolveFailureCategory.INDEX_MISSING_OR_NO_MATCH
        if any(tok in lower for tok in ("could not open", "invalid", "unable to read")):
            return SolveFailureCategory.BAD_INPUT_FRAME
        return SolveFailureCategory.INSUFFICIENT_CONFIDENCE
