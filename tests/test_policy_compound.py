"""Tests for compound triggers and staged policies (Phase 5)."""

import pytest

from ssflow.policy import (
    AnnounceAction,
    MutateAction,
    Policy,
    TradeAction,
)
from ssflow.policy_engine import (
    PolicyStage,
    StagedPolicy,
    compile_trigger,
    create_exchange_inquiry_policy,
    create_national_team_etf_buy_policy,
    create_short_sell_restriction_policy,
    create_suspension_risk_policy,
    evaluate_staged_policies,
    get_ashare_policy_templates,
)
from ssflow.world import SimAgent, SimGraph


# ─────────────────────── Compound Trigger Tests ───────────────────────


class TestCompoundTriggerParsing:
    """Test AND, OR, NOT compound trigger expressions."""

    def _agent(self, **state: float) -> SimAgent:
        return SimAgent(
            id="test", kind="trader", display_name="Test",
            state=state,
        )

    def test_simple_and(self):
        fn = compile_trigger("margin > 0.5 AND leverage > 2.0")
        assert fn(self._agent(margin=0.8, leverage=3.0)) is True
        assert fn(self._agent(margin=0.8, leverage=1.0)) is False
        assert fn(self._agent(margin=0.3, leverage=3.0)) is False

    def test_simple_or(self):
        fn = compile_trigger("margin > 0.9 OR leverage > 5.0")
        assert fn(self._agent(margin=0.95, leverage=1.0)) is True
        assert fn(self._agent(margin=0.5, leverage=6.0)) is True
        assert fn(self._agent(margin=0.5, leverage=1.0)) is False

    def test_not(self):
        fn = compile_trigger("NOT (active > 0.5)")
        assert fn(self._agent(active=0.0)) is True
        assert fn(self._agent(active=1.0)) is False

    def test_and_with_negative_values(self):
        fn = compile_trigger("cumulative_delta < -0.07 AND round_idx >= 2.0")
        assert fn(self._agent(cumulative_delta=-0.10, round_idx=3.0)) is True
        assert fn(self._agent(cumulative_delta=-0.10, round_idx=1.0)) is False
        assert fn(self._agent(cumulative_delta=-0.01, round_idx=3.0)) is False

    def test_complex_or_with_and(self):
        expr = "limit_down_days >= 2.0 OR (delta_pct < -0.05 AND stress_level == 'crisis')"
        fn = compile_trigger(expr)
        # First branch: limit_down_days >= 2
        assert fn(self._agent(limit_down_days=3.0, delta_pct=0.0)) is True
        # Second branch: delta_pct < -0.05 AND stress_level == 'crisis'
        # We need string state for stress_level
        agent = self._agent(limit_down_days=0.0, delta_pct=-0.08)
        agent.state["stress_level"] = "crisis"
        assert fn(agent) is True
        # Neither branch
        assert fn(self._agent(limit_down_days=1.0, delta_pct=-0.01)) is False

    def test_not_with_and(self):
        fn = compile_trigger("NOT (national_team_active > 0.5) AND delta_pct < -0.03")
        assert fn(self._agent(national_team_active=0.0, delta_pct=-0.05)) is True
        assert fn(self._agent(national_team_active=1.0, delta_pct=-0.05)) is False
        assert fn(self._agent(national_team_active=0.0, delta_pct=0.01)) is False

    def test_parenthesised_expression(self):
        fn = compile_trigger("(margin > 0.5)")
        assert fn(self._agent(margin=0.8)) is True
        assert fn(self._agent(margin=0.3)) is False

    def test_string_comparison(self):
        fn = compile_trigger("stress_level == 'crisis'")
        agent = self._agent()
        agent.state["stress_level"] = "crisis"
        assert fn(agent) is True
        agent.state["stress_level"] = "normal"
        assert fn(agent) is False

    def test_multi_and(self):
        fn = compile_trigger("a > 1.0 AND b > 2.0 AND c > 3.0")
        assert fn(self._agent(a=2.0, b=3.0, c=4.0)) is True
        assert fn(self._agent(a=2.0, b=3.0, c=1.0)) is False

    def test_multi_or(self):
        fn = compile_trigger("a > 10.0 OR b > 10.0 OR c > 10.0")
        assert fn(self._agent(a=0.0, b=0.0, c=11.0)) is True
        assert fn(self._agent(a=0.0, b=0.0, c=0.0)) is False

    def test_backward_compatible_simple(self):
        """Simple triggers should still work exactly as before."""
        fn = compile_trigger("margin_utilization > 0.72")
        a = SimAgent(id="a", kind="trader", display_name="A",
                     state={"margin_utilization": 0.8})
        assert fn(a) is True

    def test_invalid_compound_raises(self):
        """An expression that is neither simple nor valid compound should raise."""
        with pytest.raises(ValueError, match="Cannot compile"):
            compile_trigger("definitely not a trigger $$$")


