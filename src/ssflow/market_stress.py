"""Market stress computation for reactive policy responses.

Computes a stress level from simulation state (price trajectory, volatility)
and generates:
  1. Context prompts for policy/regulator personas so they react to stress
  2. Stabilization policies for national_team when crisis thresholds are crossed

This enables the V-shape reversal pattern: market drops > 7% → CSRC issues
stabilization statement → national_team buys → price recovers.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .policy import Policy, TradeAction

if TYPE_CHECKING:
    from .world import SimGraph

log = logging.getLogger(__name__)


@dataclass
class MarketStressSnapshot:
    """Computed market stress metrics for the current round."""

    cumulative_price_change_pct: float   # total change from initial, as %
    round_price_change_pct: float        # this round's change, as %
    intraday_volatility: float           # stddev of round-over-round deltas
    stress_level: str                    # "normal" | "elevated" | "crisis"
    urgency_score: float                 # 0.0 - 1.0

    @property
    def is_stressed(self) -> bool:
        return self.stress_level != "normal"


def compute_market_stress(
    rounds_so_far: list[Any],
    current_price: float,
    initial_price: float,
    round_idx: int,
) -> MarketStressSnapshot:
    """Compute market stress from price trajectory.

    Thresholds calibrated from A-share historical precedent:
      - Normal: abs(cumulative) < 3%
      - Elevated: 3-5% cumulative or 2%+ single-round
      - Crisis: > 5% cumulative or > 3% single-round

    Args:
        rounds_so_far: list of RoundRecord objects (need .delta_pct attribute)
        current_price: current round's price
        initial_price: initial price at simulation start
        round_idx: current round index
    """
    cum_pct = (current_price / initial_price - 1.0) * 100 if initial_price > 0 else 0.0

    # Last round's price change
    round_pct = 0.0
    if rounds_so_far:
        last = rounds_so_far[-1]
        round_pct = getattr(last, "delta_pct", 0.0) * 100

    # Volatility: stddev of all round deltas
    deltas = [getattr(r, "delta_pct", 0.0) * 100 for r in rounds_so_far]
    if len(deltas) >= 2:
        mean_d = sum(deltas) / len(deltas)
        var = sum((d - mean_d) ** 2 for d in deltas) / len(deltas)
        volatility = math.sqrt(var)
    else:
        volatility = abs(round_pct)

    # Determine stress level
    abs_cum = abs(cum_pct)
    abs_round = abs(round_pct)

    if abs_cum > 5.0 or abs_round > 3.0:
        stress_level = "crisis"
        urgency = min(1.0, max(0.8, abs_cum / 10.0))
    elif abs_cum > 3.0 or abs_round > 2.0:
        stress_level = "elevated"
        urgency = min(0.7, max(0.3, abs_cum / 8.0))
    else:
        stress_level = "normal"
        urgency = 0.0

    return MarketStressSnapshot(
        cumulative_price_change_pct=cum_pct,
        round_price_change_pct=round_pct,
        intraday_volatility=volatility,
        stress_level=stress_level,
        urgency_score=urgency,
    )


def render_policy_context(
    stress: MarketStressSnapshot,
    persona_id: str,
    entity_role: str,
    round_idx: int,
) -> str:
    """Render market stress briefing for policy/regulator persona prompts.

    Returns a Chinese-language prompt block that is injected into the
    persona's OASIS agent profile, giving it awareness of market conditions
    so it can react appropriately.
    """
    lines = [f"\n# 市场状况简报 / Market Stress Briefing (R{round_idx})"]
    lines.append(f"  累计涨跌: {stress.cumulative_price_change_pct:+.1f}%")
    lines.append(f"  本轮波动: {stress.round_price_change_pct:+.1f}%")
    lines.append(f"  波动率: {stress.intraday_volatility:.1f}%")

    level_zh = {
        "normal": "正常",
        "elevated": "关注",
        "crisis": "危机",
    }
    lines.append(f"  压力等级: {level_zh.get(stress.stress_level, stress.stress_level)}")

    if stress.stress_level == "crisis":
        direction = "下跌" if stress.cumulative_price_change_pct < 0 else "上涨"
        lines.append(
            f"  ⚠ 市场压力等级为危机级 (累计{direction}"
            f"{abs(stress.cumulative_price_change_pct):.1f}%)。"
        )
        if entity_role == "regulator":
            lines.append(
                "  历史上类似情况下，证监会通常在24小时内发声维稳，"
                "可能包括暂停IPO、鼓励回购、限制做空等措施。"
            )
            lines.append("  请根据你的职能考虑是否需要发布监管声明。")
        elif entity_role == "policy":
            lines.append(
                "  请根据你的政策职能考虑是否需要发布支持性政策声明"
                "（如降准降息预期、产业支持政策等）。"
            )
    elif stress.stress_level == "elevated":
        lines.append(
            "  市场波动加大。请根据你的职能考虑是否需要表态或发布指导性意见。"
        )

    return "\n".join(lines)


_STRESS_MARKER = "\n# 市场状况简报"


def create_stabilization_policies(
    stress: MarketStressSnapshot,
    sim_graph: SimGraph,
    round_idx: int,
) -> list[Policy]:
    """Create stabilization trading policies when crisis thresholds are crossed.

    When the market drops > 7%, the national_team persona gets a buy policy
    that fires via normal policy evaluation next round. This models the
    real-world behavior of 汇金/证金 stepping in during crashes.

    Only creates policies if:
      - stress_level == "crisis"
      - cumulative_price_change_pct < -7%
      - No existing stabilization policy has fired recently
    """
    policies: list[Policy] = []

    if stress.stress_level != "crisis":
        return policies
    if stress.cumulative_price_change_pct > -7.0:
        return policies

    # Find national_team SimAgent
    national_team = None
    for agent in sim_graph.agents.values():
        if agent.persona_id and "national_team" in agent.persona_id:
            national_team = agent
            break

    if national_team is None:
        return policies

    # Check if a stabilization policy already fired recently
    for p in national_team.policies:
        if p.source == "market_stress" and p.last_fired_round >= round_idx - 2:
            return policies  # Already active, don't pile on

    policy = Policy(
        id=f"stabilization_R{round_idx}",
        name=f"国家队维稳 (R{round_idx}, 累计{stress.cumulative_price_change_pct:+.1f}%)",
        description=(
            f"Market stabilization: buy 15% of capital when cumulative "
            f"price change hits {stress.cumulative_price_change_pct:+.1f}%"
        ),
        trigger_expr="avg_position_pct < 1.0",  # always true if not fully invested
        action=TradeAction(
            side="buy",
            quantity_pct=0.15,
            pool="cash",
        ),
        source="market_stress",
        cooldown_rounds=3,
        priority=25,  # Higher than risk policies
    )
    national_team.policies.append(policy)
    policies.append(policy)
    log.info(
        "  Stabilization policy created for national_team at R%d "
        "(cum_change=%.1f%%)",
        round_idx, stress.cumulative_price_change_pct,
    )
    return policies


__all__ = [
    "MarketStressSnapshot",
    "compute_market_stress",
    "create_stabilization_policies",
    "render_policy_context",
]
