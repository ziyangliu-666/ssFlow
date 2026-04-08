"""Aggregation: turn raw reactions into a structured report data object.

Computes:
    - Sentiment distribution (mean, std, histogram)
    - Action intent breakdown (% by intent label)
    - Implied price move range (descriptive, derived from sentiment + weights)
    - Top blind spots (extracted from disagreement / outlier reactions)

The implied price move is INTENTIONALLY described as "what the simulated crowd
implies", not "what the price will do". This is the P1 language discipline.
"""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .persona import Persona
from .simulation import Reaction, SimulationResult


# ─────────────────────── Data classes ───────────────────────


@dataclass
class AggregatedReport:
    simulation_id: str
    sentiment_mean: float
    sentiment_std: float
    sentiment_histogram: dict[str, int]  # negative/neutral/positive bucket counts
    action_intent_breakdown: dict[str, float]  # intent -> share (0..1)
    implied_move_low: float  # in percent (e.g., -4.2)
    implied_move_high: float  # e.g., -1.1
    implied_move_confidence: float  # 0..1
    dispersion: float  # how much disagreement is in the room
    blind_spots: list[str]  # 3-5 short bullet phrases
    standout_reactions: list[Reaction]  # most surprising / contrarian reactions
    persona_summary: list[dict[str, Any]] = field(default_factory=list)


# ─────────────────────── Stats helpers ───────────────────────


def _bucket_sentiment(sentiments: list[float]) -> dict[str, int]:
    buckets = {"strongly_negative": 0, "negative": 0, "neutral": 0, "positive": 0, "strongly_positive": 0}
    for s in sentiments:
        if s < -0.6:
            buckets["strongly_negative"] += 1
        elif s < -0.2:
            buckets["negative"] += 1
        elif s <= 0.2:
            buckets["neutral"] += 1
        elif s <= 0.6:
            buckets["positive"] += 1
        else:
            buckets["strongly_positive"] += 1
    return buckets


def _intent_breakdown(reactions: list[Reaction]) -> dict[str, float]:
    if not reactions:
        return {}
    counts = Counter(r.action_intent for r in reactions)
    n = len(reactions)
    return {intent: round(count / n, 3) for intent, count in counts.most_common()}


DEFAULT_WEIGHT_DIMENSION = "by_volume"


def _weight_for(persona: Persona | None, dimension: str) -> float:
    """Read a persona's weight from market_share[dimension].

    Falls back to 0.0 if the persona is missing or its market_share is unset.
    Strategic personas (`contributes_to_sentiment_mean=False`) are excluded
    from sentiment-mean weighting and return 0.0 here regardless of their
    market_share value.
    """
    if persona is None or persona.market_share is None:
        return 0.0
    if not persona.contributes_to_sentiment_mean:
        return 0.0
    val = persona.market_share.get(dimension)
    return float(val) if val is not None else 0.0


def _weighted_sentiment(
    reactions: list[Reaction],
    personas: list[Persona],
    dimension: str = DEFAULT_WEIGHT_DIMENSION,
) -> float:
    """Persona-weight-adjusted sentiment mean.

    Weight comes from `persona.market_share[dimension]`. Strategic personas
    are silently dropped (their `contributes_to_sentiment_mean=False`).
    """
    by_id = {p.id: p for p in personas}
    total_weight = 0.0
    total_weighted = 0.0
    for r in reactions:
        w = _weight_for(by_id.get(r.persona_id), dimension)
        total_weight += w
        total_weighted += w * r.sentiment
    return total_weighted / total_weight if total_weight else 0.0


def _implied_move_from_sentiment(
    weighted_sentiment: float,
    dispersion: float,
    context_completeness: float,
) -> tuple[float, float, float]:
    """Map (sentiment, dispersion) -> (low%, high%, confidence).

    This is INTENTIONALLY a simple heuristic, not a calibrated regression.
    Per Codex critique #2, calibration requires a holdout dataset we don't have
    yet. The MVP outputs a descriptive range that scales with sentiment magnitude
    and widens with dispersion. The Live Scorecard will collect data to calibrate
    a real model later.

    Mapping (rough):
        sentiment in [-1, 1] -> center move in [-8%, +8%]
        dispersion in [0, 1] -> width in [0.5pp, 4pp]
        confidence shrinks when context is missing or dispersion is high
    """
    center = weighted_sentiment * 8.0  # max ±8% center
    half_width = 0.5 + dispersion * 3.5  # ±0.5 to ±4 pp
    low = round(center - half_width, 1)
    high = round(center + half_width, 1)

    # Confidence: starts at 0.7, penalized by dispersion and missing context
    confidence = 0.7
    confidence -= dispersion * 0.25  # high dispersion -> lower confidence
    confidence -= (1.0 - context_completeness) * 0.20  # missing context -> lower
    confidence = max(0.20, min(0.85, confidence))

    return low, high, round(confidence, 2)


