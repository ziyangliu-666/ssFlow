"""Order generator: ValidatedIntent -> concrete LOB orders.

Converts a validated intent into one or more orders suitable for the
Exchange. Handles the translation from "I want to buy 30% of my cash
patiently" to "limit buy 500 shares @ 45.48" (one tick below best ask).

Order type selection:
- IMMEDIATE urgency -> market order
- PATIENT urgency -> limit order at estimated price
- TWAP urgency -> sequence of smaller limit orders across rounds
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .constraint_solver import ValidatedIntent
from .intent_resolver import Urgency
from ..market.ashare_rules import round_tick, LOT_SIZE
from ..market.order_book import OrderSide, OrderType

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class OrderSpec:
    """A concrete order ready to submit to the Exchange.

    This is the output of the cognition pipeline — the Exchange's
    submit_order method accepts these fields directly.
    """

    agent_id: str
    persona_id: str
    side: str                    # "buy" or "sell"
    order_type: str              # "limit" or "market"
    price: float                 # limit price (0 for market)
    quantity: float              # shares
    is_short: bool = False
    twap_round: int = 0          # which slice of a TWAP (0 = not TWAP)
    twap_total_rounds: int = 1   # total TWAP rounds


class OrderGenerator:
    """Converts validated intents to concrete order specs.

    Usage:
        gen = OrderGenerator()
        specs = gen.generate(validated_intent, last_price, best_bid, best_ask)
        for spec in specs:
            exchange.submit_order(**spec.__dict__)
    """

    def __init__(
        self,
        *,
        tick_offset: int = 1,         # limit orders placed N ticks from market
        twap_max_slices: int = 5,
    ) -> None:
        self.tick_offset = tick_offset
        self.twap_max_slices = twap_max_slices

    def generate(
        self,
        validated: ValidatedIntent,
        last_price: float,
        best_bid: float | None = None,
        best_ask: float | None = None,
        *,
        current_twap_round: int = 0,
    ) -> list[OrderSpec]:
        """Generate order spec(s) from a validated intent.

        Returns a list because TWAP intents produce one order per slice.
        For non-TWAP, returns a single-element list.
        """
        if validated.effective_shares <= 0:
            return []

        side = validated.side
        if side == "short":
            side = "sell"  # shorts are sells on the book
            is_short = True
        else:
            is_short = False

        urgency = validated.urgency

        if urgency == Urgency.IMMEDIATE:
            return [self._market_order(validated, side, is_short)]
        elif urgency == Urgency.TWAP:
            return self._twap_slice(validated, side, is_short, last_price, best_bid, best_ask, current_twap_round)
        else:
            # PATIENT or default
            return [self._limit_order(validated, side, is_short, last_price, best_bid, best_ask)]

    def _market_order(
        self,
        validated: ValidatedIntent,
        side: str,
        is_short: bool,
    ) -> OrderSpec:
        """Generate a market order (immediate execution)."""
        return OrderSpec(
            agent_id=validated.intent.agent_id,
            persona_id=validated.intent.persona_id,
            side=side,
            order_type="market",
            price=0.0,
            quantity=validated.effective_shares,
            is_short=is_short,
        )

    def _limit_order(
        self,
        validated: ValidatedIntent,
        side: str,
        is_short: bool,
        last_price: float,
        best_bid: float | None,
        best_ask: float | None,
    ) -> OrderSpec:
        """Generate a patient limit order.

        Buy: place at best_ask - tick_offset (or estimated price)
        Sell: place at best_bid + tick_offset (or estimated price)
        """
        tick = 0.01  # A-share tick size

        if side == "buy":
            if best_ask is not None:
                price = round_tick(best_ask - self.tick_offset * tick)
            else:
                price = round_tick(validated.estimated_price)
        else:
            if best_bid is not None:
                price = round_tick(best_bid + self.tick_offset * tick)
            else:
                price = round_tick(validated.estimated_price)

        price = max(tick, price)

        return OrderSpec(
            agent_id=validated.intent.agent_id,
            persona_id=validated.intent.persona_id,
            side=side,
            order_type="limit",
            price=price,
            quantity=validated.effective_shares,
            is_short=is_short,
        )

    def _twap_slice(
        self,
        validated: ValidatedIntent,
        side: str,
        is_short: bool,
        last_price: float,
        best_bid: float | None,
        best_ask: float | None,
        current_round: int,
    ) -> list[OrderSpec]:
        """Generate one TWAP slice for the current round.

        Splits the total quantity evenly across twap_rounds.
        Returns a single-element list (one slice per call).
        """
        n_rounds = min(validated.intent.twap_rounds, self.twap_max_slices)
        if n_rounds <= 0:
            n_rounds = 1

        slice_shares = validated.effective_shares / n_rounds
        # Round to lot size
        slice_shares = float(int(slice_shares / LOT_SIZE) * LOT_SIZE)
        if slice_shares <= 0:
            slice_shares = float(LOT_SIZE)  # at least one lot

        # Place as limit order at reasonable price
        tick = 0.01
        if side == "buy":
            price = round_tick((best_ask or last_price) - self.tick_offset * tick)
        else:
            price = round_tick((best_bid or last_price) + self.tick_offset * tick)
        price = max(tick, price)

        return [OrderSpec(
            agent_id=validated.intent.agent_id,
            persona_id=validated.intent.persona_id,
            side=side,
            order_type="limit",
            price=price,
            quantity=slice_shares,
            is_short=is_short,
            twap_round=current_round,
            twap_total_rounds=n_rounds,
        )]


__all__ = ["OrderGenerator", "OrderSpec"]
