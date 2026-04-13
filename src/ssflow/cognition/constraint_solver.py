"""Constraint solver: StructuredIntent -> ValidatedIntent or RejectedIntent.

Checks an agent's trading intent against hard constraints:
- Cash available for buys
- Holdings available for sells (minus T+1 locked)
- Position concentration limits
- Price limit bounds (A-share 涨跌停)
- Margin requirements for shorts

This is the "mechanism" layer that prevents agents from doing things they
can't actually do. The LLM expresses INTENT; this module enforces REALITY.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .intent_resolver import StructuredIntent, Urgency

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ValidatedIntent:
    """An intent that passed all constraint checks.

    `effective_quantity` is the actual shares/value the agent can trade,
    after clamping to available resources.
    """

    intent: StructuredIntent
    effective_shares: float       # clamped to what's actually available
    effective_value: float        # shares * estimated price
    estimated_price: float        # price the order generator should target
    pool: str                     # "cash" (buy) or "holdings" (sell) or "margin" (short)

    @property
    def side(self) -> str:
        return self.intent.side

    @property
    def urgency(self) -> Urgency:
        return self.intent.urgency


@dataclass(frozen=True)
class RejectedIntent:
    """An intent that failed constraint checks."""

    intent: StructuredIntent
    code: str
    reason: str


class ConstraintSolver:
    """Validates trading intents against portfolio and market constraints.

    Usage:
        solver = ConstraintSolver()
        result = solver.validate(
            intent=intent,
            cash=agent.cash,
            holdings=1000.0,
            t1_locked=200.0,
            last_price=45.50,
            upper_limit=50.05,
            lower_limit=40.95,
        )
        if isinstance(result, ValidatedIntent):
            # proceed to order generator
        else:
            # log rejection
    """

    def __init__(
        self,
        *,
        max_position_pct: float = 0.95,   # max fraction of capital in one instrument
        min_trade_value: float = 1000.0,   # minimum trade value (avoid dust orders)
        lot_size: int = 100,
    ) -> None:
        self.max_position_pct = max_position_pct
        self.min_trade_value = min_trade_value
        self.lot_size = lot_size

    def validate(
        self,
        intent: StructuredIntent,
        cash: float,
        holdings: float,         # shares held in target instrument
        t1_locked: float,        # shares locked by T+1 this round
        last_price: float,
        upper_limit: float,
        lower_limit: float,
        capital: float = 0.0,    # total NAV (for position limit)
        holdings_value: float = 0.0,  # current value of all holdings
    ) -> ValidatedIntent | RejectedIntent:
        """Validate an intent against all constraints.

        Returns ValidatedIntent if feasible, RejectedIntent if not.
        """
        if not intent.is_active:
            return ValidatedIntent(
                intent=intent,
                effective_shares=0.0,
                effective_value=0.0,
                estimated_price=last_price,
                pool="none",
            )

        if last_price <= 0:
            return RejectedIntent(intent, "NO_PRICE", "No valid price available")

        if intent.side == "buy":
            return self._validate_buy(intent, cash, last_price, upper_limit, capital, holdings_value)
        elif intent.side == "sell":
            return self._validate_sell(intent, holdings, t1_locked, last_price, lower_limit)
        elif intent.side == "short":
            return self._validate_short(intent, cash, last_price, lower_limit, capital, holdings_value)
        else:
            return RejectedIntent(intent, "UNKNOWN_SIDE", f"Unknown side: {intent.side}")

    def _validate_buy(
        self,
        intent: StructuredIntent,
        cash: float,
        last_price: float,
        upper_limit: float,
        capital: float,
        holdings_value: float,
    ) -> ValidatedIntent | RejectedIntent:
        """Validate a buy intent."""
        if cash <= 0:
            return RejectedIntent(intent, "NO_CASH", "No cash available for buying")

        # Position limit check
        if capital > 0:
            remaining_capacity = capital * self.max_position_pct - holdings_value
            if remaining_capacity <= 0:
                return RejectedIntent(
                    intent, "POSITION_LIMIT",
                    f"Position at {holdings_value/capital:.0%} of capital, limit is {self.max_position_pct:.0%}"
                )
            available_cash = min(cash, remaining_capacity)
        else:
            available_cash = cash

        # Compute target value and shares
        target_value = available_cash * intent.size_fraction
        if target_value < self.min_trade_value:
            return RejectedIntent(
                intent, "BELOW_MIN",
                f"Trade value {target_value:.0f} below minimum {self.min_trade_value:.0f}"
            )

        # Estimate execution price (apply price preference)
        est_price = last_price * (1.0 + intent.price_preference)
        est_price = min(est_price, upper_limit)  # can't buy above limit
        est_price = max(est_price, 0.01)

        shares = target_value / est_price
        # Round down to lot size
        shares = float(int(shares / self.lot_size) * self.lot_size)
        if shares <= 0:
            return RejectedIntent(intent, "ZERO_SHARES", "Computed shares rounds to 0")

        actual_value = shares * est_price

        return ValidatedIntent(
            intent=intent,
            effective_shares=shares,
            effective_value=actual_value,
            estimated_price=est_price,
            pool="cash",
        )

    def _validate_sell(
        self,
        intent: StructuredIntent,
        holdings: float,
        t1_locked: float,
        last_price: float,
        lower_limit: float,
    ) -> ValidatedIntent | RejectedIntent:
        """Validate a sell intent."""
        sellable = holdings - t1_locked
        if sellable <= 0:
            return RejectedIntent(
                intent, "NO_SELLABLE",
                f"No sellable shares (holdings={holdings:.0f}, T+1 locked={t1_locked:.0f})"
            )

        # Compute target shares
        target_shares = sellable * intent.size_fraction
        # Sells can be partial lots in A-share, but not less than 1 share
        target_shares = max(1.0, float(int(target_shares)))

        if target_shares > sellable:
            target_shares = sellable

        est_price = last_price * (1.0 + intent.price_preference)
        est_price = max(est_price, lower_limit)  # can't sell below limit
        actual_value = target_shares * est_price

        if actual_value < self.min_trade_value:
            # Still allow — selling even small amounts should be possible
            pass

        return ValidatedIntent(
            intent=intent,
            effective_shares=target_shares,
            effective_value=actual_value,
            estimated_price=est_price,
            pool="holdings",
        )

    def _validate_short(
        self,
        intent: StructuredIntent,
        cash: float,
        last_price: float,
        lower_limit: float,
        capital: float,
        holdings_value: float,
    ) -> ValidatedIntent | RejectedIntent:
        """Validate a short sell intent.

        Simplified: requires margin (cash collateral). Real A-share short
        selling has additional restrictions (restricted list, borrow
        availability, margin rates) that we don't model yet.
        """
        if cash <= 0:
            return RejectedIntent(intent, "NO_MARGIN", "No cash for margin collateral")

        # Short selling requires ~130% margin in A-share
        margin_rate = 1.3
        available_for_short = cash / margin_rate
        target_value = available_for_short * intent.size_fraction

        if target_value < self.min_trade_value:
            return RejectedIntent(intent, "BELOW_MIN", "Short value below minimum")

        est_price = last_price * (1.0 + intent.price_preference)
        est_price = max(est_price, lower_limit)

        shares = target_value / est_price
        shares = float(int(shares / self.lot_size) * self.lot_size)
        if shares <= 0:
            return RejectedIntent(intent, "ZERO_SHARES", "Computed short shares rounds to 0")

        return ValidatedIntent(
            intent=intent,
            effective_shares=shares,
            effective_value=shares * est_price,
            estimated_price=est_price,
            pool="margin",
        )


__all__ = ["ConstraintSolver", "RejectedIntent", "ValidatedIntent"]
