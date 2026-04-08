"""Tests for sandbox.py — sandbox primitives.

This file covers the Phase B foundational pieces:
    - B1c: compute_price_impact() — Kyle square-root formula
    - B2:  normalize_action_distribution() (Gotcha 2 lock-in)
    - B2:  chat_action_distribution() — LLM wrapper, mocked
    - B1a: Agent dataclass + spawn_agents()
    - B1b: apply_action_to_agent + sample_actions + aggregate_class_flow
"""

from __future__ import annotations

import math
import random
import statistics
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ssfish.persona import MarketShare, Persona, SandboxConfig
from ssfish.sandbox import (
    ActionDistributionParseError,
    ActionDistributionResult,
    Agent,
    ClassFlowResult,
    LAMBDA_LITERATURE,
    NORMALIZATION_TOLERANCE,
    aggregate_class_flow,
    apply_action_to_agent,
    chat_action_distribution,
    compute_price_impact,
    normalize_action_distribution,
    sample_actions,
    spawn_agents,
    update_holdings_for_price,
)


# ─────────────────────── Test fixtures ───────────────────────


def _make_sandbox_persona(
    pid: str = "test_retail",
    instance_count: int = 100,
    median_capital: float = 120000,
    sigma: float = 0.6,
    prob_holding: float = 0.6,
    pos_min: float = 0.10,
    pos_max: float = 0.40,
    action_space: list[dict[str, Any]] | None = None,
) -> Persona:
    """Build a minimal Persona with a sandbox config for spawn/aggregate tests."""
    if action_space is None:
        action_space = [
            {"name": "hold", "side": "none", "pool": "none", "fraction": 0.0},
            {
                "name": "panic_sell_50pct",
                "side": "sell",
                "pool": "holdings_in_target",
                "fraction": 0.5,
            },
            {
                "name": "fomo_buy_30pct",
                "side": "buy",
                "pool": "cash",
                "fraction": 0.3,
            },
        ]
    return Persona(
        id=pid,
        archetype="test_archetype",
        display_name=f"{pid} display",
        voice_prompt="x",
        decision_mode="discretionary",
        role="directional_speculator",
        market_share=MarketShare(by_volume=0.10),
        sandbox=SandboxConfig(
            instance_count=instance_count,
            capital_distribution={
                "type": "lognormal",
                "median_cny": median_capital,
                "sigma": sigma,
            },
            initial_position_distribution={
                "type": "bernoulli",
                "prob_holding": prob_holding,
                "position_size_pct_when_holding": {
                    "type": "uniform",
                    "min": pos_min,
                    "max": pos_max,
                },
            },
            risk={"max_position_pct": 0.95},
            action_space=action_space,
        ),
    )


# ─────────────────────── compute_price_impact (B1c) ───────────────────────


