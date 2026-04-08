"""Market microstructure primitives — Kyle square-root price impact.

This module is intentionally tiny: pure functions, no runtime dependencies.
Anything that wants to compute "what happens to a price when N order flow
hits the book" imports from here. Called once per round by `oasis_engine`
after summing the per-trader net flows.

Source references:
    - Kyle 1985, "Continuous Auctions and Insider Trading"
    - Almgren & Chriss 2001, "Optimal Execution of Portfolio Transactions"
    - Lillo, Farmer, Mantegna 2003 (calibrated λ for equity markets)
    - Bouchaud 2010 review (Chinese A-share approximations)
"""

from __future__ import annotations

import math


# Market impact coefficients from the literature. Calibration from 30+
# historical events is a separate research project; for now these are the
# defaults. Override per-call by passing `lambda_market` explicitly to
# `compute_price_impact`.
LAMBDA_LITERATURE: dict[str, float] = {
    "ashare": 0.5,        # Bouchaud 2010, Chinese A-share approximations
    "us-equity": 0.3,     # Almgren et al., US large-cap
    "crude-oil-wti": 0.4, # Petroleum derivatives literature
    "default": 0.5,
}

# Per-round price impact cap. Models A-share daily 涨停/跌停 limit (±10%).
# Without this cap, the unbounded Kyle formula produces nonsense at very large
# net flows (e.g., ¥3000億 flow into a stock with ¥80億 ADV would yield
# ΔP = +325% per round, which is physically impossible). The cap is applied
# AFTER the Kyle formula computes the raw delta, so the formula's sign +
# magnitude ranking is preserved up to the cap.
MAX_DELTA_PCT_PER_ROUND: float = 0.10


def compute_price_impact(
    net_flow_value: float,
    adv_value: float,
    lambda_market: float = LAMBDA_LITERATURE["default"],
    max_delta_pct: float = MAX_DELTA_PCT_PER_ROUND,
) -> float:
    """Square-root market impact (Kyle 1985 / Almgren-Chriss 2001).

        ΔP/P = clip(λ × sign(net_flow) × sqrt(|net_flow| / ADV), ±max_delta_pct)

    The result is clipped to ±max_delta_pct (default ±10%, modeling A-share
    daily 涨停板 limit; override for markets with different rules). Without
    the cap, unbounded Kyle output produces nonsense at extreme net flows.

    Currency-agnostic: net_flow_value and adv_value must be in the SAME unit
    (CNY for A-share, USD for US equity, USD for crude oil futures, etc.).

    Per Gotcha 5 (locked in plan §9): λ applies ONCE to the summed net flow
    across all persona classes, NOT per-class then summed. Per-class application
    overestimates impact dramatically because sqrt is concave.

    Args:
        net_flow_value: Net order flow this round (positive = net buy,
            negative = net sell). Already aggregated across all persona classes.
        adv_value: Average daily volume in the same currency as net_flow.
            Trailing 30 days is the conventional window. Must be > 0.
        lambda_market: Market impact coefficient (dimensionless). See
            LAMBDA_LITERATURE for default values per market.
        max_delta_pct: Per-round absolute price-change cap (default ±0.10).

    Returns:
        Fractional price change as a float (e.g., -0.097 means -9.7%).
        Returns 0.0 if net_flow_value is exactly 0.

    Raises:
        ValueError: if adv_value <= 0.

    Example (BYD case from spec §9.3):
        >>> compute_price_impact(net_flow_value=-3e8, adv_value=8e9, lambda_market=0.5)
        -0.0968...  # ≈ -9.7% (within the ±10% cap)
    """
    if adv_value <= 0:
        raise ValueError(f"adv_value must be > 0, got {adv_value}")
    if net_flow_value == 0:
        return 0.0

    sign = 1.0 if net_flow_value > 0 else -1.0
    magnitude = math.sqrt(abs(net_flow_value) / adv_value)
    raw = lambda_market * sign * magnitude
    return max(-max_delta_pct, min(max_delta_pct, raw))


def lambda_for_market(market_slug: str | None) -> float:
    """Look up λ from the literature table by market slug, with fallback."""
    if not market_slug:
        return LAMBDA_LITERATURE["default"]
    return LAMBDA_LITERATURE.get(market_slug, LAMBDA_LITERATURE["default"])


__all__ = [
    "LAMBDA_LITERATURE",
    "MAX_DELTA_PCT_PER_ROUND",
    "compute_price_impact",
    "lambda_for_market",
]
