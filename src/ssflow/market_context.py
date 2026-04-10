"""Fetch rich market context for instruments during distillation.

Goes beyond basic price/ADV to pull:
  - Shareholder structure → maps to persona initial holdings
  - Margin balance → leverage/sentiment signal
  - Shareholder count → retail concentration
  - Float shares → calculate persona position sizing

Data sources: Eastmoney datacenter API (primary), akshare (fallback).
All fetchers are fail-soft: return empty/None on failure, never raise.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

_EM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": "https://data.eastmoney.com",
}


# ─────────────────────── Holder classification ───────────────────────

# Map holder names to persona types via pattern matching.
# Order matters: first match wins.
_HOLDER_PATTERNS: list[tuple[str, str]] = [
    # Northbound / HKSCC
    (r"HKSCC|香港中央结算", "northbound_qfii"),
    # National team
    (r"中央汇金|证金公司|梧桐树投资|中国证券金融", "national_team"),
    # Social security / pension
    (r"社保基金|养老保险|年金|企业年金", "insurance_pension"),
    # Insurance
    (r"人寿保险|平安保险|太平洋保险|泰康|新华保险|中国人保|人保资管|太保", "insurance_pension"),
    # Mutual funds (公募)
    (r"交易型开放式|指数证券投资基金|ETF|开放式基金|封闭式基金", "etf_ap_or_mutual"),
    (r"银行股份有限公司-|基金管理|华夏基金|易方达|南方基金|招商基金|广发基金|嘉实|博时|汇添富|富国", "mutual_fund"),
    # Private equity / hedge
    (r"私募|合伙企业|有限合伙|投资管理|资产管理有限公司", "private_equity"),
    # QFII
    (r"QFII|合格境外|摩根|高盛|瑞银|花旗|巴克莱|贝莱德|先锋领航|阿布达比", "northbound_qfii"),
    # Industrial capital (investment companies, group corps)
    (r"集团有限公司|控股有限公司|实业有限公司|投资有限公司|发展有限公司", "industrial_capital"),
    # Government / state
    (r"国资委|国有资产|财政厅|财政局", "government_state"),
    # Individual major holders
    (r"^[\u4e00-\u9fff]{2,4}$", "major_individual"),  # 2-4 Chinese chars = person name
]


def classify_holder(name: str) -> str:
    """Classify a holder name into a persona type."""
    for pattern, persona_type in _HOLDER_PATTERNS:
        if re.search(pattern, name):
            return persona_type
    return "other"


# ─────────────────────── Data structures ───────────────────────


@dataclass
class HolderEntry:
    """One entry in the top-10 holder list."""
    name: str
    pct: float          # % of total shares or float
    shares: float       # number of shares
    nature: str         # 流通A股, 限售流通股, etc.
    persona_type: str   # classified persona type


@dataclass
class MarketContext:
    """Rich context data for one instrument, beyond price/ADV."""
    ticker: str

    # Shareholder structure
    top_holders: list[HolderEntry] = field(default_factory=list)
    shareholder_count: int = 0       # 股东总数
    avg_shares_per_holder: float = 0 # 人均持股

    # Holdings by persona type (% of float, derived from top holders)
    holdings_by_persona: dict[str, float] = field(default_factory=dict)

    # Margin trading
    margin_long_balance: float = 0   # 融资余额 (CNY)
    margin_short_balance: float = 0  # 融券余额 (CNY)

    # Float info
    total_shares: float = 0          # 总股本
    float_shares: float = 0          # 流通A股
    float_market_cap: float = 0      # 流通市值

    # Turnover
    turnover_rate_5d: float = 0      # 5日平均换手率

    def to_serializable(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "top_holders": [
                {"name": h.name, "pct": h.pct, "persona_type": h.persona_type}
                for h in self.top_holders
            ],
            "holdings_by_persona": dict(self.holdings_by_persona),
            "shareholder_count": self.shareholder_count,
            "margin_long_balance": self.margin_long_balance,
            "margin_short_balance": self.margin_short_balance,
            "float_market_cap": self.float_market_cap,
            "turnover_rate_5d": self.turnover_rate_5d,
        }

    def prompt_block(self) -> str:
        """Format as context for agent prompts."""
        lines = []
        if self.holdings_by_persona:
            lines.append("持仓结构:")
            for ptype, pct in sorted(
                self.holdings_by_persona.items(), key=lambda x: -x[1]
            ):
                if pct > 0.1:
                    lines.append(f"  {ptype}: {pct:.1f}%")
        if self.shareholder_count:
            lines.append(f"股东总数: {self.shareholder_count:,}")
        if self.margin_long_balance:
            lines.append(f"融资余额: {self.margin_long_balance/1e8:.1f}亿")
        if self.margin_short_balance:
            lines.append(f"融券余额: {self.margin_short_balance/1e8:.1f}亿")
        if self.turnover_rate_5d:
            lines.append(f"5日换手率: {self.turnover_rate_5d:.2f}%")
        return "\n".join(lines)


# ─────────────────────── Fetchers ───────────────────────


async def _fetch_holders_em(ticker: str) -> list[HolderEntry]:
    """Fetch top-10 holders from Eastmoney datacenter API."""
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    params = {
        "reportName": "RPT_F10_EH_FREEHOLDERS",
        "columns": "SECURITY_CODE,HOLDER_NAME,HOLD_NUM,FREE_HOLDNUM_RATIO,HOLDER_TYPE,END_DATE",
        "filter": f'(SECURITY_CODE="{ticker}")',
        "pageSize": 10,
        "sortColumns": "END_DATE,FREE_HOLDNUM_RATIO",
        "sortTypes": "-1,-1",
        "pageNumber": 1,
    }
    try:
        async with httpx.AsyncClient(headers=_EM_HEADERS, timeout=8.0, verify=False) as client:
            r = await client.get(url, params=params)
        data = r.json()
        if not data.get("result") or not data["result"].get("data"):
            return []
        entries = []
        for row in data["result"]["data"][:10]:
            name = row.get("HOLDER_NAME", "")
            pct = float(row.get("FREE_HOLDNUM_RATIO", 0) or 0)
            shares = float(row.get("HOLD_NUM", 0) or 0)
            entries.append(HolderEntry(
                name=name, pct=pct, shares=shares,
                nature="", persona_type=classify_holder(name),
            ))
        return entries
    except Exception as exc:
        log.warning("EM holder fetch failed for %s: %s", ticker, exc)
        return []


async def _fetch_holders_akshare(ticker: str) -> list[HolderEntry]:
    """Fallback: fetch top-10 holders via akshare (blocking, wrapped)."""
    def _sync():
        try:
            import akshare as ak
            df = ak.stock_main_stock_holder(stock=ticker)
            if df is None or df.empty:
                return []
            latest = df["截至日期"].max()
            latest_df = df[df["截至日期"] == latest]
            entries = []
            for _, row in latest_df.iterrows():
                name = str(row.get("股东名称", ""))
                pct = float(row.get("持股比例", 0) or 0)
                shares = float(row.get("持股数量", 0) or 0)
                nature = str(row.get("股本性质", ""))
                entries.append(HolderEntry(
                    name=name, pct=pct, shares=shares,
                    nature=nature, persona_type=classify_holder(name),
                ))
            return entries
        except Exception as exc:
            log.warning("akshare holder fetch failed for %s: %s", ticker, exc)
            return []
    return await asyncio.to_thread(_sync)


async def _fetch_margin_em(ticker: str) -> tuple[float, float]:
    """Fetch latest margin balance from Eastmoney."""
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    params = {
        "reportName": "RPTA_WEB_RZRQ_GGMX",
        "columns": "SCODE,RZYE,RQYE,TRADE_DATE",
        "filter": f'(SCODE="{ticker}")',
        "pageSize": 1,
        "sortColumns": "TRADE_DATE",
        "sortTypes": "-1",
    }
    try:
        async with httpx.AsyncClient(headers=_EM_HEADERS, timeout=8.0, verify=False) as client:
            r = await client.get(url, params=params)
        data = r.json()
        if data.get("result") and data["result"].get("data"):
            row = data["result"]["data"][0]
            return float(row.get("RZYE", 0) or 0), float(row.get("RQYE", 0) or 0)
    except Exception as exc:
        log.debug("EM margin fetch failed for %s: %s", ticker, exc)
    return 0.0, 0.0


def _aggregate_holdings(holders: list[HolderEntry]) -> dict[str, float]:
    """Aggregate holder entries into per-persona-type percentages.

    Also infers retail holdings as the remainder.
    """
    by_type: dict[str, float] = {}
    for h in holders:
        by_type[h.persona_type] = by_type.get(h.persona_type, 0) + h.pct

    # Map internal types to persona IDs used in ashare.yaml
    PERSONA_MAP = {
        "northbound_qfii": "northbound_qfii",
        "national_team": "national_team_strategic",
        "insurance_pension": "insurance_pension",
        "mutual_fund": "mutual_fund_active_pm",
        "etf_ap_or_mutual": "etf_authorized_participant",
        "private_equity": "private_equity_active",
        "industrial_capital": "industrial_capital_strategic",
        "government_state": "government_state_strategic",
        "major_individual": "major_individual_holder",
    }
    result: dict[str, float] = {}
    total_known = 0.0
    for internal, persona_id in PERSONA_MAP.items():
        pct = by_type.get(internal, 0)
        if pct > 0:
            result[persona_id] = pct
            total_known += pct

    # Retail = 100% - known institutional - other
    other_pct = by_type.get("other", 0)
    total_known += other_pct
    retail_pct = max(0, 100.0 - total_known)
    if retail_pct > 0:
        # Split retail across the 3 retail persona types
        result["retail_short_term_chaser"] = retail_pct * 0.3
        result["retail_passive_holder"] = retail_pct * 0.5
        result["retail_pro_am_value"] = retail_pct * 0.2

    return result


async def fetch_market_context(ticker: str) -> MarketContext:
    """Fetch rich market context for one A-share instrument.

    Tries Eastmoney first, falls back to akshare for holders.
    All failures are soft — returns a MarketContext with whatever
    data was available.
    """
    ctx = MarketContext(ticker=ticker)

    # Parallel fetch: holders (EM primary, akshare fallback) + margin
    holders_em, margin, holders_ak = await asyncio.gather(
        _fetch_holders_em(ticker),
        _fetch_margin_em(ticker),
        _fetch_holders_akshare(ticker),
        return_exceptions=True,
    )

    # Use EM holders if available, else akshare
    holders = []
    if isinstance(holders_em, list) and holders_em:
        holders = holders_em
        log.info("Market context %s: %d holders from Eastmoney", ticker, len(holders))
    elif isinstance(holders_ak, list) and holders_ak:
        holders = holders_ak
        log.info("Market context %s: %d holders from akshare", ticker, len(holders))

    ctx.top_holders = holders
    ctx.holdings_by_persona = _aggregate_holdings(holders)

    # Shareholder count from akshare data
    if isinstance(holders_ak, list) and holders_ak:
        # akshare entries don't carry count, but we fetched it in the df
        pass  # TODO: pipe through if needed

    # Margin
    if isinstance(margin, tuple):
        ctx.margin_long_balance, ctx.margin_short_balance = margin

    return ctx


async def fetch_market_contexts(tickers: list[str]) -> dict[str, MarketContext]:
    """Fetch market context for multiple tickers in parallel."""
    tasks = [fetch_market_context(t) for t in tickers]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    out: dict[str, MarketContext] = {}
    for ticker, result in zip(tickers, results):
        if isinstance(result, MarketContext):
            out[ticker] = result
        else:
            log.warning("fetch_market_context failed for %s: %s", ticker, result)
            out[ticker] = MarketContext(ticker=ticker)
    return out


__all__ = [
    "MarketContext",
    "HolderEntry",
    "classify_holder",
    "fetch_market_context",
    "fetch_market_contexts",
]
