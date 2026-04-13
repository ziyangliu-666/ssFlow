"""Append-only trade tape — the audit trail for all fills.

Every price tick in the simulation is traceable to a specific fill on this
tape: who bought, who sold, at what price, what quantity, and who was the
aggressor. This is the foundation for auditable price discovery.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Fill:
    """A single matched trade on the tape."""

    fill_id: str
    ticker: str
    buyer_order_id: str
    seller_order_id: str
    buyer_agent_id: str
    seller_agent_id: str
    buyer_persona_id: str
    seller_persona_id: str
    price: float
    quantity: float              # shares traded
    timestamp: int               # simulation tick
    aggressor_side: str          # "buy" or "sell" — who crossed the spread

    @property
    def value(self) -> float:
        """Trade value in currency."""
        return self.price * self.quantity


class TradeTape:
    """Append-only, queryable trade tape for one instrument.

    Usage:
        tape = TradeTape(ticker="002594.SZ")
        tape.record(fill)
        tape.last_price          # price of most recent fill
        tape.vwap(start, end)    # volume-weighted average price in range
        tape.volume_by_agent(agent_id)
        tape.fills_in_range(start_tick, end_tick)
    """

    def __init__(self, ticker: str) -> None:
        self.ticker = ticker
        self._fills: list[Fill] = []
        self._fill_count: int = 0

    def record(self, fill: Fill) -> None:
        """Append a fill to the tape."""
        self._fills.append(fill)
        self._fill_count += 1

    @property
    def last_price(self) -> float | None:
        """Price of the most recent fill, or None if no trades."""
        return self._fills[-1].price if self._fills else None

    @property
    def last_fill(self) -> Fill | None:
        return self._fills[-1] if self._fills else None

    def fills_in_range(self, start_tick: int, end_tick: int) -> list[Fill]:
        """All fills with start_tick <= timestamp <= end_tick."""
        return [f for f in self._fills if start_tick <= f.timestamp <= end_tick]

    def fills_by_persona(self, persona_id: str) -> list[Fill]:
        """Fills where the given persona was buyer or seller."""
        return [
            f for f in self._fills
            if f.buyer_persona_id == persona_id or f.seller_persona_id == persona_id
        ]

    def fills_by_agent(self, agent_id: str) -> list[Fill]:
        """Fills where the given agent was buyer or seller."""
        return [
            f for f in self._fills
            if f.buyer_agent_id == agent_id or f.seller_agent_id == agent_id
        ]

    def vwap(self, start_tick: int = 0, end_tick: int = 2**63) -> float | None:
        """Volume-weighted average price in [start_tick, end_tick]."""
        fills = self.fills_in_range(start_tick, end_tick)
        if not fills:
            return None
        total_value = sum(f.value for f in fills)
        total_qty = sum(f.quantity for f in fills)
        return total_value / total_qty if total_qty > 0 else None

    def total_volume(self, start_tick: int = 0, end_tick: int = 2**63) -> float:
        """Total shares traded in range."""
        return sum(f.quantity for f in self.fills_in_range(start_tick, end_tick))

    def total_value(self, start_tick: int = 0, end_tick: int = 2**63) -> float:
        """Total traded value (currency) in range."""
        return sum(f.value for f in self.fills_in_range(start_tick, end_tick))

    def net_flow_by_persona(
        self, start_tick: int = 0, end_tick: int = 2**63
    ) -> dict[str, float]:
        """Net flow (buy value - sell value) per persona in range."""
        flows: dict[str, float] = {}
        for f in self.fills_in_range(start_tick, end_tick):
            flows[f.buyer_persona_id] = flows.get(f.buyer_persona_id, 0.0) + f.value
            flows[f.seller_persona_id] = flows.get(f.seller_persona_id, 0.0) - f.value
        return flows

    def price_attribution(
        self, start_tick: int, end_tick: int
    ) -> list[dict]:
        """Attribute price moves to specific fills in the range.

        Returns a list of dicts, each describing a fill and its contribution
        to the price path. Useful for report generation.
        """
        fills = self.fills_in_range(start_tick, end_tick)
        if not fills:
            return []

        attributions = []
        prev_price = fills[0].price
        for fill in fills:
            delta = (fill.price / prev_price - 1.0) if prev_price > 0 else 0.0
            attributions.append({
                "fill_id": fill.fill_id,
                "timestamp": fill.timestamp,
                "price": fill.price,
                "quantity": fill.quantity,
                "value": fill.value,
                "delta_pct": delta,
                "aggressor_side": fill.aggressor_side,
                "buyer_persona": fill.buyer_persona_id,
                "seller_persona": fill.seller_persona_id,
            })
            prev_price = fill.price
        return attributions

    @property
    def all_fills(self) -> list[Fill]:
        return list(self._fills)

    def __len__(self) -> int:
        return self._fill_count

    def to_summary(self) -> dict:
        """Summary stats for reports."""
        if not self._fills:
            return {
                "ticker": self.ticker,
                "n_fills": 0,
                "total_volume": 0.0,
                "total_value": 0.0,
            }
        return {
            "ticker": self.ticker,
            "n_fills": self._fill_count,
            "first_price": self._fills[0].price,
            "last_price": self._fills[-1].price,
            "high": max(f.price for f in self._fills),
            "low": min(f.price for f in self._fills),
            "vwap": self.vwap(),
            "total_volume": self.total_volume(),
            "total_value": self.total_value(),
        }
