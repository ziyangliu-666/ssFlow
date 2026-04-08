"""Tests for the pure-Python trading_layer module.

Verifies the framework-agnostic math core used by `oasis_engine`:
  - Agent dataclass invariants (NAV, P&L, buy headroom)
  - spawn_agents stochastic distribution
  - apply_action mutation correctness (buy / sell / hold)
  - normalize_action_distribution edge cases
  - apply_distribution_to_agent_pop end-to-end (the entry point the engine
    calls per trader per round)
"""

from __future__ import annotations

import random
from typing import Any

import pytest

from ssfish.persona import MarketShare, Persona, SandboxConfig
from ssfish.trading_layer import (
    Agent,
    ClassFlowResult,
    apply_action,
    apply_distribution_to_agent_pop,
    normalize_action_distribution,
    spawn_agents,
)


# ─────────────────────── Fixtures ───────────────────────


def _make_action_space() -> list[dict[str, Any]]:
    return [
        {"name": "hold", "side": "none", "pool": "none", "fraction": 0.0},
        {"name": "buy_30pct", "side": "buy", "pool": "cash", "fraction": 0.30},
        {"name": "sell_50pct", "side": "sell", "pool": "holdings_in_target", "fraction": 0.50},
    ]


def _make_persona(pid: str = "test_retail", instance_count: int = 100) -> Persona:
    return Persona(
        id=pid,
        archetype="test_archetype",
        display_name="测试 persona",
        voice_prompt="test voice",
        decision_mode="discretionary",
        role="directional_speculator",
        market_share=MarketShare(by_volume=0.10),
        sandbox=SandboxConfig(
            instance_count=instance_count,
            capital_distribution={"type": "lognormal", "median_cny": 100000, "sigma": 0.6},
            initial_position_distribution={
                "type": "bernoulli",
                "prob_holding": 0.6,
                "position_size_pct_when_holding": {"type": "uniform", "min": 0.10, "max": 0.30},
            },
            risk={"max_position_pct": 0.95},
            action_space=_make_action_space(),
        ),
    )


# ─────────────────────── Agent dataclass ───────────────────────


class TestAgent:

    def test_holdings_value_derived_from_price(self):
        agent = Agent(
            persona_id="x", capital=1000, cash=500,
            holdings_shares=10, max_holdings_value=900,
        )
        assert agent.holdings_value(50.0) == 500
        assert agent.holdings_value(60.0) == 600

    def test_nav_is_cash_plus_holdings(self):
        agent = Agent(
            persona_id="x", capital=1000, cash=500,
            holdings_shares=10, max_holdings_value=900,
        )
        assert agent.nav(50.0) == 1000  # 500 cash + 500 holdings
        assert agent.nav(60.0) == 1100  # 500 cash + 600 holdings

    def test_pnl_at_starting_price_is_zero(self):
        agent = Agent(
            persona_id="x", capital=1000, cash=500,
            holdings_shares=10, max_holdings_value=900,
        )
        # holdings = 10 shares @ 50 = 500, cash = 500, total = 1000 = capital
        assert agent.pnl(50.0) == 0

    def test_pnl_positive_when_price_rises(self):
        agent = Agent(
            persona_id="x", capital=1000, cash=500,
            holdings_shares=10, max_holdings_value=900,
        )
        # Price doubles → holdings double from 500 to 1000
        assert agent.pnl(100.0) == 500

    def test_buy_headroom_capped_by_max_position(self):
        agent = Agent(
            persona_id="x", capital=1000, cash=900,
            holdings_shares=2, max_holdings_value=500,
        )
        # holdings_value at price 100 = 200, max = 500, headroom = 300
        assert agent.buy_headroom(100.0) == 300


# ─────────────────────── spawn_agents ───────────────────────


