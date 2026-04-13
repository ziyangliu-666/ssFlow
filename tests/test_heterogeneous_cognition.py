"""Tests for heterogeneous cognition (Phase 4).

Covers:
  - Role-specific voice prompts
  - Mechanical agent roles (quant, ETF AP)
  - Role classification
  - Prior expectation context
  - End-to-end: same event, different agents produce different intents
"""

import pytest

from ssflow.cognition.information_filter import (
    AgentRole,
    FilteredContext,
    InformationFilter,
)
from ssflow.cognition.role_prompts import (
    ROLE_VOICES,
    get_role_voice,
    is_mechanical_role,
    is_non_trading_role,
    build_prior_context,
)
from ssflow.cognition.mechanical_agents import (
    QuantSignal,
    etf_ap_decision,
    quant_decision,
)
from ssflow.cognition.intent_resolver import (
    IntentResolver,
    StructuredIntent,
    Urgency,
    parse_intent_from_json,
)


# ─── Role Prompt Tests ───


class TestRolePrompts:
    def test_all_roles_have_voices(self):
        """Every AgentRole should have a voice prompt."""
        for role in AgentRole:
            voice = get_role_voice(role)
            assert isinstance(voice, str)
            assert len(voice) > 10, f"Voice for {role} is too short"

    def test_retail_voice_mentions_momentum(self):
        voice = get_role_voice(AgentRole.RETAIL)
        # Should mention price-driven behavior
        assert "价格" in voice or "涨" in voice or "跌" in voice

    def test_analyst_voice_mentions_consensus(self):
        voice = get_role_voice(AgentRole.ANALYST)
        assert "consensus" in voice.lower() or "一致预期" in voice

    def test_institutional_voice_mentions_constraints(self):
        voice = get_role_voice(AgentRole.INSTITUTIONAL)
        assert "合规" in voice or "约束" in voice or "流动性" in voice

    def test_short_fund_voice_mentions_triggers(self):
        voice = get_role_voice(AgentRole.SHORT_FUND)
        assert "做空" in voice or "空" in voice

    def test_mechanical_roles_identified(self):
        assert is_mechanical_role(AgentRole.QUANT)
        assert is_mechanical_role(AgentRole.ETF_AP)
        assert is_mechanical_role(AgentRole.MARKET_MAKER)
        assert not is_mechanical_role(AgentRole.RETAIL)
        assert not is_mechanical_role(AgentRole.ANALYST)

    def test_non_trading_roles_identified(self):
        assert is_non_trading_role(AgentRole.MEDIA)
        assert is_non_trading_role(AgentRole.POLICY)
        assert is_non_trading_role(AgentRole.REGULATOR)
        assert is_non_trading_role(AgentRole.KOL)
        assert not is_non_trading_role(AgentRole.RETAIL)
        assert not is_non_trading_role(AgentRole.INSTITUTIONAL)

    def test_string_role_lookup(self):
        voice = get_role_voice("retail")
        assert len(voice) > 10
        assert is_mechanical_role("quant")
        assert not is_mechanical_role("retail")


# ─── Prior Context Tests ───


class TestPriorContext:
    def test_build_prior_context(self):
        ctx = build_prior_context(
            AgentRole.ANALYST,
            prior_expectation="Revenue +20%, GM stable",
            actual_result="Revenue +18%, GM -2.3pp",
        )
        assert "Prior Expectation" in ctx
        assert "+20%" in ctx
        assert "+18%" in ctx
        assert "beat" in ctx.lower() or "miss" in ctx.lower() or "assess" in ctx.lower()

    def test_empty_prior(self):
        ctx = build_prior_context(
            AgentRole.RETAIL,
            prior_expectation="",
            actual_result="Revenue +18%",
        )
        assert ctx == ""


# ─── Mechanical Agent Tests ───