def _extract_blind_spots(
    final_reactions: list[Reaction],
    personas: list[Persona],
    sentiment_mean: float,
) -> tuple[list[str], list[Reaction]]:
    """Extract 3-5 short blind-spot bullets from outlier reactions.

    A blind spot = a reaction whose sentiment deviates strongly from the mean,
    OR whose action_intent is uncommon. We surface those personas' comments
    verbatim because they represent the angles a typical analyst missed.
    """
    if not final_reactions:
        return [], []

    by_id = {p.id: p for p in personas}
    intent_counts = Counter(r.action_intent for r in final_reactions)
    n = len(final_reactions)

    scored = []
    for r in final_reactions:
        # Distance from group mean (sentiment outlier-ness)
        senti_outlier = abs(r.sentiment - sentiment_mean)
        # Rarity of action intent
        intent_rarity = 1.0 - (intent_counts[r.action_intent] / n)
        score = senti_outlier + 0.6 * intent_rarity
        scored.append((score, r))

    scored.sort(key=lambda t: t[0], reverse=True)
    standouts = [r for _, r in scored[:5]]

    bullets = []
    for r in standouts[:4]:
        persona = by_id.get(r.persona_id)
        archetype = persona.archetype if persona else r.persona_id
        bullet = f"{archetype} ({r.persona_id}): {r.comment}"
        bullets.append(bullet[:200])

    return bullets, standouts


def _persona_summary_rows(
    final_reactions: list[Reaction],
    personas: list[Persona],
    dimension: str = DEFAULT_WEIGHT_DIMENSION,
) -> list[dict[str, Any]]:
    by_id = {p.id: p for p in personas}
    rows = []
    for r in final_reactions:
        p = by_id.get(r.persona_id)
        rows.append(
            {
                "persona_id": r.persona_id,
                "archetype": p.archetype if p else "?",
                "sentiment": round(r.sentiment, 2),
                "action_intent": r.action_intent,
                "comment": r.comment,
                "confidence": round(r.confidence, 2),
                "weight": round(_weight_for(p, dimension), 4),
            }
        )
    return rows


# ─────────────────────── Public API ───────────────────────


def aggregate(result: SimulationResult) -> AggregatedReport:
    final = result.final_reactions
    if not final:
        raise ValueError("Cannot aggregate empty simulation result")

    sentiments = [r.sentiment for r in final]
    sentiment_mean = round(statistics.mean(sentiments), 3)
    sentiment_std = round(statistics.pstdev(sentiments) if len(sentiments) > 1 else 0.0, 3)
    histogram = _bucket_sentiment(sentiments)
    intent_breakdown = _intent_breakdown(final)

    weighted_mean = _weighted_sentiment(final, result.personas)
    # Dispersion: stdev normalized to [0, 1] (max possible stdev for [-1,1] is ~1.0)
    dispersion = min(1.0, sentiment_std)

    low, high, confidence = _implied_move_from_sentiment(
        weighted_sentiment=weighted_mean,
        dispersion=dispersion,
        context_completeness=result.event.context_completeness,
    )

    blind_spots, standouts = _extract_blind_spots(
        final_reactions=final,
        personas=result.personas,
        sentiment_mean=sentiment_mean,
    )

    return AggregatedReport(
        simulation_id=result.simulation_id,
        sentiment_mean=sentiment_mean,
        sentiment_std=sentiment_std,
        sentiment_histogram=histogram,
        action_intent_breakdown=intent_breakdown,
        implied_move_low=low,
        implied_move_high=high,
        implied_move_confidence=confidence,
        dispersion=round(dispersion, 3),
        blind_spots=blind_spots,
        standout_reactions=standouts,
        persona_summary=_persona_summary_rows(final, result.personas),
    )


__all__ = ["AggregatedReport", "aggregate"]
