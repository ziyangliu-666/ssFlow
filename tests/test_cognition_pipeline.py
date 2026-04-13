"""Tests for the cognition pipeline (Phase 1).

Covers:
  - Intent parsing from JSON and free text
  - Constraint solver: cash, holdings, T+1, position limits, margin
  - Order generator: market, limit, TWAP orders
  - Information filter: role-based access rules
  - Agent memory: fill recording, P&L tracking, portfolio prompts
"""

import pytest

from ssflow.cognition.intent_resolver import (
    IntentResolver,
    StructuredIntent,
    Urgency,
    parse_intent_from_json,
    parse_intent_from_text,
)
from ssflow.cognition.constraint_solver import (
    ConstraintSolver,
    ValidatedIntent,
    RejectedIntent,
)
from ssflow.cognition.order_generator import OrderGenerator, OrderSpec
from ssflow.cognition.information_filter import (
    AgentRole,
    FilteredContext,
    InformationFilter,
)
from ssflow.cognition.agent_memory import AgentMemory, FillRecord


# ─── Intent Resolver Tests ───


class TestIntentResolver:
    def test_parse_json_buy(self):
        raw = {
            "side": "buy",
            "size_fraction": 0.3,
            "urgency": "patient",
            "price_preference": -0.005,
            "rationale": "Earnings beat expectations",
            "confidence": 0.8,
        }
        intent = parse_intent_from_json(raw, "retail_01", "a001")
        assert intent.side == "buy"
        assert intent.size_fraction == pytest.approx(0.3)
        assert intent.urgency == Urgency.PATIENT
        assert intent.confidence == pytest.approx(0.8)
        assert intent.is_active

    def test_parse_json_hold(self):
        raw = {"side": "hold"}
        intent = parse_intent_from_json(raw, "inst_01", "a002")
        assert intent.side == "hold"
        assert intent.size_fraction == 0.0
        assert intent.urgency == Urgency.NONE
        assert not intent.is_active

    def test_parse_json_clamps(self):
        raw = {
            "side": "sell",
            "size_fraction": 2.5,       # should be clamped to 1.0
            "confidence": -0.5,          # should be clamped to 0.0
            "price_preference": 0.1,     # should be clamped to 0.05
        }
        intent = parse_intent_from_json(raw, "p", "a")
        assert intent.size_fraction == 1.0
        assert intent.confidence == 0.0
        assert intent.price_preference == 0.05

    def test_parse_text_buy_chinese(self):
        text = '看好后市，准备买入30%仓位'
        intent = parse_intent_from_text(text, "retail", "a1")
        assert intent.side == "buy"
        assert intent.size_fraction == pytest.approx(0.3)

    def test_parse_text_sell_english(self):
        text = "Time to sell and lock profits"
        intent = parse_intent_from_text(text, "inst", "a2")
        assert intent.side == "sell"

    def test_parse_text_with_json(self):
        text = 'My analysis: ```json\n{"side": "buy", "size_fraction": 0.5, "confidence": 0.9}\n```'
        intent = parse_intent_from_text(text, "analyst", "a3")
        assert intent.side == "buy"
        assert intent.size_fraction == pytest.approx(0.5)

    def test_parse_text_hold(self):
        text = "Market is uncertain, I will observe and wait"
        intent = parse_intent_from_text(text, "p", "a")
        assert intent.side == "hold"

    def test_resolver_build_prompt(self):
        resolver = IntentResolver()
        msgs = resolver.build_prompt(
            persona_voice="You are a retail investor...",
            market_context="BYD Q1 earnings beat",
            portfolio_state="Cash: 50,000",
            information_summary="Revenue +18%",
        )
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"
        assert "Revenue +18%" in msgs[1]["content"]

    def test_resolver_hold_intent(self):
        resolver = IntentResolver()
        intent = resolver.hold_intent("p1", "a1", "Too uncertain")
        assert intent.side == "hold"
        assert not intent.is_active