class TestSpawnAgents:

    def test_count_matches_instance_count(self):
        persona = _make_persona(instance_count=50)
        agents = spawn_agents(persona, current_price=100.0, rng=random.Random(42))
        assert len(agents) == 50

    def test_persona_id_propagated(self):
        persona = _make_persona(pid="my_class", instance_count=10)
        agents = spawn_agents(persona, current_price=100.0, rng=random.Random(42))
        assert all(a.persona_id == "my_class" for a in agents)

    def test_capital_positive(self):
        persona = _make_persona(instance_count=20)
        agents = spawn_agents(persona, current_price=100.0, rng=random.Random(42))
        assert all(a.capital > 0 for a in agents)

    def test_max_holdings_value_respects_pct(self):
        persona = _make_persona(instance_count=20)
        agents = spawn_agents(persona, current_price=100.0, rng=random.Random(42))
        for a in agents:
            assert a.max_holdings_value == pytest.approx(a.capital * 0.95)

    def test_deterministic_with_same_seed(self):
        persona = _make_persona(instance_count=20)
        a1 = spawn_agents(persona, current_price=100.0, rng=random.Random(42))
        a2 = spawn_agents(persona, current_price=100.0, rng=random.Random(42))
        assert [(a.capital, a.cash, a.holdings_shares) for a in a1] == \
               [(a.capital, a.cash, a.holdings_shares) for a in a2]

    def test_no_sandbox_raises(self):
        persona = Persona(
            id="x", archetype="a", display_name="a", voice_prompt="x",
            decision_mode="discretionary", role="r",
            market_share=MarketShare(by_volume=0.1),
        )
        with pytest.raises(ValueError, match="no sandbox config"):
            spawn_agents(persona, current_price=100.0, rng=random.Random(42))

    def test_zero_price_raises(self):
        persona = _make_persona()
        with pytest.raises(ValueError, match="current_price must be > 0"):
            spawn_agents(persona, current_price=0.0, rng=random.Random(42))


# ─────────────────────── apply_action ───────────────────────


class TestApplyAction:

    def _agent(self, cash=1000.0, shares=5.0, max_pct_value=2000.0) -> Agent:
        return Agent(
            persona_id="t", capital=2000.0, cash=cash,
            holdings_shares=shares, max_holdings_value=max_pct_value,
        )

    def test_hold_returns_zero(self):
        agent = self._agent()
        amount = apply_action(
            agent,
            {"name": "hold", "side": "none", "pool": "none", "fraction": 0.0},
            current_price=100.0,
        )
        assert amount == 0
        assert agent.cash == 1000
        assert agent.holdings_shares == 5

    def test_sell_50pct_holdings(self):
        agent = self._agent(cash=0, shares=10)
        amount = apply_action(
            agent,
            {"name": "sell_50pct", "side": "sell", "pool": "holdings_in_target", "fraction": 0.5},
            current_price=100.0,
        )
        # Sell 5 shares @ 100 = 500, returns -500, holdings now 5, cash now 500
        assert amount == -500
        assert agent.holdings_shares == 5
        assert agent.cash == 500

    def test_buy_30pct_cash(self):
        agent = self._agent(cash=1000, shares=0)
        amount = apply_action(
            agent,
            {"name": "buy_30pct", "side": "buy", "pool": "cash", "fraction": 0.30},
            current_price=50.0,
        )
        # Buy 300 worth = 6 shares
        assert amount == 300
        assert agent.cash == 700
        assert agent.holdings_shares == 6

    def test_buy_clipped_by_headroom(self):
        agent = self._agent(cash=1000, shares=0, max_pct_value=200)
        # We try to buy 300 worth (30% of cash) but max_holdings_value is 200
        amount = apply_action(
            agent,
            {"name": "buy", "side": "buy", "pool": "cash", "fraction": 0.30},
            current_price=50.0,
        )
        # Should clip to 200 (the headroom)
        assert amount == 200
        assert agent.cash == 800
        assert agent.holdings_shares == 4  # 200 / 50

    def test_sell_with_no_holdings_returns_zero(self):
        agent = self._agent(cash=1000, shares=0)
        amount = apply_action(
            agent,
            {"name": "sell", "side": "sell", "pool": "holdings_in_target", "fraction": 0.5},
            current_price=100.0,
        )
        assert amount == 0


# ─────────────────────── normalize_action_distribution ───────────────────────