class TestComputePriceImpact:
    """Square-root price impact formula tests."""

    def test_spec_byd_example(self):
        """The canonical spec §9.3 example: BYD net flow -3億 / ADV 80億 / λ=0.5
        should yield ≈ -9.7%."""
        result = compute_price_impact(
            net_flow_cny=-3e8,  # -3億
            adv_cny=8e9,         # 80億
            lambda_market=0.5,
        )
        # Hand calc: 0.5 * (-1) * sqrt(3e8 / 8e9)
        #          = -0.5 * sqrt(0.0375)
        #          = -0.5 * 0.19365
        #          ≈ -0.0968
        assert result == pytest.approx(-0.0968, abs=0.001)

    def test_zero_net_flow_returns_zero(self):
        """Exactly zero net flow → exactly zero price change."""
        assert compute_price_impact(0.0, adv_cny=1e9) == 0.0

    def test_positive_net_flow_yields_positive_delta(self):
        """Net buy pressure pushes price up."""
        result = compute_price_impact(net_flow_cny=2e8, adv_cny=8e9, lambda_market=0.5)
        assert result > 0
        assert result == pytest.approx(0.5 * math.sqrt(2e8 / 8e9), abs=1e-6)

    def test_negative_net_flow_yields_negative_delta(self):
        """Net sell pressure pushes price down."""
        result = compute_price_impact(net_flow_cny=-5e8, adv_cny=1e10, lambda_market=0.4)
        assert result < 0

    def test_concavity_large_flow_underestimated_relative_to_linear(self):
        """sqrt is concave, so doubling the flow should less-than-double the impact.
        This is the empirical reason we use sqrt rather than linear (Bouchaud 2010)."""
        small = compute_price_impact(1e8, adv_cny=1e10, lambda_market=0.5)
        large = compute_price_impact(2e8, adv_cny=1e10, lambda_market=0.5)
        # large / small should be sqrt(2) ≈ 1.414, NOT 2
        ratio = large / small
        assert ratio == pytest.approx(math.sqrt(2), rel=0.01)
        assert ratio < 1.5

    def test_zero_adv_raises(self):
        with pytest.raises(ValueError, match="adv_cny must be > 0"):
            compute_price_impact(net_flow_cny=1e8, adv_cny=0)

    def test_negative_adv_raises(self):
        with pytest.raises(ValueError, match="adv_cny must be > 0"):
            compute_price_impact(net_flow_cny=1e8, adv_cny=-1e9)

    def test_default_lambda_used_when_omitted(self):
        """If lambda_market is not passed, the default literature value is used."""
        result_default = compute_price_impact(1e8, adv_cny=1e9)
        result_explicit = compute_price_impact(
            1e8, adv_cny=1e9, lambda_market=LAMBDA_LITERATURE["default"]
        )
        assert result_default == result_explicit

    def test_lambda_scales_linearly(self):
        """Doubling λ should double the resulting price impact."""
        a = compute_price_impact(1e8, adv_cny=1e10, lambda_market=0.3)
        b = compute_price_impact(1e8, adv_cny=1e10, lambda_market=0.6)
        assert b == pytest.approx(2 * a, rel=1e-6)

    def test_market_lambdas_make_sense(self):
        """A股 λ should be higher than US equity λ (less liquid market)."""
        assert LAMBDA_LITERATURE["ashare"] > LAMBDA_LITERATURE["us-equity"]


# ─────────────────────── normalize_action_distribution (B2 helper) ───────────────────────


class TestNormalizeActionDistribution:

    def test_clean_distribution_pass_through(self):
        """A distribution that already sums to 1.0 with valid keys is unchanged."""
        raw = {"hold": 0.5, "buy": 0.3, "sell": 0.2}
        normalized, warning = normalize_action_distribution(
            raw, expected_actions=["hold", "buy", "sell"]
        )
        assert normalized == pytest.approx({"hold": 0.5, "buy": 0.3, "sell": 0.2})
        assert warning is None

    def test_sum_slightly_off_within_tolerance_silent(self):
        """Sum within ±tolerance is silently renormalized without warning."""
        raw = {"hold": 0.51, "buy": 0.30, "sell": 0.21}  # sums to 1.02
        normalized, warning = normalize_action_distribution(
            raw, expected_actions=["hold", "buy", "sell"]
        )
        assert sum(normalized.values()) == pytest.approx(1.0, abs=1e-9)
        # Within tolerance band of 0.05 → no warning about sum
        assert warning is None or "sum" not in (warning or "")

    def test_sum_outside_tolerance_warns(self):
        """Sum outside ±tolerance triggers a warning but still normalizes."""
        raw = {"hold": 0.7, "buy": 0.2, "sell": 0.2}  # sums to 1.10, > 1.05
        normalized, warning = normalize_action_distribution(
            raw, expected_actions=["hold", "buy", "sell"]
        )
        assert sum(normalized.values()) == pytest.approx(1.0, abs=1e-9)
        assert warning is not None
        assert "sum was 1.1" in warning

    def test_negative_clipped(self):
        """Negative probabilities are clipped to 0 and warned."""
        raw = {"hold": 0.6, "buy": 0.5, "sell": -0.1}
        normalized, warning = normalize_action_distribution(
            raw, expected_actions=["hold", "buy", "sell"]
        )
        assert normalized["sell"] == 0.0
        assert sum(normalized.values()) == pytest.approx(1.0)
        assert warning is not None
        assert "negative" in warning

    def test_unknown_action_dropped_and_warned(self):
        """Actions not in expected_actions are silently dropped + warned."""
        raw = {"hold": 0.5, "buy": 0.3, "panic_dance": 0.2}  # invalid action
        normalized, warning = normalize_action_distribution(
            raw, expected_actions=["hold", "buy", "sell"]
        )
        assert "panic_dance" not in normalized
        assert sum(normalized.values()) == pytest.approx(1.0)
        assert warning is not None
        assert "unknown" in warning

    def test_missing_expected_action_filled_with_zero(self):
        """Expected actions not in raw distribution are added with prob=0."""
        raw = {"hold": 0.5, "buy": 0.5}  # missing "sell"
        normalized, _ = normalize_action_distribution(
            raw, expected_actions=["hold", "buy", "sell"]
        )
        assert normalized["sell"] == 0.0

    def test_all_zero_distribution_falls_back_to_uniform(self):
        """If everything is zero/negative, fall back to uniform with warning."""
        raw = {"hold": 0.0, "buy": 0.0, "sell": 0.0}
        normalized, warning = normalize_action_distribution(
            raw, expected_actions=["hold", "buy", "sell"]
        )
        assert sum(normalized.values()) == pytest.approx(1.0)
        # Each action gets 1/3
        for v in normalized.values():
            assert v == pytest.approx(1.0 / 3)
        assert warning is not None
        assert "uniform" in warning

    def test_empty_raw_distribution_falls_back_to_uniform(self):
        """An empty input falls back to uniform over expected_actions."""
        normalized, warning = normalize_action_distribution(
            {}, expected_actions=["a", "b"]
        )
        assert normalized == pytest.approx({"a": 0.5, "b": 0.5})
        assert warning is not None


