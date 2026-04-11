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
from dataclasses import dataclass, field

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
# At knee=0.08: flow/ADV=1% → ~4.3%, 3% → ~7.8%, 5% → ~9.1%, 10%+ → capped
# at ±10% (涨停板). The knee is set so that the A-share price limit is the
# binding constraint, not the compression curve.
FLOW_KNEE: float = 0.08


# ── Cadence-aware caps ──
# The daily-calibrated 10% cap + 8% knee are appropriate when each round
# represents one trading day. Monthly backtests (backtest_monthly.py) saw
# real policy events move +70% in a single month — the daily cap clipped
# the sim's M+1 move to +3-4% on a +70% real event. These per-cadence
# overrides preserve the daily defaults for the common case while letting
# longer-horizon schedules express realistic month-scale impact.
#
# CADENCE_CAPS[cadence] = (max_delta_pct, base_knee)
CADENCE_CAPS: dict[str, tuple[float, float]] = {
    "daily": (0.10, 0.08),   # A-share 涨跌停 + standard knee (default)
    "weekly": (0.20, 0.15),  # relaxed but still bounded
    "monthly": (0.40, 0.30), # policy-level events can move >30% in a month
}


def caps_for_cadence(cadence: str) -> tuple[float, float]:
    """Return (max_delta_pct, base_knee) for a schedule cadence label.

    Unknown labels fall through to the daily defaults so existing callers
    that don't pass a cadence are unaffected.
    """
    return CADENCE_CAPS.get((cadence or "daily").lower(), CADENCE_CAPS["daily"])


def compute_dynamic_knee(
    n_active_classes: int,
    total_classes: int,
    cumulative_abs_delta: float,
    round_idx: int,
    base_knee: float = FLOW_KNEE,
    *,
    resistance_scale: float = 3.0,
) -> float:
    """Compute a round-specific flow knee that adapts to market state.

    Fewer active participant classes → less liquidity → lower knee → smaller
    maximum delta.  Large cumulative price moves → more resistance from
    thinning marginal flow → lower effective knee.

    The ``resistance_scale`` parameter is the slope of the cumulative-move
    resistance curve. 3.0 is calibrated for daily cadence where +5%
    cumulative is already a notable move. For monthly cadence, callers
    should pass a smaller value (e.g. 0.8) so that +20% cumulative over
    the first month doesn't starve the knee on M+1.

    Returns an adjusted knee value in [0.002, base_knee].
    """
    participation_frac = n_active_classes / max(total_classes, 1)
    scaled = base_knee * participation_frac
    resistance = 1.0 / (1.0 + resistance_scale * cumulative_abs_delta)
    adjusted = scaled * resistance
    return max(0.002, min(base_knee, adjusted))


@dataclass
class AdaptiveADV:
    """Tracks round-over-round effective ADV for Kyle price impact.

    Blends observed round volume with a baseline via exponential smoothing.
    Clamped to [floor_pct, ceiling_pct] of baseline to prevent runaway
    feedback loops (e.g., volume collapse → infinite impact → more collapse).

    Usage:
        adv = AdaptiveADV(baseline=8e9)
        # Each round, after computing class flows:
        effective = adv.update(total_abs_flow)
        delta = compute_price_impact(net_flow, adv_value=effective, ...)
    """

    baseline: float               # Original event.adv_value (immutable reference)
    alpha: float = 0.3            # EMA smoothing (higher = more responsive to recent)
    floor_pct: float = 0.20       # ADV never drops below 20% of baseline
    ceiling_pct: float = 3.0      # ADV never exceeds 300% of baseline
    _current: float = 0.0
    _history: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self._current == 0.0:
            self._current = self.baseline

    @property
    def effective(self) -> float:
        """Current effective ADV value."""
        return self._current

    def update(self, observed_volume: float) -> float:
        """Update ADV based on this round's observed absolute flow.

        Returns the new effective ADV after EMA blending + clamping.
        """
        raw = self.alpha * observed_volume + (1.0 - self.alpha) * self._current
        floor = self.baseline * self.floor_pct
        ceiling = self.baseline * self.ceiling_pct
        self._current = max(floor, min(ceiling, raw))
        self._history.append(self._current)
        if abs(self._current - self.baseline) / self.baseline > 0.1:
            log.info(
                "AdaptiveADV: baseline=%.2e, effective=%.2e (%.0f%% of baseline), "
                "observed=%.2e",
                self.baseline, self._current,
                self._current / self.baseline * 100, observed_volume,
            )
        return self._current

    @property
    def history(self) -> list[float]:
        """Effective ADV after each update() call."""
        return list(self._history)


def compute_price_impact(
    net_flow_value: float,
    adv_value: float,
    lambda_market: float = LAMBDA_LITERATURE["default"],
    max_delta_pct: float = MAX_DELTA_PCT_PER_ROUND,
    flow_knee: float = FLOW_KNEE,
    sentiment_modifier: float = 0.0,
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
        sentiment_modifier: Multiplier on the raw delta from announcements/
            regulations. Positive amplifies, negative dampens. Default 0.0.

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

    # Stage 2.5: sentiment modifier — announcements/regulations shift the delta
    if sentiment_modifier != 0.0:
        raw_delta = raw_delta * (1.0 + sentiment_modifier)

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
    "AdaptiveADV",
    "FLOW_KNEE",
    "LAMBDA_LITERATURE",
    "MAX_DELTA_PCT_PER_ROUND",
    "compute_dynamic_knee",
    "compute_multi_instrument_impact",
    "compute_price_impact",
    "lambda_for_market",
]
