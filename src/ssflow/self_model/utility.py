"""Utility component library for the self-model subsystem.

Each component is a ``(state_dict) -> float`` scoring function. Personas
compose utility by picking components and assigning weights in their
``SelfModelSpec.utility_weights`` dict. The evaluator's
``compute_utility`` does a linear combination:

    U = sum(w_i * f_i(state) for i in chosen_components)

Components are intentionally simple — one-line readers of the state
dict, no side effects, no cross-component dependencies. This keeps them
unit-testable in isolation and keeps the weighted composition linear
and interpretable.

Design rules for adding a new component:
- Read only from the state dict (no engine, no round ctx)
- Use ``.get(key, default)`` for every read so a persona that omits
  the underlying atom doesn't KeyError
- Return a float in roughly [-10, +10] — weights will re-scale, so
  what matters is that components are commensurate with each other
- Register in ``UTILITY_COMPONENTS`` at module bottom with a docstring
"""

from __future__ import annotations

from typing import Callable


# ─────────────────────── Component implementations ───────────────────────


def _nav_growth(s: dict[str, float]) -> float:
    """Portfolio return in percent (positive = profit).

    Linear in unrealized_pnl_pct so a 20% gain scores +20.
    """
    return s.get("unrealized_pnl_pct", 0.0)


def _drawdown_penalty(s: dict[str, float]) -> float:
    """Loss aversion: drawdown hurts 2x as much as nav_growth helps.

    Returns a NEGATIVE number (multiplied by a POSITIVE weight in the
    spec, so the total utility term is negative).
    """
    return -2.0 * s.get("max_drawdown_pct", 0.0)


def _realized_pnl_gain(s: dict[str, float]) -> float:
    """Locked-in profits, scaled to percentage of initial NAV."""
    initial = s.get("initial_nav", 1.0) or 1.0
    return s.get("realized_pnl_cum", 0.0) / initial * 100.0


def _benchmark_outperformance(s: dict[str, float]) -> float:
    """Relative return vs peer benchmark, directly in pp."""
    return s.get("benchmark_gap_pct", 0.0)


def _peer_rank_lead(s: dict[str, float]) -> float:
    """How far above median in peer rank. Positive = top half.

    Returns in roughly [-50, +50]: 0.0 rank = best (+50), 1.0 = worst (-50).
    """
    rank = s.get("peer_rank_percentile", 0.5)
    return (0.5 - rank) * 100.0


def _mandate_breach_penalty(s: dict[str, float]) -> float:
    """Quadratic penalty for breaching the risk mandate.

    Zero until risk_budget_used_pct exceeds 1.0, then heavy.
    """
    breach = max(0.0, s.get("risk_budget_used_pct", 0.0) - 1.0)
    return -100.0 * breach


def _compliance_breach_penalty(s: dict[str, float]) -> float:
    """Per-breach flat penalty. Careers end on these."""
    return -50.0 * s.get("compliance_breaches_this_sim", 0)


def _client_retention(s: dict[str, float]) -> float:
    """Client net redemption is painful for clientele-facing roles."""
    return -10.0 * s.get("client_net_redemption", 0.0)


def _career_anxiety(s: dict[str, float]) -> float:
    """Career risk pressure drags utility down for roles with bosses."""
    return -5.0 * s.get("career_risk_pressure", 0.0)


def _reputation_erosion(s: dict[str, float]) -> float:
    """Reputation is bounded [0,1]; score its distance from 0.5 midpoint.

    Positive when above midpoint (good reputation), negative when below.
    """
    return 10.0 * (s.get("reputation_score", 0.5) - 0.5)


def _conviction_reward(s: dict[str, float]) -> float:
    """Being decisive in the RIGHT direction gets rewarded; wrong direction penalised.

    Combines conviction level with unrealized pnl sign.
    """
    conviction = s.get("conviction", 0.5)
    pnl = s.get("unrealized_pnl_pct", 0.0)
    if pnl > 0:
        return 2.0 * conviction
    elif pnl < 0:
        return -2.0 * conviction
    return 0.0


def _regret_penalty(s: dict[str, float]) -> float:
    """FOMO / missed-move pain, already computed in [0,1]."""
    return -1.0 * s.get("regret", 0.0)


def _stress_penalty(s: dict[str, float]) -> float:
    """Stress reduces utility (even independently of NAV).

    Represents the psychological cost of white-knuckling a drawdown.
    """
    return -0.5 * s.get("stress", 0.0)


def _runway_safety(s: dict[str, float]) -> float:
    """[company] Cash runway relative to a 12-month healthy target.

    0 months → -1.0, 12 months → 1.0, 24+ months → 2.0 (capped).
    """
    months = s.get("cash_runway_months", 12.0)
    return min(2.0, months / 12.0)


def _board_pressure_pain(s: dict[str, float]) -> float:
    """[company] Board pressure index (0-1) as negative utility."""
    return -10.0 * s.get("board_pressure_index", 0.0)


def _customer_loss_pain(s: dict[str, float]) -> float:
    """[company] Customer concentration risk."""
    return -10.0 * s.get("customer_concentration_risk", 0.0)


def _opportunity_cost(s: dict[str, float]) -> float:
    """Sitting on cash that's not deployed is a small per-round drag.

    Only hurts if the persona has significant idle capital and hasn't
    traded in several rounds.
    """
    idle = s.get("rounds_since_last_trade", 0)
    if idle < 2:
        return 0.0
    cash_pct = s.get("cash", 0) / max(1e-6, s.get("nav", 1))
    if cash_pct < 0.5:
        return 0.0
    return -0.2 * (idle - 1)


# ─────────────────────── Registry ───────────────────────


UTILITY_COMPONENTS: dict[str, Callable[[dict[str, float]], float]] = {
    "nav_growth": _nav_growth,
    "drawdown_penalty": _drawdown_penalty,
    "realized_pnl_gain": _realized_pnl_gain,
    "benchmark_outperformance": _benchmark_outperformance,
    "peer_rank_lead": _peer_rank_lead,
    "mandate_breach_penalty": _mandate_breach_penalty,
    "compliance_breach_penalty": _compliance_breach_penalty,
    "client_retention": _client_retention,
    "career_anxiety": _career_anxiety,
    "reputation_erosion": _reputation_erosion,
    "conviction_reward": _conviction_reward,
    "regret_penalty": _regret_penalty,
    "stress_penalty": _stress_penalty,
    "runway_safety": _runway_safety,
    "board_pressure_pain": _board_pressure_pain,
    "customer_loss_pain": _customer_loss_pain,
    "opportunity_cost": _opportunity_cost,
}


def compute_utility(
    state: dict[str, float],
    weights: dict[str, float],
) -> tuple[float, dict[str, float]]:
    """Linear combination with per-component breakdown.

    Unknown component keys in ``weights`` are silently ignored — the
    validation guardrail in ``schema.validate_self_model_spec`` should
    have stripped them upstream, but we defend here too so tests can
    feed arbitrary dicts.
    """
    total = 0.0
    breakdown: dict[str, float] = {}
    for comp_key, w in weights.items():
        fn = UTILITY_COMPONENTS.get(comp_key)
        if fn is None:
            continue
        score = fn(state)
        weighted = w * score
        breakdown[comp_key] = weighted
        total += weighted
    return total, breakdown


__all__ = ["UTILITY_COMPONENTS", "compute_utility"]