# ─────────────────────── chat_action_distribution (B2 wrapper) ───────────────────────


def _make_mock_json_result(parsed: dict[str, Any]):
    """Build a stand-in JsonChatResult."""
    from ssfish.llm_client import JsonChatResult

    return JsonChatResult(
        parsed=parsed,
        model="gpt-4o-mini",
        system_fingerprint="fp_test_001",
        prompt_tokens=1500,
        completion_tokens=230,
    )


class TestChatActionDistribution:

    @pytest.mark.asyncio
    async def test_happy_path(self, monkeypatch):
        """Standard well-formed LLM response: distribution + rationale + confidence."""
        mock_response = _make_mock_json_result(
            {
                "action_distribution": {
                    "hold": 0.4,
                    "panic_sell_50pct": 0.3,
                    "average_down_10pct": 0.2,
                    "fomo_buy_30pct": 0.1,
                },
                "rationale": "Q1 beat 但 margin miss 引发分歧, 短线散户分裂",
                "confidence": 0.7,
            }
        )

        async def fake_chat_json(*args, **kwargs):
            return mock_response

        monkeypatch.setattr("ssfish.sandbox.chat_json", fake_chat_json)

        result = await chat_action_distribution(
            messages=[{"role": "user", "content": "test"}],
            persona_id="retail_short_term_chaser",
            expected_actions=["hold", "panic_sell_50pct", "average_down_10pct", "fomo_buy_30pct"],
        )

        assert isinstance(result, ActionDistributionResult)
        assert result.persona_id == "retail_short_term_chaser"
        assert sum(result.action_distribution.values()) == pytest.approx(1.0)
        assert result.action_distribution["hold"] == pytest.approx(0.4)
        assert result.rationale.startswith("Q1 beat")
        assert result.confidence == 0.7
        assert result.system_fingerprint == "fp_test_001"
        assert result.normalization_warning is None
        assert result.strategic_signal is None

    @pytest.mark.asyncio
    async def test_unknown_actions_dropped(self, monkeypatch):
        """LLM hallucinates an action name → dropped, warning recorded."""
        mock = _make_mock_json_result(
            {
                "action_distribution": {
                    "hold": 0.5,
                    "buy": 0.3,
                    "telepath_market": 0.2,  # hallucinated
                },
                "rationale": "test",
                "confidence": 0.5,
            }
        )

        async def fake_chat_json(*args, **kwargs):
            return mock

        monkeypatch.setattr("ssfish.sandbox.chat_json", fake_chat_json)

        result = await chat_action_distribution(
            messages=[{"role": "user", "content": "x"}],
            persona_id="test",
            expected_actions=["hold", "buy", "sell"],
        )
        assert "telepath_market" not in result.action_distribution
        assert result.normalization_warning is not None
        assert "unknown" in result.normalization_warning

    @pytest.mark.asyncio
    async def test_distribution_renormalized_to_one(self, monkeypatch):
        """LLM returns distribution summing to 1.15 → renormalized + warned."""
        mock = _make_mock_json_result(
            {
                "action_distribution": {"hold": 0.6, "buy": 0.3, "sell": 0.25},  # 1.15
                "rationale": "x",
                "confidence": 0.5,
            }
        )

        async def fake_chat_json(*args, **kwargs):
            return mock

        monkeypatch.setattr("ssfish.sandbox.chat_json", fake_chat_json)

        result = await chat_action_distribution(
            messages=[{"role": "user", "content": "x"}],
            persona_id="test",
            expected_actions=["hold", "buy", "sell"],
        )
        assert sum(result.action_distribution.values()) == pytest.approx(1.0)
        assert result.normalization_warning is not None

    @pytest.mark.asyncio
    async def test_missing_action_distribution_key_raises(self, monkeypatch):
        """LLM forgets the wrapping key entirely → ParseError."""
        mock = _make_mock_json_result({"some_random_key": "blah"})

        async def fake_chat_json(*args, **kwargs):
            return mock

        monkeypatch.setattr("ssfish.sandbox.chat_json", fake_chat_json)

        with pytest.raises(ActionDistributionParseError, match="missing 'action_distribution'"):
            await chat_action_distribution(
                messages=[{"role": "user", "content": "x"}],
                persona_id="test",
                expected_actions=["hold", "buy"],
            )

    @pytest.mark.asyncio
    async def test_flat_response_with_action_keys_recovered(self, monkeypatch):
        """LLM flattens the dict — top-level keys match expected actions → recovered."""
        mock = _make_mock_json_result(
            {
                "hold": 0.4,
                "buy": 0.3,
                "sell": 0.3,
                "rationale": "flat response",
            }
        )

        async def fake_chat_json(*args, **kwargs):
            return mock

        monkeypatch.setattr("ssfish.sandbox.chat_json", fake_chat_json)

        result = await chat_action_distribution(
            messages=[{"role": "user", "content": "x"}],
            persona_id="test",
            expected_actions=["hold", "buy", "sell"],
        )
        assert sum(result.action_distribution.values()) == pytest.approx(1.0)
        assert result.action_distribution["hold"] == pytest.approx(0.4)

    @pytest.mark.asyncio
    async def test_strategic_signal_parsed_when_expected(self, monkeypatch):
        """Strategic personas can also emit a strategic_signal field."""
        mock = _make_mock_json_result(
            {
                "action_distribution": {"do_nothing": 0.95, "block_sale_5pct": 0.05},
                "rationale": "维持持有, 等待 Q2 解禁窗口",
                "confidence": 0.6,
                "strategic_signal": {
                    "direction": "reduce",
                    "magnitude": "medium",
                    "time_horizon_days": 90,
                },
            }
        )

        async def fake_chat_json(*args, **kwargs):
            return mock

        monkeypatch.setattr("ssfish.sandbox.chat_json", fake_chat_json)

        result = await chat_action_distribution(
            messages=[{"role": "user", "content": "x"}],
            persona_id="industrial_capital",
            expected_actions=["do_nothing", "block_sale_5pct", "increase_holding_2pct"],
            expects_strategic_signal=True,
        )
        assert result.strategic_signal is not None
        assert result.strategic_signal["direction"] == "reduce"
        assert result.strategic_signal["time_horizon_days"] == 90

    @pytest.mark.asyncio
    async def test_strategic_signal_ignored_when_not_expected(self, monkeypatch):
        """Non-strategic personas: strategic_signal field is ignored."""
        mock = _make_mock_json_result(
            {
                "action_distribution": {"hold": 1.0},
                "rationale": "x",
                "confidence": 0.5,
                "strategic_signal": {"direction": "anything"},  # should be ignored
            }
        )

        async def fake_chat_json(*args, **kwargs):
            return mock

        monkeypatch.setattr("ssfish.sandbox.chat_json", fake_chat_json)

        result = await chat_action_distribution(
            messages=[{"role": "user", "content": "x"}],
            persona_id="retail",
            expected_actions=["hold"],
            expects_strategic_signal=False,
        )
        assert result.strategic_signal is None