# ─── Constraint Solver Tests ───


class TestConstraintSolver:
    def _buy_intent(self, size=0.3, urgency=Urgency.PATIENT):
        return StructuredIntent(
            persona_id="p1", agent_id="a1", side="buy",
            size_fraction=size, urgency=urgency, confidence=0.7,
        )

    def _sell_intent(self, size=0.5):
        return StructuredIntent(
            persona_id="p1", agent_id="a1", side="sell",
            size_fraction=size, urgency=Urgency.PATIENT, confidence=0.6,
        )

    def _short_intent(self, size=0.2):
        return StructuredIntent(
            persona_id="p1", agent_id="a1", side="short",
            size_fraction=size, urgency=Urgency.IMMEDIATE, confidence=0.5,
        )

    def test_buy_validated(self):
        solver = ConstraintSolver()
        result = solver.validate(
            intent=self._buy_intent(0.3),
            cash=100_000,
            holdings=0,
            t1_locked=0,
            last_price=50.0,
            upper_limit=55.0,
            lower_limit=45.0,
            capital=200_000,
        )
        assert isinstance(result, ValidatedIntent)
        assert result.effective_shares > 0
        assert result.effective_value <= 100_000 * 0.3 * 1.01  # within tolerance
        assert result.pool == "cash"

    def test_buy_rejected_no_cash(self):
        solver = ConstraintSolver()
        result = solver.validate(
            intent=self._buy_intent(),
            cash=0,
            holdings=0,
            t1_locked=0,
            last_price=50.0,
            upper_limit=55.0,
            lower_limit=45.0,
        )
        assert isinstance(result, RejectedIntent)
        assert result.code == "NO_CASH"

    def test_buy_rejected_position_limit(self):
        solver = ConstraintSolver(max_position_pct=0.50)
        result = solver.validate(
            intent=self._buy_intent(),
            cash=100_000,
            holdings=0,
            t1_locked=0,
            last_price=50.0,
            upper_limit=55.0,
            lower_limit=45.0,
            capital=100_000,
            holdings_value=60_000,  # already 60% in, limit is 50%
        )
        assert isinstance(result, RejectedIntent)
        assert result.code == "POSITION_LIMIT"

    def test_sell_validated(self):
        solver = ConstraintSolver()
        result = solver.validate(
            intent=self._sell_intent(0.5),
            cash=0,
            holdings=1000,
            t1_locked=0,
            last_price=50.0,
            upper_limit=55.0,
            lower_limit=45.0,
        )
        assert isinstance(result, ValidatedIntent)
        assert result.effective_shares == 500  # 50% of 1000
        assert result.pool == "holdings"

    def test_sell_rejected_t1_locked(self):
        solver = ConstraintSolver()
        result = solver.validate(
            intent=self._sell_intent(),
            cash=0,
            holdings=500,
            t1_locked=500,       # all locked by T+1
            last_price=50.0,
            upper_limit=55.0,
            lower_limit=45.0,
        )
        assert isinstance(result, RejectedIntent)
        assert result.code == "NO_SELLABLE"

    def test_sell_partial_t1(self):
        solver = ConstraintSolver()
        result = solver.validate(
            intent=self._sell_intent(1.0),  # sell all
            cash=0,
            holdings=1000,
            t1_locked=300,       # 300 locked
            last_price=50.0,
            upper_limit=55.0,
            lower_limit=45.0,
        )
        assert isinstance(result, ValidatedIntent)
        assert result.effective_shares == 700  # can only sell 700

    def test_short_validated(self):
        solver = ConstraintSolver()
        result = solver.validate(
            intent=self._short_intent(0.2),
            cash=100_000,
            holdings=0,
            t1_locked=0,
            last_price=50.0,
            upper_limit=55.0,
            lower_limit=45.0,
            capital=200_000,
        )
        assert isinstance(result, ValidatedIntent)
        assert result.pool == "margin"

    def test_hold_passthrough(self):
        solver = ConstraintSolver()
        hold = StructuredIntent(
            persona_id="p", agent_id="a", side="hold",
            size_fraction=0.0, urgency=Urgency.NONE,
        )
        result = solver.validate(
            intent=hold, cash=0, holdings=0, t1_locked=0,
            last_price=50.0, upper_limit=55.0, lower_limit=45.0,
        )
        assert isinstance(result, ValidatedIntent)
        assert result.effective_shares == 0.0

    def test_buy_lot_rounding(self):
        solver = ConstraintSolver()
        result = solver.validate(
            intent=self._buy_intent(0.5),
            cash=5_000,           # 5000 cash
            holdings=0,
            t1_locked=0,
            last_price=45.0,      # at 45/share, 5000*0.5/45 = ~55 shares -> rounds to 0 lots
            upper_limit=49.50,
            lower_limit=40.50,
        )
        # 2500 / 45 = ~55 shares -> rounds down to 0 lots -> rejected
        assert isinstance(result, RejectedIntent)
        assert result.code == "ZERO_SHARES"


