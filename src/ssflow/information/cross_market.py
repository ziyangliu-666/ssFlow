"""Cross-market information injection for A-share simulations.

A-share traders don't operate in isolation — institutional investors check
US futures, Hong Kong markets, and commodity prices before the open. This
module extracts cross-market references from the event text and injects them
as high-authority ExternalEvents so agents with global awareness (institutional,
strategic, analyst) can factor them into their decisions.

Two extraction modes:
  1. Automatic: regex-based extraction from event_text for common references
     (NVIDIA, S&P 500, 恒指, 油价, etc.)
  2. Explicit: operator provides structured cross_market_context on the Event
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from .external_events import ExternalEvent

log = logging.getLogger(__name__)


@dataclass
class CrossMarketDataPoint:
    """One cross-market data point (e.g., "NVIDIA -17%")."""

    market: str              # "us_equity", "hk_equity", "commodities", "fx"
    ticker: str              # "NVDA", "HSI", "CL=F", "DXY"
    display_name: str        # "NVIDIA", "恒生指数", "WTI原油"
    change_pct: float        # -17.0 means -17%
    context: str = ""        # "after hours trading, down on export controls"
    relevance: str = ""      # "supply chain", "sector beta", "macro"


@dataclass
class CrossMarketContext:
    """Aggregated cross-market data for injection into the simulation."""

    data_points: list[CrossMarketDataPoint] = field(default_factory=list)

    @property
    def summary_text_zh(self) -> str:
        """Pre-rendered Chinese summary for profile injection."""
        if not self.data_points:
            return ""
        lines = ["# 隔夜外围市场 / Overnight global markets"]
        for dp in self.data_points:
            ctx = f" ({dp.context})" if dp.context else ""
            lines.append(
                f"  - {dp.display_name} ({dp.ticker}): "
                f"{dp.change_pct:+.1f}%{ctx}"
            )
        return "\n".join(lines)

    def to_feed_text(self) -> str:
        """Render as a news-wire style post for OASIS feed injection."""
        if not self.data_points:
            return ""
        parts = []
        for dp in self.data_points:
            ctx = f"({dp.context})" if dp.context else ""
            parts.append(f"{dp.display_name} {dp.change_pct:+.1f}% {ctx}".strip())
        return "[外围市场] " + "; ".join(parts)


# ─────────────────────── Extraction ───────────────────────

# Patterns: (regex, market, ticker, display_name, default_relevance)
_CROSS_MARKET_PATTERNS: list[tuple[str, str, str, str, str]] = [
    # US equities
    (r"NVIDIA\D*?([+-]?\d+\.?\d*)%", "us_equity", "NVDA", "NVIDIA", "supply chain"),
    (r"英伟达\D*?([+-]?\d+\.?\d*)%", "us_equity", "NVDA", "NVIDIA", "supply chain"),
    (r"S&P\s*500\D*?([+-]?\d+\.?\d*)%", "us_equity", "SPX", "S&P 500", "macro"),
    (r"标普\D*?([+-]?\d+\.?\d*)%", "us_equity", "SPX", "S&P 500", "macro"),
    (r"纳斯达克\D*?([+-]?\d+\.?\d*)%", "us_equity", "NDX", "纳斯达克", "macro"),
    (r"道琼斯\D*?([+-]?\d+\.?\d*)%", "us_equity", "DJI", "道琼斯", "macro"),
    (r"特斯拉\D*?([+-]?\d+\.?\d*)%", "us_equity", "TSLA", "特斯拉", "sector beta"),
    (r"苹果\D*?([+-]?\d+\.?\d*)%", "us_equity", "AAPL", "苹果", "sector beta"),
    # Hong Kong
    (r"恒[生指]*\D*?([+-]?\d+\.?\d*)%", "hk_equity", "HSI", "恒生指数", "sector beta"),
    (r"港股\D*?([+-]?\d+\.?\d*)%", "hk_equity", "HSI", "恒生指数", "sector beta"),
    # Commodities
    (r"(?:原油|WTI|布伦特)\D*?([+-]?\d+\.?\d*)%", "commodities", "CL", "原油", "macro"),
    (r"(?:黄金|金价)\D*?([+-]?\d+\.?\d*)%", "commodities", "GC", "黄金", "macro"),
    (r"(?:铜价|伦铜)\D*?([+-]?\d+\.?\d*)%", "commodities", "HG", "铜", "macro"),
    (r"碳酸锂\D*?([+-]?\d+\.?\d*)%", "commodities", "Li2CO3", "碳酸锂", "supply chain"),
    # FX
    (r"(?:美元指数|DXY)\D*?([+-]?\d+\.?\d*)%", "fx", "DXY", "美元指数", "macro"),
    (r"(?:人民币|USDCNY)\D*?([+-]?\d+\.?\d*)%", "fx", "USDCNY", "人民币", "macro"),
]

# Generic sentiment patterns (no specific percentage)
_GENERIC_PATTERNS: list[tuple[str, str, str, str, float]] = [
    (r"隔夜美股大跌", "us_equity", "SPX", "S&P 500", -3.0),
    (r"隔夜美股暴跌", "us_equity", "SPX", "S&P 500", -5.0),
    (r"美股大涨", "us_equity", "SPX", "S&P 500", +3.0),
    (r"隔夜美股下跌", "us_equity", "SPX", "S&P 500", -1.5),
    (r"港股大跌", "hk_equity", "HSI", "恒生指数", -3.0),
    (r"港股走弱", "hk_equity", "HSI", "恒生指数", -1.5),
    (r"油价暴跌", "commodities", "CL", "原油", -5.0),
    (r"油价大跌", "commodities", "CL", "原油", -3.0),
]


def extract_cross_market_from_event(
    event_text: str,
    sector_context: str = "",
) -> CrossMarketContext:
    """Extract cross-market references from event text via regex.

    Scans for known patterns (NVIDIA -17%, 隔夜美股大跌, etc.) and produces
    structured CrossMarketDataPoints.
    """
    combined = f"{event_text} {sector_context}"
    seen_tickers: set[str] = set()
    points: list[CrossMarketDataPoint] = []

    # Specific patterns with extracted percentages
    for pattern, market, ticker, display, relevance in _CROSS_MARKET_PATTERNS:
        m = re.search(pattern, combined, re.IGNORECASE)
        if m and ticker not in seen_tickers:
            try:
                pct = float(m.group(1))
                # Detect negative context around the match
                # Check both before the match and within the matched text itself
                context_window = combined[max(0, m.start()-20):m.end()+10]
                neg_words = ("跌", "下跌", "暴跌", "下降", "下挫", "重挫")
                if pct > 0 and any(w in context_window for w in neg_words):
                    pct = -abs(pct)
            except (ValueError, IndexError):
                continue
            seen_tickers.add(ticker)
            points.append(CrossMarketDataPoint(
                market=market, ticker=ticker,
                display_name=display, change_pct=pct,
                relevance=relevance,
            ))

    # Generic sentiment patterns (no percentage in text)
    for pattern, market, ticker, display, default_pct in _GENERIC_PATTERNS:
        if re.search(pattern, combined) and ticker not in seen_tickers:
            seen_tickers.add(ticker)
            points.append(CrossMarketDataPoint(
                market=market, ticker=ticker,
                display_name=display, change_pct=default_pct,
                relevance="macro",
            ))

    return CrossMarketContext(data_points=points)


def cross_market_from_explicit(
    data: list[dict[str, Any]],
) -> CrossMarketContext:
    """Build CrossMarketContext from operator-provided structured data.

    Expected format per entry:
        {"market": "us_equity", "ticker": "NVDA", "display_name": "NVIDIA",
         "change_pct": -17.0, "context": "export controls", "relevance": "supply chain"}
    """
    points = []
    for entry in data:
        points.append(CrossMarketDataPoint(
            market=str(entry.get("market", "")),
            ticker=str(entry.get("ticker", "")),
            display_name=str(entry.get("display_name", entry.get("ticker", ""))),
            change_pct=float(entry.get("change_pct", 0)),
            context=str(entry.get("context", "")),
            relevance=str(entry.get("relevance", "")),
        ))
    return CrossMarketContext(data_points=points)


# ─────────────────────── Feed Injection ───────────────────────

# Agent types that see cross-market data in their profiles
_ALWAYS_SEE = {"institutional", "strategic", "analyst"}
_SOMETIMES_SEE = {"kol"}  # 50% chance
_NEVER_SEE = {"retail", "news_wire", "media", "regulator", "policy", "company_ir"}


def should_persona_see_cross_market(agent_type: str) -> bool:
    """Whether a persona of this agent_type should receive cross-market context."""
    if agent_type in _ALWAYS_SEE:
        return True
    if agent_type in _SOMETIMES_SEE:
        return True  # Simplified: always include for KOLs who track global
    return False


def cross_market_to_external_events(
    ctx: CrossMarketContext,
    author_persona_id: str = "news_wire_bloomberg_cn",
) -> list[ExternalEvent]:
    """Convert cross-market context into ExternalEvents for round 0 injection.

    Produces a single high-authority news wire post summarizing overnight
    global market moves. Injected at round 0 so agents see it before their
    first decision.
    """
    feed_text = ctx.to_feed_text()
    if not feed_text:
        return []
    return [ExternalEvent(
        round_idx=0,
        content_type="news_brief",
        author_persona_id=author_persona_id,
        text=feed_text,
        authority_weight=0.90,
        topic_tags=["cross_market", "overnight"],
    )]


__all__ = [
    "CrossMarketContext",
    "CrossMarketDataPoint",
    "cross_market_from_explicit",
    "cross_market_to_external_events",
    "extract_cross_market_from_event",
    "should_persona_see_cross_market",
]
