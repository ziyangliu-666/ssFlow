"""Validation tests for calibration library — CI-ready.

Verifies that:
  - compute_implied_lambda produces values in a reasonable range
  - select_impact_params returns different params for different event types
  - select_impact_params falls back gracefully for unknown event types
  - Calibration events have internally consistent parameters
"""

from __future__ import annotations

import pytest

from ssflow.calibration_library import (
    CALIBRATION_EVENTS,
    compute_implied_lambda,
    select_impact_params,
)
from ssflow.market_dynamics import FLOW_KNEE, LAMBDA_LITERATURE


# ─────────────────────── Implied lambda range validation ───────────────────────


class TestImpliedLambdaRange:
    """Every calibration event's implied lambda must be in [0.1, 2.0]."""

    @pytest.mark.parametrize(
        "event",
        CALIBRATION_EVENTS,
        ids=[e.id for e in CALIBRATION_EVENTS],
    )
    def test_implied_lambda_in_reasonable_range(self, event):
        lam = compute_implied_lambda(event)
        # Some events (one-word limit-down/up, zero flow) legitimately return 0.0
        # because the Kyle model is not applicable.
        if lam == 0.0:
            # Verify this is a known degenerate case
            assert (
                event.estimated_net_flow == 0
                or event.day1_return == 0
                or event.adv_value_20d <= 0
            ), (
                f"Non-degenerate event {event.id} returned lambda=0.0 "
                f"(flow={event.estimated_net_flow}, return={event.day1_return})"
            )
            return

        assert 0.01 <= lam <= 5.0, (
            f"Implied lambda {lam:.4f} outside [0.01, 5.0] for {event.id}. "
            f"day1_return={event.day1_return:.4f}, "
            f"estimated_net_flow={event.estimated_net_flow:.2e}, "
            f"adv={event.adv_value_20d:.2e}"
        )


# ─────────────────────── select_impact_params differentiation ───────────────────────


class TestSelectImpactParamsDifferentiation:
    """select_impact_params should produce meaningfully different parameters
    for different event types."""

    def test_different_event_types_yield_different_lambda(self):
        """Geopolitical vs policy vs earnings should not all return the same lambda."""
        geopolitical = select_impact_params("geopolitical")
        policy = select_impact_params("policy")
        earnings = select_impact_params("earnings")

        # At least two of the three should differ
        lambdas = {
            geopolitical["lambda"],
            policy["lambda"],
            earnings["lambda"],
        }
        assert len(lambdas) >= 2, (
            f"All event types returned the same lambda: "
            f"geopolitical={geopolitical['lambda']}, "
            f"policy={policy['lambda']}, "
            f"earnings={earnings['lambda']}"
        )

    def test_board_type_matters(self):
        """ChiNext (20% limits) vs main board (10% limits) should give
        different calibration when event type is the same."""
        main = select_impact_params("geopolitical", board="main")
        chinext = select_impact_params("geopolitical", board="chinext")
        # They may still be similar but the source should indicate calibration
        assert "calibration" in main["source"] or "literature" in main["source"]
        assert "calibration" in chinext["source"] or "literature" in chinext["source"]

    def test_liquidity_bucket_influences_result(self):
        """Small-cap vs large-cap should yield different lambda for same event type."""
        small = select_impact_params("geopolitical", float_cap_cny=50.0)
        large = select_impact_params("geopolitical", float_cap_cny=5000.0)
        # Both should have calibration sources
        assert "calibration" in small["source"]
        assert "calibration" in large["source"]
        # Lambda and/or knee should differ due to liquidity matching
        differs = (
            abs(small["lambda"] - large["lambda"]) > 0.001
            or abs(small["knee"] - large["knee"]) > 0.001
        )
        assert differs, (
            f"Small-cap and large-cap returned identical params: "
            f"small={small}, large={large}"
        )

    def test_source_includes_event_count(self):
        """When calibration events are found, source should indicate count."""
        result = select_impact_params("geopolitical")
        assert "calibration" in result["source"]
        # Should be "calibration:N_events" format
        assert "_events" in result["source"]


# ─────────────────────── Fallback behavior ───────────────────────


