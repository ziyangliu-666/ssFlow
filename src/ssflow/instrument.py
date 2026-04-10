"""Multi-instrument universe for market simulations.

A simulation involves multiple tradeable instruments (stocks, ETFs, indices).
All instruments are **equal peers** — there is no privileged "primary"
instrument. One instrument may be tagged with relationship="event_subject"
to indicate it is the direct subject of the triggering event, but this
carries no behavioral privilege in the engine or UI.

Each Instrument carries its own price, ADV, optional K-line history,
and optional financial summary. Agents trade freely across the universe.
"""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass, field
from typing import Any


log = logging.getLogger(__name__)


VALID_RELATIONSHIPS = {
    "event_subject",
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
    # Legacy — accepted on deserialization, mapped to event_subject
    "primary",
}

# Directional modifier per relationship type. Captures economic logic:
# suppliers/customers co-move, competitors may diverge, opposing plays
# are anti-correlated. Applied as sign+scale on top of statistical beta.
RELATIONSHIP_DIRECTION: dict[str, float] = {
    "event_subject": 1.0,
    "primary": 1.0,       # legacy alias
    "supplier": 0.8,
    "customer": 0.8,
    "upstream": 0.8,
    "downstream": 0.8,
    "peer": 0.9,
    "sector_etf": 0.9,
    "competitor": -0.3,
    "opposing": -0.8,
    "index": 0.5,
    "other": 0.3,
}


