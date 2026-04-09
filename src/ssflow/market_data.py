"""Market data fetcher — pull real prices and ADV from upstream APIs.

Replaces the old "trust the LLM to guess from web scrape" approach. For
each supported market, there's a small fetch function that hits a
well-known public endpoint and returns a `MarketQuote` dataclass with
the fields the sandbox engine needs (current_price, adv_value,
price_currency).

Why not a single unified library like akshare?

    akshare is great but heavy — the first `stock_zh_a_spot_em()` call
    loads the full A-share market (~5000 rows, ~30s) which is overkill
    for a single-ticker lookup. Direct HTTP calls to Sina + Eastmoney
    return in <500ms each and the response shape is stable enough for
    our use case. yfinance stays as the fallback for non-Chinese
    markets.

Sources by market:

    ashare (SSE + SZSE) → Sina Finance realtime + Sina K-line (for ADV)
    us-equity            → yfinance
    hk-equity            → yfinance (with .HK suffix)
    crude-oil-wti        → yfinance CL=F
    gold-spot            → yfinance GC=F
    btc-spot             → yfinance BTC-USD
    (others)             → None (caller falls back to LLM)

Eastmoney was the first-choice historical source but its
`push2his.eastmoney.com` endpoint started disconnecting without
responding mid-morning 2026-04-09 — the Sina K-line endpoint is used
instead because it's equally stable and uses the same headers as the
realtime call. Kept as a note in case EM comes back.

Fail-soft: every fetcher catches all exceptions and logs a warning
rather than raising. An empty MarketQuote is a valid "couldn't find
anything" signal.

The caller (event_extractor.py, Stage 0b) should treat a populated
MarketQuote as authoritative and overlay it on whatever the LLM
synthesis produced.
"""

from __future__ import annotations

import asyncio
import logging
import re
import statistics
from dataclasses import dataclass, field
from typing import Optional

import httpx


log = logging.getLogger(__name__)


# Browser-like headers for all Sina endpoints. Without them Sina's
# /quotes_service/* K-line API occasionally returns 403 or drops the
# connection — Referer is the key one they check.
_SINA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": "https://finance.sina.com.cn",
}


# ─────────────────────── Result dataclass ───────────────────────


@dataclass
class MarketQuote:
    """Result of a real-time + ADV lookup for one instrument."""

    ticker: str
    market: str
    source: str                       # "sina+em" | "yfinance" | ...
    current_price: float | None = None    # in price_currency
    adv_value: float | None = None        # 20-day mean 成交额, same currency
    price_currency: str = "CNY"
    last_trade_date: str = ""             # YYYY-MM-DD
    diagnostics: dict[str, object] = field(default_factory=dict)

    @property
    def is_populated(self) -> bool:
        return (
            self.current_price is not None
            and self.current_price > 0
            and self.adv_value is not None
            and self.adv_value > 0
        )


# ─────────────────────── A-share (Sina + Eastmoney) ───────────────────────


_ASHARE_TICKER_RE = re.compile(r"^\d{6}$")


def _ashare_prefix(ticker: str) -> tuple[str, str]:
    """Return (sina_prefix, eastmoney_prefix) for a 6-digit A-share ticker.

    Sina uses ``sh`` / ``sz`` prefixes; Eastmoney uses ``1.`` / ``0.``.
    Rules (the broad-stroke ones that cover > 99% of listed names):
        - 6xxxxx → SSE main board (sh / 1.)
        - 688xxx → STAR market (sh / 1.)
        - 000xxx, 001xxx, 002xxx, 003xxx → SZSE main/SME (sz / 0.)
        - 300xxx, 301xxx → ChiNext (sz / 0.)
        - 430xxx, 830xxx, 832xxx, 870xxx, 872xxx → BSE (bj / 0.)
    """
    t = ticker.strip()
    if t.startswith(("6",)):
        return "sh", "1"
    if t.startswith(("000", "001", "002", "003", "300", "301")):
        return "sz", "0"
    if t.startswith(("4", "8")):
        return "bj", "0"
    # Default to Shenzhen for unknowns
    return "sz", "0"


