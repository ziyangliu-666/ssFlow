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
你是一个金融市场分析师。给定一个市场事件描述，你需要识别：
1. 事件的主体标的（primary）：哪支股票/ETF 是事件的直接主体
2. 关联标的（3-5 个）：与事件相关的股票/ETF，说明关系类型
   关系类型: supplier(供应商), customer(客户), competitor(竞品),
             upstream(上游), downstream(下游), peer(同行),
             sector_etf(板块ETF), opposing(对立), index(指数)

输出严格 JSON 格式:
{
  "primary": {
    "ticker": "6位代码",
    "name": "公司名",
    "market": "ashare",
    "relationship": "primary"
  },
  "related": [
    {"ticker": "代码", "name": "名称", "market": "ashare", "relationship": "类型", "reason": "原因"}
  ]
}

只输出你有信心的标的。related 最多 5 个。"""


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
        # Ticker already known — just ask LLM for related instruments
        user_prompt = (
            f"事件主题: {topic}\n"
            f"已知主体标的: {event_ticker} (市场: {market})\n"
            f"请识别 3-5 个关联标的。"
        )
    else:
        user_prompt = f"事件主题: {topic}\n市场: {market}\n请识别主体标的和关联标的。"

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

    # Extract primary
    primary_data = llm_result.get("primary", {})
    primary_ticker = event_ticker or primary_data.get("ticker", "")
    primary_name = primary_data.get("name", primary_ticker)
    primary_market = primary_data.get("market", market)

    if not primary_ticker:
        raise ValueError(
            f"Distillation could not identify a primary instrument for: {topic!r}"
        )

    # Extract related
    related_data = llm_result.get("related", [])

    # Step 2: Fetch market data in parallel
    all_tickers = [primary_ticker] + [r.get("ticker", "") for r in related_data if r.get("ticker")]

    log.info("Distillation: fetching data for %d instruments: %s", len(all_tickers), all_tickers)

    # Parallel fetch: quotes + klines
    quote_tasks = [fetch_market_quote(primary_market, t) for t in all_tickers]
    kline_tasks = [fetch_kline_30d(t, primary_market) for t in all_tickers]

    quote_results = await asyncio.gather(*quote_tasks, return_exceptions=True)
    kline_results = await asyncio.gather(*kline_tasks, return_exceptions=True)

    quotes: dict[str, Any] = {}
    klines: dict[str, list[dict]] = {}
    for ticker, qr, kr in zip(all_tickers, quote_results, kline_results):
        quotes[ticker] = qr if not isinstance(qr, Exception) else None
        klines[ticker] = kr if not isinstance(kr, Exception) else []

    # Step 3: Assemble primary instrument
    primary_quote = quotes.get(primary_ticker)
    primary_price = event_price or (
        primary_quote.current_price if primary_quote and primary_quote.current_price else 0.0
    )
    primary_adv = (
        primary_quote.adv_value if primary_quote and primary_quote.adv_value else 0.0
    )

    primary = Instrument(
        ticker=primary_ticker,
        name=primary_name,
        market=primary_market,
        relationship="primary",
        current_price=primary_price,
        adv_value=primary_adv,
        kline_30d=klines.get(primary_ticker, []),
    )

    # Step 4: Assemble related instruments
    related: list[Instrument] = []
    for r_data in related_data:
        r_ticker = r_data.get("ticker", "")
        if not r_ticker or r_ticker == primary_ticker:
            continue

        r_quote = quotes.get(r_ticker)
        r_price = r_quote.current_price if r_quote and r_quote.current_price else 0.0
        r_adv = r_quote.adv_value if r_quote and r_quote.adv_value else 0.0

        related.append(Instrument(
            ticker=r_ticker,
            name=r_data.get("name", r_ticker),
            market=r_data.get("market", primary_market),
            relationship=r_data.get("relationship", "peer"),
            current_price=r_price,
            adv_value=r_adv,
            kline_30d=klines.get(r_ticker, []),
        ))

    universe = InstrumentUniverse(
        primary=primary,
        related=related,
        topic=topic,
    )

    log.info(
        "Distillation complete: primary=%s (%s), %d related instruments",
        primary.name, primary.ticker, len(related),
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
