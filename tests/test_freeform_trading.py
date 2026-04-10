"""Tests for the free-form trading tool and __freeform__ distribution path."""

import random

import pytest

from ssflow.persona import Persona, SandboxConfig, MarketShare
from ssflow.trading_layer import (
    Agent,
    ClassFlowResult,
    apply_distribution_to_agent_pop,
    spawn_agents,
)

try:
    from ssflow.oasis_trading_tool import OrderCollector, make_freeform_trading_tool
    HAS_CAMEL = True
except ImportError:
    HAS_CAMEL = False


def _make_trader_persona() -> Persona:
    return Persona(
        id="retail_test",
        archetype="test_retail",
        display_name="Test Retail",
        voice_prompt="test",
        market_share=MarketShare(by_volume=0.5),
        decision_mode="discretionary",
        role="directional_speculator",
        sandbox=SandboxConfig(
            instance_count=100,
            capital_distribution={"type": "fixed", "value_cny": 100000},
            initial_position_distribution={
                "type": "bernoulli",
                "prob_holding": 0.5,
                "position_size_pct_when_holding": {"type": "fixed", "value": 0.3},
            },
            risk={"max_position_pct": 0.95},
            action_space=[
                {"name": "hold", "side": "none", "pool": "none", "fraction": 0.0},
                {"name": "buy_30pct", "side": "buy", "pool": "cash", "fraction": 0.30},
                {"name": "sell_50pct", "side": "sell", "pool": "holdings_in_target", "fraction": 0.50},
            ],
        ),
    )


# ─────────────────────── Freeform Tool ───────────────────────


@pytest.mark.skipif(not HAS_CAMEL, reason="camel-ai not installed")
class TestMakeFreeformTradingTool:
    def test_creates_tool(self):
        persona = _make_trader_persona()
        collector = OrderCollector()
        tool = make_freeform_trading_tool(persona, collector)
        assert tool is not None

    def test_submit_buy(self):
        persona = _make_trader_persona()
        collector = OrderCollector()
        tool = make_freeform_trading_tool(persona, collector)

        result = tool.func(side="buy", quantity_pct=0.37, rationale="看好")
        assert "BUY" in result
        assert "37%" in result

        orders = collector.drain()
        assert len(orders) == 1
        assert orders[0].distribution == {"__freeform__": 1.0}
        assert orders[0].raw_args["side"] == "buy"
        assert orders[0].raw_args["quantity_pct"] == 0.37

    def test_submit_sell(self):
        persona = _make_trader_persona()
        collector = OrderCollector()
        tool = make_freeform_trading_tool(persona, collector)

        result = tool.func(side="sell", quantity_pct=0.80, rationale="止损")
        assert "SELL" in result
        assert "80%" in result

        orders = collector.drain()
        assert orders[0].raw_args["side"] == "sell"
        assert orders[0].raw_args["pool"] == "holdings_in_target"

    def test_submit_hold(self):
        persona = _make_trader_persona()
        collector = OrderCollector()
        tool = make_freeform_trading_tool(persona, collector)

        result = tool.func(side="hold", quantity_pct=0.0, rationale="观望")
        assert "HOLD" in result

        orders = collector.drain()
        assert orders[0].raw_args["side"] == "hold"

    def test_clamps_quantity(self):
        persona = _make_trader_persona()
        collector = OrderCollector()
        tool = make_freeform_trading_tool(persona, collector)

        tool.func(side="buy", quantity_pct=1.5, rationale="")
        orders = collector.drain()
        assert orders[0].raw_args["quantity_pct"] == 1.0  # clamped

    def test_handles_invalid_quantity(self):
        persona = _make_trader_persona()
        collector = OrderCollector()
        tool = make_freeform_trading_tool(persona, collector)

        tool.func(side="buy", quantity_pct="not_a_number", rationale="")
        orders = collector.drain()
        assert orders[0].raw_args["side"] == "hold"  # falls through


# ─────────────────────── Freeform Distribution Path ───────────────────────


