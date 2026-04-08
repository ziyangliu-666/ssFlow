"""Tests for market_dynamics.py — Kyle square-root price impact.

Lifted from the legacy tests/test_sandbox.py during the Phase H Concordia
rewrite. Same coverage, just imports from the new module.
"""

from __future__ import annotations

import math

import pytest

from ssflow.market_dynamics import (
    LAMBDA_LITERATURE,
    MAX_DELTA_PCT_PER_ROUND,
    compute_price_impact,
    lambda_for_market,
)


class TestComputePriceImpact:
    """Square-root price impact formula tests."""

    def test_spec_byd_example(self):
        """The canonical spec §9.3 example: BYD net flow -3億 / ADV 80億 / λ=0.5
        should yield ≈ -9.7%."""
        result = compute_price_impact(
            net_flow_value=-3e8,
            adv_value=8e9,
            lambda_market=0.5,
        )
        # Hand calc: 0.5 * (-1) * sqrt(3e8 / 8e9) ≈ -0.0968
        assert result == pytest.approx(-0.0968, abs=0.001)

    def test_zero_net_flow_returns_zero(self):
        assert compute_price_impact(0.0, adv_value=1e9) == 0.0

    def test_positive_net_flow_yields_positive_delta(self):
        result = compute_price_impact(net_flow_value=2e8, adv_value=8e9, lambda_market=0.5)
        assert result > 0
        assert result == pytest.approx(0.5 * math.sqrt(2e8 / 8e9), abs=1e-6)

    def test_negative_net_flow_yields_negative_delta(self):
        result = compute_price_impact(net_flow_value=-5e8, adv_value=1e10, lambda_market=0.4)
        assert result < 0

    def test_concavity_large_flow_underestimated_relative_to_linear(self):
        """sqrt is concave, so doubling the flow should less-than-double the impact.
        Empirical reason we use sqrt rather than linear (Bouchaud 2010)."""
        small = compute_price_impact(1e8, adv_value=1e10, lambda_market=0.5)
        large = compute_price_impact(2e8, adv_value=1e10, lambda_market=0.5)
        ratio = large / small
        assert ratio == pytest.approx(math.sqrt(2), rel=0.01)
        assert ratio < 1.5

    def test_zero_adv_raises(self):
        with pytest.raises(ValueError, match="adv_value must be > 0"):
            compute_price_impact(net_flow_value=1e8, adv_value=0)

    def test_negative_adv_raises(self):
        with pytest.raises(ValueError, match="adv_value must be > 0"):
            compute_price_impact(net_flow_value=1e8, adv_value=-1e9)

    def test_default_lambda_used_when_omitted(self):
        result_default = compute_price_impact(1e8, adv_value=1e9)
        result_explicit = compute_price_impact(
            1e8, adv_value=1e9, lambda_market=LAMBDA_LITERATURE["default"]
        )
        assert result_default == result_explicit

    def test_lambda_scales_linearly(self):
        a = compute_price_impact(1e8, adv_value=1e10, lambda_market=0.3)
        b = compute_price_impact(1e8, adv_value=1e10, lambda_market=0.6)
        assert b == pytest.approx(2 * a, rel=1e-6)

    def test_market_lambdas_make_sense(self):
        """A股 λ should be higher than US equity λ (less liquid market)."""
        assert LAMBDA_LITERATURE["ashare"] > LAMBDA_LITERATURE["us-equity"]

    def test_extreme_sell_pressure_clipped_to_negative_cap(self):
        """Extreme flows (>> ADV) clip to ±10% (modeling A-share 涨停板)."""
        result = compute_price_impact(net_flow_value=-1e12, adv_value=1e10, lambda_market=0.5)
        assert result == pytest.approx(-MAX_DELTA_PCT_PER_ROUND, abs=1e-9)

    def test_extreme_buy_pressure_clipped_to_positive_cap(self):
        result = compute_price_impact(net_flow_value=+1e12, adv_value=1e10, lambda_market=0.5)
        assert result == pytest.approx(+MAX_DELTA_PCT_PER_ROUND, abs=1e-9)

    def test_max_delta_pct_override(self):
        """Caller can pass a different cap (e.g., for crypto / HK markets)."""
        result = compute_price_impact(
            net_flow_value=-1e12,
            adv_value=1e10,
            lambda_market=0.5,
            max_delta_pct=0.30,
        )
        assert result == pytest.approx(-0.30, abs=1e-9)


class TestLambdaForMarket:
    def test_known_market(self):
        assert lambda_for_market("ashare") == LAMBDA_LITERATURE["ashare"]
        assert lambda_for_market("us-equity") == LAMBDA_LITERATURE["us-equity"]

    def test_unknown_market_falls_back_to_default(self):
        assert lambda_for_market("mars-futures") == LAMBDA_LITERATURE["default"]

    def test_none_falls_back_to_default(self):
        assert lambda_for_market(None) == LAMBDA_LITERATURE["default"]
