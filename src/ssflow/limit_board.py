"""A-share limit-board engine — 涨跌停板 / T+1 / gap-open mechanics.

Models the distinctive microstructure rules of China's A-share market that
differentiate it from other equity markets:

  1. **Daily price limits** (涨跌停板): ±10% (main board), ±20% (ChiNext/STAR),
     ±5% (ST/*ST), with special IPO rules.
  2. **Limit-board states**: NORMAL → LIMIT_UP / LIMIT_DOWN, with sub-states
     for "one-word boards" (封死涨停/跌停 — opened at limit, never traded away).
  3. **Unfilled order tracking** (封单): when at limit, one side of the book
     can't be filled. The unfilled volume is tracked as queue depth.
  4. **T+1 sell constraint**: shares bought today cannot be sold until the
     next trading day. Enforced via `T1Ledger`.
  5. **Gap-open modeling**: overnight sentiment can shift the opening price
     away from previous close (within limit bounds).
  6. **Seal strength** (封单强度): unfilled_volume / ADV — measures how firmly
     the limit board is sealed.

Integration with the engine:

    from .limit_board import LimitBoard, T1Ledger, BoardState

    board = LimitBoard(prev_close=50.0, board_type="normal")
    clamped = board.clamp_delta(raw_delta_pct)
    board.update(clamped, buy_volume=..., sell_volume=...)
    strength = board.seal_strength(adv)

    ledger = T1Ledger()
    ledger.record_buy("agent_1", "000001.SZ", 1000, round_idx=3)
    sellable = ledger.sellable_shares("agent_1", "000001.SZ", round_idx=3)
    # → 0 (T+1: can't sell same day)
    sellable = ledger.sellable_shares("agent_1", "000001.SZ", round_idx=4)
    # → 1000

Source references:
    - SZSE/SSE trading rules (深交所/上交所交易规则)
    - CSRC "涨跌幅限制制度" (price limit system)
    - 科创板/创业板注册制改革 (registration reform, ±20%)
    - IPO first-day rules (主板+44%/-36%, 科创板/创业板前5日不设涨跌幅)
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

log = logging.getLogger(__name__)


# ─────────────────────── Board type → limit percentages ───────────────────────


class BoardType(str, Enum):
    """A-share board classification determining price limit rules."""

    NORMAL = "normal"          # 主板 (SSE/SZSE main board): ±10%
    CHINEXT = "chinext"        # 创业板 (ChiNext, 300xxx): ±20%
    STAR = "star"              # 科创板 (STAR Market, 688xxx): ±20%
    ST = "st"                  # ST / *ST stocks: ±5%
    IPO_MAIN = "ipo_main"     # 主板 IPO first day: +44% / -36%
    IPO_REFORM = "ipo_reform" # 科创板/创业板 IPO first 5 days: no limit


# Limit percentages: (upper_pct, lower_pct) relative to previous close.
# Positive upper means price can rise by that fraction; negative lower means
# price can fall by that fraction.
LIMIT_PCT: dict[BoardType, tuple[float, float]] = {
    BoardType.NORMAL:     (+0.10, -0.10),
    BoardType.CHINEXT:    (+0.20, -0.20),
    BoardType.STAR:       (+0.20, -0.20),
    BoardType.ST:         (+0.05, -0.05),
    BoardType.IPO_MAIN:   (+0.44, -0.36),
    BoardType.IPO_REFORM: (+float("inf"), -float("inf")),  # no limit
}


def resolve_board_type(raw: str) -> BoardType:
    """Parse a board type string, accepting common aliases.

    Accepts: "normal", "chinext", "star", "st", "ipo_main", "ipo_reform",
    "创业板", "科创板", "主板", etc.
    """
    normalized = raw.strip().lower().replace(" ", "_").replace("-", "_")
    aliases: dict[str, BoardType] = {
        "normal": BoardType.NORMAL,
        "main": BoardType.NORMAL,
        "主板": BoardType.NORMAL,
        "chinext": BoardType.CHINEXT,
        "创业板": BoardType.CHINEXT,
        "gem": BoardType.CHINEXT,
        "star": BoardType.STAR,
        "科创板": BoardType.STAR,
        "star_market": BoardType.STAR,
        "st": BoardType.ST,
        "*st": BoardType.ST,
        "ipo_main": BoardType.IPO_MAIN,
        "ipo_reform": BoardType.IPO_REFORM,
        "ipo_chinext": BoardType.IPO_REFORM,
        "ipo_star": BoardType.IPO_REFORM,
    }
    if normalized in aliases:
        return aliases[normalized]
    # Try direct enum value match
    try:
        return BoardType(normalized)
    except ValueError:
        log.warning("Unknown board type %r, defaulting to NORMAL", raw)
        return BoardType.NORMAL


# ─────────────────────── Board state ───────────────────────


class BoardState(str, Enum):
    """Current limit-board state for one instrument in one trading day."""

    NORMAL = "normal"
    LIMIT_UP = "limit_up"           # 涨停: price at upper limit
    LIMIT_DOWN = "limit_down"       # 跌停: price at lower limit
    ONE_WORD_UP = "one_word_up"     # 一字涨停 (封死): opened at limit-up, never below
    ONE_WORD_DOWN = "one_word_down" # 一字跌停 (封死): opened at limit-down, never above


# ─────────────────────── LimitBoard ───────────────────────


@dataclass
class LimitBoard:
    """Per-instrument, per-day limit-board tracker.

    Created at the start of each trading day with the previous close.
    Tracks state transitions, unfilled volume, and seal strength across
    rounds within a single day.

    Usage:
        board = LimitBoard(prev_close=50.0, board_type="normal")
        # Each round:
        clamped = board.clamp_delta(raw_delta_pct)
        board.update(clamped, buy_volume=1e9, sell_volume=8e8)
        print(board.state)          # BoardState.NORMAL
        print(board.unfilled_volume) # 0.0
    """

    prev_close: float
    board_type: str | BoardType = BoardType.NORMAL

    # ── Computed at init ──
    upper_limit: float = 0.0
    lower_limit: float = 0.0
    upper_pct: float = 0.0
    lower_pct: float = 0.0

    # ── Mutable state (updated each round within the day) ──
    state: BoardState = BoardState.NORMAL
    current_price: float = 0.0
    unfilled_volume: float = 0.0      # 封单量: queued volume that can't fill
    _opened_at_limit_up: bool = False  # True if first round opened at upper
    _opened_at_limit_down: bool = False
    _ever_below_upper: bool = False    # True if price was ever below upper limit
    _ever_above_lower: bool = False    # True if price was ever above lower limit
    _round_count: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.board_type, str):
            self.board_type = resolve_board_type(self.board_type)

        upper_pct, lower_pct = LIMIT_PCT[self.board_type]
        self.upper_pct = upper_pct
        self.lower_pct = lower_pct

        # A-share price limits are computed from previous close, rounded to
        # nearest 0.01 (tick size). For IPO_REFORM (no limit), we use large
        # bounds to avoid special-casing everywhere.
        if math.isinf(upper_pct):
            self.upper_limit = self.prev_close * 10.0   # effectively no limit
            self.lower_limit = 0.01                      # price floor
        else:
            self.upper_limit = _round_tick(self.prev_close * (1.0 + upper_pct))
            self.lower_limit = max(0.01, _round_tick(self.prev_close * (1.0 + lower_pct)))

        self.current_price = self.prev_close

    def clamp_delta(self, raw_delta_pct: float) -> float:
        """Clamp a raw price-change percentage to the day's limit bounds.

        Takes the raw delta_pct from Kyle (e.g., +0.12 for +12%) and returns
        the clamped value that keeps the price within [lower_limit, upper_limit].

        This is the primary integration point with market_dynamics.py:
            raw = compute_price_impact(...)
            clamped = board.clamp_delta(raw)
            new_price = current_price * (1.0 + clamped)
        """
        if self.current_price <= 0:
            return raw_delta_pct

        proposed = self.current_price * (1.0 + raw_delta_pct)

        if proposed >= self.upper_limit:
            clamped_price = self.upper_limit
        elif proposed <= self.lower_limit:
            clamped_price = self.lower_limit
        else:
            return raw_delta_pct  # within bounds, no clamping

        clamped_delta = (clamped_price / self.current_price) - 1.0

        if abs(clamped_delta - raw_delta_pct) > 1e-9:
            log.info(
                "LimitBoard clamp: raw %.2f%% → %.2f%% "
                "(price %.2f → %.2f, limits [%.2f, %.2f])",
                raw_delta_pct * 100, clamped_delta * 100,
                self.current_price, clamped_price,
                self.lower_limit, self.upper_limit,
            )

        return clamped_delta

    def update(
        self,
        applied_delta_pct: float,
        buy_volume: float = 0.0,
        sell_volume: float = 0.0,
    ) -> BoardState:
        """Update state after a round's price move is applied.

        Args:
            applied_delta_pct: the clamped delta that was actually applied.
            buy_volume: total buy-side order volume this round (in currency).
            sell_volume: total sell-side order volume this round (in currency).

        Returns:
            The new BoardState.
        """
        self._round_count += 1
        new_price = self.current_price * (1.0 + applied_delta_pct)
        # Snap to limits when very close (avoids floating-point drift)
        if abs(new_price - self.upper_limit) < 0.005:
            new_price = self.upper_limit
        if abs(new_price - self.lower_limit) < 0.005:
            new_price = self.lower_limit

        self.current_price = new_price

        at_upper = abs(self.current_price - self.upper_limit) < 0.005
        at_lower = abs(self.current_price - self.lower_limit) < 0.005

        # Track whether price ever moved away from limits (for one-word detection)
        if not at_upper:
            self._ever_below_upper = True
        if not at_lower:
            self._ever_above_lower = True

        # First round: detect opening at limit
        if self._round_count == 1:
            if at_upper:
                self._opened_at_limit_up = True
            if at_lower:
                self._opened_at_limit_down = True

        # Compute unfilled volume
        if at_upper:
            # At limit-up: buy orders queue, only sells fill.
            # Unfilled = excess buy demand over sell supply.
            self.unfilled_volume = max(0.0, buy_volume - sell_volume)
            if self._opened_at_limit_up and not self._ever_below_upper:
                self.state = BoardState.ONE_WORD_UP
            else:
                self.state = BoardState.LIMIT_UP
        elif at_lower:
            # At limit-down: sell orders queue, only buys fill.
            # Unfilled = excess sell supply over buy demand.
            self.unfilled_volume = max(0.0, sell_volume - buy_volume)
            if self._opened_at_limit_down and not self._ever_above_lower:
                self.state = BoardState.ONE_WORD_DOWN
            else:
                self.state = BoardState.LIMIT_DOWN
        else:
            self.unfilled_volume = 0.0
            self.state = BoardState.NORMAL

        return self.state

    def seal_strength(self, adv: float) -> float:
        """Seal strength = unfilled_volume / ADV.

        Measures how firmly the limit is sealed:
          - < 0.5: weak seal, likely to break
          - 0.5 - 2.0: moderate seal
          - > 2.0: very strong, extremely unlikely to break

        Returns 0.0 when not at limit (or adv <= 0).
        """
        if adv <= 0 or self.unfilled_volume <= 0:
            return 0.0
        return self.unfilled_volume / adv

    @property
    def at_limit(self) -> bool:
        """True if the board is at either limit (up or down)."""
        return self.state in (
            BoardState.LIMIT_UP,
            BoardState.LIMIT_DOWN,
            BoardState.ONE_WORD_UP,
            BoardState.ONE_WORD_DOWN,
        )

    @property
    def at_limit_up(self) -> bool:
        return self.state in (BoardState.LIMIT_UP, BoardState.ONE_WORD_UP)

    @property
    def at_limit_down(self) -> bool:
        return self.state in (BoardState.LIMIT_DOWN, BoardState.ONE_WORD_DOWN)

    def to_serializable(self) -> dict[str, Any]:
        """Serialize for round records / event bus."""
        return {
            "state": self.state.value,
            "board_type": self.board_type.value if isinstance(self.board_type, BoardType) else str(self.board_type),
            "prev_close": self.prev_close,
            "current_price": self.current_price,
            "upper_limit": self.upper_limit,
            "lower_limit": self.lower_limit,
            "unfilled_volume": self.unfilled_volume,
            "seal_strength_raw": self.unfilled_volume,  # caller divides by ADV
            "round_count": self._round_count,
        }


# ─────────────────────── T+1 Sell Constraint Ledger ───────────────────────


@dataclass
class T1Ledger:
    """Tracks T+1 sell constraint: shares bought in round R cannot be sold
    until round R+1 (or a later trading day).

    In A-share markets, this is a hard regulatory constraint. The ledger
    records each buy with its round index and computes sellable shares
    by excluding same-round purchases.

    Usage:
        ledger = T1Ledger()
        ledger.record_buy("agent_1", "000001.SZ", shares=1000, round_idx=3)
        ledger.sellable_shares("agent_1", "000001.SZ", round_idx=3)
        # → 0.0 (bought this round, can't sell yet)
        ledger.sellable_shares("agent_1", "000001.SZ", round_idx=4)
        # → 1000.0

    The `total_holdings` for computing sellable shares must be supplied by
    the caller (from TraderInstance.holdings) — this ledger only tracks the
    T+1 lockup, not the total portfolio.
    """

    # {(agent_id, ticker): [(round_idx, shares), ...]}
    _buys: dict[tuple[str, str], list[tuple[int, float]]] = field(
        default_factory=dict
    )

    def record_buy(
        self,
        agent_id: str,
        ticker: str,
        shares: float,
        round_idx: int,
    ) -> None:
        """Record a buy that is subject to T+1 lockup."""
        if shares <= 0:
            return
        key = (agent_id, ticker)
        if key not in self._buys:
            self._buys[key] = []
        self._buys[key].append((round_idx, shares))

    def locked_shares(
        self,
        agent_id: str,
        ticker: str,
        current_round: int,
    ) -> float:
        """Shares locked by T+1: bought in `current_round`, not yet sellable."""
        key = (agent_id, ticker)
        entries = self._buys.get(key, [])
        return sum(shares for r, shares in entries if r == current_round)

    def sellable_shares(
        self,
        agent_id: str,
        ticker: str,
        current_round: int,
        total_holdings: float | None = None,
    ) -> float:
        """Shares that can be sold in `current_round`.

        If total_holdings is provided, returns min(total_holdings - locked, total_holdings).
        Otherwise, returns total bought in prior rounds (which is a lower bound
        on sellable — the agent may hold shares from before the simulation too).
        """
        key = (agent_id, ticker)
        entries = self._buys.get(key, [])

        locked = sum(shares for r, shares in entries if r == current_round)

        if total_holdings is not None:
            return max(0.0, total_holdings - locked)

        # Without total_holdings, return sum of shares from prior rounds
        return sum(shares for r, shares in entries if r < current_round)

    def advance_day(self) -> None:
        """Clear all entries — call at the start of a new trading day.

        After advancing, all previously locked shares become sellable.
        """
        self._buys.clear()

    def to_serializable(self) -> dict[str, Any]:
        """Serialize for debugging / event bus."""
        return {
            str(k): [(r, s) for r, s in v]
            for k, v in self._buys.items()
        }


# ─────────────────────── Gap-open modeling ───────────────────────


def gap_open(
    prev_close: float,
    overnight_sentiment: float,
    board_type: str | BoardType = BoardType.NORMAL,
    volatility: float = 0.02,
) -> float:
    """Compute a gap-open price at the start of a trading day.

    Models the common A-share pattern where overnight news (earnings, policy,
    global markets) causes the opening price to gap from the previous close.

    The gap is bounded by the day's price limits — you can't gap beyond
    涨停/跌停.

    Args:
        prev_close: previous trading day's closing price.
        overnight_sentiment: directional signal in [-1, +1].
            -1 = extremely negative, 0 = neutral, +1 = extremely positive.
        board_type: determines price limit bounds.
        volatility: base gap magnitude as a fraction of price (default 2%).
            The actual gap = volatility * sentiment, so a sentiment of +1
            with volatility 0.02 produces a +2% gap.

    Returns:
        The opening price, clamped to the day's limit bounds.

    Example:
        >>> gap_open(50.0, overnight_sentiment=-0.8, volatility=0.03)
        48.80  # -2.4% gap, within ±10%
    """
    if isinstance(board_type, str):
        board_type = resolve_board_type(board_type)

    # Clamp sentiment to [-1, +1]
    sentiment = max(-1.0, min(1.0, overnight_sentiment))

    # Raw gap: proportional to sentiment * volatility
    raw_gap_pct = sentiment * volatility

    # Compute limit bounds
    upper_pct, lower_pct = LIMIT_PCT[board_type]

    if math.isinf(upper_pct):
        # No limit (IPO reform) — still enforce a positive price
        clamped_pct = raw_gap_pct
    else:
        clamped_pct = max(lower_pct, min(upper_pct, raw_gap_pct))

    open_price = prev_close * (1.0 + clamped_pct)
    open_price = max(0.01, _round_tick(open_price))

    if abs(raw_gap_pct - clamped_pct) > 1e-9:
        log.info(
            "gap_open: sentiment=%.2f, raw_gap=%.2f%% clamped to %.2f%% "
            "(prev_close=%.2f → open=%.2f)",
            sentiment, raw_gap_pct * 100, clamped_pct * 100,
            prev_close, open_price,
        )

    return open_price


# ─────────────────────── Helpers ───────────────────────


def _round_tick(price: float, tick: float = 0.01) -> float:
    """Round to the nearest tick size (A-share: 0.01 CNY)."""
    return round(price / tick) * tick


def infer_board_type(ticker: str) -> BoardType:
    """Infer board type from a ticker code.

    Heuristic based on A-share ticker conventions:
      - 688xxx.SH → STAR (科创板)
      - 300xxx.SZ → ChiNext (创业板)
      - ST prefix in name → ST
      - Everything else → NORMAL (主板)

    For IPO detection, the caller must check independently (requires
    listing date context, not just ticker).
    """
    # Strip exchange suffix
    code = ticker.split(".")[0]

    if code.startswith("688"):
        return BoardType.STAR
    if code.startswith("300"):
        return BoardType.CHINEXT
    # 600xxx, 601xxx, 603xxx (SSE main), 000xxx, 001xxx, 002xxx (SZSE main)
    return BoardType.NORMAL


# ─────────────────────── Exports ───────────────────────


__all__ = [
    "BoardState",
    "BoardType",
    "LIMIT_PCT",
    "LimitBoard",
    "T1Ledger",
    "gap_open",
    "infer_board_type",
    "resolve_board_type",
]
