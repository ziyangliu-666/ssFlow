"""A-share exchange — the facade that ties together LOB, matching, rules, and tape.

This is the single entry point for all market operations. Agents submit
orders here; the exchange validates (A-share rules), matches (LOB with
price-time priority), and records (trade tape).

Usage:
    exchange = Exchange(
        ticker="002594.SZ",
        prev_close=218.50,
        board_type=BoardType.NORMAL,
    )
    fills = exchange.submit_order(
        agent_id="agent_001",
        persona_id="retail_chaser",
        side="buy",
        order_type="limit",
        price=219.00,
        quantity=500,
        agent_holdings=0.0,
        agent_cash=200_000.0,
        t1_locked=0.0,
    )
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .ashare_rules import (
    AShareRules,
    BoardType,
    RejectionReason,
    SessionPhase,
    T1Ledger,
    round_tick,
)
from .matching import MatchingEngine
from .order_book import Order, OrderBook, OrderSide, OrderStatus, OrderType
from .trade_tape import Fill, TradeTape

log = logging.getLogger(__name__)


@dataclass
class OrderResult:
    """Result of submitting an order to the exchange."""

    order: Order
    fills: list[Fill]
    rejection: RejectionReason | None = None

    @property
    def is_rejected(self) -> bool:
        return self.rejection is not None

    @property
    def is_filled(self) -> bool:
        return self.order.status == OrderStatus.FILLED

    @property
    def is_resting(self) -> bool:
        return self.order.status in (OrderStatus.RESTING, OrderStatus.PARTIAL)

    @property
    def filled_quantity(self) -> float:
        return sum(f.quantity for f in self.fills)

    @property
    def filled_value(self) -> float:
        return sum(f.value for f in self.fills)

    @property
    def avg_fill_price(self) -> float | None:
        if not self.fills:
            return None
        return self.filled_value / self.filled_quantity


class Exchange:
    """A-share exchange for a single instrument.

    Combines:
    - AShareRules for order validation
    - OrderBook for price-time priority resting
    - MatchingEngine for continuous and call auction matching
    - TradeTape for the audit trail
    - T1Ledger for T+1 enforcement
    """

    def __init__(
        self,
        ticker: str,
        prev_close: float,
        board_type: BoardType | str = BoardType.NORMAL,
    ) -> None:
        if isinstance(board_type, str):
            board_type = BoardType(board_type)

        self.ticker = ticker
        self.prev_close = prev_close
        self.rules = AShareRules(
            ticker=ticker,
            prev_close=prev_close,
            board_type=board_type,
        )
        self.book = OrderBook(ticker=ticker)
        self.tape = TradeTape(ticker=ticker)
        self.engine = MatchingEngine(book=self.book, tape=self.tape)
        self.t1 = T1Ledger()

        self._order_counter: int = 0
        self._tick: int = 0

        # Track the current "last traded price" — starts at prev_close
        self._last_price: float = prev_close

    # ── Properties ──

    @property
    def last_price(self) -> float:
        """Most recent traded price, or prev_close if no trades yet."""
        return self.tape.last_price or self._last_price

    @property
    def best_bid(self) -> float | None:
        return self.book.best_bid

    @property
    def best_ask(self) -> float | None:
        return self.book.best_ask

    @property
    def spread(self) -> float | None:
        return self.book.spread

    @property
    def midpoint(self) -> float | None:
        return self.book.midpoint

    @property
    def session_phase(self) -> SessionPhase:
        return self.rules.session_phase

    # ── Core operations ──

    def set_tick(self, tick: int) -> None:
        """Advance the simulation clock."""
        self._tick = tick
        self.engine.set_tick(tick)

    def set_session(self, phase: SessionPhase) -> None:
        """Update the session phase (affects which orders are accepted)."""
        self.rules.set_session(phase)

    def submit_order(
        self,
        agent_id: str,
        persona_id: str,
        side: str,
        order_type: str,
        price: float,
        quantity: float,
        agent_holdings: float,
        agent_cash: float,
        *,
        is_short: bool = False,
    ) -> OrderResult:
        """Submit an order. Validates, matches, and records.

        Returns an OrderResult with the order status and any fills.
        """
        self._order_counter += 1
        order_id = f"O{self._order_counter:06d}"

        # Round price to tick
        if order_type == "limit":
            price = round_tick(price)

        order = Order(
            order_id=order_id,
            agent_id=agent_id,
            persona_id=persona_id,
            ticker=self.ticker,
            side=OrderSide(side),
            order_type=OrderType(order_type),
            price=price,
            quantity=quantity,
            timestamp=self._tick,
        )

        # Compute T+1 locked shares
        t1_locked = self.t1.locked_shares(agent_id, self.ticker, self._current_round)

        # Validate against A-share rules
        rejection = self.rules.validate_order(
            side=side,
            order_type=order_type,
            price=price,
            quantity=quantity,
            agent_id=agent_id,
            agent_holdings=agent_holdings,
            agent_cash=agent_cash,
            t1_locked=t1_locked,
            is_short=is_short,
        )

        if rejection:
            order.status = OrderStatus.REJECTED
            log.debug(
                "Order rejected: %s %s %s %.0f @ %.2f — %s",
                agent_id, side, order_type, quantity, price, rejection.message,
            )
            return OrderResult(order=order, fills=[], rejection=rejection)

        # Submit to matching engine
        fills = self.engine.submit(order)

        # Record T+1 for buy fills
        if side == "buy":
            filled_qty = sum(f.quantity for f in fills)
            if filled_qty > 0:
                self.t1.record_buy(agent_id, self.ticker, filled_qty, self._current_round)

        if fills:
            self._last_price = fills[-1].price

        return OrderResult(order=order, fills=fills)

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a resting order."""
        return self.engine.cancel(order_id)

    def run_call_auction(self) -> list[Fill]:
        """Execute call auction matching."""
        fills = self.engine.run_call_auction()
        if fills:
            self._last_price = fills[-1].price
            # Record T+1 for auction buy fills
            for f in fills:
                self.t1.record_buy(f.buyer_agent_id, self.ticker, f.quantity, self._current_round)
        return fills

    def advance_day(self) -> None:
        """Start a new trading day. Clears T+1 ledger, cancels resting orders."""
        self.t1.advance_day()
        cancelled = self.book.cancel_all()
        if cancelled > 0:
            log.info("Day advance: cancelled %d resting orders", cancelled)
        self._last_price = self.last_price  # carry over

    # ── Queries ──

    def book_snapshot(self) -> dict:
        """Full book state for reports."""
        return self.book.to_snapshot()

    def tape_summary(self) -> dict:
        """Trade tape summary."""
        return self.tape.to_summary()

    def get_order(self, order_id: str) -> Order | None:
        return self.book.get_order(order_id)

    def sellable_shares(self, agent_id: str, total_holdings: float) -> float:
        """How many shares agent can sell (respecting T+1)."""
        return self.t1.sellable_shares(
            agent_id, self.ticker, self._current_round, total_holdings
        )

    # ── Round tracking ──

    _current_round: int = 0

    def set_round(self, round_idx: int) -> None:
        """Set the current simulation round (for T+1 tracking)."""
        self._current_round = round_idx

    def to_serializable(self) -> dict:
        return {
            "ticker": self.ticker,
            "prev_close": self.prev_close,
            "last_price": self.last_price,
            "rules": self.rules.to_serializable(),
            "book": self.book.to_snapshot(),
            "tape": self.tape.to_summary(),
        }


__all__ = ["Exchange", "OrderResult"]