# ─────────────────────── spawn_agents (B1a) ───────────────────────


class TestSpawnAgents:

    def test_spawn_count_matches_instance_count(self):
        persona = _make_sandbox_persona(instance_count=200)
        rng = random.Random(42)
        agents = spawn_agents(persona, current_price=100.0, rng=rng)
        assert len(agents) == 200
        assert all(isinstance(a, Agent) for a in agents)
        assert all(a.persona_id == persona.id for a in agents)

    def test_spawn_reproducible_with_same_seed(self):
        """Same seed → identical agents (critical for reproducibility)."""
        persona = _make_sandbox_persona(instance_count=50)
        rng1 = random.Random(123)
        rng2 = random.Random(123)
        agents1 = spawn_agents(persona, current_price=100.0, rng=rng1)
        agents2 = spawn_agents(persona, current_price=100.0, rng=rng2)
        assert len(agents1) == len(agents2)
        for a1, a2 in zip(agents1, agents2):
            assert a1.capital_cny == pytest.approx(a2.capital_cny)
            assert a1.cash_cny == pytest.approx(a2.cash_cny)
            assert a1.holdings_shares == pytest.approx(a2.holdings_shares)

    def test_spawn_different_seeds_diverge(self):
        """Different seeds → different agent samples."""
        persona = _make_sandbox_persona(instance_count=20)
        agents1 = spawn_agents(persona, 100.0, rng=random.Random(1))
        agents2 = spawn_agents(persona, 100.0, rng=random.Random(2))
        # Not all the same
        diff = sum(
            1 for a, b in zip(agents1, agents2)
            if abs(a.capital_cny - b.capital_cny) > 1
        )
        assert diff > 0

    def test_spawn_capital_distribution_centered_on_median(self):
        """Median of sampled capital should be close to spec.median_cny."""
        persona = _make_sandbox_persona(
            instance_count=2000, median_capital=120000, sigma=0.6
        )
        agents = spawn_agents(persona, 100.0, rng=random.Random(7))
        capitals = [a.capital_cny for a in agents]
        median = statistics.median(capitals)
        # Lognormal sample median converges to spec median
        assert 90000 < median < 150000  # rough ±25% tolerance

    def test_spawn_initial_position_bernoulli_holding_ratio(self):
        """About 60% of agents should hold (prob_holding=0.6)."""
        persona = _make_sandbox_persona(
            instance_count=2000, prob_holding=0.60
        )
        agents = spawn_agents(persona, 100.0, rng=random.Random(11))
        holding_count = sum(1 for a in agents if a.holdings_shares > 0)
        ratio = holding_count / len(agents)
        # ±5% tolerance
        assert 0.55 < ratio < 0.65

    def test_spawn_holdings_value_matches_position_pct(self):
        """For agents that hold, holdings_value/capital should be in [pos_min, pos_max]."""
        persona = _make_sandbox_persona(
            instance_count=500, prob_holding=1.0, pos_min=0.10, pos_max=0.40
        )
        agents = spawn_agents(persona, current_price=100.0, rng=random.Random(13))
        for a in agents:
            holdings_value = a.holdings_shares * 100.0
            position_pct = holdings_value / a.capital_cny
            assert 0.099 < position_pct < 0.401  # tiny float tolerance

    def test_spawn_cash_plus_holdings_equals_capital(self):
        """At spawn time (current_price = price agents are valued at), nav == capital."""
        persona = _make_sandbox_persona(instance_count=100)
        agents = spawn_agents(persona, current_price=100.0, rng=random.Random(17))
        for a in agents:
            assert a.nav_at(100.0) == pytest.approx(a.capital_cny, rel=1e-9)
            # pnl is 0 modulo float precision (CNY-scale values, so 1e-3 is plenty)
            assert abs(a.pnl_at(100.0)) < 1e-3

    def test_spawn_no_sandbox_raises(self):
        persona = Persona(
            id="x",
            archetype="x",
            display_name="x",
            voice_prompt="x",
            decision_mode="discretionary",
            role="x",
            market_share=MarketShare(by_volume=0.1),
        )
        with pytest.raises(ValueError, match="no sandbox config"):
            spawn_agents(persona, 100.0, rng=random.Random())

    def test_spawn_zero_price_raises(self):
        persona = _make_sandbox_persona()
        with pytest.raises(ValueError, match="current_price must be > 0"):
            spawn_agents(persona, 0.0, rng=random.Random())

    def test_spawn_zero_instance_count_raises(self):
        persona = _make_sandbox_persona(instance_count=0)
        with pytest.raises(ValueError, match="instance_count must be > 0"):
            spawn_agents(persona, 100.0, rng=random.Random())


