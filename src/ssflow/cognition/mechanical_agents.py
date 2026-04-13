"""Mechanical agents: signal-based decision logic, no LLM.

Some agent types should NOT use LLM for trading decisions:
- Quant: signal-driven (momentum, mean-reversion, stat-arb)
- ETF AP: NAV arbitrage (premium -> redeem, discount -> create)
- Market maker: two-sided quoting with inventory management

These agents produce StructuredIntents from pure math,
ensuring they behave like their real-world counterparts
instead of "writing like a financial commentator".
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from .intent_resolver import StructuredIntent, Urgency

log = logging.getLogger(__name__)


# ─── Quant Signal Agent ───


@dataclass
class QuantSignal:
    """Quantitative trading signal."""

    name: str              # "momentum_5d", "mean_reversion_30d", etc.
    value: float           # signal strength, typically [-1, +1]
    confidence: float      # 0-1
    half_life_rounds: int  # signal decay


def quant_decision(
    signals: list[QuantSignal],
    current_price: float,
    vwap: float | None,
    price_history: list[float],
    holdings: float,
    cash: float,
    persona_id: str = "quant",
    agent_id: str = "quant_001",
) -> StructuredIntent:
    """Compute a quant agent's trading intent from signals.

    No LLM — pure math. Combines signals into a composite score,
    then maps to a trading intent.
    """
    if not signals and not price_history:
        return StructuredIntent(
            persona_id=persona_id, agent_id=agent_id,
            side="hold", size_fraction=0.0, urgency=Urgency.NONE,
            rationale="No signals available",
        )

    # Auto-generate signals from price history if none provided
    if not signals and price_history:
        signals = _auto_signals(price_history, current_price, vwap)

    # Composite signal: weighted average
    total_weight = sum(abs(s.value) * s.confidence for s in signals)
    if total_weight == 0:
        return StructuredIntent(
            persona_id=persona_id, agent_id=agent_id,
            side="hold", size_fraction=0.0, urgency=Urgency.NONE,
            rationale="All signals neutral",
        )

    composite = sum(s.value * s.confidence for s in signals) / len(signals)

    # Decision thresholds
    BUY_THRESHOLD = 0.15
    SELL_THRESHOLD = -0.15

    if composite > BUY_THRESHOLD:
        side = "buy"
        size = min(0.3, abs(composite))
        urgency = Urgency.PATIENT
        rationale = f"Composite signal {composite:+.2f} > threshold {BUY_THRESHOLD}"
    elif composite < SELL_THRESHOLD:
        if holdings > 0:
            side = "sell"
            size = min(0.5, abs(composite))
            rationale = f"Composite signal {composite:+.2f} < threshold {SELL_THRESHOLD}"
        else:
            side = "short"
            size = min(0.2, abs(composite))
            rationale = f"Short signal {composite:+.2f}, no long position to sell"
        urgency = Urgency.PATIENT
    else:
        side = "hold"
        size = 0.0
        urgency = Urgency.NONE
        rationale = f"Composite signal {composite:+.2f} within neutral zone"

    # Signal names for rationale
    sig_summary = ", ".join(f"{s.name}={s.value:+.2f}" for s in signals[:3])

    return StructuredIntent(
        persona_id=persona_id,
        agent_id=agent_id,
        side=side,
        size_fraction=size,
        urgency=urgency,
        confidence=min(1.0, abs(composite)),
        rationale=f"{rationale}. Signals: [{sig_summary}]",
    )


def _auto_signals(
    price_history: list[float],
    current_price: float,
    vwap: float | None,
) -> list[QuantSignal]:
    """Generate basic signals from price history."""
    signals = []

    if len(price_history) >= 2:
        # Momentum: recent return
        ret = (current_price / price_history[-1]) - 1.0
        signals.append(QuantSignal(
            name="momentum_1d",
            value=max(-1.0, min(1.0, ret * 10)),  # scale: 10% move -> signal 1.0
            confidence=0.6,
            half_life_rounds=3,
        ))

    if len(price_history) >= 5:
        # 5-period momentum
        ret5 = (current_price / price_history[-5]) - 1.0
        signals.append(QuantSignal(
            name="momentum_5d",
            value=max(-1.0, min(1.0, ret5 * 5)),
            confidence=0.5,
            half_life_rounds=5,
        ))

    if vwap and current_price > 0:
        # VWAP deviation (mean-reversion signal)
        dev = (current_price - vwap) / vwap
        signals.append(QuantSignal(
            name="vwap_deviation",
            value=max(-1.0, min(1.0, -dev * 10)),  # negative: price above VWAP -> sell
            confidence=0.7,
            half_life_rounds=2,
        ))

    return signals


# ─── ETF AP Agent ───


def etf_ap_decision(
    etf_price: float,
    nav: float,
    premium_threshold: float = 0.005,  # 0.5% premium -> redeem
    discount_threshold: float = -0.005,  # 0.5% discount -> create
    persona_id: str = "etf_ap",
    agent_id: str = "etf_ap_001",
) -> StructuredIntent:
    """ETF Authorized Participant decision: pure NAV arbitrage.

    Premium (ETF price > NAV): redeem shares (sell ETF, buy basket)
    Discount (ETF price < NAV): create shares (buy ETF, sell basket)

    No LLM — this is pure mechanical arbitrage logic.
    """
    if nav <= 0 or etf_price <= 0:
        return StructuredIntent(
            persona_id=persona_id, agent_id=agent_id,
            side="hold", size_fraction=0.0, urgency=Urgency.NONE,
            rationale="No valid NAV/price data",
        )

    premium = (etf_price - nav) / nav

    if premium > premium_threshold:
        # ETF trading at premium -> sell ETF (in the ETF market)
        # In terms of underlying: this means selling the underlying basket
        return StructuredIntent(
            persona_id=persona_id,
            agent_id=agent_id,
            side="sell",
            size_fraction=min(0.3, premium * 10),
            urgency=Urgency.IMMEDIATE,  # arb is time-sensitive
            confidence=min(1.0, premium * 20),
            rationale=f"ETF premium {premium*100:+.1f}% > threshold {premium_threshold*100:.1f}%, redeem/sell",
        )
    elif premium < discount_threshold:
        # ETF trading at discount -> buy ETF
        return StructuredIntent(
            persona_id=persona_id,
            agent_id=agent_id,
            side="buy",
            size_fraction=min(0.3, abs(premium) * 10),
            urgency=Urgency.IMMEDIATE,
            confidence=min(1.0, abs(premium) * 20),
            rationale=f"ETF discount {premium*100:+.1f}% < threshold {discount_threshold*100:.1f}%, create/buy",
        )
    else:
        return StructuredIntent(
            persona_id=persona_id,
            agent_id=agent_id,
            side="hold",
            size_fraction=0.0,
            urgency=Urgency.NONE,
            rationale=f"ETF premium {premium*100:+.1f}% within no-arb band [{discount_threshold*100:.1f}%, {premium_threshold*100:.1f}%]",
        )


__all__ = [
    "QuantSignal",
    "etf_ap_decision",
    "quant_decision",
]
