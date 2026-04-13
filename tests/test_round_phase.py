"""Tests for the round_phase module: ExecutionPlan, PhaseResult, classification."""

from __future__ import annotations

import pytest

from ssflow.round_phase import (
    FAST_AGENT_TYPES,
    INFO_ONLY_TYPES,
    SLOW_AGENT_TYPES,
    ExecutionPlan,
    PhaseResult,
    RoundPhase,
    classify_agent_phase,
    create_execution_plan,
)


# ── classify_agent_phase ────────────────────────────────────────────────


class TestClassifyAgentPhase:
    def test_retail_is_fast(self):
        assert classify_agent_phase("retail") == RoundPhase.FAST_REACT

    def test_kol_is_fast(self):
        assert classify_agent_phase("kol") == RoundPhase.FAST_REACT

    def test_quant_is_fast(self):
        assert classify_agent_phase("quant") == RoundPhase.FAST_REACT

    def test_media_is_fast(self):
        assert classify_agent_phase("media") == RoundPhase.FAST_REACT

    def test_news_wire_is_fast(self):
        assert classify_agent_phase("news_wire") == RoundPhase.FAST_REACT

    def test_institutional_is_slow(self):
        assert classify_agent_phase("institutional") == RoundPhase.SLOW_REACT

    def test_strategic_is_slow(self):
        assert classify_agent_phase("strategic") == RoundPhase.SLOW_REACT

    def test_analyst_is_slow(self):
        assert classify_agent_phase("analyst") == RoundPhase.SLOW_REACT

    def test_regulator_info_only_routes_to_fast(self):
        """INFO_ONLY types route to FAST so their posts are visible to slow agents."""
        assert classify_agent_phase("regulator") == RoundPhase.FAST_REACT

    def test_policy_info_only_routes_to_fast(self):
        assert classify_agent_phase("policy") == RoundPhase.FAST_REACT

    def test_company_ir_info_only_routes_to_fast(self):
        assert classify_agent_phase("company_ir") == RoundPhase.FAST_REACT

    def test_unknown_defaults_to_fast(self):
        assert classify_agent_phase("unknown_type") == RoundPhase.FAST_REACT

    def test_sets_are_disjoint(self):
        assert not (FAST_AGENT_TYPES & SLOW_AGENT_TYPES)
        assert not (FAST_AGENT_TYPES & INFO_ONLY_TYPES)
        assert not (SLOW_AGENT_TYPES & INFO_ONLY_TYPES)


# ── ExecutionPlan lifecycle ─────────────────────────────────────────────


class TestExecutionPlan:
    def _make_plan(self, **kwargs) -> ExecutionPlan:
        defaults = dict(
            persona_id="test_inst_01",
            side="buy",
            total_pct=0.20,
            n_rounds=4,
            round_idx=0,
            current_price=100.0,
            rationale="test plan",
        )
        defaults.update(kwargs)
        return create_execution_plan(**defaults)

    def test_creation_basics(self):
        plan = self._make_plan()
        assert plan.total_pct == 0.20
        assert plan.remaining_pct == 0.20
        assert plan.per_round_slice == pytest.approx(0.05)
        assert plan.n_rounds_total == 4
        assert plan.n_rounds_remaining == 4
        assert not plan.is_complete

    def test_next_slice(self):
        plan = self._make_plan()
        assert plan.next_slice() == pytest.approx(0.05)

    def test_advance(self):
        plan = self._make_plan()
        s = plan.next_slice()
        plan.advance(s)
        assert plan.remaining_pct == pytest.approx(0.15)
        assert plan.n_rounds_remaining == 3

    def test_full_execution(self):
        plan = self._make_plan()
        for _ in range(4):
            assert not plan.is_complete
            s = plan.next_slice()
            plan.advance(s)
        assert plan.is_complete
        assert plan.remaining_pct <= 0.001
        assert plan.next_slice() == 0.0

    def test_slice_number(self):
        plan = self._make_plan()
        assert plan.slice_number() == 1
        plan.advance(plan.next_slice())
        assert plan.slice_number() == 2

    def test_should_cancel_buy_adverse_up(self):
        plan = self._make_plan(
            side="buy",
            cancel_conditions={"max_adverse_move_pct": 0.05},
        )
        # Price up 6% — adverse for a buy plan
        assert plan.should_cancel(106.0)

    def test_should_not_cancel_buy_within_threshold(self):
        plan = self._make_plan(
            side="buy",
            cancel_conditions={"max_adverse_move_pct": 0.05},
        )
        assert not plan.should_cancel(104.0)

    def test_should_cancel_sell_adverse_down(self):
        plan = self._make_plan(
            side="sell",
            cancel_conditions={"max_adverse_move_pct": 0.05},
        )
        # Price down 6% — adverse for a sell plan
        assert plan.should_cancel(94.0)

    def test_no_cancel_without_conditions(self):
        plan = self._make_plan()
        # No cancel_conditions → never cancels
        assert not plan.should_cancel(200.0)
        assert not plan.should_cancel(50.0)

    def test_last_slice_uses_remaining(self):
        """If remaining_pct < per_round_slice, next_slice returns remaining."""
        plan = self._make_plan(total_pct=0.10, n_rounds=3)
        # per_round_slice ≈ 0.0333
        plan.advance(0.04)  # over-execute slightly
        plan.advance(0.04)
        # remaining ≈ 0.02, less than per_round_slice
        assert plan.next_slice() == pytest.approx(plan.remaining_pct)


# ── PhaseResult ─────────────────────────────────────────────────────────


class TestPhaseResult:
    def test_to_serializable(self):
        pr = PhaseResult(
            phase=RoundPhase.FAST_REACT,
            class_flows=[],
            net_flow=1e8,
            price_before=100.0,
            price_after=101.5,
            delta_pct=0.015,
            n_active_personas=5,
            n_plan_executions=1,
        )
        d = pr.to_serializable()
        assert d["phase"] == "fast_react"
        assert d["delta_pct"] == 0.015
        assert d["n_active_personas"] == 5


# ── RoundPhase enum ─────────────────────────────────────────────────────


class TestRoundPhase:
    def test_values(self):
        assert RoundPhase.INFO.value == "info"
        assert RoundPhase.FAST_REACT.value == "fast_react"
        assert RoundPhase.SLOW_REACT.value == "slow_react"
        assert RoundPhase.SETTLE.value == "settle"

    def test_is_str_enum(self):
        assert isinstance(RoundPhase.FAST_REACT, str)
        assert RoundPhase.FAST_REACT == "fast_react"