async def _fetch_sina_realtime(ticker: str) -> dict[str, float] | None:
    """Sina hq.sinajs.cn — returns a flat CSV with 32 fields.

    Field layout (A-share):
        0: 名字     1: 今开     2: 昨收     3: 现价     4: 最高     5: 最低
        6: 买一价   7: 卖一价   8: 成交量(股)  9: 成交额(元)
        10-19: 买一到买五 (股数, 报价)
        20-29: 卖一到卖五
        30: 日期    31: 时间
    """
    sina_prefix, _ = _ashare_prefix(ticker)
    url = f"https://hq.sinajs.cn/list={sina_prefix}{ticker}"
    try:
        async with httpx.AsyncClient(headers=_SINA_HEADERS, timeout=5.0) as client:
            r = await client.get(url)
        if r.status_code != 200:
            return None
        m = re.search(r'="([^"]+)"', r.text)
        if not m:
            return None
        fields = m.group(1).split(",")
        if len(fields) < 32:
            return None
        name = fields[0]
        open_px = _safe_float(fields[1])
        prev_close = _safe_float(fields[2])
        current = _safe_float(fields[3])
        high = _safe_float(fields[4])
        low = _safe_float(fields[5])
        bid1 = _safe_float(fields[6])
        ask1 = _safe_float(fields[7])
        volume_shares = _safe_float(fields[8])
        turnover_cny = _safe_float(fields[9])
        date = fields[30]
        time_str = fields[31]

        # Pick the "most authoritative current price". In order:
        # 1. 现价 if > 0 (market is open/has traded today)
        # 2. 昨收 (always a valid baseline for pre-market or closed)
        # If 昨收 is also 0 something is badly wrong.
        px = current if current > 0 else prev_close
        if px <= 0:
            return None

        return {
            "name": name,
            "price": px,
            "prev_close": prev_close,
            "open": open_px,
            "high": high,
            "low": low,
            "bid1": bid1,
            "ask1": ask1,
            "volume_shares": volume_shares,
            "turnover_cny": turnover_cny,
            "date": date,
            "time": time_str,
        }
    except Exception as exc:
        log.warning("sina fetch failed for %s: %s", ticker, exc)
        return None


async def _fetch_sina_kline_adv(
    ticker: str, lookback_days: int = 20
) -> float | None:
    """Sina K-line — fetch N daily bars and compute mean `volume × close`
    as a proxy for daily turnover (ADV in CNY).

    Sina's endpoint returns OHLCV where `volume` is in shares. True
    turnover would be `mean_price × volume`, but `close × volume` is
    within a few percent and is good enough for the sandbox lambda
    coefficient. Returns None if the endpoint fails or returns no data.
    """
    sina_prefix, _ = _ashare_prefix(ticker)
    url = (
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData"
    )
    params = {
        "symbol": f"{sina_prefix}{ticker}",
        "scale": "240",         # 240 minutes = daily bar
        "ma": "no",
        "datalen": str(lookback_days),
    }
    try:
        async with httpx.AsyncClient(headers=_SINA_HEADERS, timeout=5.0) as client:
            r = await client.get(url, params=params, follow_redirects=True)
        if r.status_code != 200:
            return None
        # Sina returns a JSON array (despite the .php url); occasionally
        # wrapped in JSONP if you pass a callback, but we don't.
        rows = r.json()
        if not isinstance(rows, list) or not rows:
            return None
        turnovers: list[float] = []
        for row in rows:
            try:
                volume_shares = float(row.get("volume", 0))
                close = float(row.get("close", 0))
                if volume_shares > 0 and close > 0:
                    turnovers.append(volume_shares * close)
            except (TypeError, ValueError):
                continue
        if not turnovers:
            return None
        return statistics.mean(turnovers)
    except Exception as exc:
        log.warning("sina kline fetch failed for %s: %s", ticker, exc)
        return None


async def fetch_ashare(ticker: str) -> MarketQuote:
    """A-share: Sina hq for realtime price, Sina K-line for 20-day ADV."""
    quote = MarketQuote(ticker=ticker, market="ashare", source="sina", price_currency="CNY")
    if not _ASHARE_TICKER_RE.match(ticker):
        quote.diagnostics["error"] = f"ticker {ticker!r} is not a 6-digit code"
        return quote

    realtime, adv = await asyncio.gather(
        _fetch_sina_realtime(ticker),
        _fetch_sina_kline_adv(ticker),
    )

    if realtime:
        quote.current_price = float(realtime["price"])
        quote.last_trade_date = str(realtime.get("date", ""))
        quote.diagnostics["name"] = realtime.get("name")
        quote.diagnostics["prev_close"] = realtime.get("prev_close")
        quote.diagnostics["intraday_open"] = realtime.get("open")
    if adv is not None:
        quote.adv_value = float(adv)

    return quote