# ─── Order Generator Tests ───


class TestOrderGenerator:
    def _make_validated(self, side="buy", shares=500, price=50.0, urgency=Urgency.PATIENT, twap=1):
        intent = StructuredIntent(
            persona_id="p1", agent_id="a1", side=side,
            size_fraction=0.3, urgency=urgency, twap_rounds=twap,
        )
        return ValidatedIntent(
            intent=intent,
            effective_shares=shares,
            effective_value=shares * price,
            estimated_price=price,
            pool="cash" if side == "buy" else "holdings",
        )

    def test_patient_buy_limit_order(self):
        gen = OrderGenerator(tick_offset=1)
        validated = self._make_validated(side="buy", shares=500, price=50.0)
        specs = gen.generate(validated, last_price=50.0, best_bid=49.90, best_ask=50.10)
        assert len(specs) == 1
        assert specs[0].order_type == "limit"
        assert specs[0].side == "buy"
        assert specs[0].price == pytest.approx(50.09)  # best_ask - 1 tick
        assert specs[0].quantity == 500

    def test_immediate_sell_market_order(self):
        gen = OrderGenerator()
        validated = self._make_validated(side="sell", shares=300, urgency=Urgency.IMMEDIATE)
        specs = gen.generate(validated, last_price=50.0)
        assert len(specs) == 1
        assert specs[0].order_type == "market"
        assert specs[0].side == "sell"
        assert specs[0].quantity == 300

    def test_twap_slicing(self):
        gen = OrderGenerator()
        validated = self._make_validated(
            side="buy", shares=1000, urgency=Urgency.TWAP, twap=4
        )
        specs = gen.generate(validated, last_price=50.0, best_ask=50.10, current_twap_round=0)
        assert len(specs) == 1
        assert specs[0].quantity == 200  # 1000/4 = 250, rounded to 200 (2 lots)
        assert specs[0].twap_round == 0
        assert specs[0].twap_total_rounds == 4

    def test_short_produces_sell(self):
        gen = OrderGenerator()
        intent = StructuredIntent(
            persona_id="p1", agent_id="a1", side="short",
            size_fraction=0.2, urgency=Urgency.PATIENT,
        )
        validated = ValidatedIntent(
            intent=intent, effective_shares=200, effective_value=10000,
            estimated_price=50.0, pool="margin",
        )
        specs = gen.generate(validated, last_price=50.0, best_bid=49.90)
        assert len(specs) == 1
        assert specs[0].side == "sell"
        assert specs[0].is_short


# ─── Information Filter Tests ───


