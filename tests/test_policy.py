"""Tests for the policy engine (Phase 2)."""

import pytest

from ssflow.policy import (
    AnnounceAction,
    MutateAction,
    Policy,
    RejectAction,
    TradeAction,
)
from ssflow.policy_engine import (
    collect_announcements,
    collect_trade_overrides,
    compile_trigger,
    evaluate_policies,
)
from ssflow.world import SimAgent, SimGraph


class TestCompileTrigger:
    def test_greater_than(self):
        fn = compile_trigger("margin > 0.72")
        a = SimAgent(id="a", kind="trader", display_name="A",
                     state={"margin": 0.8})
        assert fn(a) is True

    def test_less_than(self):
        fn = compile_trigger("price_change_pct < -5.0")
        a = SimAgent(id="a", kind="trader", display_name="A",
                     state={"price_change_pct": -7.0})
        assert fn(a) is True

    def test_not_met(self):
        fn = compile_trigger("margin > 0.72")
        a = SimAgent(id="a", kind="trader", display_name="A",
                     state={"margin": 0.5})
        assert fn(a) is False

    def test_missing_var_returns_zero(self):
        fn = compile_trigger("x > 0.5")
        a = SimAgent(id="a", kind="trader", display_name="A", state={})
        assert fn(a) is False  # 0.0 > 0.5 is False

    def test_rejects_invalid(self):
        with pytest.raises(ValueError, match="Cannot compile"):
            compile_trigger("x AND y")


class TestPolicySerialization:
    def test_roundtrip_trade(self):
        p = Policy(
            id="t1", name="margin call", description="margin too high",
            trigger_expr="margin > 0.72",
            action=TradeAction(side="sell", quantity_pct=0.5),
            source="template", cooldown_rounds=3, priority=10,
        )
        data = p.to_serializable()
        p2 = Policy.from_serializable(data)
        assert p2.id == "t1"
        assert p2.trigger_expr == "margin > 0.72"
        assert isinstance(p2.action, TradeAction)
        assert p2.action.side == "sell"
        assert p2.action.quantity_pct == 0.5
        assert p2.priority == 10

    def test_roundtrip_announce(self):
        p = Policy(
            id="a1", name="warn", description="low inventory",
            trigger_expr="inv > 3",
            action=AnnounceAction(text="low inventory alert"),
        )
        data = p.to_serializable()
        p2 = Policy.from_serializable(data)
        assert isinstance(p2.action, AnnounceAction)
        assert p2.action.text == "low inventory alert"

    def test_roundtrip_mutate(self):
        p = Policy(
            id="m1", name="cut capex", description="cash low",
            trigger_expr="cash_bn < 15",
            action=MutateAction(mutations={"capacity": 0.6}),
        )
        data = p.to_serializable()
        p2 = Policy.from_serializable(data)
        assert isinstance(p2.action, MutateAction)
        assert p2.action.mutations == {"capacity": 0.6}