# ─────────────────────── US-equity / crypto / commodities (yfinance) ───────────────────────


def _fetch_yfinance_sync(symbol: str) -> tuple[float | None, float | None, str]:
    """Blocking yfinance lookup. Returns (price, adv_value_in_currency, currency)."""
    try:
        import yfinance as yf  # lazy import — heavy
    except ImportError:
        return None, None, "USD"

    try:
        ticker_obj = yf.Ticker(symbol)
        # Try fast info first (doesn't hit the slow .info API)
        fast = getattr(ticker_obj, "fast_info", None)
        price = None
        currency = "USD"
        if fast:
            try:
                price = float(fast.get("last_price") or fast.get("last_close") or 0) or None
                currency = fast.get("currency") or "USD"
            except Exception:
                pass

        # Fallback to .info if fast_info is empty
        if not price:
            info = ticker_obj.info
            price = info.get("regularMarketPrice") or info.get("previousClose")
            currency = info.get("currency") or currency

        # ADV = 20-day avg volume (in shares) × price
        hist = ticker_obj.history(period="1mo", interval="1d", auto_adjust=False)
        if hist is not None and not hist.empty:
            vols = hist["Volume"].dropna()
            closes = hist["Close"].dropna()
            if len(vols) > 0 and len(closes) > 0:
                mean_volume = float(vols.tail(20).mean())
                mean_price = float(closes.tail(20).mean())
                adv_value = mean_volume * mean_price
                return price, adv_value, currency

        return price, None, currency
    except Exception as exc:
        log.warning("yfinance fetch failed for %s: %s", symbol, exc)
        return None, None, "USD"


async def fetch_yfinance(symbol: str, market: str) -> MarketQuote:
    """Thin async wrapper around the blocking yfinance call."""
    price, adv_value, currency = await asyncio.to_thread(_fetch_yfinance_sync, symbol)
    return MarketQuote(
        ticker=symbol,
        market=market,
        source="yfinance",
        current_price=price,
        adv_value=adv_value,
        price_currency=currency,
    )


# ─────────────────────── Top-level router ───────────────────────


# Map markets to yfinance ticker transformers
_YFINANCE_TRANSFORMERS: dict[str, callable] = {
    "us-equity": lambda t: t,                             # AAPL → AAPL
    "hk-equity": lambda t: t if "." in t else f"{t}.HK",  # 0700 → 0700.HK
    "jp-equity": lambda t: t if "." in t else f"{t}.T",   # 7203 → 7203.T
    "crude-oil-wti": lambda t: t or "CL=F",
    "crude-oil-brent": lambda t: t or "BZ=F",
    "gold-spot": lambda t: t or "GC=F",
    "silver-spot": lambda t: t or "SI=F",
    "btc-spot": lambda t: t or "BTC-USD",
    "eth-spot": lambda t: t or "ETH-USD",
}


async def fetch_market_quote(
    market: str,
    ticker: str,
    *,
    instrument_hint: str | None = None,
) -> MarketQuote | None:
    """Route to the right fetcher based on market.

    Returns None if the market is unsupported or the fetcher couldn't
    find any data. Returns a populated MarketQuote on success, and a
    partially-populated one (is_populated=False) if only part of the
    data came back.
    """
    market_norm = (market or "").strip().lower()
    ticker_norm = (ticker or "").strip()

    # A-share: Sina realtime + Sina K-line for ADV. Requires an
    # explicit 6-digit ticker.
    if market_norm in ("ashare", "a-share", "cn-equity", "sse", "szse"):
        if not ticker_norm:
            return None
        return await fetch_ashare(ticker_norm)

    # yfinance-backed markets. Some (crypto, commodities) have default
    # symbols so an empty ticker is OK — the transformer substitutes.
    if market_norm in _YFINANCE_TRANSFORMERS:
        symbol = _YFINANCE_TRANSFORMERS[market_norm](ticker_norm)
        if not symbol:
            return None
        return await fetch_yfinance(symbol, market_norm)

    # Unknown market → no data
    log.info("fetch_market_quote: no fetcher for market=%s", market_norm)
    return None


# ─────────────────────── Helpers ───────────────────────


def _safe_float(s: str) -> float:
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


__all__ = [
    "MarketQuote",
    "fetch_market_quote",
    "fetch_ashare",
    "fetch_yfinance",
]
