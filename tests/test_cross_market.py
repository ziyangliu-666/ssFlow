"""Tests for cross-market information sources (Fix 5)."""

import pytest

from ssflow.information.cross_market import (
    CrossMarketContext,
    CrossMarketDataPoint,
    cross_market_from_explicit,
    cross_market_to_external_events,
    extract_cross_market_from_event,
    should_persona_see_cross_market,
)


class TestExtractCrossMarket:
    def test_nvidia_with_percentage(self):
        ctx = extract_cross_market_from_event("NVIDIA -17% 隔夜暴跌")
        assert len(ctx.data_points) >= 1
        nvda = next(dp for dp in ctx.data_points if dp.ticker == "NVDA")
        assert nvda.change_pct == pytest.approx(-17.0)
        assert nvda.market == "us_equity"

    def test_nvidia_chinese(self):
        ctx = extract_cross_market_from_event("英伟达暴跌17%")
        assert any(dp.ticker == "NVDA" for dp in ctx.data_points)

    def test_sp500(self):
        ctx = extract_cross_market_from_event("S&P 500下跌2.3%")
        assert any(dp.ticker == "SPX" for dp in ctx.data_points)

    def test_generic_us_market(self):
        ctx = extract_cross_market_from_event("隔夜美股大跌导致恐慌")
        assert len(ctx.data_points) >= 1
        assert any(dp.ticker == "SPX" for dp in ctx.data_points)

    def test_oil_price(self):
        ctx = extract_cross_market_from_event("原油暴跌5%")
        assert any(dp.ticker == "CL" for dp in ctx.data_points)

    def test_hk_market(self):
        ctx = extract_cross_market_from_event("恒指跌3.5%")
        assert any(dp.ticker == "HSI" for dp in ctx.data_points)

    def test_no_cross_market(self):
        ctx = extract_cross_market_from_event("BYD发布Q1财报，营收超预期")
        assert len(ctx.data_points) == 0

    def test_multiple_references(self):
        ctx = extract_cross_market_from_event(
            "NVIDIA -17%, S&P 500 -2.3%, 恒指跌1.8%"
        )
        assert len(ctx.data_points) >= 3

    def test_sector_context_combined(self):
        ctx = extract_cross_market_from_event(
            "DeepSeek发布新模型",
            sector_context="NVIDIA -17% 受出口管制影响",
        )
        assert any(dp.ticker == "NVDA" for dp in ctx.data_points)


class TestCrossMarketContext:
    def test_summary_text_zh(self):
        ctx = CrossMarketContext(data_points=[
            CrossMarketDataPoint(
                market="us_equity", ticker="NVDA",
                display_name="NVIDIA", change_pct=-17.0,
                context="出口管制",
            ),
        ])
        text = ctx.summary_text_zh
        assert "NVIDIA" in text
        assert "-17.0%" in text
        assert "出口管制" in text

    def test_to_feed_text(self):
        ctx = CrossMarketContext(data_points=[
            CrossMarketDataPoint(
                market="us_equity", ticker="NVDA",
                display_name="NVIDIA", change_pct=-17.0,
            ),
        ])
        text = ctx.to_feed_text()
        assert "[外围市场]" in text
        assert "NVIDIA" in text

    def test_empty_context(self):
        ctx = CrossMarketContext()
        assert ctx.summary_text_zh == ""
        assert ctx.to_feed_text() == ""


class TestExplicitContext:
    def test_from_explicit(self):
        data = [
            {"market": "us_equity", "ticker": "NVDA",
             "change_pct": -17.0, "context": "export controls"},
        ]
        ctx = cross_market_from_explicit(data)
        assert len(ctx.data_points) == 1
        assert ctx.data_points[0].ticker == "NVDA"
        assert ctx.data_points[0].change_pct == -17.0


class TestToExternalEvents:
    def test_produces_round_0_event(self):
        ctx = CrossMarketContext(data_points=[
            CrossMarketDataPoint(
                market="us_equity", ticker="NVDA",
                display_name="NVIDIA", change_pct=-17.0,
            ),
        ])
        events = cross_market_to_external_events(ctx)
        assert len(events) == 1
        ev = events[0]
        assert ev.round_idx == 0
        assert ev.authority_weight == 0.90
        assert "NVIDIA" in ev.text

    def test_empty_produces_nothing(self):
        ctx = CrossMarketContext()
        events = cross_market_to_external_events(ctx)
        assert len(events) == 0


class TestPersonaVisibility:
    def test_institutional_sees(self):
        assert should_persona_see_cross_market("institutional")

    def test_strategic_sees(self):
        assert should_persona_see_cross_market("strategic")

    def test_analyst_sees(self):
        assert should_persona_see_cross_market("analyst")

    def test_retail_does_not_see(self):
        assert not should_persona_see_cross_market("retail")

    def test_news_wire_does_not_see(self):
        assert not should_persona_see_cross_market("news_wire")