# ─────────────────────── apply_action_to_agent (B1b) ───────────────────────


class TestApplyActionToAgent:

    def _agent_with_holdings(self) -> Agent:
        return Agent(
            persona_id="test",
            capital_cny=100000.0,
            cash_cny=40000.0,           # 40k cash
            holdings_shares=600.0,       # 600 shares × 100 = 60k holdings
        )

    def test_sell_action_reduces_holdings_increases_cash(self):
        agent = self._agent_with_holdings()
        action = {"side": "sell", "pool": "holdings_in_target", "fraction": 0.5}
        order = apply_action_to_agent(agent, action, current_price=100.0)
        # Sold 50% of 600 shares = 300 shares × 100 = 30,000 CNY
        assert order == pytest.approx(-30000)
        assert agent.holdings_shares == pytest.approx(300.0)
        assert agent.cash_cny == pytest.approx(70000.0)
        # NAV preserved
        assert agent.nav_at(100.0) == pytest.approx(100000.0)

    def test_buy_action_increases_holdings_decreases_cash(self):
        agent = self._agent_with_holdings()
        action = {"side": "buy", "pool": "cash", "fraction": 0.5}
        order = apply_action_to_agent(agent, action, current_price=100.0)
        # Spent 50% of 40,000 = 20,000 CNY → 200 shares
        assert order == pytest.approx(+20000)
        assert agent.holdings_shares == pytest.approx(800.0)
        assert agent.cash_cny == pytest.approx(20000.0)

    def test_hold_action_returns_zero(self):
        agent = self._agent_with_holdings()
        action = {"side": "none", "pool": "none", "fraction": 0.0}
        order = apply_action_to_agent(agent, action, current_price=100.0)
        assert order == 0.0
        assert agent.holdings_shares == 600.0
        assert agent.cash_cny == 40000.0

    def test_sell_with_no_holdings_returns_zero(self):
        agent = Agent(
            persona_id="t", capital_cny=10000, cash_cny=10000, holdings_shares=0
        )
        action = {"side": "sell", "pool": "holdings_in_target", "fraction": 0.5}
        assert apply_action_to_agent(agent, action, 100.0) == 0.0

    def test_buy_with_no_cash_returns_zero(self):
        agent = Agent(
            persona_id="t", capital_cny=10000, cash_cny=0, holdings_shares=100
        )
        action = {"side": "buy", "pool": "cash", "fraction": 0.5}
        assert apply_action_to_agent(agent, action, 100.0) == 0.0

    def test_zero_fraction_returns_zero(self):
        agent = self._agent_with_holdings()
        action = {"side": "sell", "pool": "holdings_in_target", "fraction": 0.0}
        assert apply_action_to_agent(agent, action, 100.0) == 0.0

    def test_unknown_pool_returns_zero(self):
        agent = self._agent_with_holdings()
        action = {"side": "sell", "pool": "magic_pool", "fraction": 0.5}
        assert apply_action_to_agent(agent, action, 100.0) == 0.0

    def test_price_change_does_not_affect_shares(self):
        """Selling 50% of shares should sell the same SHARE count regardless of price.
        The CNY amount of the order will scale with price."""
        agent1 = self._agent_with_holdings()
        agent2 = self._agent_with_holdings()
        action = {"side": "sell", "pool": "holdings_in_target", "fraction": 0.5}
        order1 = apply_action_to_agent(agent1, action, current_price=100.0)
        order2 = apply_action_to_agent(agent2, action, current_price=110.0)
        # Same share count sold (300), different CNY (30k vs 33k)
        assert agent1.holdings_shares == pytest.approx(agent2.holdings_shares)
        assert order2 == pytest.approx(order1 * 1.10)