class TestInformationFilter:
    def _full_context(self):
        return {
            "event_summary": "BYD Q1 revenue +18%",
            "price_data": "Current: 218.50, +2.3% today",
            "social_feed": "KOL: BYD strong, 散户: all-in!",
            "analyst_reports": "CS: Buy target 250, GS: Hold target 220",
            "financial_data": "Revenue 238B, GM 22.1%, EPS 4.83",
            "order_flow_data": "Net buy 800M, institutional ratio 45%",
            "macro_context": "CNY/USD 7.25, US10Y 4.3%, oil 72",
            "risk_signals": "Short interest 3.2%, borrow cost 2.5%",
            "regulatory_context": "CSRC: monitoring transparency",
            "nav_data": "ETF NAV 3.456, premium 0.2%",
        }

    def test_retail_sees_limited_info(self):
        filt = InformationFilter()
        ctx = filt.filter(AgentRole.RETAIL, self._full_context())
        assert ctx.event_summary != ""
        assert ctx.price_data != ""
        assert ctx.social_feed != ""
        assert ctx.analyst_reports == ""     # no access
        assert ctx.financial_data == ""      # no access
        assert ctx.order_flow_data == ""     # no access
        assert ctx.macro_context == ""       # no access

    def test_analyst_sees_financials(self):
        filt = InformationFilter()
        ctx = filt.filter(AgentRole.ANALYST, self._full_context())
        assert ctx.financial_data != ""      # has access
        assert ctx.analyst_reports != ""     # peer research
        assert ctx.social_feed == ""         # no Weibo
        assert ctx.macro_context != ""       # yes

    def test_quant_only_sees_price(self):
        filt = InformationFilter()
        ctx = filt.filter(AgentRole.QUANT, self._full_context())
        assert ctx.price_data != ""
        assert ctx.order_flow_data != ""
        assert ctx.event_summary == ""       # no narrative
        assert ctx.social_feed == ""
        assert ctx.analyst_reports == ""

    def test_institutional_full_access(self):
        filt = InformationFilter()
        ctx = filt.filter(AgentRole.INSTITUTIONAL, self._full_context())
        assert ctx.analyst_reports != ""
        assert ctx.order_flow_data != ""
        assert ctx.macro_context != ""
        assert ctx.risk_signals != ""

    def test_etf_ap_only_nav(self):
        filt = InformationFilter()
        ctx = filt.filter(AgentRole.ETF_AP, self._full_context())
        assert ctx.nav_data != ""
        assert ctx.price_data != ""
        assert ctx.event_summary == ""
        assert ctx.analyst_reports == ""

    def test_short_fund_sees_risk(self):
        filt = InformationFilter()
        ctx = filt.filter(AgentRole.SHORT_FUND, self._full_context())
        assert ctx.risk_signals != ""
        assert ctx.financial_data != ""

    def test_classify_role(self):
        filt = InformationFilter()
        assert filt.classify_role("retail") == AgentRole.RETAIL
        assert filt.classify_role("quant") == AgentRole.QUANT
        assert filt.classify_role("unknown") == AgentRole.RETAIL

    def test_to_prompt_section(self):
        filt = InformationFilter()
        ctx = filt.filter(AgentRole.RETAIL, self._full_context())
        prompt = ctx.to_prompt_section()
        assert "Event" in prompt
        assert "Price Data" in prompt
        assert "Social Feed" in prompt
        assert "Analyst Research" not in prompt  # filtered out


# ─── Agent Memory Tests ───


