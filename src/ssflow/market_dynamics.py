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

import logging
import math

log = logging.getLogger(__name__)


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

# ── Liquidity constraint (soft compression) ──
# Real A-share net directional flow is typically 1-3% of ADV because most
# volume is offsetting. Even on extreme days, net flow rarely exceeds 5%.
# But our sim aggregates all participant classes into one pool, producing
# flow/ADV ratios of 10-70% — physically impossible to execute.
#
# We use a soft compression: effective_ratio = knee × (1 - e^(-raw/knee))
# This is approximately linear for small flows (preserves signal) and
# asymptotes to FLOW_KNEE for large flows (prevents saturation).
# At knee=0.03: flow/ADV=3% passes through at ~1.9%, flow/ADV=30% compresses
# to ~3.0%, flow/ADV=100% → ~3.0%. This produces Kyle deltas of 1-9%.
FLOW_KNEE: float = 0.03


def compute_price_impact(
    net_flow_value: float,
    adv_value: float,
    lambda_market: float = LAMBDA_LITERATURE["default"],
    max_delta_pct: float = MAX_DELTA_PCT_PER_ROUND,
    flow_knee: float = FLOW_KNEE,
) -> float:
    """Square-root market impact (Kyle 1985) with soft flow compression.

        raw_ratio    = |net_flow| / ADV
        eff_ratio    = knee × (1 - e^(-raw_ratio / knee))   [soft compress]
        effective_flow = sign(net_flow) × eff_ratio × ADV
        ΔP/P = clip(λ × sign × sqrt(eff_ratio), ±max_delta_pct)

    Two-stage guard:
      1. **Soft liquidity compression** (flow_knee): maps the raw flow/ADV
         ratio through an exponential saturation curve. Small flows pass
         nearly linearly; large flows asymptote to `flow_knee`. This models
         the real-market constraint that net directional flow rarely exceeds
         a few percent of ADV, while preserving the *relative* ordering of
         different conviction levels (unlike a hard cap which collapses them).
      2. **Price limit** (max_delta_pct): the resulting delta is capped to
         ±10% (A-share 涨停/跌停). Rarely triggered after soft compression.

    Args:
        net_flow_value: Net order flow this round, aggregated across classes.
        adv_value: Average daily volume. Must be > 0.
        lambda_market: Market impact coefficient (default 0.5 for A-share).
        max_delta_pct: Per-round price-change cap (default ±10%).
        flow_knee: Soft compression knee point (default 0.03 = 3% of ADV).

    Returns:
        Fractional price change (e.g., -0.05 means -5%).

    Example:
        >>> compute_price_impact(net_flow_value=-3e8, adv_value=8e9, lambda_market=0.5)
        -0.05...
    """
    if adv_value <= 0:
        raise ValueError(f"adv_value must be > 0, got {adv_value}")
    if net_flow_value == 0:
        return 0.0

    # Stage 1: soft flow compression
    sign = 1.0 if net_flow_value > 0 else -1.0
    raw_ratio = abs(net_flow_value) / adv_value
    eff_ratio = flow_knee * (1.0 - math.exp(-raw_ratio / flow_knee))
    effective_flow = sign * eff_ratio * adv_value

    if raw_ratio > flow_knee * 1.5:
        log.info(
            "Flow compressed: raw |flow|/ADV=%.1f%% → effective=%.2f%% "
            "(knee=%.1f%%, raw=%.2e)",
            raw_ratio * 100, eff_ratio * 100,
            flow_knee * 100, net_flow_value,
        )

    # Stage 2: Kyle
    magnitude = math.sqrt(eff_ratio)
    raw_delta = lambda_market * sign * magnitude
    clamped = max(-max_delta_pct, min(max_delta_pct, raw_delta))
    if abs(raw_delta) > max_delta_pct:
        log.warning(
            "Kyle raw delta %.2f%% capped to %.2f%% "
            "(eff_flow=%.2e, adv=%.2e)",
            raw_delta * 100, clamped * 100,
            effective_flow, adv_value,
        )
    else:
        log.info(
            "Kyle delta %+.2f%% (eff_ratio=%.2f%%, raw_ratio=%.1f%%)",
            raw_delta * 100, eff_ratio * 100, raw_ratio * 100,
        )
    return clamped


def lambda_for_market(market_slug: str | None) -> float:
    """Look up λ from the literature table by market slug, with fallback."""
    if not market_slug:
        return LAMBDA_LITERATURE["default"]
    return LAMBDA_LITERATURE.get(market_slug, LAMBDA_LITERATURE["default"])


def compute_multi_instrument_impact(
    flows_by_ticker: dict[str, float],
    adv_by_ticker: dict[str, float],
    lambda_market: float = LAMBDA_LITERATURE["default"],
    max_delta_pct: float = MAX_DELTA_PCT_PER_ROUND,
) -> dict[str, float]:
    """Per-instrument Kyle impact for multiple instruments.

    Each ticker with non-zero flow gets its own independent Kyle
    calculation using its own ADV. Tickers not in flows_by_ticker
    are excluded (they may get spillover elsewhere).

    Returns dict[ticker, delta_pct].
    """
    result: dict[str, float] = {}
    for ticker, flow in flows_by_ticker.items():
        adv = adv_by_ticker.get(ticker)
        if adv is None or adv <= 0:
            continue
        result[ticker] = compute_price_impact(
            net_flow_value=flow,
            adv_value=adv,
            lambda_market=lambda_market,
            max_delta_pct=max_delta_pct,
        )
    return result


__all__ = [
    "LAMBDA_LITERATURE",
    "MAX_DELTA_PCT_PER_ROUND",
    "compute_price_impact",
    "compute_multi_instrument_impact",
    "lambda_for_market",
]