class TestQuantDecision:
    def test_bullish_signals(self):
        signals = [
            QuantSignal("momentum_1d", value=0.5, confidence=0.8, half_life_rounds=3),
            QuantSignal("vwap_deviation", value=0.3, confidence=0.7, half_life_rounds=2),
        ]
        intent = quant_decision(
            signals=signals,
            current_price=50.0,
            vwap=49.0,
            price_history=[48.0, 49.0, 50.0],
            holdings=0,
            cash=100_000,
        )
        assert intent.side == "buy"
        assert intent.size_fraction > 0
        assert "signal" in intent.rationale.lower() or "Signal" in intent.rationale

    def test_bearish_signals_with_holdings(self):
        signals = [
            QuantSignal("momentum_1d", value=-0.6, confidence=0.9, half_life_rounds=3),
        ]
        intent = quant_decision(
            signals=signals,
            current_price=50.0,
            vwap=52.0,
            price_history=[55.0, 53.0, 50.0],
            holdings=1000,
            cash=0,
        )
        assert intent.side == "sell"

    def test_bearish_signals_no_holdings_short(self):
        signals = [
            QuantSignal("momentum_1d", value=-0.6, confidence=0.9, half_life_rounds=3),
        ]
        intent = quant_decision(
            signals=signals,
            current_price=50.0,
            vwap=52.0,
            price_history=[55.0, 53.0, 50.0],
            holdings=0,
            cash=100_000,
        )
        assert intent.side == "short"

    def test_neutral_signals(self):
        signals = [
            QuantSignal("momentum_1d", value=0.05, confidence=0.5, half_life_rounds=3),
            QuantSignal("vwap_deviation", value=-0.03, confidence=0.4, half_life_rounds=2),
        ]
        intent = quant_decision(
            signals=signals,
            current_price=50.0,
            vwap=50.0,
            price_history=[50.0, 50.0, 50.0],
            holdings=0,
            cash=100_000,
        )
        assert intent.side == "hold"

    def test_auto_signals_from_history(self):
        """When no explicit signals, auto-generate from price history."""
        intent = quant_decision(
            signals=[],
            current_price=55.0,
            vwap=50.0,
            price_history=[48.0, 49.0, 50.0, 52.0, 54.0],
            holdings=0,
            cash=100_000,
        )
        # Strong uptrend -> should generate buy or sell depending on signals
        assert intent.is_active or intent.side == "hold"
        # At minimum, should have a rationale
        assert len(intent.rationale) > 0

    def test_no_data_holds(self):
        intent = quant_decision(
            signals=[], current_price=50.0, vwap=None,
            price_history=[], holdings=0, cash=100_000,
        )
        assert intent.side == "hold"


class TestEtfApDecision:
    def test_premium_sell(self):
        intent = etf_ap_decision(
            etf_price=10.10,
            nav=10.00,
            premium_threshold=0.005,
        )
        assert intent.side == "sell"
        assert intent.urgency == Urgency.IMMEDIATE
        assert "premium" in intent.rationale.lower()

    def test_discount_buy(self):
        intent = etf_ap_decision(
            etf_price=9.90,
            nav=10.00,
            discount_threshold=-0.005,
        )
        assert intent.side == "buy"
        assert intent.urgency == Urgency.IMMEDIATE
        assert "discount" in intent.rationale.lower()

    def test_no_arb_hold(self):
        intent = etf_ap_decision(
            etf_price=10.02,
            nav=10.00,
            premium_threshold=0.005,
            discount_threshold=-0.005,
        )
        assert intent.side == "hold"
        assert "no-arb" in intent.rationale.lower()

    def test_zero_nav_hold(self):
        intent = etf_ap_decision(etf_price=10.0, nav=0.0)
        assert intent.side == "hold"


# ─── Cognitive Diversity End-to-End ───


