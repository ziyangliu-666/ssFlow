"""Multi-instrument universe for market simulations.

A simulation can involve multiple tradeable instruments (stocks, ETFs, indices).
The InstrumentUniverse holds:

  - A **primary** instrument (the event's direct subject)
  - A list of **related** instruments (suppliers, competitors, customers,
    sector ETFs, opposing plays)

Each Instrument carries its own price, ADV, optional K-line history,
and optional financial summary. Agents trade freely across the universe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


VALID_RELATIONSHIPS = {
    "primary",
    "supplier",
    "competitor",
    "customer",
    "sector_etf",
    "upstream",
    "downstream",
    "peer",
    "opposing",
    "index",
    "other",
}


@dataclass
class Instrument:
    """One tradeable instrument in the simulation universe."""

    ticker: str
    name: str
    market: str
    relationship: str  # to the primary instrument
    current_price: float
    adv_value: float
    price_currency: str = "CNY"

    # Financial summary (populated by distillation or user)
    # e.g., {"revenue_bn": 300, "net_profit_bn": 40, "gross_margin": 0.22,
    #        "debt_ratio": 0.45, "pe_ttm": 25.0}
    financials: dict[str, Any] = field(default_factory=dict)

    # Recent 30-day K-line data
    # [{date: "2026-03-10", open: 380.0, high: 392.0, low: 375.0,
    #   close: 388.5, volume: 12345678}, ...]
    kline_30d: list[dict[str, Any]] = field(default_factory=list)

    def to_serializable(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "name": self.name,
            "market": self.market,
            "relationship": self.relationship,
            "current_price": self.current_price,
            "adv_value": self.adv_value,
            "price_currency": self.price_currency,
            "financials": dict(self.financials),
            "kline_30d": list(self.kline_30d),
        }


@dataclass
class InstrumentUniverse:
    """The full set of tradeable instruments for one simulation.

    Design: 主体 + 3-5 个关联标的。Agent 在这个宇宙里自由选择标的交易。
    """

    primary: Instrument
    related: list[Instrument] = field(default_factory=list)
    topic: str = ""

    @property
    def all_instruments(self) -> list[Instrument]:
        return [self.primary] + list(self.related)

    @property
    def tickers(self) -> list[str]:
        return [inst.ticker for inst in self.all_instruments]

    @property
    def primary_ticker(self) -> str:
        return self.primary.ticker

    def get(self, ticker: str) -> Instrument | None:
        for inst in self.all_instruments:
            if inst.ticker == ticker:
                return inst
        return None

    def prices(self) -> dict[str, float]:
        return {inst.ticker: inst.current_price for inst in self.all_instruments}

    def adv_values(self) -> dict[str, float]:
        return {inst.ticker: inst.adv_value for inst in self.all_instruments}

    def to_serializable(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "primary": self.primary.to_serializable(),
            "related": [r.to_serializable() for r in self.related],
        }

    def prompt_summary(self) -> str:
        """One-paragraph summary for agent prompts listing all instruments."""
        lines = ["本次模拟涉及以下标的:"]
        for inst in self.all_instruments:
            rel = {"primary": "主体", "supplier": "供应商", "competitor": "竞品",
                   "customer": "客户", "sector_etf": "板块ETF", "upstream": "上游",
                   "downstream": "下游", "peer": "同行", "opposing": "对立",
                   "index": "指数", "other": "其他"}.get(inst.relationship, inst.relationship)
            lines.append(
                f"  - {inst.name} ({inst.ticker}) [{rel}] "
                f"当前价 {inst.price_currency}{inst.current_price:.2f}"
            )
        return "\n".join(lines)

    @classmethod
    def from_serializable(cls, data: dict[str, Any]) -> "InstrumentUniverse":
        primary = Instrument(**{
            k: v for k, v in data["primary"].items()
            if k in Instrument.__dataclass_fields__
        })
        related = [
            Instrument(**{
                k: v for k, v in r.items()
                if k in Instrument.__dataclass_fields__
            })
            for r in data.get("related", [])
        ]
        return cls(primary=primary, related=related, topic=data.get("topic", ""))


__all__ = [
    "Instrument",
    "InstrumentUniverse",
    "VALID_RELATIONSHIPS",
]
