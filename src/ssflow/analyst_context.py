"""Analyst counter-narrative context injection.

Real sell-side analysts don't echo consensus — they generate contrarian
research within 48h of major events ("overreaction thesis", "sell the news",
"neglected risk factors"). This module computes per-analyst context that
nudges LLM-powered analyst personas toward more balanced or contrarian
research notes as the simulation progresses.

Counter-narrative triggers:
  1. Price overreaction (abs cumulative move > 5%) → suggest overreaction thesis
  2. Time decay (round >= 2) → deeper analysis is now appropriate
  3. Mixed signals in event text (beat + miss) → highlight neglected dimension

The intensity of the nudge is calibrated per analyst sub_archetype:
  - sellside_research_contrarian: 90% probability, strong nudge
  - sellside_research_bearish: 85%, strong nudge
  - sellside_research_conservative: 50%, balanced
  - sellside_research_growth: 35%, gentle nudge
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .event import Event
    from .persona import Persona

log = logging.getLogger(__name__)


# ─────────────────────── Calibration ───────────────────────

# Per sub_archetype: (contrarian_probability, intensity)
# intensity: "strong" = explicit contrarian framing
#            "balanced" = suggest considering both sides
#            "gentle" = minor suggestion to consider risks
_CONTRARIAN_CALIBRATION: dict[str, tuple[float, str]] = {
    "sellside_research_contrarian": (0.90, "strong"),
    "sellside_research_bearish": (0.85, "strong"),
    "sellside_research_conservative": (0.50, "balanced"),
    "sellside_research_growth": (0.35, "gentle"),
}

# Default for unknown sub_archetypes
_DEFAULT_CALIBRATION = (0.40, "balanced")


# ─────────────────────── Mixed Signal Detection ───────────────────────

_POSITIVE_MARKERS = [
    "超预期", "增长", "突破", "创新高", "签约", "盈利", "beat",
    "超出预期", "利好", "大涨", "营收增长", "净利润增", "扩张",
    "份额提升", "订单增加", "产能扩张", "毛利率提升",
]

_NEGATIVE_MARKERS = [
    "低于预期", "毛利率下降", "成本上升", "竞争加剧", "miss",
    "下滑", "承压", "亏损", "裁员", "降价", "库存积压",
    "市场份额下降", "增速放缓", "不及预期", "毛利率下滑",
    "净利润下", "debt", "违约", "减持",
]


def detect_mixed_signals(event_text: str) -> tuple[list[str], list[str]]:
    """Scan event text for both positive and negative markers.

    Returns (positive_signals, negative_signals). If both lists are non-empty,
    the event has mixed signals — the analyst prompt should highlight the
    neglected dimension.
    """
    text = event_text.lower()
    positives = [m for m in _POSITIVE_MARKERS if m.lower() in text]
    negatives = [m for m in _NEGATIVE_MARKERS if m.lower() in text]
    return positives, negatives


# ─────────────────────── Context Computation ───────────────────────


@dataclass
class AnalystRoundContext:
    """Computed context for one analyst persona for one round."""

    price_move_since_event_pct: float      # cumulative % change
    rounds_since_event: int
    positive_signals: list[str]
    negative_signals: list[str]
    contrarian_prompt: str                  # the prompt text to inject (empty = no injection)
    should_inject: bool                     # whether this analyst gets a contrarian nudge


def compute_analyst_context(
    persona: Persona,
    current_price: float,
    initial_price: float,
    round_idx: int,
    event: Event,
) -> AnalystRoundContext:
    """Compute counter-narrative context for an analyst persona.

    The counter-narrative prompt is only generated when:
      1. round_idx >= 2 (enough time for deeper analysis)
      2. abs(price move) > 5% OR event has mixed signals
      3. Random check passes based on persona's contrarian_probability
         (deterministic: uses round_idx + persona hash for reproducibility)
    """
    cum_pct = (current_price / initial_price - 1.0) * 100 if initial_price > 0 else 0.0
    positives, negatives = detect_mixed_signals(event.event_text)
    has_mixed = bool(positives) and bool(negatives)

    # Determine if we should inject based on calibration
    sub_arch = persona.sub_archetype or ""
    prob, intensity = _CONTRARIAN_CALIBRATION.get(sub_arch, _DEFAULT_CALIBRATION)

    # Deterministic "random" based on round + persona id
    seed_val = hash(f"{persona.id}_{round_idx}") % 100
    passes_probability = seed_val < int(prob * 100)

    # Trigger conditions
    significant_move = abs(cum_pct) > 5.0
    enough_time = round_idx >= 2

    should_inject = (
        enough_time
        and (significant_move or has_mixed)
        and passes_probability
    )

    if not should_inject:
        return AnalystRoundContext(
            price_move_since_event_pct=cum_pct,
            rounds_since_event=round_idx,
            positive_signals=positives,
            negative_signals=negatives,
            contrarian_prompt="",
            should_inject=False,
        )

    # Build the contrarian prompt
    prompt = _build_contrarian_prompt(
        persona, cum_pct, round_idx, positives, negatives, intensity,
    )

    return AnalystRoundContext(
        price_move_since_event_pct=cum_pct,
        rounds_since_event=round_idx,
        positive_signals=positives,
        negative_signals=negatives,
        contrarian_prompt=prompt,
        should_inject=True,
    )


_ANALYST_MARKER = "\n# 分析师深度思考"


def _build_contrarian_prompt(
    persona: Persona,
    cum_pct: float,
    round_idx: int,
    positives: list[str],
    negatives: list[str],
    intensity: str,
) -> str:
    """Build the counter-narrative prompt for injection into analyst profile."""
    direction_zh = "看多" if cum_pct > 0 else "看空"
    abs_pct = abs(cum_pct)
    lines = [f"\n# 分析师深度思考 / Analyst Deep Dive (R{round_idx})"]
    lines.append(f"  累计涨跌幅: {cum_pct:+.1f}%")

    if abs_pct > 5.0:
        lines.append(f"  市场初始反应是{direction_zh}，价格已累计变动{abs_pct:.1f}%。")

    # Mixed signal highlighting
    has_mixed = bool(positives) and bool(negatives)
    if has_mixed:
        pos_str = "、".join(positives[:3])
        neg_str = "、".join(negatives[:3])
        dominant = "正面" if cum_pct > 0 else "负面"
        neglected = "负面" if cum_pct > 0 else "正面"
        lines.append(
            f"  事件包含正面信号({pos_str})和负面信号({neg_str})。"
        )
        lines.append(
            f"  市场可能只关注了{dominant}因素。请考虑是否应该重点分析{neglected}因素。"
        )

    # Intensity-calibrated prompt
    if intensity == "strong":
        lines.append("  请考虑:")
        if abs_pct > 5.0:
            lines.append("  - 这个涨跌幅是否反应过度？估值是否已经偏离基本面？")
        lines.append("  - 有哪些风险/机会因素被当前市场情绪忽视了？")
        lines.append("  - 同行估值对比是否支持当前价格？")
        lines.append("  你的分析应该具有独立性和批判性思维。")
    elif intensity == "balanced":
        lines.append("  请考虑:")
        if abs_pct > 5.0:
            lines.append("  - 当前价格变动是否已充分反映了已知信息？")
        lines.append("  - 你的分析应该更深入、更平衡，而不是简单跟随市场情绪。")
    else:  # gentle
        if abs_pct > 5.0:
            lines.append(
                "  注意: 市场已经大幅反应。即使你的基本观点不变，"
                "也请考虑短期估值是否过度拉伸。"
            )

    return "\n".join(lines)


__all__ = [
    "AnalystRoundContext",
    "compute_analyst_context",
    "detect_mixed_signals",
]