@dataclass
class Instrument:
    """One tradeable instrument in the simulation universe."""

    ticker: str
    name: str
    market: str
    relationship: str  # descriptive relationship to the triggering event
    current_price: float
    adv_value: float
    price_currency: str = "CNY"

    # Financial summary (populated by distillation or user)
    financials: dict[str, Any] = field(default_factory=dict)

    # Recent 30-day K-line data
    kline_30d: list[dict[str, Any]] = field(default_factory=list)

    # Holdings by persona type (% of total/float shares).
    # Populated by market_context.fetch_market_context during distillation.
    # e.g. {"northbound_qfii": 12.5, "mutual_fund_active_pm": 8.3, ...}
    holdings_by_persona: dict[str, float] = field(default_factory=dict)

    # Margin balance (CNY)
    margin_long_balance: float = 0
    margin_short_balance: float = 0

    def to_serializable(self) -> dict[str, Any]:
        d = {
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
        if self.holdings_by_persona:
            d["holdings_by_persona"] = dict(self.holdings_by_persona)
        if self.margin_long_balance:
            d["margin_long_balance"] = self.margin_long_balance
        if self.margin_short_balance:
            d["margin_short_balance"] = self.margin_short_balance
        return d


@dataclass
class InstrumentUniverse:
    """The full set of tradeable instruments for one simulation.

    All instruments are equal peers. Agents freely choose which to trade.
    One instrument may have relationship="event_subject" to indicate it is
    the direct subject of the triggering event — this is descriptive metadata,
    not a behavioral privilege.
    """

    instruments: list[Instrument] = field(default_factory=list)
    topic: str = ""

    @property
    def all_instruments(self) -> list[Instrument]:
        return self.instruments

    @property
    def tickers(self) -> list[str]:
        return [inst.ticker for inst in self.instruments]

    @property
    def event_subject_ticker(self) -> str:
        """Ticker of the event's direct subject (for backward compat scalars).

        Returns the first instrument with relationship="event_subject",
        or instruments[0] as fallback.
        """
        for inst in self.instruments:
            if inst.relationship in ("event_subject", "primary"):
                return inst.ticker
        return self.instruments[0].ticker if self.instruments else ""

    @property
    def primary_ticker(self) -> str:
        """Deprecated alias for event_subject_ticker."""
        return self.event_subject_ticker

    def get(self, ticker: str) -> Instrument | None:
        for inst in self.instruments:
            if inst.ticker == ticker:
                return inst
        return None

    def prices(self) -> dict[str, float]:
        return {inst.ticker: inst.current_price for inst in self.instruments}

    def adv_values(self) -> dict[str, float]:
        return {inst.ticker: inst.adv_value for inst in self.instruments}

    def to_serializable(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "instruments": [inst.to_serializable() for inst in self.instruments],
        }

    # ── Beta / spillover computation ──

    def _log_returns(self, inst: Instrument) -> list[float]:
        """Extract log returns from K-line closing prices."""
        closes = [bar.get("close") for bar in inst.kline_30d
                  if bar.get("close") is not None and bar.get("close") > 0]
        if len(closes) < 5:
            return []
        return [math.log(closes[i] / closes[i - 1])
                for i in range(1, len(closes))
                if closes[i - 1] > 0]

    def compute_pairwise_beta(self, ticker_a: str, ticker_b: str) -> float:
        """Compute statistical beta of instrument A vs instrument B.

        Beta = corr(r_a, r_b) * (std_a / std_b). Falls back to 0.3
        if insufficient data.
        """
        inst_a = self.get(ticker_a)
        inst_b = self.get(ticker_b)
        if inst_a is None or inst_b is None:
            return 0.3

        ret_a = self._log_returns(inst_a)
        ret_b = self._log_returns(inst_b)
        n = min(len(ret_a), len(ret_b))
        if n < 4:
            return 0.3

        ret_a, ret_b = ret_a[:n], ret_b[:n]
        try:
            corr = statistics.correlation(ret_a, ret_b)
            std_a = statistics.stdev(ret_a)
            std_b = statistics.stdev(ret_b)
            if std_b > 0:
                return max(-3.0, min(3.0, corr * (std_a / std_b)))
        except (statistics.StatisticsError, ZeroDivisionError):
            pass
        return 0.3

    def compute_spillover(
        self,
        source_deltas: dict[str, float],
    ) -> dict[str, float]:
        """Compute spillover deltas for instruments with no direct order flow.

        For each instrument NOT in source_deltas, its spillover is the
        weighted average of all source deltas, weighted by:
          |beta(target, source)| * direction_modifier(target.relationship)

        This is bidirectional: any instrument with direct flow can
        influence any instrument without.

        Returns:
            dict of {ticker: spillover_delta} for instruments not in source_deltas.
        """
        from .market_dynamics import MAX_DELTA_PCT_PER_ROUND

        result: dict[str, float] = {}
        source_tickers = set(source_deltas.keys())

        for inst in self.instruments:
            if inst.ticker in source_tickers:
                continue  # already has direct pricing

            direction = RELATIONSHIP_DIRECTION.get(inst.relationship, 0.3)
            weighted_sum = 0.0
            weight_total = 0.0

            for src_ticker, src_delta in source_deltas.items():
                if src_delta == 0.0:
                    continue
                beta = abs(self.compute_pairwise_beta(inst.ticker, src_ticker))
                w = beta
                weighted_sum += w * src_delta
                weight_total += w

            if weight_total > 0:
                raw_spill = direction * (weighted_sum / weight_total)
                result[inst.ticker] = max(-MAX_DELTA_PCT_PER_ROUND,
                                          min(MAX_DELTA_PCT_PER_ROUND, raw_spill))
            else:
                result[inst.ticker] = 0.0

        return result

    # Legacy compat: compute_betas and spillover_coefficients still work
    # but compute relative to event_subject.

    def compute_betas(self) -> dict[str, float]:
        """Compute beta of each instrument vs. the event subject."""
        subj = self.event_subject_ticker
        betas: dict[str, float] = {}
        for inst in self.instruments:
            if inst.ticker == subj:
                continue
            betas[inst.ticker] = self.compute_pairwise_beta(inst.ticker, subj)
        return betas

    def spillover_coefficients(self) -> dict[str, float]:
        """Spillover coefficient per non-subject ticker: |beta| * direction."""
        betas = self.compute_betas()
        result: dict[str, float] = {}
        for inst in self.instruments:
            if inst.ticker == self.event_subject_ticker:
                continue
            beta = abs(betas.get(inst.ticker, 0.3))
            direction = RELATIONSHIP_DIRECTION.get(inst.relationship, 0.3)
            result[inst.ticker] = beta * direction
        return result

    def prompt_summary(self) -> str:
        """Summary for agent prompts listing all instruments equally."""
        REL_ZH = {
            "event_subject": "事件相关", "primary": "事件相关",
            "supplier": "供应商", "competitor": "竞品",
            "customer": "客户", "sector_etf": "板块ETF", "upstream": "上游",
            "downstream": "下游", "peer": "同行", "opposing": "对立",
            "index": "指数", "other": "其他",
        }
        lines = ["本次模拟涉及以下标的（你可以交易其中任何一个）:"]
        for inst in self.instruments:
            rel = REL_ZH.get(inst.relationship, inst.relationship)
            lines.append(
                f"  - {inst.name} ({inst.ticker}) [{rel}] "
                f"当前价 {inst.price_currency}{inst.current_price:.2f}"
            )
        return "\n".join(lines)

    @classmethod
    def from_serializable(cls, data: dict[str, Any]) -> "InstrumentUniverse":
        """Deserialize from either flat or legacy format."""
        inst_fields = set(Instrument.__dataclass_fields__.keys())

        if "instruments" in data:
            # New flat format
            instruments = [
                Instrument(**{k: v for k, v in d.items() if k in inst_fields})
                for d in data["instruments"]
            ]
        elif "primary" in data:
            # Legacy format: primary + related → flat list
            primary_data = data["primary"]
            # Map "primary" relationship to "event_subject"
            if primary_data.get("relationship") == "primary":
                primary_data = dict(primary_data)
                primary_data["relationship"] = "event_subject"
            instruments = [
                Instrument(**{k: v for k, v in primary_data.items() if k in inst_fields})
            ]
            for r in data.get("related", []):
                instruments.append(
                    Instrument(**{k: v for k, v in r.items() if k in inst_fields})
                )
        else:
            instruments = []

        return cls(instruments=instruments, topic=data.get("topic", ""))


__all__ = [
    "Instrument",
    "InstrumentUniverse",
    "VALID_RELATIONSHIPS",
]
