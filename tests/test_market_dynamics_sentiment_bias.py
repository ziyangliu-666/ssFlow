"""Regression tests for the sentiment_modifier semantic fix in
``ssflow.market_dynamics.compute_price_impact``.

The pre-fix formula was ``raw_delta = raw_delta * (1 + sentiment_modifier)``,
which AMPLIFIED whatever direction the net flow already pointed to rather than
BIASING the delta toward the sentiment. That meant a policy-bull event
(sentiment=+0.5) layered on top of a mildly bearish net flow produced an
*even more bearish* delta — the exact wrong semantic. The full 5-event
monthly backtest on 2026-04-11 showed this symptom on 宁德时代 and 东方财富
(both policy-bull events that fell in sim despite +0.6 severity prior).

The fix is additive:

    raw_delta = raw_delta + sentiment_modifier * max_delta_pct * 0.3

with ``sentiment_modifier`` already clamped to ``[-0.5, 0.5]`` upstream in
``oasis_engine.py``. The 0.3 scale is deliberately conservative — the
max additive bias on a daily cadence is ±0.015 (±1.5pp) and on a monthly
cadence (max_delta_pct=0.40) is ±0.06 (±6pp). The goal is a tilt, not a
price command.

The four-quadrant matrix below locks in the correct semantic:

    +---------------+---------------+---------------+
    |               | flow > 0 (buy)| flow < 0(sell)|
    +---------------+---------------+---------------+
    | sentiment > 0 | delta MORE +  | delta LESS -  |
    | sentiment < 0 | delta LESS +  | delta MORE -  |
    +---------------+---------------+---------------+

Under the OLD formula row 1 col 2 and row 2 col 1 were inverted (sentiment
amplified the wrong direction). These tests assert the NEW formula.
"""

from __future__ import annotations

import pytest

from ssflow.market_dynamics import compute_price_impact


# Reference inputs: pick magnitudes that produce a non-trivial raw_delta
# that is still far from the ±10% cap, so the sentiment term is observable.
_BUY_FLOW = 2.0e8
_SELL_FLOW = -2.0e8
_ADV = 8.0e9
_LAMBDA = 0.5
_KNEE = 0.08


def _baseline(net_flow: float) -> float:
    """Delta with zero sentiment — the unbiased Kyle+compression result."""
    return compute_price_impact(
        net_flow_value=net_flow,
        adv_value=_ADV,
        lambda_market=_LAMBDA,
        flow_knee=_KNEE,
        sentiment_modifier=0.0,
    )


class TestSentimentBiasIsAdditive:
    """Four-quadrant coverage of the sentiment + flow cartesian product."""

    def test_quadrant_bull_sentiment_buy_flow_pushes_higher(self):
        """+sent + buy → delta MORE positive than baseline (reinforces bull)."""
        base = _baseline(_BUY_FLOW)
        with_sent = compute_price_impact(
            net_flow_value=_BUY_FLOW,
            adv_value=_ADV,
            lambda_market=_LAMBDA,
            flow_knee=_KNEE,
            sentiment_modifier=0.5,
        )
        assert with_sent > base
        # Quantitative: additive term should be +0.5 * 0.10 * 0.3 = +0.015
        assert with_sent == pytest.approx(base + 0.015, abs=1e-4)

    def test_quadrant_bull_sentiment_sell_flow_dampens_fall(self):
        """+sent + sell → delta LESS negative than baseline.

        This is the load-bearing case: policy-bull event on a panicky day
        should soften the sell-driven drop, NOT amplify it. The old formula
        got this wrong.
        """
        base = _baseline(_SELL_FLOW)  # negative
        with_sent = compute_price_impact(
            net_flow_value=_SELL_FLOW,
            adv_value=_ADV,
            lambda_market=_LAMBDA,
            flow_knee=_KNEE,
            sentiment_modifier=0.5,
        )
        assert base < 0  # sanity: baseline is bearish
        assert with_sent > base  # less negative = dampened
        assert with_sent == pytest.approx(base + 0.015, abs=1e-4)

    def test_quadrant_bear_sentiment_buy_flow_dampens_rally(self):
        """-sent + buy → delta LESS positive than baseline.

        Symmetric opposite of the load-bearing case. Regulatory event on
        a bullish day should fade the rally, not amplify it.
        """
        base = _baseline(_BUY_FLOW)  # positive
        with_sent = compute_price_impact(
            net_flow_value=_BUY_FLOW,
            adv_value=_ADV,
            lambda_market=_LAMBDA,
            flow_knee=_KNEE,
            sentiment_modifier=-0.5,
        )
        assert base > 0  # sanity
        assert with_sent < base
        assert with_sent == pytest.approx(base - 0.015, abs=1e-4)

    def test_quadrant_bear_sentiment_sell_flow_pushes_lower(self):
        """-sent + sell → delta MORE negative than baseline (reinforces bear)."""
        base = _baseline(_SELL_FLOW)
        with_sent = compute_price_impact(
            net_flow_value=_SELL_FLOW,
            adv_value=_ADV,
            lambda_market=_LAMBDA,
            flow_knee=_KNEE,
            sentiment_modifier=-0.5,
        )
        assert with_sent < base
        assert with_sent == pytest.approx(base - 0.015, abs=1e-4)