# ─────────────────────── Staged Policy Tests ───────────────────────


class TestStagedPolicyEscalation:
    """Test staged policy evaluation and escalation."""

    def _agent(self, **state: float) -> SimAgent:
        return SimAgent(
            id="nt", kind="trader", display_name="National Team",
            state=state,
            persona_id="national_team_strategic",
        )

    def _make_staged_policy(self) -> StagedPolicy:
        return StagedPolicy(
            name="test_staged",
            stages=[
                PolicyStage(
                    level=1,
                    trigger="severity > 1.0",
                    actions=[AnnounceAction(text="Level 1 warning")],
                    description="Stage 1 verbal warning",
                ),
                PolicyStage(
                    level=2,
                    trigger="severity > 2.0",
                    actions=[
                        AnnounceAction(text="Level 2 action"),
                        TradeAction(side="buy", quantity_pct=0.05),
                    ],
                    description="Stage 2 operational",
                ),
                PolicyStage(
                    level=3,
                    trigger="severity > 3.0",
                    actions=[TradeAction(side="buy", quantity_pct=0.15)],
                    description="Stage 3 emergency",
                ),
            ],
            cooldown_rounds=1,
        )

    def test_stage_1_fires(self):
        sp = self._make_staged_policy()
        agent = self._agent(severity=1.5)
        actions = sp.evaluate(agent, round_idx=0)
        assert actions is not None
        assert len(actions) == 1
        assert isinstance(actions[0], AnnounceAction)
        assert sp.current_stage == 1

    def test_escalation_to_stage_2(self):
        sp = self._make_staged_policy()
        agent = self._agent(severity=1.5)
        sp.evaluate(agent, round_idx=0)
        assert sp.current_stage == 1

        # Escalate
        agent.set("severity", 2.5)
        actions = sp.evaluate(agent, round_idx=2)
        assert actions is not None
        assert len(actions) == 2  # Announce + Trade
        assert sp.current_stage == 2

    def test_escalation_to_stage_3(self):
        sp = self._make_staged_policy()
        agent = self._agent(severity=1.5)
        sp.evaluate(agent, round_idx=0)
        agent.set("severity", 2.5)
        sp.evaluate(agent, round_idx=2)
        agent.set("severity", 3.5)
        actions = sp.evaluate(agent, round_idx=4)
        assert actions is not None
        assert sp.current_stage == 3

    def test_no_fire_below_threshold(self):
        sp = self._make_staged_policy()
        agent = self._agent(severity=0.5)
        actions = sp.evaluate(agent, round_idx=0)
        assert actions is None
        assert sp.current_stage == 0

    def test_no_repeat_same_stage(self):
        """Once a stage fires, it shouldn't fire again."""
        sp = self._make_staged_policy()
        agent = self._agent(severity=1.5)
        sp.evaluate(agent, round_idx=0)
        assert sp.current_stage == 1
        # Same severity, should not re-fire stage 1
        actions = sp.evaluate(agent, round_idx=5)
        assert actions is None

    def test_skip_stage(self):
        """If severity jumps, intermediate stages can be skipped."""
        sp = self._make_staged_policy()
        agent = self._agent(severity=3.5)
        # Stage 1 fires first (lowest level that hasn't fired)
        actions = sp.evaluate(agent, round_idx=0)
        assert sp.current_stage == 1

    def test_reset(self):
        sp = self._make_staged_policy()
        agent = self._agent(severity=1.5)
        sp.evaluate(agent, round_idx=0)
        assert sp.current_stage == 1
        sp.reset()
        assert sp.current_stage == 0


