"""Tests for market stress computation (Fix 4: Policy Reactive Response)."""

import pytest
from dataclasses import dataclass

from ssflow.market_stress import (
    MarketStressSnapshot,
    compute_market_stress,
    create_stabilization_policies,
    render_policy_context,
)
from ssflow.world import SimAgent, SimGraph


@dataclass
class FakeRound:
    """Minimal RoundRecord-like object for testing."""
    delta_pct: float = 0.0


class TestComputeMarketStress:
    def test_normal_small_change(self):
        rounds = [FakeRound(delta_pct=0.01), FakeRound(delta_pct=0.005)]
        stress = compute_market_stress(rounds, 101.5, 100.0, round_idx=2)
        assert stress.stress_level == "normal"
        assert stress.urgency_score == 0.0

    def test_elevated_on_cumulative(self):
        rounds = [FakeRound(delta_pct=0.02), FakeRound(delta_pct=0.015)]
        # cum = +3.5%
        stress = compute_market_stress(rounds, 103.5, 100.0, round_idx=2)
        assert stress.stress_level == "elevated"
        assert 0.3 <= stress.urgency_score <= 0.7

    def test_crisis_on_large_drop(self):
        rounds = [FakeRound(delta_pct=-0.04), FakeRound(delta_pct=-0.03)]
        # cum = ~-7%
        stress = compute_market_stress(rounds, 93.0, 100.0, round_idx=2)
        assert stress.stress_level == "crisis"
        assert stress.urgency_score >= 0.8

    def test_crisis_on_single_round(self):
        rounds = [FakeRound(delta_pct=-0.035)]
        stress = compute_market_stress(rounds, 96.5, 100.0, round_idx=1)
        assert stress.stress_level == "crisis"

    def test_elevated_on_single_round(self):
        rounds = [FakeRound(delta_pct=0.025)]
        stress = compute_market_stress(rounds, 102.5, 100.0, round_idx=1)
        assert stress.stress_level == "elevated"

    def test_normal_no_rounds(self):
        stress = compute_market_stress([], 100.0, 100.0, round_idx=0)
        assert stress.stress_level == "normal"

    def test_is_stressed_property(self):
        rounds = [FakeRound(delta_pct=-0.05)]
        stress = compute_market_stress(rounds, 95.0, 100.0, round_idx=1)
        assert stress.is_stressed


class TestRenderPolicyContext:
    def test_crisis_regulator(self):
        stress = MarketStressSnapshot(
            cumulative_price_change_pct=-8.0,
            round_price_change_pct=-3.5,
            intraday_volatility=3.0,
            stress_level="crisis",
            urgency_score=0.9,
        )
        ctx = render_policy_context(stress, "csrc", "regulator", 3)
        assert "危机" in ctx
        assert "证监会" in ctx
        assert "-8.0%" in ctx

    def test_crisis_policy(self):
        stress = MarketStressSnapshot(
            cumulative_price_change_pct=-6.0,
            round_price_change_pct=-2.5,
            intraday_volatility=2.0,
            stress_level="crisis",
            urgency_score=0.85,
        )
        ctx = render_policy_context(stress, "pboc", "policy", 2)
        assert "政策职能" in ctx

    def test_elevated(self):
        stress = MarketStressSnapshot(
            cumulative_price_change_pct=4.0,
            round_price_change_pct=2.5,
            intraday_volatility=1.5,
            stress_level="elevated",
            urgency_score=0.5,
        )
        ctx = render_policy_context(stress, "csrc", "regulator", 1)
        assert "波动加大" in ctx
        assert "危机" not in ctx

    def test_normal_minimal(self):
        stress = MarketStressSnapshot(
            cumulative_price_change_pct=1.0,
            round_price_change_pct=0.5,
            intraday_volatility=0.5,
            stress_level="normal",
            urgency_score=0.0,
        )
        ctx = render_policy_context(stress, "csrc", "regulator", 0)
        assert "正常" in ctx


class TestCreateStabilizationPolicies:
    def _make_graph_with_national_team(self):
        graph = SimGraph()
        agent = SimAgent(
            id="nt", kind="trader", display_name="National Team",
            state={"avg_position_pct": 0.3},
            persona_id="national_team_strategic",
        )
        graph.add_agent(agent)
        return graph

    def test_creates_on_crisis_negative(self):
        graph = self._make_graph_with_national_team()
        stress = MarketStressSnapshot(
            cumulative_price_change_pct=-8.0,
            round_price_change_pct=-3.0,
            intraday_volatility=3.0,
            stress_level="crisis",
            urgency_score=0.9,
        )
        policies = create_stabilization_policies(stress, graph, round_idx=3)
        assert len(policies) == 1
        assert policies[0].action.side == "buy"
        # Policy should be attached to the agent
        nt = graph.agents["nt"]
        assert any(p.source == "market_stress" for p in nt.policies)

    def test_not_created_when_normal(self):
        graph = self._make_graph_with_national_team()
        stress = MarketStressSnapshot(
            cumulative_price_change_pct=-2.0,
            round_price_change_pct=-1.0,
            intraday_volatility=1.0,
            stress_level="normal",
            urgency_score=0.0,
        )
        policies = create_stabilization_policies(stress, graph, round_idx=3)
        assert len(policies) == 0

    def test_not_created_when_positive_crisis(self):
        """Crisis from upside (>5%) should not trigger buy stabilization."""
        graph = self._make_graph_with_national_team()
        stress = MarketStressSnapshot(
            cumulative_price_change_pct=8.0,
            round_price_change_pct=3.5,
            intraday_volatility=3.0,
            stress_level="crisis",
            urgency_score=0.9,
        )
        policies = create_stabilization_policies(stress, graph, round_idx=3)
        assert len(policies) == 0

    def test_not_created_without_national_team(self):
        graph = SimGraph()
        agent = SimAgent(
            id="retail", kind="trader", display_name="Retail",
            state={}, persona_id="retail_short_term_chaser",
        )
        graph.add_agent(agent)
        stress = MarketStressSnapshot(
            cumulative_price_change_pct=-10.0,
            round_price_change_pct=-4.0,
            intraday_volatility=4.0,
            stress_level="crisis",
            urgency_score=0.95,
        )
        policies = create_stabilization_policies(stress, graph, round_idx=3)
        assert len(policies) == 0