class TestNormalizeDistribution:

    def test_clean_distribution_passes(self):
        normalized, warning = normalize_action_distribution(
            {"hold": 0.5, "buy": 0.3, "sell": 0.2},
            ["hold", "buy", "sell"],
        )
        assert normalized == {"hold": 0.5, "buy": 0.3, "sell": 0.2}
        assert warning is None

    def test_unknown_action_dropped(self):
        normalized, warning = normalize_action_distribution(
            {"hold": 0.5, "frob": 0.5},
            ["hold", "buy"],
        )
        assert "frob" not in normalized
        assert warning is not None and "frob" in warning

    def test_negative_clipped(self):
        normalized, warning = normalize_action_distribution(
            {"hold": -0.1, "buy": 1.0},
            ["hold", "buy"],
        )
        assert normalized["hold"] == 0
        assert normalized["buy"] == 1.0
        assert warning is not None

    def test_missing_action_filled_with_zero(self):
        normalized, warning = normalize_action_distribution(
            {"buy": 1.0},
            ["hold", "buy", "sell"],
        )
        assert normalized["hold"] == 0
        assert normalized["sell"] == 0
        assert normalized["buy"] == 1.0

    def test_off_by_one_renormalized(self):
        normalized, warning = normalize_action_distribution(
            {"hold": 0.4, "buy": 0.4},
            ["hold", "buy"],
        )
        # Sum is 0.8, gets rescaled to 0.5/0.5
        assert normalized["hold"] == pytest.approx(0.5)
        assert normalized["buy"] == pytest.approx(0.5)
        assert warning is not None

    def test_zero_sum_falls_back_to_uniform(self):
        normalized, warning = normalize_action_distribution(
            {"hold": 0.0, "buy": 0.0},
            ["hold", "buy"],
        )
        assert normalized["hold"] == 0.5
        assert normalized["buy"] == 0.5
        assert warning is not None


# ─────────────────────── apply_distribution_to_agent_pop end-to-end ───────────────────────


class TestApplyDistributionToAgentPop:
    """The main entry point the oasis_engine calls per trader per round.

    Takes a pre-parsed distribution (from the submit_order_distribution tool
    call collected via OrderCollector), samples agents, applies actions,
    returns a ClassFlowResult. No LLM involved in the function itself.
    """

    def test_basic_call_returns_class_flow(self):
        persona = _make_persona(instance_count=50)
        agents = spawn_agents(persona, current_price=100.0, rng=random.Random(42))

        result = apply_distribution_to_agent_pop(
            persona=persona,
            agents=agents,
            distribution={"hold": 0.4, "buy_30pct": 0.5, "sell_50pct": 0.1},
            current_price=100.0,
            rng=random.Random(7),
            rationale="Bullish on the news",
            confidence=0.7,
        )

        assert isinstance(result, ClassFlowResult)
        assert result.persona_id == persona.id
        assert result.n_agents == 50
        assert "Bullish" in result.rationale
        assert result.confidence == 0.7

    def test_action_histogram_sums_to_n_agents(self):
        persona = _make_persona(instance_count=100)
        agents = spawn_agents(persona, current_price=100.0, rng=random.Random(42))
        result = apply_distribution_to_agent_pop(
            persona=persona,
            agents=agents,
            distribution={"hold": 0.4, "buy_30pct": 0.5, "sell_50pct": 0.1},
            current_price=100.0,
            rng=random.Random(7),
        )
        assert sum(result.action_histogram.values()) == 100

    def test_bullish_distribution_yields_positive_net_flow(self):
        persona = _make_persona(instance_count=200)
        agents = spawn_agents(persona, current_price=100.0, rng=random.Random(42))
        result = apply_distribution_to_agent_pop(
            persona=persona,
            agents=agents,
            distribution={"hold": 0.4, "buy_30pct": 0.5, "sell_50pct": 0.1},
            current_price=100.0,
            rng=random.Random(7),
        )
        # 50% buy / 40% hold / 10% sell → expect net buy
        assert result.net_flow > 0

    def test_agent_state_mutated_in_place(self):
        persona = _make_persona(instance_count=20)
        agents = spawn_agents(persona, current_price=100.0, rng=random.Random(42))
        cash_before = sum(a.cash for a in agents)
        shares_before = sum(a.holdings_shares for a in agents)
        apply_distribution_to_agent_pop(
            persona=persona,
            agents=agents,
            distribution={"hold": 0.4, "buy_30pct": 0.5, "sell_50pct": 0.1},
            current_price=100.0,
            rng=random.Random(7),
        )
        cash_after = sum(a.cash for a in agents)
        shares_after = sum(a.holdings_shares for a in agents)
        assert cash_after != cash_before or shares_after != shares_before