class TestCooldownEnforcement:
    """Test cooldown between stage escalations."""

    def test_cooldown_blocks_immediate_escalation(self):
        sp = StagedPolicy(
            name="cooldown_test",
            stages=[
                PolicyStage(
                    level=1, trigger="x > 1.0",
                    actions=[AnnounceAction(text="L1")],
                    description="Stage 1",
                ),
                PolicyStage(
                    level=2, trigger="x > 2.0",
                    actions=[AnnounceAction(text="L2")],
                    description="Stage 2",
                ),
            ],
            cooldown_rounds=3,
        )
        agent = SimAgent(
            id="t", kind="trader", display_name="T",
            state={"x": 1.5},
        )

        # Stage 1 fires at round 0
        actions = sp.evaluate(agent, round_idx=0)
        assert sp.current_stage == 1

        # Try to escalate at round 1 — blocked by cooldown (3 rounds required)
        agent.set("x", 2.5)
        actions = sp.evaluate(agent, round_idx=1)
        assert actions is None
        assert sp.current_stage == 1

        # Try at round 2 — still blocked
        actions = sp.evaluate(agent, round_idx=2)
        assert actions is None

        # Round 3 — cooldown expired, should escalate
        actions = sp.evaluate(agent, round_idx=3)
        assert actions is not None
        assert sp.current_stage == 2

    def test_zero_cooldown_allows_immediate(self):
        sp = StagedPolicy(
            name="no_cooldown",
            stages=[
                PolicyStage(
                    level=1, trigger="x > 1.0",
                    actions=[AnnounceAction(text="L1")],
                    description="Stage 1",
                ),
                PolicyStage(
                    level=2, trigger="x > 2.0",
                    actions=[AnnounceAction(text="L2")],
                    description="Stage 2",
                ),
            ],
            cooldown_rounds=0,
        )
        agent = SimAgent(
            id="t", kind="trader", display_name="T",
            state={"x": 2.5},
        )
        sp.evaluate(agent, round_idx=0)
        assert sp.current_stage == 1
        # With cooldown=0, should escalate immediately
        actions = sp.evaluate(agent, round_idx=0)
        assert actions is not None
        assert sp.current_stage == 2


# ─────────────────────── A-Share Policy Template Tests ───────────────────────


