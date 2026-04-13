"""Phase-based round execution and multi-round execution plans.

Splits each simulation round into two execution phases so fast-reacting
participants (retail, KOL, quant, media) trade and move prices before
slow-reacting participants (institutional, strategic, analyst) see the
updated price and make their decisions.  This decouples the three
real-world clocks — information reception, decision, execution — that
the old single-pass loop collapsed into one.

Also provides ExecutionPlan for institutional TWAP-style multi-round
order slicing: instead of dumping an entire position in one round, the
agent specifies ``execution_rounds`` and the engine auto-dequeues one
slice per round until complete or cancelled.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from enum import Enum

log = logging.getLogger(__name__)


# ── Phase enum ──────────────────────────────────────────────────────────

class RoundPhase(str, Enum):
    """Phases within a single simulation round."""
    INFO = "info"                # external events arrive, publications visible
    FAST_REACT = "fast_react"   # retail, kol, quant, media, news_wire
    SLOW_REACT = "slow_react"   # institutional, strategic, analyst
    SETTLE = "settle"           # policy eval, self-model, state sync


# ── Agent type classification ───────────────────────────────────────────
#
# These must stay in sync with the ``agent_type`` values produced by
# ``persona._infer_agent_type`` and the activity curves in
# ``oasis_engine._activity_level``.

FAST_AGENT_TYPES: frozenset[str] = frozenset({
    "retail", "kol", "quant", "media", "news_wire",
})

SLOW_AGENT_TYPES: frozenset[str] = frozenset({
    "institutional", "strategic", "analyst",
})

INFO_ONLY_TYPES: frozenset[str] = frozenset({
    "regulator", "policy", "company_ir",
})


def classify_agent_phase(agent_type: str) -> RoundPhase:
    """Map an agent_type string to the execution phase it belongs to.

    INFO_ONLY types are routed into FAST_REACT so their social posts
    (policy statements, regulator announcements) are visible to slow
    agents in the same round.
    """
    if agent_type in FAST_AGENT_TYPES or agent_type in INFO_ONLY_TYPES:
        return RoundPhase.FAST_REACT
    if agent_type in SLOW_AGENT_TYPES:
        return RoundPhase.SLOW_REACT
    # Unknown type → default to fast (safe: participates earlier)
    return RoundPhase.FAST_REACT


# ── PhaseResult ─────────────────────────────────────────────────────────

@dataclass
class PhaseResult:
    """Result of one execution phase within a round."""
    phase: RoundPhase
    class_flows: list  # list[ClassFlowResult] — avoid circular import
    net_flow: float
    price_before: float
    price_after: float
    delta_pct: float
    n_active_personas: int = 0
    n_plan_executions: int = 0

    def to_serializable(self) -> dict:
        return {
            "phase": self.phase.value,
            "net_flow": self.net_flow,
            "price_before": self.price_before,
            "price_after": self.price_after,
            "delta_pct": self.delta_pct,
            "n_active_personas": self.n_active_personas,
            "n_plan_executions": self.n_plan_executions,
        }


# ── ExecutionPlan (TWAP) ────────────────────────────────────────────────

@dataclass
class ExecutionPlan:
    """Multi-round TWAP execution plan for institutional/strategic agents.

    When an agent decides to buy/sell a large position, it can request
    ``execution_rounds > 1`` in the trading tool.  The engine creates an
    ExecutionPlan that auto-dequeues one slice per round.  The agent's
    LLM is still called each round — if it submits a *new* order, the
    existing plan is cancelled and replaced.
    """
    plan_id: str
    persona_id: str
    side: str                    # "buy" or "sell"
    total_pct: float             # total intended allocation change
    remaining_pct: float
    per_round_slice: float       # fraction per round
    n_rounds_total: int
    n_rounds_remaining: int
    created_at_round: int
    price_at_creation: float
    cancel_conditions: dict = field(default_factory=dict)
    rationale: str = ""
    instrument: str = "_default"
    pool: str = ""               # "cash", "holdings_in_target", "margin"

    @property
    def is_complete(self) -> bool:
        return self.n_rounds_remaining <= 0 or self.remaining_pct <= 0.001

    def next_slice(self) -> float:
        """Return the next round's execution slice as a pct."""
        if self.is_complete:
            return 0.0
        return min(self.per_round_slice, self.remaining_pct)

    def advance(self, executed_pct: float) -> None:
        """Mark one slice as executed."""
        self.remaining_pct = max(0.0, self.remaining_pct - executed_pct)
        self.n_rounds_remaining -= 1

    def should_cancel(self, current_price: float) -> bool:
        """Check if adverse price move exceeds plan's cancel threshold."""
        if not self.cancel_conditions:
            return False
        max_adverse = self.cancel_conditions.get("max_adverse_move_pct", 0.10)
        price_move = (current_price / self.price_at_creation - 1.0)
        if self.side == "buy" and price_move > max_adverse:
            return True
        if self.side == "sell" and price_move < -max_adverse:
            return True
        return False

    def slice_number(self) -> int:
        """1-based index of the current slice."""
        return self.n_rounds_total - self.n_rounds_remaining + 1


def create_execution_plan(
    persona_id: str,
    side: str,
    total_pct: float,
    n_rounds: int,
    round_idx: int,
    current_price: float,
    rationale: str = "",
    instrument: str = "_default",
    pool: str = "",
    cancel_conditions: dict | None = None,
) -> ExecutionPlan:
    """Factory for building an ExecutionPlan from a trading tool call."""
    n_rounds = max(1, n_rounds)
    per_slice = total_pct / n_rounds
    return ExecutionPlan(
        plan_id=uuid.uuid4().hex[:12],
        persona_id=persona_id,
        side=side,
        total_pct=total_pct,
        remaining_pct=total_pct,
        per_round_slice=per_slice,
        n_rounds_total=n_rounds,
        n_rounds_remaining=n_rounds,
        created_at_round=round_idx,
        price_at_creation=current_price,
        cancel_conditions=cancel_conditions or {},
        rationale=rationale,
        instrument=instrument,
        pool=pool,
    )


__all__ = [
    "FAST_AGENT_TYPES",
    "INFO_ONLY_TYPES",
    "ExecutionPlan",
    "PhaseResult",
    "RoundPhase",
    "SLOW_AGENT_TYPES",
    "classify_agent_phase",
    "create_execution_plan",
]