class TestSentimentBiasEdgeCases:
    def test_zero_sentiment_is_no_op(self):
        assert _baseline(_BUY_FLOW) == compute_price_impact(
            net_flow_value=_BUY_FLOW,
            adv_value=_ADV,
            lambda_market=_LAMBDA,
            flow_knee=_KNEE,
            sentiment_modifier=0.0,
        )

    def test_bias_respects_max_delta_pct_cap(self):
        """Additive bias should not push delta above the ±max_delta_pct cap."""
        # Construct a huge buy flow that already lands near the cap
        huge = 5.0e10
        result = compute_price_impact(
            net_flow_value=huge,
            adv_value=_ADV,
            lambda_market=_LAMBDA,
            flow_knee=_KNEE,
            sentiment_modifier=0.5,
            max_delta_pct=0.10,
        )
        assert result <= 0.10 + 1e-9

    def test_sentiment_scales_with_max_delta_pct(self):
        """Monthly cadence (max_delta_pct=0.40) gets a stronger bias than
        daily (0.10) — because the scale factor is ``max_delta_pct * 0.3``."""
        daily = compute_price_impact(
            net_flow_value=_BUY_FLOW,
            adv_value=_ADV,
            lambda_market=_LAMBDA,
            flow_knee=_KNEE,
            sentiment_modifier=0.5,
            max_delta_pct=0.10,
        )
        daily_base = compute_price_impact(
            net_flow_value=_BUY_FLOW,
            adv_value=_ADV,
            lambda_market=_LAMBDA,
            flow_knee=_KNEE,
            sentiment_modifier=0.0,
            max_delta_pct=0.10,
        )
        monthly = compute_price_impact(
            net_flow_value=_BUY_FLOW,
            adv_value=_ADV,
            lambda_market=_LAMBDA,
            flow_knee=_KNEE,
            sentiment_modifier=0.5,
            max_delta_pct=0.40,
        )
        monthly_base = compute_price_impact(
            net_flow_value=_BUY_FLOW,
            adv_value=_ADV,
            lambda_market=_LAMBDA,
            flow_knee=_KNEE,
            sentiment_modifier=0.0,
            max_delta_pct=0.40,
        )
        daily_bias = daily - daily_base
        monthly_bias = monthly - monthly_base
        # Monthly bias should be ~4x daily bias because 0.40/0.10 = 4
        assert monthly_bias == pytest.approx(4.0 * daily_bias, rel=1e-3)

    def test_zero_flow_with_nonzero_sentiment_stays_zero(self):
        """sentiment alone can't move price without a net flow — the short-
        circuit at ``net_flow_value == 0`` returns early."""
        result = compute_price_impact(
            net_flow_value=0.0,
            adv_value=_ADV,
            lambda_market=_LAMBDA,
            flow_knee=_KNEE,
            sentiment_modifier=0.5,
        )
        assert result == 0.0