class TestASharePolicyTemplates:
    """Test pre-built A-share specific policy templates."""

    def test_exchange_inquiry_structure(self):
        sp = create_exchange_inquiry_policy()
        assert sp.name == "exchange_inquiry"
        assert len(sp.stages) == 2
        assert sp.stages[0].level == 1
        assert sp.stages[1].level == 2
        # Stage 2 should include a MutateAction (conviction damper)
        stage2_actions = sp.stages[1].actions
        has_mutate = any(isinstance(a, MutateAction) for a in stage2_actions)
        assert has_mutate

    def test_exchange_inquiry_fires_on_rise(self):
        sp = create_exchange_inquiry_policy()
        agent = SimAgent(
            id="co", kind="company", display_name="公司",
            state={"cumulative_delta": 0.35},
        )
        actions = sp.evaluate(agent, round_idx=0)
        assert actions is not None
        assert sp.current_stage == 1
        assert any(isinstance(a, AnnounceAction) for a in actions)

    def test_short_sell_restriction_structure(self):
        sp = create_short_sell_restriction_policy()
        assert sp.name == "short_sell_restriction"
        assert len(sp.stages) == 2
        # Stage 2 should include MutateAction for short_sell_allowed
        stage2_actions = sp.stages[1].actions
        has_short_sell_block = any(
            isinstance(a, MutateAction) and "short_sell_allowed" in a.mutations
            for a in stage2_actions
        )
        assert has_short_sell_block

    def test_short_sell_restriction_fires(self):
        sp = create_short_sell_restriction_policy()
        agent = SimAgent(
            id="mkt", kind="market_env", display_name="Market",
            state={"limit_down_days": 2.0},
        )
        sp.evaluate(agent, round_idx=0)  # Stage 1
        assert sp.current_stage == 1
        actions = sp.evaluate(agent, round_idx=2)  # Stage 2
        assert actions is not None
        assert sp.current_stage == 2

    def test_national_team_etf_buy_staged(self):
        sp = create_national_team_etf_buy_policy()
        assert sp.name == "national_team_etf_buy"
        assert len(sp.stages) == 3

        agent = SimAgent(
            id="nt", kind="trader", display_name="National Team",
            state={"cumulative_delta": -0.04},
            persona_id="national_team_strategic",
        )

        # Stage 1: verbal support
        actions = sp.evaluate(agent, round_idx=0)
        assert actions is not None
        assert sp.current_stage == 1
        assert any(isinstance(a, AnnounceAction) for a in actions)
        assert not any(isinstance(a, TradeAction) for a in actions)

        # Stage 2: small buy
        agent.set("cumulative_delta", -0.06)
        actions = sp.evaluate(agent, round_idx=3)
        assert actions is not None
        assert sp.current_stage == 2
        trade_actions = [a for a in actions if isinstance(a, TradeAction)]
        assert len(trade_actions) == 1
        assert trade_actions[0].quantity_pct == 0.05

        # Stage 3: large buy
        agent.set("cumulative_delta", -0.10)
        actions = sp.evaluate(agent, round_idx=6)
        assert actions is not None
        assert sp.current_stage == 3
        trade_actions = [a for a in actions if isinstance(a, TradeAction)]
        assert len(trade_actions) == 1
        assert trade_actions[0].quantity_pct == 0.15

    def test_suspension_risk_structure(self):
        sp = create_suspension_risk_policy()
        assert sp.name == "suspension_risk"
        assert len(sp.stages) == 2
        # Stage 2 should set conviction_damper to 0 (halts trading)
        stage2_actions = sp.stages[1].actions
        has_halt = any(
            isinstance(a, MutateAction) and a.mutations.get("conviction_damper") == 0.0
            for a in stage2_actions
        )
        assert has_halt

    def test_suspension_risk_fires_with_compound_trigger(self):
        """Suspension risk uses AND compound trigger."""
        sp = create_suspension_risk_policy()
        agent = SimAgent(
            id="co", kind="company", display_name="公司",
            state={"st_flagged": 1.0, "consecutive_loss_quarters": 3.0},
        )
        actions = sp.evaluate(agent, round_idx=0)
        assert actions is not None
        assert sp.current_stage == 1

    def test_get_all_templates(self):
        templates = get_ashare_policy_templates()
        assert "exchange_inquiry" in templates
        assert "short_sell_restriction" in templates
        assert "national_team_etf_buy" in templates
        assert "suspension_risk" in templates
        assert len(templates) == 4


class TestEvaluateStagedPolicies:
    """Test the evaluate_staged_policies helper function."""

    def test_produces_policy_fires(self):
        sp = create_national_team_etf_buy_policy()
        agent = SimAgent(
            id="nt", kind="trader", display_name="National Team",
            state={"cumulative_delta": -0.04},
            persona_id="national_team_strategic",
        )
        fires = evaluate_staged_policies([(agent, sp)], round_idx=0)
        assert len(fires) >= 1
        assert fires[0].agent_id == "nt"
        assert fires[0].round_idx == 0

    def test_mutate_action_applied_immediately(self):
        sp = create_exchange_inquiry_policy()
        agent = SimAgent(
            id="co", kind="company", display_name="公司",
            state={"cumulative_delta": 0.55, "conviction_damper": 1.0},
        )
        # Fire stage 1 first
        sp.evaluate(agent, round_idx=0)
        # Fire stage 2 which has MutateAction
        fires = evaluate_staged_policies([(agent, sp)], round_idx=3)
        assert agent.get("conviction_damper") == 0.5

    def test_no_fires_when_not_triggered(self):
        sp = create_national_team_etf_buy_policy()
        agent = SimAgent(
            id="nt", kind="trader", display_name="NT",
            state={"cumulative_delta": -0.01},
        )
        fires = evaluate_staged_policies([(agent, sp)], round_idx=0)
        assert len(fires) == 0
