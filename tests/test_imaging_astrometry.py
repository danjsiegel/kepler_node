"""Tests for AstrometryNetSolverBackend and SolveResult normalization."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kepler_node.imaging.astrometry import AstrometryNetSolverBackend
from kepler_node.imaging.protocols import SolveFailureCategory, SolveResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SOLVE_SUCCESS_OUTPUT = (
    "Field center: (RA,Dec) = (202.469, 47.195) deg.\nField size: 2.5 x 1.6 degrees\n"
)
_FIELD_NOT_SOLVED = "Field not solved.\n"


def _proc(
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> MagicMock:
    proc = MagicMock(spec=subprocess.CompletedProcess)
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


def _make_backend() -> AstrometryNetSolverBackend:
    return AstrometryNetSolverBackend(
        solve_field_bin="solve-field",
        hinted_timeout_seconds=15.0,
        blind_timeout_seconds=45.0,
    )


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_solve_returns_bad_input_frame_when_image_does_not_exist(
    tmp_path: Path,
) -> None:
    backend = _make_backend()
    result = backend.solve(tmp_path / "missing.fits")

    assert result.success is False
    assert result.failure_category == SolveFailureCategory.BAD_INPUT_FRAME
    assert "not found" in (result.confidence_summary or "")


# ---------------------------------------------------------------------------
# Solver unavailable
# ---------------------------------------------------------------------------


def test_solve_returns_solver_unavailable_when_binary_missing(
    tmp_path: Path,
) -> None:
    backend = _make_backend()
    image = tmp_path / "frame.fits"
    image.touch()

    with patch("subprocess.run", side_effect=FileNotFoundError):
        result = backend.solve(image)

    assert result.success is False
    assert result.failure_category == SolveFailureCategory.SOLVER_UNAVAILABLE


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


def test_solve_returns_timeout_on_process_timeout(tmp_path: Path) -> None:
    backend = _make_backend()
    image = tmp_path / "frame.fits"
    image.touch()

    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired("solve-field", 15),
    ):
        result = backend.solve(image)

    assert result.success is False
    assert result.failure_category == SolveFailureCategory.TIMEOUT


# ---------------------------------------------------------------------------
# Failure category mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("output_fragment", "expected_category"),
    [
        ("no sources detected", SolveFailureCategory.NO_STARS_DETECTED),
        ("no stars found", SolveFailureCategory.NO_STARS_DETECTED),
        ("0 sources", SolveFailureCategory.NO_STARS_DETECTED),
        ("time limit exceeded", SolveFailureCategory.TIMEOUT),
        ("did not match", SolveFailureCategory.INDEX_MISSING_OR_NO_MATCH),
        ("no index files", SolveFailureCategory.INDEX_MISSING_OR_NO_MATCH),
        ("could not open file", SolveFailureCategory.BAD_INPUT_FRAME),
        ("unable to read", SolveFailureCategory.BAD_INPUT_FRAME),
        ("something went wrong", SolveFailureCategory.INSUFFICIENT_CONFIDENCE),
    ],
)
def test_classify_failure_maps_output_to_category(
    output_fragment: str,
    expected_category: SolveFailureCategory,
) -> None:
    category = AstrometryNetSolverBackend._classify_failure(f"Field not solved.\n{output_fragment}")
    assert category == expected_category


def test_solve_returns_field_not_solved_failure_category(tmp_path: Path) -> None:
    backend = _make_backend()
    image = tmp_path / "frame.fits"
    image.touch()

    with patch(
        "subprocess.run",
        return_value=_proc(stdout="Field not solved.\nno sources detected", returncode=1),
    ):
        result = backend.solve(image)

    assert result.success is False
    assert result.failure_category == SolveFailureCategory.NO_STARS_DETECTED


# ---------------------------------------------------------------------------
# Successful solve
# ---------------------------------------------------------------------------


def test_solve_returns_normalized_result_on_success(tmp_path: Path) -> None:
    backend = _make_backend()
    image = tmp_path / "frame.fits"
    image.touch()

    with patch(
        "subprocess.run",
        return_value=_proc(stdout=_SOLVE_SUCCESS_OUTPUT, returncode=0),
    ):
        result = backend.solve(
            image,
            expected_ra_hours=13.498,
            expected_dec_deg=47.195,
        )

    assert result.success is True
    assert result.solved_at is not None
    assert result.solved_ra_hours is not None
    assert abs(result.solved_ra_hours - 202.469 / 15.0) < 0.001
    assert result.solved_dec_deg is not None
    assert abs(result.solved_dec_deg - 47.195) < 0.001
    assert result.residual_arcmin is not None
    assert result.failure_category is None
    assert result.confidence_summary == "solved"


def test_solve_result_has_no_residual_when_no_expected_target(
    tmp_path: Path,
) -> None:
    backend = _make_backend()
    image = tmp_path / "frame.fits"
    image.touch()

    with patch(
        "subprocess.run",
        return_value=_proc(stdout=_SOLVE_SUCCESS_OUTPUT, returncode=0),
    ):
        result = backend.solve(image)

    assert result.success is True
    assert result.residual_arcmin is None


def test_solve_passes_ra_dec_hints_as_args(tmp_path: Path) -> None:
    backend = _make_backend()
    image = tmp_path / "frame.fits"
    image.touch()
    captured_args: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        captured_args.append(cmd)
        return _proc(stdout=_SOLVE_SUCCESS_OUTPUT, returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        backend.solve(image, expected_ra_hours=13.498, expected_dec_deg=47.195)

    assert captured_args
    flat = [a for cmd in captured_args for a in cmd]
    assert "--ra" in flat
    assert "--dec" in flat


def test_blind_solve_omits_ra_dec_args_and_uses_longer_timeout(
    tmp_path: Path,
) -> None:
    backend = _make_backend()
    image = tmp_path / "frame.fits"
    image.touch()
    captured_args: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        captured_args.append(cmd)
        return _proc(stdout=_SOLVE_SUCCESS_OUTPUT, returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        backend.solve(
            image,
            expected_ra_hours=13.498,
            expected_dec_deg=47.195,
            blind=True,
        )

    flat = [a for cmd in captured_args for a in cmd]
    assert "--ra" not in flat
    assert "--dec" not in flat
    # blind timeout = 45 seconds
    time_limit_idx = flat.index("--time-limit")
    assert flat[time_limit_idx + 1] == "45"


# ---------------------------------------------------------------------------
# SolveResult contract
# ---------------------------------------------------------------------------


def test_solve_result_model_enforces_v1_contract() -> None:
    result = SolveResult(
        success=True,
        solved_ra_hours=13.5,
        solved_dec_deg=47.2,
        residual_arcmin=0.5,
        confidence_summary="solved",
    )
    assert result.success is True
    assert result.failure_category is None

    failed = SolveResult(
        success=False,
        failure_category=SolveFailureCategory.TIMEOUT,
        confidence_summary="timed out",
    )
    assert failed.solved_ra_hours is None
    assert failed.residual_arcmin is None