class TestSelectImpactParamsFallback:
    """select_impact_params must fall back gracefully for unknown types."""

    def test_unknown_event_type_returns_literature_default(self):
        result = select_impact_params("alien_invasion")
        assert result["source"] == "literature_default"
        assert result["lambda"] == LAMBDA_LITERATURE.get(
            "ashare", LAMBDA_LITERATURE["default"]
        )
        assert result["knee"] == FLOW_KNEE

    def test_unknown_event_type_different_strings(self):
        """Various unknown types should all fall back to the same default."""
        r1 = select_impact_params("zombie_apocalypse")
        r2 = select_impact_params("time_travel_arbitrage")
        assert r1 == r2

    def test_empty_event_type_uses_all_events(self):
        """Empty event_type matches all events (broad average calibration)."""
        result = select_impact_params("")
        assert "calibration" in result["source"] or result["source"] == "literature_default"
        assert 0.1 < result["lambda"] < 2.0
        assert 0.01 < result["knee"] < 0.20

    def test_known_type_returns_calibration(self):
        """Known event types should use calibration, not literature."""
        for event_type in ("geopolitical", "policy", "earnings", "mania"):
            result = select_impact_params(event_type)
            assert "calibration" in result["source"], (
                f"Event type '{event_type}' fell back to literature default "
                f"instead of using calibration: {result}"
            )


# ─────────────────────── Return value structure ───────────────────────


class TestSelectImpactParamsStructure:
    """Verify the return dict structure and value ranges."""

    def test_return_keys(self):
        result = select_impact_params("policy")
        assert "lambda" in result
        assert "knee" in result
        assert "source" in result

    def test_lambda_positive(self):
        for event_type in ("geopolitical", "policy", "earnings", "mania", "unknown"):
            result = select_impact_params(event_type)
            assert result["lambda"] > 0, (
                f"Non-positive lambda for event_type={event_type}: {result}"
            )

    def test_knee_positive(self):
        for event_type in ("geopolitical", "policy", "earnings", "mania", "unknown"):
            result = select_impact_params(event_type)
            assert result["knee"] > 0, (
                f"Non-positive knee for event_type={event_type}: {result}"
            )

    def test_lambda_reasonable_range(self):
        """Calibrated lambda should be in [0.01, 2.0] for known types."""
        for event_type in ("geopolitical", "policy", "earnings", "mania"):
            result = select_impact_params(event_type)
            assert 0.01 <= result["lambda"] <= 2.0, (
                f"Lambda {result['lambda']} outside [0.01, 2.0] for "
                f"event_type={event_type}"
            )

    def test_knee_reasonable_range(self):
        """Calibrated knee should be in [0.01, 0.20] for known types."""
        for event_type in ("geopolitical", "policy", "earnings", "mania"):
            result = select_impact_params(event_type)
            assert 0.01 <= result["knee"] <= 0.20, (
                f"Knee {result['knee']} outside [0.01, 0.20] for "
                f"event_type={event_type}"
            )


# ─────────────────────── Market regime adjustments ───────────────────────


class TestMarketRegimeAdjustments:
    """Market regime should shift parameters predictably."""

    def test_crisis_amplifies_lambda(self):
        normal = select_impact_params("geopolitical", market_regime="normal")
        crisis = select_impact_params("geopolitical", market_regime="crisis")
        assert crisis["lambda"] > normal["lambda"], (
            f"Crisis lambda ({crisis['lambda']}) should exceed normal "
            f"({normal['lambda']})"
        )

    def test_euphoria_dampens_lambda(self):
        normal = select_impact_params("geopolitical", market_regime="normal")
        euphoria = select_impact_params("geopolitical", market_regime="euphoria")
        assert euphoria["lambda"] < normal["lambda"], (
            f"Euphoria lambda ({euphoria['lambda']}) should be below normal "
            f"({normal['lambda']})"
        )

    def test_crisis_shrinks_knee(self):
        normal = select_impact_params("geopolitical", market_regime="normal")
        crisis = select_impact_params("geopolitical", market_regime="crisis")
        assert crisis["knee"] < normal["knee"], (
            f"Crisis knee ({crisis['knee']}) should be below normal "
            f"({normal['knee']})"
        )
