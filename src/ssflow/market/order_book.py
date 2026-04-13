"""Limit order book with price-time priority matching.

Uses sortedcontainers for O(log n) price-level operations. Each price level
is a FIFO queue of resting orders — matching respects time priority within
the same price.

This is a pure data structure: no A-share rules, no sessions, no validation.
The Exchange layer handles all of that before orders reach the book.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterator

from sortedcontainers import SortedDict

log = logging.getLogger(__name__)


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    LIMIT = "limit"
    MARKET = "market"


class OrderStatus(str, Enum):
    PENDING = "pending"          # submitted but not yet in book
    RESTING = "resting"          # in book, waiting for contra match
    PARTIAL = "partial"          # partially filled, remainder resting
    FILLED = "filled"            # fully filled
    CANCELLED = "cancelled"      # cancelled by agent or system
    REJECTED = "rejected"        # rejected by exchange rules


@dataclass
class Order:
    """A single order submitted to the book."""

    order_id: str
    agent_id: str                # unique agent identifier
    persona_id: str              # persona class (for reporting)
    ticker: str
    side: OrderSide
    order_type: OrderType
    price: float                 # limit price (0.0 for market orders)
    quantity: float              # original quantity (shares)
    remaining: float = 0.0      # unfilled quantity
    timestamp: int = 0           # simulation tick (monotonic)
    status: OrderStatus = OrderStatus.PENDING

    def __post_init__(self) -> None:
        if self.remaining == 0.0:
            self.remaining = self.quantity

    @property
    def is_active(self) -> bool:
        return self.status in (OrderStatus.RESTING, OrderStatus.PARTIAL)

    @property
    def filled_quantity(self) -> float:
        return self.quantity - self.remaining


@dataclass
class MatchResult:
    """Result of matching an incoming order against the book."""

    fills: list[tuple[Order, Order, float, float]]  # (aggressor, resting, price, qty)
    remaining: float             # unfilled quantity of the incoming order


class OrderBook:
    """Price-time priority limit order book.

    Bids (buy orders) are stored highest-price-first.
    Asks (sell orders) are stored lowest-price-first.
    Within each price level, orders are FIFO by insertion time.

    Usage:
        book = OrderBook(ticker="002594.SZ")
        fills = book.match_limit(order)   # try to fill, rest remainder
        fills = book.match_market(order)  # fill what's available
        book.cancel(order_id)
    """

    def __init__(self, ticker: str) -> None:
        self.ticker = ticker
        # Bids: SortedDict keyed by negative price (so highest bid first)
        # Each value is a deque of Order objects (FIFO)
        self._bids: SortedDict[float, deque[Order]] = SortedDict()
        # Asks: SortedDict keyed by positive price (so lowest ask first)
        self._asks: SortedDict[float, deque[Order]] = SortedDict()
        # Index for O(1) lookup by order_id
        self._orders: dict[str, Order] = {}

    # ── Queries ──

    @property
    def best_bid(self) -> float | None:
        """Highest resting buy price, or None if empty."""
        if not self._bids:
            return None
        return -self._bids.peekitem(0)[0]

    @property
    def best_ask(self) -> float | None:
        """Lowest resting sell price, or None if empty."""
        if not self._asks:
            return None
        return self._asks.peekitem(0)[0]

    @property
    def spread(self) -> float | None:
        """Best ask - best bid, or None if either side is empty."""
        b, a = self.best_bid, self.best_ask
        if b is None or a is None:
            return None
        return a - b

    @property
    def midpoint(self) -> float | None:
        """(best_bid + best_ask) / 2, or None."""
        b, a = self.best_bid, self.best_ask
        if b is None or a is None:
            return None
        return (a + b) / 2.0

    def bid_depth(self, n_levels: int = 5) -> list[tuple[float, float]]:
        """Top n bid levels as [(price, total_qty), ...]."""
        result = []
        for neg_price, queue in self._bids.items():
            if len(result) >= n_levels:
                break
            total = sum(o.remaining for o in queue if o.is_active)
            if total > 0:
                result.append((-neg_price, total))
        return result

    def ask_depth(self, n_levels: int = 5) -> list[tuple[float, float]]:
        """Top n ask levels as [(price, total_qty), ...]."""
        result = []
        for price, queue in self._asks.items():
            if len(result) >= n_levels:
                break
            total = sum(o.remaining for o in queue if o.is_active)
            if total > 0:
                result.append((price, total))
        return result

    def total_bid_volume(self) -> float:
        """Total resting buy volume across all levels."""
        return sum(
            o.remaining
            for queue in self._bids.values()
            for o in queue
            if o.is_active
        )

    def total_ask_volume(self) -> float:
        """Total resting sell volume across all levels."""
        return sum(
            o.remaining
            for queue in self._asks.values()
            for o in queue
            if o.is_active
        )

    def get_order(self, order_id: str) -> Order | None:
        return self._orders.get(order_id)

    # ── Matching ──

    def match_limit(self, order: Order) -> MatchResult:
        """Match a limit order: fill what crosses, rest the remainder.

        A buy limit order matches against asks at or below its price.
        A sell limit order matches against bids at or above its price.
        """
        fills = self._match_against_book(order)
        if order.remaining > 0:
            self._insert(order)
        return MatchResult(fills=fills, remaining=order.remaining)

    def match_market(self, order: Order) -> MatchResult:
        """Match a market order: fill what's available, cancel remainder.

        Market orders have no price limit — they walk the book until filled
        or the book is exhausted. Unfilled remainder is cancelled (not rested).
        """
        fills = self._match_against_book(order)
        if order.remaining > 0:
            order.status = OrderStatus.CANCELLED
        return MatchResult(fills=fills, remaining=order.remaining)

    def cancel(self, order_id: str) -> bool:
        """Cancel a resting order. Returns True if found and cancelled."""
        order = self._orders.get(order_id)
        if order is None or not order.is_active:
            return False
        order.status = OrderStatus.CANCELLED
        order.remaining = 0.0
        self._remove_from_level(order)
        return True

    def cancel_all(self) -> int:
        """Cancel all resting orders. Returns count cancelled."""
        count = 0
        for order in list(self._orders.values()):
            if order.is_active:
                order.status = OrderStatus.CANCELLED
                order.remaining = 0.0
                count += 1
        self._bids.clear()
        self._asks.clear()
        return count

    # ── Call auction support ──

    def collect_for_call_auction(self) -> tuple[list[Order], list[Order]]:
        """Return all resting (bids, asks) for call auction matching.

        Does NOT remove them — the caller should process the clearing
        price and update order statuses.
        """
        bids = [
            o for queue in self._bids.values()
            for o in queue if o.is_active
        ]
        asks = [
            o for queue in self._asks.values()
            for o in queue if o.is_active
        ]
        return bids, asks

    # ── Internal ──

    def _match_against_book(self, incoming: Order) -> list[tuple[Order, Order, float, float]]:
        """Walk the book and fill the incoming order.

        Returns list of (aggressor, resting, fill_price, fill_qty) tuples.
        """
        fills: list[tuple[Order, Order, float, float]] = []

        if incoming.side == OrderSide.BUY:
            book_side = self._asks
            price_ok = (
                (lambda ask_price: True)
                if incoming.order_type == OrderType.MARKET
                else (lambda ask_price: ask_price <= incoming.price)
            )
        else:
            book_side = self._bids
            price_ok = (
                (lambda bid_neg: True)
                if incoming.order_type == OrderType.MARKET
                else (lambda bid_neg: -bid_neg >= incoming.price)
            )

        keys_to_remove: list[float] = []

        for price_key in list(book_side.keys()):
            if incoming.remaining <= 0:
                break
            if not price_ok(price_key):
                break

            queue = book_side[price_key]
            while queue and incoming.remaining > 0:
                resting = queue[0]
                if not resting.is_active:
                    queue.popleft()
                    continue

                # Bids keyed by -price, asks keyed by +price
                fill_price = price_key if incoming.side == OrderSide.BUY else -price_key
                fill_qty = min(incoming.remaining, resting.remaining)

                incoming.remaining -= fill_qty
                resting.remaining -= fill_qty

                if resting.remaining <= 0:
                    resting.status = OrderStatus.FILLED
                    queue.popleft()
                else:
                    resting.status = OrderStatus.PARTIAL

                if incoming.remaining <= 0:
                    incoming.status = OrderStatus.FILLED
                else:
                    incoming.status = OrderStatus.PARTIAL

                fills.append((incoming, resting, fill_price, fill_qty))

            if not queue:
                keys_to_remove.append(price_key)

        for k in keys_to_remove:
            if k in book_side:
                del book_side[k]

        return fills

    def _insert(self, order: Order) -> None:
        """Insert a limit order into the resting book."""
        if order.side == OrderSide.BUY:
            key = -order.price  # negative so SortedDict gives highest first
            book_side = self._bids
        else:
            key = order.price
            book_side = self._asks

        if key not in book_side:
            book_side[key] = deque()
        book_side[key].append(order)

        if order.status != OrderStatus.PARTIAL:
            order.status = OrderStatus.RESTING
        self._orders[order.order_id] = order

    def _remove_from_level(self, order: Order) -> None:
        """Remove a cancelled order from its price level."""
        if order.side == OrderSide.BUY:
            key = -order.price
            book_side = self._bids
        else:
            key = order.price
            book_side = self._asks

        if key in book_side:
            queue = book_side[key]
            try:
                queue.remove(order)
            except ValueError:
                pass
            if not queue:
                del book_side[key]

    def __len__(self) -> int:
        """Total number of active resting orders."""
        return sum(1 for o in self._orders.values() if o.is_active)

    def to_snapshot(self) -> dict:
        """Serializable snapshot for event bus / reports."""
        return {
            "ticker": self.ticker,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "spread": self.spread,
            "midpoint": self.midpoint,
            "bid_depth": self.bid_depth(),
            "ask_depth": self.ask_depth(),
            "total_bid_volume": self.total_bid_volume(),
            "total_ask_volume": self.total_ask_volume(),
            "n_resting_orders": len(self),
        }
