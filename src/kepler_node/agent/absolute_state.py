"""Canonical absolute-state model for Kepler v1.1 supervisory policy.

The canonical absolute state synthesizes multiple runtime inputs into a
single trustworthy source of truth that ClawController uses to make
intervention and ownership decisions.

Key principle: when sources disagree, the most conservative interpretation
wins.  Unknown is always the conservative default when freshness or
confirmation is absent.

Spec reference: V1_1_HANDOFF.md §Absolute State And Canonical Source Of Truth
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

# Default freshness TTL: snapshots older than this are considered stale.
_DEFAULT_FRESHNESS_TTL_SECONDS: float = 60.0


# ---------------------------------------------------------------------------
# Runtime state enumerations
# ---------------------------------------------------------------------------


class ActiveOwner(StrEnum):
    """Who currently owns the shared INDI device path.

    Spec outputs (lines 432-437):
      ekos     — normal imaging execution; Ekos is the default owner
      kepler   — confirmed open intervention window; Kepler owns the path
      operator — direct operator action confirmed on the path
      unknown  — pause confirmed or contradictory signals; ownership unclear
      none     — no managed session; path is unowned
    """

    EKOS = "ekos"
    KEPLER = "kepler"
    OPERATOR = "operator"
    UNKNOWN = "unknown"
    NONE = "none"


class EkosRuntimeState(StrEnum):
    """Normalized Ekos session state.

    Maps to the spec's required Ekos state vocabulary (lines 432-437):
      idle        — sequence engine present but not running
      running     — capture sequence actively executing
      paused      — sequence explicitly paused (confirmed by adapter)
      resuming    — resume requested but not yet confirmed running
      unavailable — Ekos or transport unreachable
      unknown     — state cannot be determined (default when unconfirmed)
    """

    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    RESUMING = "resuming"
    ABORTED = "aborted"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class BrokerRuntimeState(StrEnum):
    """Normalized INDI broker / semaphore state.

    Spec (lines 432-437):
      ready       — broker reachable, profile active, device path available
      degraded    — broker reachable but profile or device path has issues
      unavailable — broker unreachable
      unknown     — state has not been confirmed yet
    """

    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class InterventionWindowState(StrEnum):
    """State of Kepler's current bounded intervention window.

    Spec §Control Handoff Protocol (lines 394-408):
      closed     — no active intervention; normal Ekos execution
      requested  — Kepler has requested Ekos pause but confirmation not yet received
      open       — pause confirmed, device activity settled; Kepler may act
      releasing  — Kepler has finished acting and is releasing the window
      unknown    — window state cannot be determined
    """

    CLOSED = "closed"
    REQUESTED = "requested"
    OPEN = "open"
    RELEASING = "releasing"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Normalized Ekos snapshot (replaces boolean EkosSequenceStatus)
# ---------------------------------------------------------------------------


class NormalizedEkosSnapshot(BaseModel):
    """Normalized snapshot of Ekos sequence/capture state with explicit unknown-state handling.

    Replaces the boolean ``EkosSequenceStatus`` with an explicit state enum,
    freshness tracking, and conservative unknown defaults.

    Spec minimum required fields (lines 219-240):
    - whether a capture sequence exists
    - whether the sequence is running, paused, idle, resuming, aborted, or unknown
    - whether an exposure is currently in progress
    - current job name or sequence item when available
    - frames completed and frames planned when available
    - whether autofocus is active when available
    - whether align or re-center work is active when available
    - freshness timestamp for the snapshot

    The adapter must say ``unknown`` rather than fabricate certainty when it
    cannot provide a trustworthy snapshot (spec lines 237-240).
    """

    ekos_state: EkosRuntimeState = EkosRuntimeState.UNKNOWN
    confirmed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    sequence_exists: bool = False
    exposure_active: bool = False
    job_name: str | None = None
    frames_done: int = 0
    frames_total: int = 0
    autofocus_active: bool = False
    align_active: bool = False
    details: dict[str, Any] = Field(default_factory=dict)
    freshness_ttl_seconds: float = _DEFAULT_FRESHNESS_TTL_SECONDS

    @property
    def is_stale(self) -> bool:
        """True when the snapshot is older than the freshness TTL."""
        age = (datetime.now(UTC) - self.confirmed_at).total_seconds()
        return age > self.freshness_ttl_seconds

    @property
    def is_unknown(self) -> bool:
        """True when state cannot be trusted: unknown ekos_state or freshness expired."""
        return self.ekos_state == EkosRuntimeState.UNKNOWN or self.is_stale

    @property
    def is_paused(self) -> bool:
        """True only when Ekos is confirmed paused and the snapshot is fresh."""
        return self.ekos_state == EkosRuntimeState.PAUSED and not self.is_stale

    @property
    def is_running(self) -> bool:
        """True only when Ekos is confirmed running and the snapshot is fresh."""
        return self.ekos_state == EkosRuntimeState.RUNNING and not self.is_stale

    # ------------------------------------------------------------------
    # Backward-compatibility shims for code still consuming .active/.paused
    # ------------------------------------------------------------------

    @property
    def active(self) -> bool:
        """Backward-compat: True when running and fresh."""
        return self.is_running

    @property
    def paused(self) -> bool:
        """Backward-compat: True when confirmed paused and fresh."""
        return self.is_paused


# ---------------------------------------------------------------------------
# Canonical absolute state
# ---------------------------------------------------------------------------


class CanonicalAbsoluteState(BaseModel):
    """Canonical local runtime state synthesized from all supervisory inputs.

    This is Kepler's source of truth for intervention and ownership decisions.
    Neither Ekos nor the broker alone determines absolute state; this model
    synthesizes all inputs under the conservative precedence rule:
    when sources disagree, the most conservative interpretation wins.

    Minimum canonical outputs (spec lines 432-437):
      active_owner        — who owns the shared device path
      ekos_state          — normalized Ekos session state
      broker_state        — INDI broker / semaphore readiness
      intervention_window — state of Kepler's current intervention window
      control_locked      — whether Kepler is actively supervising the session
    """

    active_owner: ActiveOwner = ActiveOwner.UNKNOWN
    ekos_state: EkosRuntimeState = EkosRuntimeState.UNKNOWN
    broker_state: BrokerRuntimeState = BrokerRuntimeState.UNKNOWN
    intervention_window: InterventionWindowState = InterventionWindowState.CLOSED
    control_locked: bool = False
    synthesized_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def is_safe_for_kepler_intervention(self) -> bool:
        """Return True only when Kepler may safely take the device path.

        Conservative rules (spec lines 444-448):
        - broker must be ready
        - Ekos must be confirmed paused (not just requested)
        - intervention window must be open (not just requested)
        - active owner must be kepler
        """
        return (
            self.broker_state == BrokerRuntimeState.READY
            and self.ekos_state == EkosRuntimeState.PAUSED
            and self.intervention_window == InterventionWindowState.OPEN
            and self.active_owner == ActiveOwner.KEPLER
        )

    def is_safe_to_resume(self) -> bool:
        """Return True when the intervention window is closed and Ekos can resume.

        Broker may be degraded (not just ready) when resuming is still safe.
        """
        return (
            self.broker_state in {BrokerRuntimeState.READY, BrokerRuntimeState.DEGRADED}
            and self.ekos_state in {EkosRuntimeState.PAUSED, EkosRuntimeState.IDLE}
            and self.intervention_window
            in {InterventionWindowState.CLOSED, InterventionWindowState.RELEASING}
        )

    def requires_conservative_pause(self) -> bool:
        """Return True when the model cannot answer ownership questions reliably.

        Spec lines 456-470 (Absolute State First Rule): if those questions
        cannot be answered, the correct default is to pause conservatively.
        """
        return self.active_owner == ActiveOwner.UNKNOWN and self.control_locked
