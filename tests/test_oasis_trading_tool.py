"""Tests for oasis_trading_tool (OrderCollector + submit_order FunctionTool).

Validates the Phase II unified-decision path:
  - OrderCollector thread-safely accumulates PendingOrders
  - make_submit_order_tool produces a FunctionTool bound to one persona
  - The tool's callable captures distribution + rationale into the collector
  - drain() returns + clears pending orders
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from ssflow.oasis_trading_tool import (
    OrderCollector,
    PendingOrder,
    make_submit_order_tool,
)
from ssflow.persona import MarketShare, Persona, SandboxConfig


def _make_trader(pid: str = "test_trader") -> Persona:
    return Persona(
        id=pid,
        archetype="测试",
        display_name="测试 trader",
        voice_prompt="test",
        decision_mode="discretionary",
        role="directional_speculator",
        market_share=MarketShare(by_volume=0.10),
        entity_role="trader",
        sandbox=SandboxConfig(
            instance_count=10,
            capital_distribution={"type": "fixed", "value_cny": 100000},
            initial_position_distribution={"type": "none"},
            risk={"max_position_pct": 0.95},
            action_space=[
                {"name": "hold", "side": "none", "pool": "none", "fraction": 0.0},
                {"name": "buy_30pct", "side": "buy", "pool": "cash", "fraction": 0.30},
                {"name": "sell_50pct", "side": "sell", "pool": "holdings_in_target", "fraction": 0.50},
            ],
        ),
    )


def _make_info() -> Persona:
    return Persona(
        id="info_entity",
        archetype="新闻",
        display_name="新闻源",
        voice_prompt="test",
        decision_mode="discretionary",
        role="information_intermediary",
        entity_role="media",
        market_share=MarketShare(by_volume=0.0),
    )


# ─────────────────────── OrderCollector ───────────────────────


class TestOrderCollector:

    def test_empty_collector(self):
        c = OrderCollector()
        assert len(c) == 0
        assert c.drain() == []

    def test_add_one(self):
        c = OrderCollector()
        c.add(persona_id="x", distribution={"hold": 1.0}, rationale="just holding")
        assert len(c) == 1

    def test_drain_returns_and_clears(self):
        c = OrderCollector()
        c.add(persona_id="x", distribution={"hold": 1.0})
        c.add(persona_id="y", distribution={"buy": 1.0})
        drained = c.drain()
        assert len(drained) == 2
        assert {o.persona_id for o in drained} == {"x", "y"}
        assert len(c) == 0

    def test_set_round_tags_new_orders(self):
        c = OrderCollector()
        c.set_round(3)
        c.add(persona_id="x", distribution={"hold": 1.0})
        drained = c.drain()
        assert drained[0].round_idx == 3

    def test_thread_safety(self):
        c = OrderCollector()
        def worker(pid_prefix: str):
            for i in range(100):
                c.add(persona_id=f"{pid_prefix}_{i}", distribution={"hold": 1.0})
        threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(c) == 500  # 5 workers × 100 adds

    def test_preserves_raw_args(self):
        c = OrderCollector()
        c.add(
            persona_id="x",
            distribution={"hold": 1.0},
            rationale="test",
            raw_args={"raw_key": "raw_val"},
        )
        drained = c.drain()
        assert drained[0].raw_args == {"raw_key": "raw_val"}


# ─────────────────────── make_submit_order_tool ───────────────────────


class TestMakeSubmitOrderTool:

    def test_returns_function_tool(self):
        from camel.toolkits import FunctionTool
        collector = OrderCollector()
        persona = _make_trader()
        tool = make_submit_order_tool(persona, collector)
        assert isinstance(tool, FunctionTool)
        assert tool.func.__name__ == "submit_order_distribution"

    def test_openai_schema_generated(self):
        collector = OrderCollector()
        persona = _make_trader()
        tool = make_submit_order_tool(persona, collector)
        schema = tool.get_openai_tool_schema()
        assert schema["type"] == "function"
        fn = schema["function"]
        assert fn["name"] == "submit_order_distribution"
        assert "action_distribution" in fn["parameters"]["properties"]
        assert "rationale" in fn["parameters"]["properties"]

    def test_docstring_contains_action_names(self):
        """The docstring (which becomes the LLM's view of the tool) should
        mention the persona's actual action names so the LLM picks valid ones."""
        collector = OrderCollector()
        persona = _make_trader()
        tool = make_submit_order_tool(persona, collector)
        doc = tool.func.__doc__
        assert "hold" in doc
        assert "buy_30pct" in doc
        assert "sell_50pct" in doc

    def test_non_trader_rejected(self):
        collector = OrderCollector()
        info_persona = _make_info()
        with pytest.raises(ValueError, match="no sandbox"):
            make_submit_order_tool(info_persona, collector)

    def test_tool_call_captures_to_collector(self):
        collector = OrderCollector()
        persona = _make_trader("my_trader")
        tool = make_submit_order_tool(persona, collector)

        # Invoke the underlying callable like CAMEL would
        result = tool.func(
            action_distribution={"hold": 0.3, "buy_30pct": 0.7},
            rationale="market looks strong",
        )

        assert isinstance(result, str)
        assert "my_trader" in result

        drained = collector.drain()
        assert len(drained) == 1
        o = drained[0]
        assert o.persona_id == "my_trader"
        assert o.distribution == {"hold": 0.3, "buy_30pct": 0.7}
        assert o.rationale == "market looks strong"

    def test_tool_call_with_string_distribution(self):
        """Some LLMs may serialize the dict to a string — the tool should
        tolerate that and parse."""
        collector = OrderCollector()
        persona = _make_trader()
        tool = make_submit_order_tool(persona, collector)

        tool.func(
            action_distribution='{"hold": 1.0}',  # string-encoded
            rationale="test",
        )
        drained = collector.drain()
        assert len(drained) == 1
        assert drained[0].distribution == {"hold": 1.0}

    def test_tool_call_with_non_dict_falls_back_to_empty(self):
        collector = OrderCollector()
        persona = _make_trader()
        tool = make_submit_order_tool(persona, collector)

        tool.func(action_distribution=None, rationale="x")  # type: ignore
        drained = collector.drain()
        assert len(drained) == 1
        assert drained[0].distribution == {}

    def test_two_tools_bind_to_different_personas(self):
        """Each call to make_submit_order_tool should bind to its own
        persona id (closure test)."""
        collector = OrderCollector()
        pa = _make_trader("trader_a")
        pb = _make_trader("trader_b")
        tool_a = make_submit_order_tool(pa, collector)
        tool_b = make_submit_order_tool(pb, collector)

        tool_a.func(action_distribution={"hold": 1.0}, rationale="a reason")
        tool_b.func(action_distribution={"buy_30pct": 1.0}, rationale="b reason")

        drained = collector.drain()
        assert len(drained) == 2
        by_id = {o.persona_id: o for o in drained}
        assert by_id["trader_a"].distribution == {"hold": 1.0}
        assert by_id["trader_b"].distribution == {"buy_30pct": 1.0}


# ─────────────────────── apply_distribution_to_agent_pop ───────────────────────


class TestApplyDistributionToAgentPop:
    """End-to-end test: the pure-math pipeline from distribution → net_flow,
    decoupled from any LLM call. This is what the Phase II engine calls once
    it has drained the collector."""

    def test_basic_flow(self):
        import random
        from ssflow.trading_layer import apply_distribution_to_agent_pop, spawn_agents

        persona = _make_trader("retail")
        # Give them some initial holdings so selling has an effect
        persona.sandbox.initial_position_distribution = {
            "type": "fixed", "value": 0.5
        }
        agents = spawn_agents(persona, current_price=100.0, rng=random.Random(42))

        # Bullish distribution
        flow = apply_distribution_to_agent_pop(
            persona=persona,
            agents=agents,
            distribution={"hold": 0.3, "buy_30pct": 0.6, "sell_50pct": 0.1},
            current_price=100.0,
            rng=random.Random(7),
            rationale="mostly bullish",
        )
        assert flow.persona_id == "retail"
        assert flow.n_agents == 10
        assert sum(flow.action_histogram.values()) == 10
        assert "mostly bullish" in flow.rationale
        # Mostly buy → expect positive net flow
        assert flow.net_flow > 0
