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
        """BYD net flow -3億 / ADV 80億 / λ=0.5. With soft compression
        (knee=0.03), flow/ADV=3.75% → eff_ratio≈2.14% → delta≈-7.3%."""
        result = compute_price_impact(
            net_flow_value=-3e8,
            adv_value=8e9,
            lambda_market=0.5,
        )
        # Soft-compressed: eff = 0.03*(1-e^(-0.0375/0.03)) ≈ 0.0214
        # delta = -0.5*sqrt(0.0214) ≈ -7.3%
        assert result == pytest.approx(-0.073, abs=0.005)

    def test_spec_byd_example_raw_kyle(self):
        """Verify raw Kyle formula (no compression) still works."""
        result = compute_price_impact(
            net_flow_value=-3e8,
            adv_value=8e9,
            lambda_market=0.5,
            flow_knee=1e6,
        )
        assert result == pytest.approx(-0.0968, abs=0.001)

    def test_zero_net_flow_returns_zero(self):
        assert compute_price_impact(0.0, adv_value=1e9) == 0.0

    def test_positive_net_flow_yields_positive_delta(self):
        result = compute_price_impact(net_flow_value=2e8, adv_value=8e9, lambda_market=0.5)
        assert result > 0
        # With soft compression, result is less than raw Kyle
        raw_kyle = 0.5 * math.sqrt(2e8 / 8e9)
        assert result < raw_kyle  # compressed
        assert result > raw_kyle * 0.5  # but not too much

    def test_negative_net_flow_yields_negative_delta(self):
        result = compute_price_impact(net_flow_value=-5e8, adv_value=1e10, lambda_market=0.4)
        assert result < 0

    def test_concavity_large_flow_underestimated_relative_to_linear(self):
        """Doubling the flow should less-than-double the impact (concavity
        is preserved even through soft compression)."""
        small = compute_price_impact(1e8, adv_value=1e10, lambda_market=0.5)
        large = compute_price_impact(2e8, adv_value=1e10, lambda_market=0.5)
        ratio = large / small
        # With soft compression, ratio < sqrt(2). The compression adds
        # extra concavity on top of the sqrt.
        assert 1.0 < ratio < math.sqrt(2)
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
        result = compute_price_impact(
            net_flow_value=-1e12, adv_value=1e10, lambda_market=0.5,
            flow_knee=1e6,  # disable compression to test price cap
        )
        assert result == pytest.approx(-MAX_DELTA_PCT_PER_ROUND, abs=1e-9)

    def test_extreme_buy_pressure_clipped_to_positive_cap(self):
        result = compute_price_impact(
            net_flow_value=+1e12, adv_value=1e10, lambda_market=0.5,
            flow_knee=1e6,  # disable compression to test price cap
        )
        assert result == pytest.approx(+MAX_DELTA_PCT_PER_ROUND, abs=1e-9)

    def test_max_delta_pct_override(self):
        """Caller can pass a different cap (e.g., for crypto / HK markets)."""
        result = compute_price_impact(
            net_flow_value=-1e12,
            adv_value=1e10,
            lambda_market=0.5,
            max_delta_pct=0.30,
            flow_knee=1e6,  # disable compression to test price cap
        )
        assert result == pytest.approx(-0.30, abs=1e-9)

    def test_soft_compression_preserves_ordering(self):
        """Larger flow produces larger delta (soft compression, not clip)."""
        adv = 1e10
        small = compute_price_impact(net_flow_value=1e8, adv_value=adv)
        medium = compute_price_impact(net_flow_value=1e9, adv_value=adv)
        large = compute_price_impact(net_flow_value=1e10, adv_value=adv)
        assert 0 < small < medium < large
        # Small flow (1% of ADV) should pass nearly linearly
        # knee=0.03: eff = 0.03*(1-e^(-0.01/0.03)) = 0.03*0.284 = 0.0085
        # delta ≈ 0.5*sqrt(0.0085) = 4.6% — reasonable single-day move
        assert small == pytest.approx(0.046, abs=0.005)

    def test_soft_compression_asymptotes(self):
        """Very large flows converge toward the asymptote."""
        adv = 1e10
        d1 = compute_price_impact(net_flow_value=5e10, adv_value=adv)
        d2 = compute_price_impact(net_flow_value=1e11, adv_value=adv)
        # Both should be close to the asymptote: 0.5*sqrt(0.03) = 8.66%
        assert d1 == pytest.approx(0.0866, abs=0.001)
        assert d2 == pytest.approx(0.0866, abs=0.001)
        # Ordering preserved for more moderate sizes
        d_small = compute_price_impact(net_flow_value=5e8, adv_value=adv)
        d_medium = compute_price_impact(net_flow_value=2e9, adv_value=adv)
        assert d_small < d_medium < d1


class TestLambdaForMarket:
    def test_known_market(self):
        assert lambda_for_market("ashare") == LAMBDA_LITERATURE["ashare"]
        assert lambda_for_market("us-equity") == LAMBDA_LITERATURE["us-equity"]

    def test_unknown_market_falls_back_to_default(self):
        assert lambda_for_market("mars-futures") == LAMBDA_LITERATURE["default"]

    def test_none_falls_back_to_default(self):
        assert lambda_for_market(None) == LAMBDA_LITERATURE["default"]
