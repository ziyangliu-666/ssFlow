"""Direction balance regression guard for persona packs.

Ensures persona sets have enough voting weight on both bullish and
bearish sides. Prevents shipping an all-bull or all-bear persona set.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ssflow.persona import load_personas


_REPO_ROOT = Path(__file__).resolve().parent.parent
_ASHARE = _REPO_ROOT / "personas" / "ashare.yaml"


def _compute_direction_weights(personas):
    """Compute aggregate buy vs sell action weight across all trading personas."""
    total_buy = 0.0
    total_sell = 0.0
    total_hold = 0.0

    for p in personas:
        if not p.sandbox or not p.sandbox.action_space:
            continue
        volume_weight = (p.market_share.get("by_volume") if p.market_share else None) or 0.01
        for action in p.sandbox.action_space:
            w = action.get("weight", 1.0) * volume_weight
            side = action.get("side", "none")
            if side == "buy":
                total_buy += w
            elif side == "sell":
                total_sell += w
            else:
                total_hold += w

    total = total_buy + total_sell + total_hold
    if total == 0:
        return 0.0, 0.0
    return total_buy / total, total_sell / total


def test_ashare_has_bearish_weight():
    personas = load_personas(str(_ASHARE))
    buy_pct, sell_pct = _compute_direction_weights(personas)
    assert sell_pct >= 0.15, (
        f"Bearish action weight too low: {sell_pct:.1%}. "
        f"Need >= 15% sell/short weight to handle bearish events."
    )


def test_ashare_has_bullish_weight():
    personas = load_personas(str(_ASHARE))
    buy_pct, sell_pct = _compute_direction_weights(personas)
    assert buy_pct >= 0.15, (
        f"Bullish action weight too low: {buy_pct:.1%}. "
        f"Need >= 15% buy weight to handle bullish events."
    )


def test_ashare_not_extremely_skewed():
    personas = load_personas(str(_ASHARE))
    buy_pct, sell_pct = _compute_direction_weights(personas)
    if buy_pct > 0 and sell_pct > 0:
        ratio = max(buy_pct, sell_pct) / min(buy_pct, sell_pct)
        assert ratio <= 5.0, (
            f"Buy/sell ratio too skewed: {ratio:.1f}x "
            f"(buy={buy_pct:.1%}, sell={sell_pct:.1%}). Max 5x."
        )