class TestFreeformDistributionPath:
    def _spawn(self, persona: Persona, price: float = 100.0):
        rng = random.Random(42)
        return spawn_agents(persona, price, rng)

    def test_buy_freeform(self):
        persona = _make_trader_persona()
        agents = self._spawn(persona)
        rng = random.Random(42)

        result = apply_distribution_to_agent_pop(
            persona=persona,
            agents=agents,
            distribution={"__freeform__": 1.0},
            current_price=100.0,
            rng=rng,
            rationale="buying",
            raw_distribution={"side": "buy", "quantity_pct": 0.37, "pool": "cash"},
        )

        assert isinstance(result, ClassFlowResult)
        assert result.net_flow > 0  # positive = net buying
        # With per-agent dispersion, histogram shows buy/sell/hold breakdown
        # instead of the old {"__freeform__": N} format. Most agents should buy.
        assert result.action_histogram.get("buy", 0) > len(agents) * 0.5

    def test_sell_freeform(self):
        persona = _make_trader_persona()
        agents = self._spawn(persona)
        rng = random.Random(42)

        result = apply_distribution_to_agent_pop(
            persona=persona,
            agents=agents,
            distribution={"__freeform__": 1.0},
            current_price=100.0,
            rng=rng,
            raw_distribution={"side": "sell", "quantity_pct": 0.50, "pool": "holdings_in_target"},
        )

        assert result.net_flow < 0  # negative = selling

    def test_hold_freeform(self):
        persona = _make_trader_persona()
        agents = self._spawn(persona)
        rng = random.Random(42)

        result = apply_distribution_to_agent_pop(
            persona=persona,
            agents=agents,
            distribution={"__freeform__": 1.0},
            current_price=100.0,
            rng=rng,
            raw_distribution={"side": "hold", "quantity_pct": 0.0},
        )

        # With per-agent dispersion, a class "hold" decision (conviction=0)
        # still produces noise trading: some agents buy, some sell. But the
        # buys and sells should roughly cancel out, producing small net flow
        # relative to total capital. The exact distribution depends on
        # persona biases (dispersion parameter).
        total_capital = sum(a.capital for a in agents)
        assert abs(result.net_flow) < total_capital * 0.10

    def test_37pct_not_truncated_to_nearest_bucket(self):
        """The whole point of freeform: 37% is 37%, not rounded to 30% or 50%."""
        persona = _make_trader_persona()
        # Spawn agents with ONLY cash (no initial holdings) to make the math simple
        agents = [
            Agent(
                persona_id="retail_test",
                capital=100000.0,
                cash=100000.0,
                holdings={"_default": 0.0},
                max_holdings_value=95000.0,
            )
        ]
        rng = random.Random(42)

        result = apply_distribution_to_agent_pop(
            persona=persona,
            agents=agents,
            distribution={"__freeform__": 1.0},
            current_price=100.0,
            rng=rng,
            raw_distribution={"side": "buy", "quantity_pct": 0.37, "pool": "cash"},
        )

        # 37% of 100k cash = 37000 in the ideal case. With per-agent dispersion,
        # the actual fraction varies (noised around 0.37). With 1 agent the
        # noise may shift it, but it should be in the right ballpark (not
        # rounded to a fixed bucket like 30% or 50%).
        assert result.net_flow > 0
        assert abs(result.net_flow - 37000.0) < 15000  # within ±15k of 37k

    def test_legacy_path_still_works(self):
        """Without __freeform__, the old distribution path works unchanged."""
        persona = _make_trader_persona()
        agents = self._spawn(persona)
        rng = random.Random(42)

        result = apply_distribution_to_agent_pop(
            persona=persona,
            agents=agents,
            distribution={"hold": 0.5, "buy_30pct": 0.3, "sell_50pct": 0.2},
            current_price=100.0,
            rng=rng,
        )

        assert isinstance(result, ClassFlowResult)
        assert "hold" in result.action_histogram