class TestEvaluatePolicies:
    def _graph_with_policy(self, state, policy):
        g = SimGraph()
        a = SimAgent(id="retail", kind="trader", display_name="散户",
                     state=state, persona_id="retail_short")
        a.policies.append(policy)
        g.add_agent(a)
        return g

    def test_fires_when_met(self):
        g = self._graph_with_policy(
            {"margin": 0.8},
            Policy(id="t1", name="margin", description="margin call",
                   trigger_expr="margin > 0.72",
                   action=TradeAction(side="sell", quantity_pct=0.5)),
        )
        fires = evaluate_policies(g, round_idx=0)
        assert len(fires) == 1
        assert fires[0].agent_id == "retail"
        assert isinstance(fires[0].action, TradeAction)

    def test_does_not_fire_when_not_met(self):
        g = self._graph_with_policy(
            {"margin": 0.5},
            Policy(id="t1", name="margin", description="margin call",
                   trigger_expr="margin > 0.72",
                   action=TradeAction(side="sell", quantity_pct=0.5)),
        )
        fires = evaluate_policies(g, round_idx=0)
        assert len(fires) == 0

    def test_cooldown(self):
        p = Policy(id="t1", name="margin", description="margin call",
                   trigger_expr="margin > 0.72",
                   action=TradeAction(side="sell", quantity_pct=0.5),
                   cooldown_rounds=3)
        g = self._graph_with_policy({"margin": 0.8}, p)
        fires = evaluate_policies(g, round_idx=0)
        assert len(fires) == 1
        # Should not fire again for 3 rounds
        fires = evaluate_policies(g, round_idx=1)
        assert len(fires) == 0
        fires = evaluate_policies(g, round_idx=2)
        assert len(fires) == 0
        fires = evaluate_policies(g, round_idx=3)
        assert len(fires) == 1

    def test_mutate_action_applied_immediately(self):
        g = self._graph_with_policy(
            {"cash_bn": 10.0, "capacity": 0.8},
            Policy(id="m1", name="cut", description="cut capex",
                   trigger_expr="cash_bn < 15.0",
                   action=MutateAction(mutations={"capacity": 0.6})),
        )
        evaluate_policies(g, round_idx=0)
        assert g.agents["retail"].get("capacity") == 0.6

    def test_disabled_policy_skipped(self):
        p = Policy(id="t1", name="x", description="x",
                   trigger_expr="margin > 0.72",
                   action=TradeAction(side="sell", quantity_pct=0.5),
                   enabled=False)
        g = self._graph_with_policy({"margin": 0.8}, p)
        fires = evaluate_policies(g, round_idx=0)
        assert len(fires) == 0


class TestCollectHelpers:
    def test_collect_trade_overrides(self):
        g = SimGraph()
        a = SimAgent(id="r", kind="trader", display_name="R",
                     state={"margin": 0.8}, persona_id="retail_short")
        a.policies.append(Policy(
            id="t1", name="m", description="margin call",
            trigger_expr="margin > 0.72",
            action=TradeAction(side="sell", quantity_pct=0.5),
            priority=10,
        ))
        g.add_agent(a)
        fires = evaluate_policies(g, round_idx=0)
        overrides = collect_trade_overrides(fires, g)
        assert "retail_short" in overrides
        assert overrides["retail_short"].action.side == "sell"

    def test_collect_announcements(self):
        g = SimGraph()
        a = SimAgent(id="co", kind="company", display_name="Co",
                     state={"inv": 5.0})
        a.policies.append(Policy(
            id="a1", name="warn", description="high inv",
            trigger_expr="inv > 3.0",
            action=AnnounceAction(text="inventory warning"),
        ))
        g.add_agent(a)
        fires = evaluate_policies(g, round_idx=0)
        anns = collect_announcements(fires)
        assert len(anns) == 1
        assert anns[0].action.text == "inventory warning"


class TestThresholdToPolicyConversion:
    def test_builder_converts_thresholds(self):
        """Verify sim_graph_builder converts entity Thresholds → Policies."""
        from ssflow.sandbox_generator import build_from_template
        from ssflow.event import Event
        from ssflow.persona import load_personas
        from ssflow.sim_graph_builder import build_sim_graph
        from pathlib import Path

        eg = build_from_template("test", market="ashare", current_price=100.0)
        event = Event(
            ticker="TEST", event_text="test", event_type="other",
            event_date="2026-01-01",
        )
        personas = load_personas(Path("personas/ashare.yaml"))
        sg = build_sim_graph(personas, eg, event)

        # Count policies across all agents. This includes both threshold-derived
        # policies and auto-generated risk policies (stop-loss, profit-take, etc.)
        total_policies = sum(len(a.policies) for a in sg.agents.values())
        threshold_policies = [
            p for a in sg.agents.values() for p in a.policies
            if p.source == "template"
        ]
        assert len(threshold_policies) == len(eg.thresholds)
        # Auto-generated risk policies exist in addition to threshold policies
        assert total_policies >= len(eg.thresholds)

        # Verify force_action thresholds became TradeAction policies
        # (filter to template-sourced only, since risk_config policies also use TradeAction)
        trade_policies_from_thresholds = [
            p for a in sg.agents.values() for p in a.policies
            if isinstance(p.action, TradeAction) and p.source == "template"
        ]
        force_thresholds = [
            t for t in eg.thresholds if t.effect.effect_type == "force_action"
        ]
        assert len(trade_policies_from_thresholds) == len(force_thresholds)
