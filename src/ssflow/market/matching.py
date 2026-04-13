"""Matching engine — continuous auction + call auction.

Continuous auction: incoming orders match against the resting book using
price-time priority. This is the standard mechanism for 09:30-14:57.

Call auction: all collected orders are matched at a single clearing price
that maximizes volume. Used for opening (09:25) and closing (15:00).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .order_book import OrderBook, Order, OrderSide, OrderType, OrderStatus, MatchResult
from .trade_tape import Fill, TradeTape

log = logging.getLogger(__name__)


@dataclass
class MatchingEngine:
    """Wraps an OrderBook with fill recording and ID generation.

    Usage:
        engine = MatchingEngine(book=OrderBook("002594.SZ"), tape=TradeTape("002594.SZ"))
        fills = engine.submit(order)
        # or for call auction:
        fills = engine.run_call_auction()
    """

    book: OrderBook
    tape: TradeTape
    _tick: int = 0
    _fill_counter: int = 0

    @property
    def ticker(self) -> str:
        return self.book.ticker

    @property
    def last_price(self) -> float | None:
        return self.tape.last_price

    def set_tick(self, tick: int) -> None:
        """Set the current simulation tick (monotonic timestamp)."""
        self._tick = tick

    def submit(self, order: Order) -> list[Fill]:
        """Submit an order for continuous auction matching.

        Returns a list of fills (may be empty if the order rests entirely).
        """
        order.timestamp = self._tick

        if order.order_type == OrderType.MARKET:
            result = self.book.match_market(order)
        else:
            result = self.book.match_limit(order)

        return self._record_fills(result, order)

    def run_call_auction(self) -> list[Fill]:
        """Execute call auction: find clearing price, match at that price.

        Algorithm (simplified A-share opening auction):
        1. Collect all resting bids and asks
        2. Find the price that maximizes matched volume
        3. At the clearing price, match all crossable orders at that single price
        4. Cancel unfilled orders that don't cross

        The clearing price is the price at which the largest volume trades.
        If multiple prices tie, use the one closest to the last traded price
        (or prev_close if no trades yet).
        """
        bids, asks = self.book.collect_for_call_auction()
        if not bids or not asks:
            return []

        # Collect all candidate prices from both sides
        prices = sorted(set(o.price for o in bids + asks))
        if not prices:
            return []

        # For each candidate price, compute matchable volume
        best_price = prices[0]
        best_volume = 0.0
        reference = self.tape.last_price or 0.0

        for p in prices:
            buy_vol = sum(o.remaining for o in bids if o.price >= p)
            sell_vol = sum(o.remaining for o in asks if o.price <= p)
            matched = min(buy_vol, sell_vol)

            if matched > best_volume or (
                matched == best_volume
                and reference > 0
                and abs(p - reference) < abs(best_price - reference)
            ):
                best_volume = matched
                best_price = p

        if best_volume <= 0:
            return []

        clearing_price = best_price
        log.info(
            "Call auction clearing: price=%.2f, volume=%.0f shares",
            clearing_price, best_volume,
        )

        # Match at the clearing price
        # Sort bids by price desc then time, asks by price asc then time
        eligible_bids = sorted(
            [o for o in bids if o.price >= clearing_price],
            key=lambda o: (-o.price, o.timestamp),
        )
        eligible_asks = sorted(
            [o for o in asks if o.price <= clearing_price],
            key=lambda o: (o.price, o.timestamp),
        )

        fills: list[Fill] = []
        bid_idx, ask_idx = 0, 0

        while bid_idx < len(eligible_bids) and ask_idx < len(eligible_asks):
            bid = eligible_bids[bid_idx]
            ask = eligible_asks[ask_idx]

            if not bid.is_active:
                bid_idx += 1
                continue
            if not ask.is_active:
                ask_idx += 1
                continue

            fill_qty = min(bid.remaining, ask.remaining)
            if fill_qty <= 0:
                break

            bid.remaining -= fill_qty
            ask.remaining -= fill_qty

            if bid.remaining <= 0:
                bid.status = OrderStatus.FILLED
                bid_idx += 1
            else:
                bid.status = OrderStatus.PARTIAL

            if ask.remaining <= 0:
                ask.status = OrderStatus.FILLED
                ask_idx += 1
            else:
                ask.status = OrderStatus.PARTIAL

            fill = self._make_fill(
                buyer=bid,
                seller=ask,
                price=clearing_price,
                quantity=fill_qty,
                aggressor_side="auction",
            )
            self.tape.record(fill)
            fills.append(fill)

        return fills

    def cancel(self, order_id: str) -> bool:
        """Cancel a resting order."""
        return self.book.cancel(order_id)

    def _record_fills(self, result: MatchResult, incoming: Order) -> list[Fill]:
        """Convert raw match results to Fill objects and record on tape."""
        fills: list[Fill] = []
        for aggressor, resting, price, qty in result.fills:
            if incoming.side == OrderSide.BUY:
                buyer, seller = incoming, resting
            else:
                buyer, seller = resting, incoming

            fill = self._make_fill(
                buyer=buyer,
                seller=seller,
                price=price,
                quantity=qty,
                aggressor_side=incoming.side.value,
            )
            self.tape.record(fill)
            fills.append(fill)
        return fills

    def _make_fill(
        self,
        buyer: Order,
        seller: Order,
        price: float,
        quantity: float,
        aggressor_side: str,
    ) -> Fill:
        self._fill_counter += 1
        return Fill(
            fill_id=f"F{self._fill_counter:06d}",
            ticker=self.ticker,
            buyer_order_id=buyer.order_id,
            seller_order_id=seller.order_id,
            buyer_agent_id=buyer.agent_id,
            seller_agent_id=seller.agent_id,
            buyer_persona_id=buyer.persona_id,
            seller_persona_id=seller.persona_id,
            price=price,
            quantity=quantity,
            timestamp=self._tick,
            aggressor_side=aggressor_side,
        )


__all__ = ["MatchingEngine"]