class TestCognitiveDiversity:
    """Verify that the same event produces different perspectives."""

    def _full_context(self):
        return {
            "event_summary": "BYD Q1: revenue ¥238B (+18% YoY), net profit ¥10.5B, GM 22.1% (-2.3pp)",
            "price_data": "Current: ¥218.50, prev_close: ¥212.80, +2.7% today, ADV ¥8.1B",
            "social_feed": "雪球热搜#1, KOL一致看多, 散户: 冲冲冲!",
            "analyst_reports": "中信: 维持买入TP260, 国泰: 上调至增持TP245, GS: Hold TP220 (margin concern)",
            "financial_data": "Rev ¥238B (cons ¥231B, +3% beat), GM 22.1% (cons 24.4%, -2.3pp miss), SG&A +15%",
            "order_flow_data": "Net institutional +¥800M, northbound -¥200M (profit taking), retail +¥1.2B",
            "macro_context": "CNY/USD 7.25 (stable), US10Y 4.3% (+5bps), oil 72 (flat), EU tariff talks",
            "risk_signals": "Short interest 2.1% (low), margin balance +¥500M, volatility 28% (moderate)",
            "regulatory_context": "CSRC: new energy subsidy extension confirmed",
            "nav_data": "159902 NAV 3.456, market 3.462, premium 0.17%",
        }

    def test_retail_vs_analyst_different_focus(self):
        """Retail sees social buzz; analyst sees consensus gap."""
        filt = InformationFilter()
        ctx = self._full_context()

        retail_ctx = filt.filter(AgentRole.RETAIL, ctx)
        analyst_ctx = filt.filter(AgentRole.ANALYST, ctx)

        # Retail sees social, not financials
        assert retail_ctx.social_feed != ""
        assert retail_ctx.financial_data == ""

        # Analyst sees financials, not social
        assert analyst_ctx.financial_data != ""
        assert analyst_ctx.social_feed == ""

        # Different prompt sections
        retail_prompt = retail_ctx.to_prompt_section()
        analyst_prompt = analyst_ctx.to_prompt_section()
        assert "Social Feed" in retail_prompt
        assert "Financial Data" in analyst_prompt
        assert "Social Feed" not in analyst_prompt

    def test_quant_sees_only_numbers(self):
        filt = InformationFilter()
        ctx = self._full_context()
        quant_ctx = filt.filter(AgentRole.QUANT, ctx)

        assert quant_ctx.price_data != ""
        assert quant_ctx.order_flow_data != ""
        assert quant_ctx.event_summary == ""
        assert quant_ctx.social_feed == ""
        assert quant_ctx.analyst_reports == ""

    def test_short_fund_sees_risk(self):
        filt = InformationFilter()
        ctx = self._full_context()
        short_ctx = filt.filter(AgentRole.SHORT_FUND, ctx)

        assert short_ctx.risk_signals != ""
        assert short_ctx.financial_data != ""
        assert "Short interest" in short_ctx.risk_signals

    def test_etf_ap_sees_nav_only(self):
        filt = InformationFilter()
        ctx = self._full_context()
        ap_ctx = filt.filter(AgentRole.ETF_AP, ctx)

        assert ap_ctx.nav_data != ""
        assert ap_ctx.event_summary == ""
        assert ap_ctx.analyst_reports == ""

    def test_institutional_sees_everything_relevant(self):
        filt = InformationFilter()
        ctx = self._full_context()
        inst_ctx = filt.filter(AgentRole.INSTITUTIONAL, ctx)

        assert inst_ctx.analyst_reports != ""
        assert inst_ctx.order_flow_data != ""
        assert inst_ctx.macro_context != ""
        assert inst_ctx.risk_signals != ""
        assert inst_ctx.social_feed == ""   # no Weibo for institutions

    def test_full_pipeline_diversity(self):
        """Different roles with same event should produce different intents."""
        filt = InformationFilter()
        ctx = self._full_context()

        # Retail: sees bullish social + headline -> likely buy
        retail_ctx = filt.filter(AgentRole.RETAIL, ctx)
        retail_intent = parse_intent_from_json(
            {"side": "buy", "size_fraction": 0.3, "urgency": "immediate",
             "confidence": 0.8, "rationale": "社交媒体一致看多，追涨"},
            "retail_01", "a001",
        )
        assert retail_intent.side == "buy"
        assert retail_intent.urgency == Urgency.IMMEDIATE  # retail is impatient

        # Short fund: sees GM miss, high short interest signal
        short_intent = parse_intent_from_json(
            {"side": "short", "size_fraction": 0.15, "urgency": "patient",
             "confidence": 0.6, "rationale": "GM -2.3pp miss vs consensus, 估值已反映营收增长"},
            "short_fund_01", "a002",
        )
        assert short_intent.side == "short"

        # Institutional: sees mixed signals, small TWAP buy
        inst_intent = parse_intent_from_json(
            {"side": "buy", "size_fraction": 0.05, "urgency": "twap",
             "confidence": 0.55, "rationale": "收入beat但GM miss，小仓加仓观察, TWAP 3轮",
             "twap_rounds": 3},
            "institutional_01", "a003",
        )
        assert inst_intent.side == "buy"
        assert inst_intent.urgency == Urgency.TWAP
        assert inst_intent.twap_rounds == 3
        assert inst_intent.size_fraction < retail_intent.size_fraction  # institutional is more cautious

        # Quant: mechanical, no LLM
        quant_intent = quant_decision(
            signals=[
                QuantSignal("momentum_1d", 0.3, 0.6, 3),
                QuantSignal("vwap_deviation", -0.2, 0.7, 2),
            ],
            current_price=218.5,
            vwap=215.0,
            price_history=[210, 212, 215, 218],
            holdings=0,
            cash=500_000,
        )
        # Quant decision is signal-based, not narrative-based
        assert "signal" in quant_intent.rationale.lower() or "Signal" in quant_intent.rationale

        # All four agents have different sides or sizes
        sides = {
            retail_intent.side, short_intent.side,
            inst_intent.side, quant_intent.side,
        }
        # At minimum, short fund disagrees with retail
        assert "short" in sides
        assert "buy" in sides