class TestAgentMemory:
    def test_record_buy(self):
        mem = AgentMemory(agent_id="a1", persona_id="p1", current_cash=100_000, initial_cash=100_000)
        mem.record_fill(FillRecord(round_idx=0, tick=5, side="buy", price=50.0, quantity=100, value=5000))
        assert mem.holdings["_default"] == 100
        assert mem.avg_cost["_default"] == pytest.approx(50.0)
        assert mem.current_cash == pytest.approx(95_000)
        assert mem.n_trades == 1

    def test_record_sell_pnl(self):
        mem = AgentMemory(agent_id="a1", persona_id="p1", current_cash=95_000, initial_cash=100_000)
        mem.holdings["_default"] = 100
        mem.avg_cost["_default"] = 50.0
        mem.record_fill(FillRecord(round_idx=1, tick=20, side="sell", price=55.0, quantity=50, value=2750))
        assert mem.holdings["_default"] == 50
        assert mem.realized_pnl == pytest.approx(250)  # (55-50)*50
        assert mem.current_cash == pytest.approx(97_750)
        assert mem.n_wins == 1

    def test_unrealized_pnl(self):
        mem = AgentMemory(agent_id="a1", persona_id="p1")
        mem.holdings["_default"] = 200
        mem.avg_cost["_default"] = 50.0
        pnl = mem.unrealized_pnl({"_default": 55.0})
        assert pnl == pytest.approx(1000)  # (55-50)*200

    def test_nav(self):
        mem = AgentMemory(agent_id="a1", persona_id="p1", current_cash=50_000)
        mem.holdings["_default"] = 100
        nav = mem.nav({"_default": 50.0})
        assert nav == pytest.approx(55_000)  # 50000 + 100*50

    def test_portfolio_prompt(self):
        mem = AgentMemory(agent_id="a1", persona_id="p1", current_cash=50_000)
        mem.holdings["_default"] = 100
        mem.avg_cost["_default"] = 45.0
        mem.realized_pnl = 500
        mem.n_trades = 3
        mem.n_wins = 2
        prompt = mem.to_portfolio_prompt(current_price=50.0)
        assert "50,000" in prompt
        assert "100" in prompt
        assert "+11.1%" in prompt  # (50/45-1)*100
        assert "67%" in prompt     # win rate 2/3

    def test_losing_streak(self):
        mem = AgentMemory(agent_id="a1", persona_id="p1", current_cash=50_000)
        mem.holdings["_default"] = 200
        mem.avg_cost["_default"] = 50.0
        # Two losing sells
        mem.record_fill(FillRecord(0, 1, "sell", 48.0, 50, 2400))
        mem.record_fill(FillRecord(1, 2, "sell", 47.0, 50, 2350))
        assert mem.current_streak == -2
        prompt = mem.to_portfolio_prompt(current_price=47.0)
        assert "losing" in prompt


# ─── End-to-End Pipeline Test ───


class TestCognitionPipeline:
    def test_buy_pipeline(self):
        """Full pipeline: intent -> constraint -> order."""
        # Parse intent
        raw = {
            "side": "buy",
            "size_fraction": 0.3,
            "urgency": "patient",
            "confidence": 0.8,
            "rationale": "Strong earnings beat",
        }
        intent = parse_intent_from_json(raw, "retail_01", "a001")
        assert intent.is_active

        # Validate
        solver = ConstraintSolver()
        validated = solver.validate(
            intent=intent,
            cash=100_000,
            holdings=0,
            t1_locked=0,
            last_price=50.0,
            upper_limit=55.0,
            lower_limit=45.0,
            capital=200_000,
        )
        assert isinstance(validated, ValidatedIntent)
        assert validated.effective_shares > 0

        # Generate order
        gen = OrderGenerator()
        specs = gen.generate(validated, last_price=50.0, best_ask=50.10)
        assert len(specs) == 1
        assert specs[0].side == "buy"
        assert specs[0].order_type == "limit"
        assert specs[0].quantity > 0

    def test_reject_pipeline(self):
        """Pipeline rejects when agent can't trade."""
        intent = parse_intent_from_json(
            {"side": "sell", "size_fraction": 1.0}, "p", "a"
        )
        solver = ConstraintSolver()
        result = solver.validate(
            intent=intent, cash=0, holdings=0, t1_locked=0,
            last_price=50.0, upper_limit=55.0, lower_limit=45.0,
        )
        assert isinstance(result, RejectedIntent)
        # No order generated