# ─────────────────────── sample_actions ───────────────────────


class TestSampleActions:

    def test_returns_correct_count(self):
        rng = random.Random(0)
        out = sample_actions({"a": 0.5, "b": 0.5}, n=100, rng=rng)
        assert len(out) == 100

    def test_distribution_faithful(self):
        """Monte Carlo: sampled ratios should converge to the distribution."""
        rng = random.Random(42)
        out = sample_actions({"a": 0.7, "b": 0.2, "c": 0.1}, n=5000, rng=rng)
        ratio_a = out.count("a") / 5000
        ratio_b = out.count("b") / 5000
        ratio_c = out.count("c") / 5000
        assert 0.66 < ratio_a < 0.74
        assert 0.17 < ratio_b < 0.23
        assert 0.07 < ratio_c < 0.13

    def test_zero_n_returns_empty(self):
        assert sample_actions({"a": 1.0}, n=0, rng=random.Random()) == []

    def test_empty_distribution_raises(self):
        with pytest.raises(ValueError, match="empty distribution"):
            sample_actions({}, n=10, rng=random.Random())


# ─────────────────────── aggregate_class_flow ───────────────────────


class TestAggregateClassFlow:

    def test_basic_aggregation_sums_orders(self):
        """1000 agents, 100% panic_sell_50pct → net flow = -sum(holdings_value/2)."""
        persona = _make_sandbox_persona(
            instance_count=1000, prob_holding=1.0, pos_min=0.30, pos_max=0.30
        )
        rng = random.Random(99)
        agents = spawn_agents(persona, current_price=100.0, rng=rng)
        # Force 100% panic sell
        distribution = {"hold": 0.0, "panic_sell_50pct": 1.0, "fomo_buy_30pct": 0.0}
        result = aggregate_class_flow(
            persona_id=persona.id,
            agents=agents,
            action_distribution=distribution,
            action_specs=persona.sandbox.action_space,
            current_price=100.0,
            rng=rng,
        )
        assert isinstance(result, ClassFlowResult)
        # All 1000 agents sold 50% of their holdings (30% of capital)
        # Total holdings before: sum(capital × 0.30)
        # Total sold: that × 0.5
        total_holdings_before = sum(a.capital_cny * 0.30 for a in agents)
        # But spawn already happened so capitals are set; let's check via direct sum
        expected_flow = -total_holdings_before * 0.5
        assert result.net_flow_cny == pytest.approx(expected_flow, rel=1e-3)
        assert result.action_histogram.get("panic_sell_50pct", 0) == 1000
        assert result.n_agents == 1000

    def test_histogram_counts_match_distribution_at_high_n(self):
        persona = _make_sandbox_persona(instance_count=2000, prob_holding=0.5)
        rng = random.Random(7)
        agents = spawn_agents(persona, 100.0, rng=rng)
        distribution = {
            "hold": 0.6,
            "panic_sell_50pct": 0.3,
            "fomo_buy_30pct": 0.1,
        }
        result = aggregate_class_flow(
            persona_id=persona.id,
            agents=agents,
            action_distribution=distribution,
            action_specs=persona.sandbox.action_space,
            current_price=100.0,
            rng=rng,
        )
        # Histograms should track distribution within ~5%
        n = sum(result.action_histogram.values())
        assert 0.55 < result.action_histogram.get("hold", 0) / n < 0.65
        assert 0.25 < result.action_histogram.get("panic_sell_50pct", 0) / n < 0.35

    def test_hold_distribution_yields_zero_flow(self):
        persona = _make_sandbox_persona(instance_count=200, prob_holding=0.8)
        rng = random.Random(0)
        agents = spawn_agents(persona, 100.0, rng=rng)
        distribution = {"hold": 1.0, "panic_sell_50pct": 0.0, "fomo_buy_30pct": 0.0}
        result = aggregate_class_flow(
            persona_id=persona.id,
            agents=agents,
            action_distribution=distribution,
            action_specs=persona.sandbox.action_space,
            current_price=100.0,
            rng=rng,
        )
        assert result.net_flow_cny == 0.0
        assert result.action_histogram == {"hold": 200}

    def test_rationale_and_strategic_signal_passed_through(self):
        persona = _make_sandbox_persona(instance_count=10)
        rng = random.Random(0)
        agents = spawn_agents(persona, 100.0, rng=rng)
        result = aggregate_class_flow(
            persona_id=persona.id,
            agents=agents,
            action_distribution={"hold": 1.0},
            action_specs=persona.sandbox.action_space,
            current_price=100.0,
            rng=rng,
            rationale="agents are observant but indecisive",
            strategic_signal={"direction": "neutral"},
        )
        assert result.rationale == "agents are observant but indecisive"
        assert result.strategic_signal == {"direction": "neutral"}


# ─────────────────────── update_holdings_for_price ───────────────────────


def test_update_holdings_for_price_is_noop():
    """The function exists for documentation but does nothing — agent
    holdings are tracked in shares, not value, so price moves don't
    require state mutation."""
    a = Agent(persona_id="t", capital_cny=10000, cash_cny=4000, holdings_shares=60.0)
    update_holdings_for_price([a], price_delta_pct=-0.05)
    assert a.cash_cny == 4000
    assert a.holdings_shares == 60.0
    # But the *value* at the new price IS lower
    assert a.holdings_value_cny(95.0) < a.holdings_value_cny(100.0)
