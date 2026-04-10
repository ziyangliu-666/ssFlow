"""Tests for Phase 4: dynamic policy creation via LLM."""

import pytest

from ssflow.action_collector import ActionCollector
from ssflow.policy import (
    AnnounceAction,
    MutateAction,
    Policy,
    TradeAction,
)
from ssflow.policy_engine import evaluate_policies
from ssflow.world import SimAgent, SimGraph


class TestFromLlmSpec:
    def test_trade_spec(self):
        p = Policy.from_llm_spec({
            "name": "止损20%",
            "trigger": "price_change_pct < -20.0",
            "action_type": "trade",
            "side": "sell",
            "quantity_pct": 1.0,
            "cooldown": 3,
        })
        assert p.name == "止损20%"
        assert p.trigger_expr == "price_change_pct < -20.0"
        assert isinstance(p.action, TradeAction)
        assert p.action.side == "sell"
        assert p.action.quantity_pct == 1.0
        assert p.cooldown_rounds == 3
        assert p.source == "llm"
        assert p.id.startswith("llm_")

    def test_announce_spec(self):
        p = Policy.from_llm_spec({
            "name": "库存预警",
            "trigger": "inventory_months > 4.0",
            "action_type": "announce",
            "text": "库存积压严重，启动促销",
        })
        assert isinstance(p.action, AnnounceAction)
        assert "促销" in p.action.text

    def test_mutate_spec(self):
        p = Policy.from_llm_spec({
            "name": "紧缩",
            "trigger": "cash_bn < 5.0",
            "action_type": "mutate",
            "mutations": {"capacity_utilization": 0.4},
        })
        assert isinstance(p.action, MutateAction)
        assert p.action.mutations == {"capacity_utilization": 0.4}

    def test_rejects_bad_trigger(self):
        with pytest.raises(ValueError, match="Invalid trigger"):
            Policy.from_llm_spec({
                "name": "bad",
                "trigger": "x AND y",
                "action_type": "trade",
                "side": "sell",
            })

    def test_rejects_missing_trigger(self):
        with pytest.raises(ValueError, match="trigger"):
            Policy.from_llm_spec({
                "name": "no trigger",
                "action_type": "trade",
                "side": "sell",
            })

    def test_rejects_bad_side(self):
        with pytest.raises(ValueError, match="side"):
            Policy.from_llm_spec({
                "name": "bad side",
                "trigger": "x > 1",
                "action_type": "trade",
                "side": "maybe",
            })

    def test_rejects_unknown_action(self):
        with pytest.raises(ValueError, match="Unknown"):
            Policy.from_llm_spec({
                "name": "wat",
                "trigger": "x > 1",
                "action_type": "teleport",
            })


class TestDynamicPolicyLifecycle:
    """Test that an LLM-created policy actually evaluates in subsequent rounds."""

    def test_created_policy_fires(self):
        g = SimGraph()
        a = SimAgent(
            id="trader_retail", kind="trader", display_name="散户",
            state={"price_change_pct": -5.0},
            persona_id="retail_short",
        )
        g.add_agent(a)

        # Simulate what the engine does: create policy from LLM spec
        new_policy = Policy.from_llm_spec({
            "name": "止损",
            "trigger": "price_change_pct < -15.0",
            "action_type": "trade",
            "side": "sell",
            "quantity_pct": 1.0,
        }, source="llm_round_0")
        a.policies.append(new_policy)

        # Round 1: price hasn't dropped enough
        fires = evaluate_policies(g, round_idx=1)
        assert len(fires) == 0

        # Round 2: price drops further
        a.set("price_change_pct", -18.0)
        fires = evaluate_policies(g, round_idx=2)
        assert len(fires) == 1
        assert fires[0].policy.name == "止损"
        assert isinstance(fires[0].action, TradeAction)
        assert fires[0].action.side == "sell"

    def test_policy_count_grows(self):
        """Verify that creating policies increases the count on the agent."""
        g = SimGraph()
        a = SimAgent(id="a", kind="trader", display_name="A",
                     state={"x": 0.0}, persona_id="p")
        g.add_agent(a)
        assert len(a.policies) == 0

        # Create 3 policies
        for i in range(3):
            p = Policy.from_llm_spec({
                "name": f"rule_{i}",
                "trigger": f"x > {i}.0",
                "action_type": "trade",
                "side": "sell",
                "quantity_pct": 0.1,
            }, source=f"llm_round_{i}")
            a.policies.append(p)

        assert len(a.policies) == 3

    def test_serialization_preserves_llm_policies(self):
        """LLM-created policies survive SimGraph round-trip."""
        g = SimGraph()
        a = SimAgent(id="a", kind="trader", display_name="A",
                     state={"x": 0.0}, persona_id="p")
        p = Policy.from_llm_spec({
            "name": "dynamic rule",
            "trigger": "x > 5.0",
            "action_type": "trade",
            "side": "sell",
            "quantity_pct": 0.5,
        }, source="llm_round_3")
        a.policies.append(p)
        g.add_agent(a)

        # Round-trip
        data = g.to_serializable()
        g2 = SimGraph.from_serializable(data)

        assert len(g2.agents["a"].policies) == 1
        p2 = g2.agents["a"].policies[0]
        assert p2.trigger_expr == "x > 5.0"
        assert isinstance(p2.action, TradeAction)
        assert p2.action.side == "sell"
