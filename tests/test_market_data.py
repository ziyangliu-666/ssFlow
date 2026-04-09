"""Tests for ssflow.market_data.

Unit tests mock the upstream HTTP so they run offline. A live
smoke test is gated on SSFLOW_RUN_E2E=1 (same marker as the real-LLM
test) and hits Sina Finance for real — cheap, no cost.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from ssflow.market_data import (
    MarketQuote,
    _ashare_prefix,
    _infer_market_from_ticker,
    fetch_ashare,
    fetch_market_quote,
)


# ─────────────────────── Prefix routing ───────────────────────


def test_ashare_prefix_shanghai_main():
    assert _ashare_prefix("600519") == ("sh", "1")
    assert _ashare_prefix("601318") == ("sh", "1")
    assert _ashare_prefix("688981") == ("sh", "1")


def test_ashare_prefix_shenzhen_main():
    assert _ashare_prefix("000001") == ("sz", "0")
    assert _ashare_prefix("002594") == ("sz", "0")
    assert _ashare_prefix("300750") == ("sz", "0")
    assert _ashare_prefix("301155") == ("sz", "0")


def test_ashare_prefix_bj():
    assert _ashare_prefix("430047") == ("bj", "0")
    assert _ashare_prefix("830799") == ("bj", "0")


# ─────────────────────── MarketQuote dataclass ───────────────────────


def test_market_quote_is_populated_requires_both_fields():
    q = MarketQuote(ticker="300750", market="ashare", source="sina")
    assert q.is_populated is False

    q.current_price = 391.3
    assert q.is_populated is False  # missing ADV

    q.adv_value = 1e10
    assert q.is_populated is True


def test_market_quote_is_populated_rejects_zero():
    q = MarketQuote(
        ticker="x", market="ashare", source="sina",
        current_price=0.0, adv_value=1e9,
    )
    assert q.is_populated is False

    q.current_price = 100.0
    q.adv_value = 0.0
    assert q.is_populated is False


# ─────────────────────── fetch_ashare (mocked) ───────────────────────


SINA_CATL_RESPONSE = (
    'var hq_str_sz300750="宁德时代,394.100,391.300,394.100,397.000,'
    '389.500,394.100,394.110,5501000,2160000000,100,394.100,200,'
    '394.090,300,394.080,400,394.070,500,394.060,600,394.110,700,'
    '394.120,800,394.130,900,394.140,1000,394.150,2026-04-09,09:30:00,00";'
)

SINA_KLINE_RESPONSE_JSON = '''[
    {"day":"2026-03-11","open":"379.58","high":"403.55","low":"378.98","close":"396.80","volume":"57598685"},
    {"day":"2026-03-12","open":"400.00","high":"400.90","low":"392.70","close":"395.50","volume":"29150375"},
    {"day":"2026-03-13","open":"393.88","high":"403.99","low":"393.80","close":"397.00","volume":"35582639"}
]'''


@pytest.mark.asyncio
async def test_fetch_ashare_parses_sina_realtime_and_kline():
    """fetch_ashare should combine the two Sina endpoints into one MarketQuote."""

    def route(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "hq.sinajs.cn" in url:
            return httpx.Response(200, text=SINA_CATL_RESPONSE)
        if "getKLineData" in url:
            return httpx.Response(
                200,
                text=SINA_KLINE_RESPONSE_JSON,
                headers={"content-type": "application/json"},
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(route)
    real_ctor = httpx.AsyncClient

    def fake_ctor(*args, **kwargs):
        kwargs["transport"] = transport
        return real_ctor(*args, **kwargs)

    with patch("ssflow.market_data.httpx.AsyncClient", fake_ctor):
        q = await fetch_ashare("300750")

    assert q.ticker == "300750"
    assert q.market == "ashare"
    assert q.source == "sina"
    assert q.price_currency == "CNY"
    assert q.current_price == pytest.approx(394.1)
    assert q.last_trade_date == "2026-04-09"
    assert q.diagnostics["name"] == "宁德时代"
    # ADV = mean(volume × close) for 3 rows
    expected_adv = (
        57598685 * 396.80 + 29150375 * 395.50 + 35582639 * 397.00
    ) / 3
    assert q.adv_value == pytest.approx(expected_adv, rel=0.01)
    assert q.is_populated is True


@pytest.mark.asyncio
async def test_fetch_ashare_rejects_non_6digit_ticker():
    q = await fetch_ashare("NVDA")
    assert q.current_price is None
    assert q.is_populated is False
    assert "not a 6-digit code" in q.diagnostics["error"]


@pytest.mark.asyncio
async def test_fetch_ashare_gracefully_handles_dead_sina():
    """If Sina returns garbage, we get None for price and an empty quote
    rather than a crash."""

    def route(request):
        return httpx.Response(500)

    transport = httpx.MockTransport(route)
    real_ctor = httpx.AsyncClient

    def fake_ctor(*args, **kwargs):
        kwargs["transport"] = transport
        return real_ctor(*args, **kwargs)

    with patch("ssflow.market_data.httpx.AsyncClient", fake_ctor):
        q = await fetch_ashare("300750")

    assert q.current_price is None
    assert q.adv_value is None
    assert q.is_populated is False


# ─────────────────────── fetch_market_quote router ───────────────────────


@pytest.mark.asyncio
async def test_fetch_market_quote_unknown_market_with_ashare_ticker_falls_back():
    """When LLM classifies market as 'other' but ticker is 6 digits,
    fall back to A-share."""
    fake_quote = MarketQuote(
        ticker="300750",
        market="ashare",
        source="sina",
        current_price=391.3,
        adv_value=1.3e10,
        price_currency="CNY",
    )
    with patch("ssflow.market_data.fetch_ashare", AsyncMock(return_value=fake_quote)) as m:
        q = await fetch_market_quote("other", "300750")
    m.assert_awaited_once_with("300750")
    assert q is fake_quote


@pytest.mark.asyncio
async def test_fetch_market_quote_unknown_market_with_us_ticker_falls_back():
    """When market is unclear but ticker is 1-5 uppercase letters,
    fall back to us-equity via yfinance."""
    fake_quote = MarketQuote(
        ticker="NVDA",
        market="us-equity",
        source="yfinance",
        current_price=182.08,
        adv_value=3e10,
    )
    with patch("ssflow.market_data.fetch_yfinance", AsyncMock(return_value=fake_quote)) as m:
        q = await fetch_market_quote("", "NVDA")
    m.assert_awaited_once()
    assert q is fake_quote


@pytest.mark.asyncio
async def test_fetch_market_quote_completely_unknown_returns_none():
    q = await fetch_market_quote("", "12abc34")
    assert q is None


@pytest.mark.asyncio
async def test_fetch_market_quote_ashare_empty_ticker_returns_none():
    q = await fetch_market_quote("ashare", "")
    assert q is None


@pytest.mark.asyncio
async def test_fetch_market_quote_routes_ashare_to_fetch_ashare():
    """Verify the routing layer — fetch_market_quote(ashare) calls fetch_ashare."""
    fake_quote = MarketQuote(
        ticker="300750",
        market="ashare",
        source="sina",
        current_price=391.3,
        adv_value=1.3e10,
        price_currency="CNY",
    )
    with patch("ssflow.market_data.fetch_ashare", AsyncMock(return_value=fake_quote)) as m:
        q = await fetch_market_quote("ashare", "300750")
    m.assert_awaited_once_with("300750")
    assert q is fake_quote


@pytest.mark.asyncio
async def test_fetch_market_quote_normalizes_market_case():
    """Market string matching is case-insensitive + whitespace-insensitive."""
    fake_quote = MarketQuote(ticker="300750", market="ashare", source="sina")
    with patch("ssflow.market_data.fetch_ashare", AsyncMock(return_value=fake_quote)):
        q1 = await fetch_market_quote("  A-Share  ", "300750")
        q2 = await fetch_market_quote("SSE", "300750")
    assert q1 is fake_quote
    assert q2 is fake_quote


# ─────────────────────── Shape inference ───────────────────────


def test_infer_ashare_from_6digit():
    assert _infer_market_from_ticker("300750")[0] == "ashare"
    assert _infer_market_from_ticker("002594")[0] == "ashare"
    assert _infer_market_from_ticker("600519")[0] == "ashare"
    assert _infer_market_from_ticker("000001")[0] == "ashare"


def test_infer_us_equity_from_uppercase():
    assert _infer_market_from_ticker("NVDA")[0] == "us-equity"
    assert _infer_market_from_ticker("AAPL")[0] == "us-equity"
    assert _infer_market_from_ticker("TSLA")[0] == "us-equity"
    assert _infer_market_from_ticker("F")[0] == "us-equity"
    # Lowercase still gets upper-cased
    assert _infer_market_from_ticker("aapl")[0] == "us-equity"


def test_infer_hk_equity_from_suffix():
    assert _infer_market_from_ticker("0700.HK") == ("hk-equity", "0700.HK")
    # Bare 5-digit HK code gets .HK appended
    assert _infer_market_from_ticker("00700") == ("hk-equity", "00700.HK")


def test_infer_btc_from_name():
    assert _infer_market_from_ticker("BTC")[0] == "btc-spot"
    assert _infer_market_from_ticker("BTC-USD")[0] == "btc-spot"
    assert _infer_market_from_ticker("", "Bitcoin spot")[0] == "btc-spot"


def test_infer_returns_none_for_unknown():
    assert _infer_market_from_ticker("RANDOM STRING")[0] is None
    assert _infer_market_from_ticker("123")[0] is None
    assert _infer_market_from_ticker("")[0] is None


# ─────────────────────── Live smoke test (gated) ───────────────────────


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_live_fetch_catl_returns_realistic_values():
    """Hit the real Sina endpoints. Only runs with SSFLOW_RUN_E2E=1."""
    q = await fetch_market_quote("ashare", "300750")
    assert q is not None
    assert q.current_price is not None
    assert 100.0 < q.current_price < 2000.0, f"CATL price out of sanity range: {q.current_price}"
    assert q.adv_value is not None
    # 10亿 < ADV < 1000亿
    assert 1e9 < q.adv_value < 1e11, f"CATL ADV out of sanity range: {q.adv_value}"
    assert q.price_currency == "CNY"
    assert q.diagnostics.get("name")  # name populated from Sina
