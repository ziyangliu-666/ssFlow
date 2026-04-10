"""Quantitative publication effects on market state.

Publications (analyst reports, exchange notices, official media, KOL posts,
broker margin calls, etc.) should have NUMERICAL effects on market dynamics,
not just prompt text. This module maps publication types to quantitative
modifiers on sentiment, participation rate, urgency, and risk budget.

The engine calls ``compute_publication_effect`` for each publication in
a round, ``aggregate_round_effects`` to combine them, and then applies
the aggregate to participation rates, sentiment, and risk parameters.

Effect semantics:
  - sentiment_shift: [-1.0, 1.0] shift applied to round sentiment
  - participation_modifier: multiplier on base participation rate (>1 = more
    active, <1 = dampened)
  - urgency_modifier: multiplier on trading urgency (affects order size)
  - risk_budget_shift: shift in risk tolerance [-0.5, 0.5]
  - affected_personas: which persona types are affected
  - decay_rounds: how many rounds the effect persists (1 = this round only)
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


# ─────────────────────── Effect Dataclasses ───────────────────────


@dataclass
class PublicationEffect:
    """Quantitative market effect of a single publication."""

    source_type: str
    sentiment_shift: float
    participation_modifier: float
    urgency_modifier: float
    risk_budget_shift: float
    affected_personas: list[str]
    decay_rounds: int

    @property
    def is_bearish(self) -> bool:
        return self.sentiment_shift < 0

    @property
    def is_bullish(self) -> bool:
        return self.sentiment_shift > 0


@dataclass
class AggregateEffect:
    """Combined effect of multiple publications in a round.

    Aggregation rules:
      - sentiment_shift: sum, clamped to [-1.0, 1.0]
      - participation_modifier: product of all modifiers
      - urgency_modifier: product of all modifiers
      - risk_budget_shift: sum, clamped to [-0.5, 0.5]
      - affected_personas: union of all affected sets
    """

    sentiment_shift: float = 0.0
    participation_modifier: float = 1.0
    urgency_modifier: float = 1.0
    risk_budget_shift: float = 0.0
    affected_personas: set[str] = field(default_factory=set)
    source_count: int = 0

    @property
    def is_neutral(self) -> bool:
        return (
            abs(self.sentiment_shift) < 0.01
            and 0.99 < self.participation_modifier < 1.01
            and 0.99 < self.urgency_modifier < 1.01
            and abs(self.risk_budget_shift) < 0.01
        )


@dataclass
class DecayingEffect:
    """A publication effect that decays over multiple rounds.

    The effect strength decays exponentially: strength at round t is
    ``base_effect * decay_factor^(t - origin_round)``.
    """

    effect: PublicationEffect
    origin_round: int
    decay_factor: float = 0.6  # 60% retained per round

    def effect_at_round(self, round_idx: int) -> PublicationEffect | None:
        """Return the decayed effect at the given round, or None if expired."""
        elapsed = round_idx - self.origin_round
        if elapsed < 0 or elapsed >= self.effect.decay_rounds:
            return None
        if elapsed == 0:
            return self.effect
        scale = self.decay_factor ** elapsed
        return PublicationEffect(
            source_type=self.effect.source_type,
            sentiment_shift=self.effect.sentiment_shift * scale,
            participation_modifier=1.0 + (self.effect.participation_modifier - 1.0) * scale,
            urgency_modifier=1.0 + (self.effect.urgency_modifier - 1.0) * scale,
            risk_budget_shift=self.effect.risk_budget_shift * scale,
            affected_personas=list(self.effect.affected_personas),
            decay_rounds=self.effect.decay_rounds - elapsed,
        )


# ─────────────────────── Effect Lookup Table ───────────────────────

# Canonical effect parameters by source type.
# Format: (sentiment_shift, participation_modifier, urgency_modifier,
#          risk_budget_shift, affected_personas, decay_rounds)

_EFFECT_TABLE: dict[str, tuple[float, float, float, float, list[str], int]] = {
    "exchange_inquiry": (
        -0.15, 0.7, 1.3, -0.20,
        ["retail", "kol"],
        3,
    ),
    "csrc_verbal_support": (
        +0.20, 1.2, 0.8, +0.10,
        ["retail", "institutional", "kol", "analyst", "strategic"],
        2,
    ),
    "national_team_buy": (
        +0.35, 1.5, 1.0, +0.30,
        ["institutional", "retail"],
        4,
    ),
    "analyst_upgrade": (
        +0.10, 1.1, 1.0, +0.05,
        ["institutional", "analyst"],
        2,
    ),
    "analyst_downgrade": (
        -0.10, 1.1, 1.0, -0.05,
        ["institutional", "analyst"],
        2,
    ),
    "kol_hype_post": (
        +0.05, 1.3, 1.5, +0.02,
        ["retail", "kol"],
        1,
    ),
    "official_media_warning": (
        -0.25, 0.5, 0.6, -0.30,
        ["retail", "institutional", "kol", "analyst", "strategic"],
        3,
    ),
    "broker_margin_call": (
        -0.10, 0.8, 2.0, -0.40,
        ["margin_retail"],
        1,
    ),
}

# Aliases for flexible lookup
_ALIASES: dict[str, str] = {
    "exchange_notice": "exchange_inquiry",
    "inquiry_letter": "exchange_inquiry",
    "csrc_support": "csrc_verbal_support",
    "verbal_support": "csrc_verbal_support",
    "national_team_confirms_buying": "national_team_buy",
    "national_team_etf_buy": "national_team_buy",
    "upgrade": "analyst_upgrade",
    "downgrade": "analyst_downgrade",
    "kol_post": "kol_hype_post",
    "media_warning": "official_media_warning",
    "margin_call": "broker_margin_call",
    "broker_call": "broker_margin_call",
}


# ─────────────────────── Core Functions ───────────────────────


def compute_publication_effect(
    pub_type: str,
    authority_weight: float = 1.0,
) -> PublicationEffect:
    """Look up the quantitative effect for a publication type.

    The authority_weight (0.0-1.0) scales the effect magnitude — higher
    authority sources produce stronger effects.

    Args:
        pub_type: publication source type (e.g. "exchange_inquiry",
            "national_team_buy"). Aliases are resolved automatically.
        authority_weight: 0.0-1.0 scaling factor for the effect magnitude.

    Returns:
        PublicationEffect with parameters scaled by authority_weight.

    Raises:
        KeyError: if pub_type is not in the lookup table or aliases.
    """
    canonical = _ALIASES.get(pub_type, pub_type)
    if canonical not in _EFFECT_TABLE:
        raise KeyError(
            f"Unknown publication type: {pub_type!r}. "
            f"Known types: {sorted(_EFFECT_TABLE.keys())}"
        )

    sent, part, urg, risk, personas, decay = _EFFECT_TABLE[canonical]
    w = max(0.0, min(1.0, authority_weight))

    return PublicationEffect(
        source_type=canonical,
        sentiment_shift=sent * w,
        participation_modifier=1.0 + (part - 1.0) * w,
        urgency_modifier=1.0 + (urg - 1.0) * w,
        risk_budget_shift=risk * w,
        affected_personas=list(personas),
        decay_rounds=decay,
    )


def aggregate_round_effects(
    effects: list[PublicationEffect],
) -> AggregateEffect:
    """Aggregate multiple publication effects into a single round effect.

    Aggregation rules:
      - sentiment_shift: additive, clamped to [-1.0, 1.0]
      - participation_modifier: multiplicative (product of all modifiers)
      - urgency_modifier: multiplicative (product of all modifiers)
      - risk_budget_shift: additive, clamped to [-0.5, 0.5]
      - affected_personas: union
    """
    if not effects:
        return AggregateEffect()

    agg = AggregateEffect(source_count=len(effects))

    for eff in effects:
        agg.sentiment_shift += eff.sentiment_shift
        agg.participation_modifier *= eff.participation_modifier
        agg.urgency_modifier *= eff.urgency_modifier
        agg.risk_budget_shift += eff.risk_budget_shift
        agg.affected_personas.update(eff.affected_personas)

    # Clamp
    agg.sentiment_shift = max(-1.0, min(1.0, agg.sentiment_shift))
    agg.risk_budget_shift = max(-0.5, min(0.5, agg.risk_budget_shift))

    return agg


def apply_effects_to_participation(
    base_rate: float,
    effects: AggregateEffect,
) -> float:
    """Apply aggregate publication effects to a base participation rate.

    Participation rate is clamped to [0.0, 1.0].

    Args:
        base_rate: the base participation rate before effects (0.0-1.0)
        effects: aggregated publication effects for the round

    Returns:
        Modified participation rate, clamped to [0.0, 1.0].
    """
    modified = base_rate * effects.participation_modifier
    return max(0.0, min(1.0, modified))


def apply_effects_to_risk_budget(
    base_risk: float,
    effects: AggregateEffect,
) -> float:
    """Apply aggregate publication effects to a base risk budget.

    Risk budget is clamped to [0.0, 1.0].

    Args:
        base_risk: the base risk budget before effects (0.0-1.0)
        effects: aggregated publication effects for the round

    Returns:
        Modified risk budget, clamped to [0.0, 1.0].
    """
    modified = base_risk + effects.risk_budget_shift
    return max(0.0, min(1.0, modified))


def apply_effects_to_sentiment(
    base_sentiment: float,
    effects: AggregateEffect,
) -> float:
    """Apply aggregate publication effects to a base sentiment score.

    Sentiment is clamped to [-1.0, 1.0].

    Args:
        base_sentiment: the base sentiment before effects (-1.0 to 1.0)
        effects: aggregated publication effects for the round

    Returns:
        Modified sentiment, clamped to [-1.0, 1.0].
    """
    modified = base_sentiment + effects.sentiment_shift
    return max(-1.0, min(1.0, modified))


# ─────────────────────── Decay Tracker ───────────────────────


class EffectTracker:
    """Track decaying publication effects across rounds.

    Add effects as they occur, then call ``effects_at_round`` each round
    to get the currently-active (possibly decayed) effects.
    """

    def __init__(self) -> None:
        self._active: list[DecayingEffect] = []

    def add(self, effect: PublicationEffect, round_idx: int) -> None:
        """Register a new publication effect at the given round."""
        self._active.append(DecayingEffect(
            effect=effect,
            origin_round=round_idx,
        ))

    def effects_at_round(self, round_idx: int) -> list[PublicationEffect]:
        """Return all active effects at the given round (with decay applied).

        Also prunes expired effects from the internal list. Effects that
        haven't started yet (origin_round > round_idx) are kept but not
        included in the result.
        """
        live: list[DecayingEffect] = []
        result: list[PublicationEffect] = []

        for de in self._active:
            eff = de.effect_at_round(round_idx)
            if eff is not None:
                result.append(eff)
                live.append(de)
            elif de.origin_round > round_idx:
                # Not yet started — keep for future rounds
                live.append(de)
            # else: expired, drop from active list

        self._active = live
        return result

    @property
    def active_count(self) -> int:
        return len(self._active)

    def clear(self) -> None:
        self._active.clear()


# ─────────────────────── Convenience ───────────────────────


def list_known_publication_types() -> list[str]:
    """Return all known canonical publication types."""
    return sorted(_EFFECT_TABLE.keys())


def list_aliases() -> dict[str, str]:
    """Return the alias → canonical mapping."""
    return dict(_ALIASES)


__all__ = [
    "AggregateEffect",
    "DecayingEffect",
    "EffectTracker",
    "PublicationEffect",
    "aggregate_round_effects",
    "apply_effects_to_participation",
    "apply_effects_to_risk_budget",
    "apply_effects_to_sentiment",
    "compute_publication_effect",
    "list_aliases",
    "list_known_publication_types",
]
