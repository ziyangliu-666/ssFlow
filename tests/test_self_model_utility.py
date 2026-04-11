"""Unit tests for ssflow.self_model.utility — utility component library.

Each component is a pure function ``(state_dict) -> float``. Tests pin
down the numeric contract + the ``compute_utility`` linear combiner.
"""

from __future__ import annotations

import pytest

from ssflow.self_model.utility import UTILITY_COMPONENTS, compute_utility


class TestUtilityComponentLibrary:
    def test_all_components_are_callable(self):
        for key, fn in UTILITY_COMPONENTS.items():
            assert callable(fn), f"component {key} not callable"

    def test_all_components_tolerate_empty_state(self):
        empty = {}
        for key, fn in UTILITY_COMPONENTS.items():
            val = fn(empty)
            assert isinstance(val, (int, float)), f"{key} returned non-numeric {val!r}"


class TestNavGrowth:
    def test_reads_unrealized_pnl_pct(self):
        assert UTILITY_COMPONENTS["nav_growth"]({"unrealized_pnl_pct": 12.5}) == 12.5
        assert UTILITY_COMPONENTS["nav_growth"]({"unrealized_pnl_pct": -8}) == -8


class TestDrawdownPenalty:
    def test_penalizes_at_2x_multiplier(self):
        # 10% drawdown → -20 score
        assert UTILITY_COMPONENTS["drawdown_penalty"]({"max_drawdown_pct": 10}) == -20
        assert UTILITY_COMPONENTS["drawdown_penalty"]({"max_drawdown_pct": 0}) == 0

    def test_abs_value_so_already_positive_drawdown_works(self):
        assert UTILITY_COMPONENTS["drawdown_penalty"]({"max_drawdown_pct": 5}) == -10


class TestBenchmarkOutperformance:
    def test_direct_read(self):
        assert UTILITY_COMPONENTS["benchmark_outperformance"](
            {"benchmark_gap_pct": 3.2}
        ) == 3.2


class TestMandateBreachPenalty:
    def test_zero_when_within_budget(self):
        assert UTILITY_COMPONENTS["mandate_breach_penalty"](
            {"risk_budget_used_pct": 0.8}
        ) == 0

    def test_heavy_quadratic_penalty_on_breach(self):
        val = UTILITY_COMPONENTS["mandate_breach_penalty"](
            {"risk_budget_used_pct": 1.1}
        )
        assert val == pytest.approx(-10.0, abs=0.01)  # -100 * 0.1


class TestClientRetention:
    def test_negative_proportional(self):
        # 5% redemption → -0.5 score
        assert UTILITY_COMPONENTS["client_retention"](
            {"client_net_redemption": 0.05}
        ) == pytest.approx(-0.5, abs=0.01)


class TestConvictionReward:
    def test_positive_conviction_x_positive_pnl(self):
        val = UTILITY_COMPONENTS["conviction_reward"](
            {"conviction": 0.8, "unrealized_pnl_pct": 5}
        )
        assert val == pytest.approx(1.6, abs=0.01)

    def test_positive_conviction_x_negative_pnl_penalized(self):
        val = UTILITY_COMPONENTS["conviction_reward"](
            {"conviction": 0.8, "unrealized_pnl_pct": -5}
        )
        assert val == pytest.approx(-1.6, abs=0.01)

    def test_zero_pnl_returns_zero(self):
        val = UTILITY_COMPONENTS["conviction_reward"](
            {"conviction": 0.9, "unrealized_pnl_pct": 0}
        )
        assert val == 0


class TestRunwaySafety:
    def test_one_at_12_months(self):
        val = UTILITY_COMPONENTS["runway_safety"]({"cash_runway_months": 12})
        assert val == 1.0

    def test_capped_at_2_for_long_runway(self):
        val = UTILITY_COMPONENTS["runway_safety"]({"cash_runway_months": 48})
        assert val == 2.0

    def test_low_runway_gets_low_score(self):
        val = UTILITY_COMPONENTS["runway_safety"]({"cash_runway_months": 3})
        assert val == pytest.approx(0.25, abs=0.01)


class TestComputeUtility:
    def test_linear_combination_sums_correctly(self):
        state = {
            "unrealized_pnl_pct": 10,
            "max_drawdown_pct": 5,
        }
        weights = {"nav_growth": 1.0, "drawdown_penalty": 0.5}
        total, breakdown = compute_utility(state, weights)
        # nav_growth = 1.0 * 10 = 10
        # drawdown_penalty = 0.5 * -10 = -5
        # total = 5
        assert total == pytest.approx(5.0, abs=0.01)
        assert breakdown["nav_growth"] == pytest.approx(10.0)
        assert breakdown["drawdown_penalty"] == pytest.approx(-5.0)

    def test_unknown_component_silently_skipped(self):
        state = {"unrealized_pnl_pct": 10}
        weights = {"nav_growth": 1.0, "bogus_component": 99}
        total, breakdown = compute_utility(state, weights)
        assert total == 10.0
        assert "bogus_component" not in breakdown

    def test_empty_weights_returns_zero(self):
        total, breakdown = compute_utility({"nav": 100}, {})
        assert total == 0.0
        assert breakdown == {}

    def test_negative_weight_flips_sign(self):
        # Someone could use negative weights for inverse interpretation
        state = {"unrealized_pnl_pct": 10}
        weights = {"nav_growth": -1.0}
        total, _ = compute_utility(state, weights)
        assert total == -10.0
