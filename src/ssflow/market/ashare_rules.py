"""A-share exchange rules — 涨跌停, T+1, tick size, session phases.

Ported from the legacy limit_board.py but redesigned for order-level
enforcement: rules are checked BEFORE an order enters the book, not
applied as a post-hoc clamp on aggregate price impact.

This module is pure logic — no I/O, no LLM calls, no state mutation
beyond the T1Ledger and session tracker.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from enum import Enum

log = logging.getLogger(__name__)

# ─── Constants ───

TICK_SIZE: float = 0.01  # A-share minimum price increment (分)
LOT_SIZE: int = 100      # A-share minimum trade unit (手 = 100 shares)


class BoardType(str, Enum):
    """A-share board classification determining price limit rules."""
    NORMAL = "normal"          # 主板 (SSE/SZSE main board): +/-10%
    CHINEXT = "chinext"        # 创业板 (300xxx): +/-20%
    STAR = "star"              # 科创板 (688xxx): +/-20%
    ST = "st"                  # ST / *ST: +/-5%
    IPO_MAIN = "ipo_main"     # 主板 IPO first day: +44% / -36%
    IPO_REFORM = "ipo_reform" # 科创板/创业板 IPO first 5 days: no limit


LIMIT_PCT: dict[BoardType, tuple[float, float]] = {
    BoardType.NORMAL:     (+0.10, -0.10),
    BoardType.CHINEXT:    (+0.20, -0.20),
    BoardType.STAR:       (+0.20, -0.20),
    BoardType.ST:         (+0.05, -0.05),
    BoardType.IPO_MAIN:   (+0.44, -0.36),
    BoardType.IPO_REFORM: (+float("inf"), -float("inf")),
}


class SessionPhase(str, Enum):
    """Intra-day session phases for A-share markets."""
    PRE_OPEN = "pre_open"             # 09:15-09:20 (orders accepted, can cancel)
    CALL_AUCTION = "call_auction"     # 09:20-09:25 (orders accepted, NO cancel)
    MATCH_PAUSE = "match_pause"       # 09:25-09:30 (no new orders, match at open)
    CONTINUOUS_AM = "continuous_am"   # 09:30-11:30 (continuous trading)
    LUNCH_BREAK = "lunch_break"       # 11:30-13:00
    CONTINUOUS_PM = "continuous_pm"   # 13:00-14:57
    CLOSING_CALL = "closing_call"     # 14:57-15:00 (closing call auction)
    CLOSED = "closed"                 # 15:00+


def round_tick(price: float, tick: float = TICK_SIZE) -> float:
    """Round to the nearest tick size (A-share: 0.01 CNY)."""
    return round(price / tick) * tick


def round_lot(shares: float, lot: int = LOT_SIZE) -> float:
    """Round down to the nearest lot size (A-share: 100 shares).

    For sell orders, allows partial lots (卖出不受100股限制，但买入必须是100股整数倍).
    This function always rounds down (floor) since you can't trade fractional lots.
    """
    return float(int(shares / lot) * lot)


def infer_board_type(ticker: str) -> BoardType:
    """Infer board type from ticker code.

    Heuristic based on A-share ticker conventions:
      688xxx.SH -> STAR, 300xxx.SZ -> ChiNext, else -> NORMAL.
    For IPO/ST detection the caller must check independently.
    """
    code = ticker.split(".")[0]
    if code.startswith("688"):
        return BoardType.STAR
    if code.startswith("300"):
        return BoardType.CHINEXT
    return BoardType.NORMAL


# ─── A-Share Rules Engine ───


@dataclass
class RejectionReason:
    """Why an order was rejected."""
    code: str
    message: str


@dataclass
class AShareRules:
    """Enforces A-share trading rules at the order level.

    Created once per instrument per trading day. Checks each incoming order
    against price limits, T+1 constraints, session rules, and lot sizes.

    Unlike the legacy LimitBoard (which clamped aggregate deltas post-hoc),
    this rejects individual orders BEFORE they enter the book.

    Usage:
        rules = AShareRules(
            ticker="002594.SZ",
            prev_close=218.50,
            board_type=BoardType.NORMAL,
        )
        rejection = rules.validate_order(order, agent_holdings, t1_ledger, round_idx)
        if rejection:
            order.status = OrderStatus.REJECTED
        else:
            exchange.submit(order)
    """

    ticker: str
    prev_close: float
    board_type: BoardType = BoardType.NORMAL

    # Computed at init
    upper_limit: float = 0.0
    lower_limit: float = 0.0
    session_phase: SessionPhase = SessionPhase.CONTINUOUS_AM

    def __post_init__(self) -> None:
        if isinstance(self.board_type, str):
            self.board_type = BoardType(self.board_type)

        upper_pct, lower_pct = LIMIT_PCT[self.board_type]

        if math.isinf(upper_pct):
            self.upper_limit = self.prev_close * 10.0
            self.lower_limit = TICK_SIZE
        else:
            self.upper_limit = round_tick(self.prev_close * (1.0 + upper_pct))
            self.lower_limit = max(TICK_SIZE, round_tick(self.prev_close * (1.0 + lower_pct)))

    def validate_order(
        self,
        side: str,
        order_type: str,
        price: float,
        quantity: float,
        agent_id: str,
        agent_holdings: float,
        agent_cash: float,
        t1_locked: float,
        *,
        is_short: bool = False,
    ) -> RejectionReason | None:
        """Validate an order against A-share rules.

        Returns None if the order is valid, or a RejectionReason if rejected.

        Args:
            side: "buy" or "sell"
            order_type: "limit" or "market"
            price: limit price (ignored for market orders)
            quantity: shares to trade
            agent_id: for logging
            agent_holdings: current shares held in this ticker
            agent_cash: available cash
            t1_locked: shares locked by T+1 (bought this session, can't sell)
            is_short: True if this is a margin short sell
        """
        # Session check
        if self.session_phase in (SessionPhase.CLOSED, SessionPhase.LUNCH_BREAK):
            return RejectionReason("SESSION_CLOSED", "Trading not allowed in current session phase")

        if self.session_phase == SessionPhase.MATCH_PAUSE:
            return RejectionReason("MATCH_PAUSE", "No new orders during 09:25-09:30 match pause")

        # Quantity check
        if quantity <= 0:
            return RejectionReason("ZERO_QTY", "Order quantity must be positive")

        # Buy-side lot size check (buys must be in lots of 100)
        if side == "buy" and quantity % LOT_SIZE != 0:
            return RejectionReason(
                "LOT_SIZE",
                f"Buy orders must be in lots of {LOT_SIZE} shares, got {quantity}"
            )

        # Price limit check for limit orders
        if order_type == "limit":
            rounded = round_tick(price)
            if abs(rounded - price) > TICK_SIZE * 0.5:
                return RejectionReason(
                    "TICK_SIZE",
                    f"Price {price} not on tick grid (nearest: {rounded})"
                )

            if rounded > self.upper_limit:
                return RejectionReason(
                    "PRICE_LIMIT_UP",
                    f"Price {rounded} exceeds upper limit {self.upper_limit} "
                    f"(prev_close={self.prev_close}, board={self.board_type.value})"
                )
            if rounded < self.lower_limit:
                return RejectionReason(
                    "PRICE_LIMIT_DOWN",
                    f"Price {rounded} below lower limit {self.lower_limit} "
                    f"(prev_close={self.prev_close}, board={self.board_type.value})"
                )

        # Sell-side checks
        if side == "sell" and not is_short:
            sellable = agent_holdings - t1_locked
            if sellable <= 0:
                return RejectionReason(
                    "NO_SELLABLE",
                    f"No sellable shares (holdings={agent_holdings}, T+1 locked={t1_locked})"
                )
            if quantity > sellable:
                return RejectionReason(
                    "EXCEEDS_SELLABLE",
                    f"Sell qty {quantity} exceeds sellable {sellable} "
                    f"(holdings={agent_holdings}, T+1 locked={t1_locked})"
                )

        # Buy-side cash check (rough estimate for limit orders)
        if side == "buy":
            est_cost = price * quantity if order_type == "limit" else self.upper_limit * quantity
            if est_cost > agent_cash * 1.001:  # 0.1% tolerance for rounding
                return RejectionReason(
                    "INSUFFICIENT_CASH",
                    f"Estimated cost {est_cost:.0f} exceeds cash {agent_cash:.0f}"
                )

        # Short sell check (融券)
        if is_short:
            # Simplified: A-share short selling requires margin account,
            # restricted list, borrow availability, etc. For now just log.
            log.debug("Short sell order from %s — margin checks not fully modeled", agent_id)

        return None

    def clamp_price(self, price: float) -> float:
        """Clamp a price to within the day's limit bounds."""
        return max(self.lower_limit, min(self.upper_limit, round_tick(price)))

    def set_session(self, phase: SessionPhase) -> None:
        """Update the current session phase."""
        self.session_phase = phase

    def to_serializable(self) -> dict:
        return {
            "ticker": self.ticker,
            "prev_close": self.prev_close,
            "board_type": self.board_type.value,
            "upper_limit": self.upper_limit,
            "lower_limit": self.lower_limit,
            "session_phase": self.session_phase.value,
        }


# ─── T+1 Ledger ───


@dataclass
class T1Ledger:
    """Tracks T+1 sell constraint: shares bought in round R cannot be sold
    until round R+1.

    Ported from legacy limit_board.T1Ledger with identical semantics.
    """

    _buys: dict[tuple[str, str], list[tuple[int, float]]] = field(
        default_factory=dict
    )

    def record_buy(self, agent_id: str, ticker: str, shares: float, round_idx: int) -> None:
        if shares <= 0:
            return
        key = (agent_id, ticker)
        if key not in self._buys:
            self._buys[key] = []
        self._buys[key].append((round_idx, shares))

    def locked_shares(self, agent_id: str, ticker: str, current_round: int) -> float:
        """Shares bought this round — locked by T+1."""
        key = (agent_id, ticker)
        return sum(s for r, s in self._buys.get(key, []) if r == current_round)

    def sellable_shares(
        self, agent_id: str, ticker: str, current_round: int, total_holdings: float
    ) -> float:
        """Holdings minus T+1 locked shares."""
        locked = self.locked_shares(agent_id, ticker, current_round)
        return max(0.0, total_holdings - locked)

    def advance_day(self) -> None:
        """Clear all entries at the start of a new trading day."""
        self._buys.clear()


__all__ = [
    "AShareRules",
    "BoardType",
    "LIMIT_PCT",
    "LOT_SIZE",
    "RejectionReason",
    "SessionPhase",
    "T1Ledger",
    "TICK_SIZE",
    "infer_board_type",
    "round_lot",
    "round_tick",
]
