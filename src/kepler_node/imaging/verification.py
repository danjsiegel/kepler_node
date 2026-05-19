"""Independent verification solve helpers for Kepler v1.1.

These helpers wrap the primary solver backend to provide audit-and-recovery
confidence solves.  They are *not* a replacement for the KStars/Ekos primary
solve-center-capture loop.

Accepted v1.1 posture:
- Ekos remains the normal execution engine for capture, solve, and centering.
- Kepler may independently solve selected frames when it needs to audit framing,
  validate recovery, or explain why it is pausing.
- Independent verification is measurement and policy input.

Usage::

    helper = VerificationSolveHelper(solver_backend, reason="post_intervention")
    result = helper.solve_for_verification(
        image_path,
        expected_ra_hours=ra,
        expected_dec_deg=dec,
    )
    if result.success:
        offset_arcmin = result.residual_arcmin
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from kepler_node.imaging.protocols import SolveResult, SolverBackend

_logger = logging.getLogger(__name__)

# Residual threshold above which the pointing confidence is degraded.
_RESIDUAL_WARN_ARCMIN = 5.0
_RESIDUAL_FAIL_ARCMIN = 15.0


class VerificationSolveResult:
    """Extended solve result carrying audit context for intervention decisions.

    Attributes:
        solve_result:    The raw normalized ``SolveResult`` from the backend.
        reason:          Why this verification solve was requested.
        solved_at:       When the solve completed.
        offset_arcmin:   Residual from expected pointing (None when unknown).
        confidence:      Human-readable confidence summary for operator surfaces.
        safe_to_resume:  Whether the pointing confidence is high enough to
                         resume normal capture.
    """

    __slots__ = (
        "solve_result",
        "reason",
        "solved_at",
        "offset_arcmin",
        "confidence",
        "safe_to_resume",
    )

    def __init__(
        self,
        solve_result: SolveResult,
        *,
        reason: str,
        solved_at: datetime,
    ) -> None:
        self.solve_result = solve_result
        self.reason = reason
        self.solved_at = solved_at

        self.offset_arcmin: float | None = solve_result.residual_arcmin
        self.confidence: str = self._build_confidence()
        self.safe_to_resume: bool = self._assess_safety()

    def _build_confidence(self) -> str:
        if not self.solve_result.success:
            cat = self.solve_result.failure_category
            return f"solve failed ({cat or 'unknown'})"
        off = self.offset_arcmin
        if off is None:
            return "solved — offset unknown"
        if off >= _RESIDUAL_FAIL_ARCMIN:
            return f"solved but large offset {off:.1f}' — re-center required"
        if off >= _RESIDUAL_WARN_ARCMIN:
            return f"solved with marginal offset {off:.1f}'"
        return f"solved — offset {off:.1f}' within tolerance"

    def _assess_safety(self) -> bool:
        if not self.solve_result.success:
            return False
        off = self.offset_arcmin
        if off is None:
            return True  # no residual info; trust the solve
        return off < _RESIDUAL_FAIL_ARCMIN

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"VerificationSolveResult(reason={self.reason!r}, "
            f"safe={self.safe_to_resume}, confidence={self.confidence!r})"
        )


class VerificationSolveHelper:
    """Wraps a ``SolverBackend`` for audit and recovery confidence solves.

    Not a replacement for Ekos primary solving.  Use only for:
    - post-intervention verification before resuming capture
    - framing audits when quality or pointing confidence degrades
    - explainable audit trails for why a pause was requested

    Args:
        solver:  Any object satisfying the ``SolverBackend`` protocol.
        reason:  Default reason tag attached to every solve result for
                 audit traceability.  Can be overridden per-call.
    """

    def __init__(self, solver: SolverBackend, *, reason: str = "verification") -> None:
        self._solver = solver
        self._default_reason = reason

    def solve_for_verification(
        self,
        image_path: Path,
        *,
        expected_ra_hours: float | None = None,
        expected_dec_deg: float | None = None,
        reason: str | None = None,
        blind: bool = False,
    ) -> VerificationSolveResult:
        """Solve a frame for independent verification.

        Args:
            image_path:         Path to the frame to solve.
            expected_ra_hours:  Hint for hinted solve (skipped when blind=True).
            expected_dec_deg:   Hint for hinted solve (skipped when blind=True).
            reason:             Override the default reason tag for this solve.
            blind:              Force a blind solve (no hint).

        Returns:
            A ``VerificationSolveResult`` with confidence assessment and audit
            context.  Never raises; failures are captured in the result.
        """
        effective_reason = reason or self._default_reason
        _logger.info(
            "verification solve: reason=%s path=%s blind=%s",
            effective_reason,
            image_path.name,
            blind,
        )
        solved_at = datetime.now(UTC)
        try:
            result = self._solver.solve(
                image_path,
                expected_ra_hours=expected_ra_hours,
                expected_dec_deg=expected_dec_deg,
                blind=blind,
            )
        except Exception as exc:
            _logger.warning(
                "verification solve raised unexpectedly for %s: %s",
                image_path.name,
                exc,
            )
            from kepler_node.imaging.protocols import SolveFailureCategory

            result = SolveResult(
                success=False,
                failure_category=SolveFailureCategory.SOLVER_UNAVAILABLE,
                confidence_summary=f"solver raised: {exc}",
            )

        vr = VerificationSolveResult(result, reason=effective_reason, solved_at=solved_at)
        _logger.info(
            "verification solve complete: reason=%s safe=%s confidence=%r",
            effective_reason,
            vr.safe_to_resume,
            vr.confidence,
        )
        return vr
