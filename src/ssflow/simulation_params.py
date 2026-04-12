"""Per-simulation calibration parameters.

Every constant in this module was previously a module-level hardcode in
``market_dynamics.py``, ``trading_layer.py``, ``oasis_engine.py``,
``sub_population_styles.py``, ``publication_effects.py``, or
``self_model/atoms.py``.  Moving them into a frozen dataclass that travels
with ``run_simulation`` means each simulation can have a different
parameterisation without editing source code.

The ``defaults()`` classmethod returns exactly the pre-v5 hardcoded values
so backward compat is zero-cost: ``sim_params=None`` → ``defaults()``.

``generate_sim_params(event, ...)`` is the formula-derived generator called
at Setup time.  It uses deterministic math (severity × ADV × volatility),
not LLM calls, so it's instant, free, and reproducible.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .sub_population_styles import STYLE_TILT as _DEFAULT_STYLE_TILT


# ─────────────────────── Nested param groups ───────────────────────


@dataclass(frozen=True)
class MarketParams:
    """Kyle impact model + price caps."""

    lambda_market: float = 0.5
    flow_knee: float = 0.08
    max_delta_pct_per_round: float = 0.10
    sentiment_scale: float = 0.3
    resistance_scale: float = 3.0
    adv_ema_alpha: float = 0.3
    adv_floor_pct: float = 0.20
    adv_ceiling_pct: float = 3.0
    cadence_caps: dict[str, tuple[float, float]] = field(
        default_factory=lambda: {
            "daily": (0.10, 0.08),
            "weekly": (0.20, 0.15),
            "monthly": (0.25, 0.20),
        }
    )


@dataclass(frozen=True)
class ParticipationParams:
    """Urgency decay, momentum exhaustion, participation floors."""

    inactive_base: float = 0.15
    urgency_decay: float = 0.3
    exhaustion_coeff: float = 2.0
    min_participation: float = 0.05
    cascade_liquidation_frac_limit: float = 0.40
    cascade_liquidation_frac_normal: float = 0.30


@dataclass(frozen=True)
class DecisionParams:
    """Conviction bucketing thresholds and dispersion formula weights."""

    conviction_threshold: float = 0.02
    herd_weight: float = 0.5
    contrarian_weight: float = 0.3
    min_dispersion: float = 0.05
    gap_volatility_base: float = 0.02
    gap_volatility_sentiment_threshold: float = 0.3
    gap_volatility_sentiment_scale: float = 0.10
    style_tilt: dict[str, dict[str, float]] = field(
        default_factory=lambda: {
            style: dict(mapping) for style, mapping in _DEFAULT_STYLE_TILT.items()
        }
    )


@dataclass(frozen=True)
class SelfModelParams:
    """Atom update rates — stress, regret, conviction learning signals."""

    conviction_win_delta: float = 0.15
    conviction_loss_delta: float = 0.10
    stress_drag_on_conviction: float = 0.05
    stress_per_10pp_loss: float = 0.1
    drawdown_stress_divisor: float = 200.0
    regret_move_threshold: float = 2.0
    regret_accumulation_divisor: float = 50.0
    regret_decay: float = 0.05
    redemption_drawdown_trigger: float = 10.0
    redemption_rate_divisor: float = 100.0


@dataclass(frozen=True)
class PublicationEffectEntry:
    """One row of the publication effect table."""

    sentiment: float = 0.0
    participation_modifier: float = 1.0
    urgency_modifier: float = 1.0
    risk_budget_shift: float = 0.0
    affected_types: list[str] = field(default_factory=lambda: ["all"])
    decay_rounds: int = 2


_DEFAULT_PUBLICATION_EFFECTS: dict[str, dict[str, Any]] = {
    "exchange_inquiry": {
        "sentiment": -0.15, "participation_modifier": 0.7,
        "urgency_modifier": 1.3, "risk_budget_shift": -0.20,
        "affected_types": ["all"], "decay_rounds": 3,
    },
    "csrc_verbal_support": {
        "sentiment": 0.20, "participation_modifier": 1.2,
        "urgency_modifier": 0.8, "risk_budget_shift": 0.10,
        "affected_types": ["all"], "decay_rounds": 2,
    },
    "national_team_buy": {
        "sentiment": 0.35, "participation_modifier": 1.5,
        "urgency_modifier": 1.0, "risk_budget_shift": 0.30,
        "affected_types": ["all"], "decay_rounds": 4,
    },
    "analyst_upgrade": {
        "sentiment": 0.10, "participation_modifier": 1.1,
        "urgency_modifier": 1.0, "risk_budget_shift": 0.05,
        "affected_types": ["institutional", "retail_active"], "decay_rounds": 2,
    },
    "analyst_downgrade": {
        "sentiment": -0.10, "participation_modifier": 1.1,
        "urgency_modifier": 1.0, "risk_budget_shift": -0.05,
        "affected_types": ["institutional", "retail_active"], "decay_rounds": 2,
    },
    "kol_hype_post": {
        "sentiment": 0.05, "participation_modifier": 1.3,
        "urgency_modifier": 1.5, "risk_budget_shift": 0.02,
        "affected_types": ["retail_active", "retail_passive"], "decay_rounds": 1,
    },
    "official_media_warning": {
        "sentiment": -0.25, "participation_modifier": 0.5,
        "urgency_modifier": 0.6, "risk_budget_shift": -0.30,
        "affected_types": ["all"], "decay_rounds": 3,
    },
    "broker_margin_call": {
        "sentiment": -0.10, "participation_modifier": 0.8,
        "urgency_modifier": 2.0, "risk_budget_shift": -0.40,
        "affected_types": ["leveraged"], "decay_rounds": 1,
    },
}


@dataclass(frozen=True)
class PublicationEffectParams:
    """Per-publication-type effect overrides."""

    effect_table: dict[str, dict[str, Any]] = field(
        default_factory=lambda: dict(_DEFAULT_PUBLICATION_EFFECTS)
    )
    decay_factor: float = 0.6


# ─────────────────────── Top-level bundle ───────────────────────


@dataclass(frozen=True)
class SimulationParams:
    """All per-simulation calibration parameters, bundled.

    ``defaults()`` reproduces exactly the pre-v5 hardcoded values.
    ``generate()`` produces context-dependent values from event + market data.
    ``to_dict()`` / ``from_dict()`` support JSON round-trip for the frontend.
    """

    market: MarketParams = field(default_factory=MarketParams)
    participation: ParticipationParams = field(default_factory=ParticipationParams)
    decision: DecisionParams = field(default_factory=DecisionParams)
    self_model: SelfModelParams = field(default_factory=SelfModelParams)
    publication: PublicationEffectParams = field(default_factory=PublicationEffectParams)
    generation_source: str = "defaults"

    @classmethod
    def defaults(cls) -> SimulationParams:
        return cls()

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)  # type: ignore[arg-type]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SimulationParams:
        return cls(
            market=MarketParams(**d.get("market", {})),
            participation=ParticipationParams(**d.get("participation", {})),
            decision=DecisionParams(**d.get("decision", {})),
            self_model=SelfModelParams(**d.get("self_model", {})),
            publication=PublicationEffectParams(**d.get("publication", {})),
            generation_source=d.get("generation_source", "dict"),
        )


# ─────────────────────── Formula-based generator ───────────────────────


def generate_sim_params(
    *,
    event_type: str = "",
    severity_signed: float = 0.0,
    adv_value: float = 0.0,
    float_cap_cny: float = 0.0,
    monthly_volatility: float = 0.0,
    board_type: str = "normal",
) -> SimulationParams:
    """Derive simulation parameters from event + market context.

    All formulas are deterministic — no LLM calls, no randomness.
    The goal is to produce *reasonable* parameters that track the
    severity / liquidity / volatility of the specific simulation,
    instead of using one-size-fits-all defaults.

    Args:
        event_type: "policy", "earnings", "regulatory", etc.
        severity_signed: [-1, +1] from EventProposal
        adv_value: average daily volume in CNY
        float_cap_cny: free-float market cap in CNY
        monthly_volatility: historical monthly return stdev (0-1)
        board_type: "normal", "chinext", "star", "st"
    """
    abs_severity = min(abs(severity_signed), 1.0)

    # ── Market params ──
    # Higher severity → lower lambda (stronger impact per unit flow)
    # Higher ADV → higher lambda (market absorbs flow more easily)
    base_lambda = 0.5
    if adv_value > 0:
        adv_bn = adv_value / 1e9
        base_lambda = max(0.10, min(0.80, 0.3 + 0.1 * math.log10(max(adv_bn, 0.01))))
    lambda_adj = base_lambda * (1.0 - 0.3 * abs_severity)
    lambda_market = max(0.06, min(0.80, lambda_adj))

    # Flow knee: tighter for high-severity events (market saturates faster)
    flow_knee = max(0.02, 0.08 - 0.04 * abs_severity)

    # Sentiment scale: stronger for high-severity
    sentiment_scale = 0.2 + 0.2 * abs_severity

    # Monthly cap: wider for volatile names
    if monthly_volatility > 0:
        monthly_cap = max(0.15, min(0.40, monthly_volatility * 1.5))
    else:
        monthly_cap = 0.25
    monthly_knee = monthly_cap * 0.8

    cadence_caps = {
        "daily": (0.10, 0.08),
        "weekly": (0.20, 0.15),
        "monthly": (monthly_cap, monthly_knee),
    }

    market = MarketParams(
        lambda_market=round(lambda_market, 4),
        flow_knee=round(flow_knee, 4),
        sentiment_scale=round(sentiment_scale, 3),
        cadence_caps=cadence_caps,
    )

    # ── Participation params ──
    # High-severity events draw more participants, slower exhaustion
    urgency_decay = max(0.10, 0.35 - 0.15 * abs_severity)
    exhaustion_coeff = max(0.5, 2.5 - 1.5 * abs_severity)

    participation = ParticipationParams(
        urgency_decay=round(urgency_decay, 3),
        exhaustion_coeff=round(exhaustion_coeff, 3),
    )

    # ── Decision params ──
    # Higher volatility → wider conviction threshold (avoid noise trading)
    conv_threshold = 0.02
    if monthly_volatility > 0:
        conv_threshold = max(0.01, min(0.06, monthly_volatility * 0.3))

    # Gap volatility scales with severity
    gap_vol = 0.02 + 0.03 * abs_severity

    decision = DecisionParams(
        conviction_threshold=round(conv_threshold, 4),
        gap_volatility_base=round(gap_vol, 4),
    )

    # Self-model and publication: use defaults for now (Phase 2 extension)
    return SimulationParams(
        market=market,
        participation=participation,
        decision=decision,
        generation_source="formula",
    )


__all__ = [
    "DecisionParams",
    "MarketParams",
    "ParticipationParams",
    "PublicationEffectParams",
    "SelfModelParams",
    "SimulationParams",
    "generate_sim_params",
]
