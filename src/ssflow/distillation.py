"""Distillation — from an event topic to a full InstrumentUniverse.

The distillation step is the critical "sync with reality" phase:

  1. LLM identifies the primary instrument and related instruments
  2. Real market data is fetched in parallel for all instruments
  3. K-line history is pulled for the primary and top related instruments
  4. An InstrumentUniverse is assembled with all data attached

This runs BEFORE sandbox generation and persona casting. The output
feeds into both.

Cost: 1 LLM call (~300 tokens output) + N parallel data fetches ≈ $0.001.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .instrument import Instrument, InstrumentUniverse
from .llm_client import chat_json_sync
from .market_data import fetch_kline_30d, fetch_market_quote

log = logging.getLogger(__name__)


_DISTILL_SYSTEM = """\
你是一个金融市场分析师。给定一个市场事件描述，你需要识别 3-6 个与事件相关的
股票/ETF，用于模拟该事件对整个板块的资金流动影响。

所有标的地位平等——不分主次。事件的直接标的标记为 "event_subject"，
其余标的按关系类型标记。

关系类型: event_subject(事件直接标的), supplier(供应商), customer(客户),
          competitor(竞品), upstream(上游), downstream(下游), peer(同行),
          sector_etf(板块ETF), opposing(对立), index(指数)

输出严格 JSON 格式:
{
  "instruments": [
    {"ticker": "6位代码", "name": "名称", "market": "ashare", "relationship": "类型", "reason": "原因"}
  ]
}

只输出你有信心的标的。3-6 个。"""


async def distill(
    topic: str,
    market: str = "ashare",
    event_ticker: str | None = None,
    event_price: float | None = None,
) -> InstrumentUniverse:
    """Distill an event topic into a full InstrumentUniverse.

    Steps:
      1. LLM identifies primary + related instruments
      2. Fetch real market data for all instruments in parallel
      3. Fetch K-line history for all instruments
      4. Assemble InstrumentUniverse

    Args:
        topic: event description string
        market: market slug (default "ashare")
        event_ticker: optional ticker if already known (skips LLM for primary)
        event_price: optional current price if already fetched

    Returns:
        InstrumentUniverse with all data populated
    """
    # Step 1: LLM identifies instruments
    if event_ticker:
        # Ticker already known — ask LLM for related instruments
        user_prompt = (
            f"事件主题: {topic}\n"
            f"已知事件标的: {event_ticker} (市场: {market})\n"
            f"请识别 3-6 个相关标的（包括已知标的，标记为 event_subject）。"
        )
    else:
        user_prompt = f"事件主题: {topic}\n市场: {market}\n请识别 3-6 个相关标的。"

    try:
        llm_response = chat_json_sync([
            {"role": "system", "content": _DISTILL_SYSTEM},
            {"role": "user", "content": user_prompt},
        ])
        llm_result = llm_response.parsed if hasattr(llm_response, 'parsed') else llm_response
        if not isinstance(llm_result, dict):
            llm_result = {}
    except Exception as exc:
        log.warning("Distillation LLM call failed: %s", exc)
        llm_result = {}

    # Parse instruments from LLM response (support both new and legacy format)
    if "instruments" in llm_result:
        instruments_data = llm_result["instruments"]
    elif "primary" in llm_result:
        # Legacy format: merge primary + related
        p = llm_result["primary"]
        p["relationship"] = "event_subject"
        instruments_data = [p] + llm_result.get("related", [])
    else:
        instruments_data = []

    # If event_ticker is known but not in the LLM response, prepend it
    known_tickers = {d.get("ticker") for d in instruments_data}
    if event_ticker and event_ticker not in known_tickers:
        instruments_data.insert(0, {
            "ticker": event_ticker,
            "name": event_ticker,
            "market": market,
            "relationship": "event_subject",
        })

    if not instruments_data:
        raise ValueError(
            f"Distillation could not identify any instruments for: {topic!r}"
        )

    # Deduplicate by ticker
    seen: set[str] = set()
    deduped: list[dict] = []
    for d in instruments_data:
        t = d.get("ticker", "")
        if t and t not in seen:
            seen.add(t)
            deduped.append(d)
    instruments_data = deduped

    # Step 2: Fetch market data in parallel
    all_tickers = [d["ticker"] for d in instruments_data]

    log.info("Distillation: fetching data for %d instruments: %s", len(all_tickers), all_tickers)

    quote_tasks = [fetch_market_quote(market, t) for t in all_tickers]
    kline_tasks = [fetch_kline_30d(t, market) for t in all_tickers]

    quote_results = await asyncio.gather(*quote_tasks, return_exceptions=True)
    kline_results = await asyncio.gather(*kline_tasks, return_exceptions=True)

    quotes: dict[str, Any] = {}
    klines: dict[str, list[dict]] = {}
    for ticker, qr, kr in zip(all_tickers, quote_results, kline_results):
        quotes[ticker] = qr if not isinstance(qr, Exception) else None
        klines[ticker] = kr if not isinstance(kr, Exception) else []

    # Step 3: Assemble instruments (skip those with failed data fetches)
    instruments: list[Instrument] = []
    for d in instruments_data:
        ticker = d.get("ticker", "")
        if not ticker:
            continue

        q = quotes.get(ticker)
        price = q.current_price if q and q.current_price else 0.0
        adv = q.adv_value if q and q.adv_value else 0.0

        # Use caller-provided price for the event ticker if available
        if ticker == event_ticker and event_price is not None:
            price = event_price

        # Guard: skip instruments where the quote fetch failed
        if price <= 0:
            log.warning(
                "Distillation: dropping %s (%s) — quote fetch returned price=%.2f",
                d.get("name", ticker), ticker, price,
            )
            continue

        # Map legacy "primary" relationship
        rel = d.get("relationship", "peer")
        if rel == "primary":
            rel = "event_subject"

        instruments.append(Instrument(
            ticker=ticker,
            name=d.get("name", ticker),
            market=d.get("market", market),
            relationship=rel,
            current_price=price,
            adv_value=adv,
            kline_30d=klines.get(ticker, []),
        ))

    if not instruments:
        raise ValueError(
            f"Distillation: all instruments had zero prices for: {topic!r}"
        )

    universe = InstrumentUniverse(instruments=instruments, topic=topic)

    log.info(
        "Distillation complete: %d instruments — %s",
        len(instruments),
        ", ".join(f"{i.name}({i.ticker})" for i in instruments),
    )

    return universe


def distill_sync(
    topic: str,
    market: str = "ashare",
    event_ticker: str | None = None,
    event_price: float | None = None,
) -> InstrumentUniverse:
    """Synchronous wrapper around distill() for use in Flask endpoints."""
    return asyncio.run(distill(
        topic=topic,
        market=market,
        event_ticker=event_ticker,
        event_price=event_price,
    ))


__all__ = [
    "distill",
    "distill_sync",
]
