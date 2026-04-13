"""Background liquidity agents — provide realistic market depth without LLM calls.

These agents continuously submit orders to the exchange, creating the
liquidity that LLM-driven strategic agents trade against. Without them,
the order book would be empty between LLM decisions and every order would
walk the entire book.

Agent types:
  - ZeroIntelligence (ZI): random limit orders around fair value.
    Models uninformed retail flow and provides base liquidity.
  - MarketMaker (MM): two-sided quotes maintaining a spread.
    Models designated market makers and HFT liquidity provision.
  - MeanReversion (MR): trades against price deviations from anchor.
    Models institutional value investors who buy dips and sell rallies.

All agents are deterministic given a seed — no LLM, no network calls.
They run every tick and are very cheap (~1μs per order).
"""

from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass, field

from .exchange import Exchange
from .order_book import OrderSide
from .ashare_rules import round_tick, round_lot, LOT_SIZE

log = logging.getLogger(__name__)


@dataclass
class ZeroIntelligence:
    """Zero-intelligence agent: random limit orders around fair value.

    Provides base liquidity by placing limit orders uniformly distributed
    around the current price. Orders are placed with random TTL and
    automatically cancelled when stale.

    Parameters:
        arrival_rate: probability of submitting an order per tick (0-1)
        price_range_pct: orders placed within +/-this% of last price
        order_size_range: (min_lots, max_lots) random size in lots
        cancel_after_ticks: cancel resting orders older than this
    """

    agent_id: str = "ZI_001"
    persona_id: str = "__zi_background__"
    arrival_rate: float = 0.3
    price_range_pct: float = 0.02   # +/-2% of last price
    order_size_range: tuple[int, int] = (1, 20)  # lots (100 shares each)
    cancel_after_ticks: int = 50
    cash: float = float("inf")      # unlimited cash (background)
    holdings: float = float("inf")  # unlimited inventory

    _rng: random.Random = field(default_factory=lambda: random.Random(42))
    _active_orders: list[tuple[str, int]] = field(default_factory=list)  # (order_id, tick_placed)

    def step(self, exchange: Exchange) -> None:
        """Run one tick: maybe place an order, maybe cancel stale ones."""
        tick = exchange._tick

        # Cancel stale orders
        to_cancel = [
            oid for oid, t in self._active_orders
            if tick - t > self.cancel_after_ticks
        ]
        for oid in to_cancel:
            exchange.cancel_order(oid)
        self._active_orders = [
            (oid, t) for oid, t in self._active_orders
            if oid not in to_cancel
        ]

        # Maybe place a new order
        if self._rng.random() > self.arrival_rate:
            return

        last = exchange.last_price
        if last <= 0:
            return

        side = "buy" if self._rng.random() < 0.5 else "sell"
        spread = last * self.price_range_pct

        # 20% of orders are aggressive (cross the spread) to generate fills
        aggressive = self._rng.random() < 0.2

        if side == "buy":
            if aggressive:
                # Buy slightly above last → will match resting asks
                price = round_tick(last + self._rng.uniform(0, spread * 0.5))
            else:
                price = round_tick(last - self._rng.uniform(0, spread))
        else:
            if aggressive:
                # Sell slightly below last → will match resting bids
                price = round_tick(last - self._rng.uniform(0, spread * 0.5))
            else:
                price = round_tick(last + self._rng.uniform(0, spread))

        lo, hi = self.order_size_range
        lots = self._rng.randint(lo, hi)
        qty = float(lots * LOT_SIZE)

        result = exchange.submit_order(
            agent_id=self.agent_id,
            persona_id=self.persona_id,
            side=side,
            order_type="limit",
            price=price,
            quantity=qty,
            agent_holdings=self.holdings,
            agent_cash=self.cash,
        )

        if result.is_resting:
            self._active_orders.append((result.order.order_id, tick))

    def seed(self, s: int) -> None:
        self._rng = random.Random(s)


@dataclass
class MarketMaker:
    """Market maker: two-sided quotes maintaining a target spread.

    Places a bid slightly below and an ask slightly above the current
    midpoint. Adjusts inventory bias: if long, quotes are biased lower
    (encourage sells); if short, biased higher (encourage buys).

    Parameters:
        half_spread_pct: half the target spread as fraction of price
        quote_size_lots: size of each side in lots
        refresh_interval: re-quote every N ticks
        max_inventory_lots: beyond this, skew quotes to reduce inventory
    """

    agent_id: str = "MM_001"
    persona_id: str = "__market_maker__"
    half_spread_pct: float = 0.002   # 0.2% half spread -> 0.4% total
    quote_size_lots: int = 50        # 5000 shares per side
    refresh_interval: int = 5        # re-quote every 5 ticks
    max_inventory_lots: int = 500    # skew beyond 50,000 shares
    cash: float = float("inf")
    _inventory: float = 0.0          # net shares held (positive = long)

    _bid_order_id: str | None = None
    _ask_order_id: str | None = None
    _last_quote_tick: int = -999

    def step(self, exchange: Exchange) -> None:
        """Run one tick: refresh quotes if due."""
        tick = exchange._tick

        if tick - self._last_quote_tick < self.refresh_interval:
            return

        # Cancel old quotes
        if self._bid_order_id:
            exchange.cancel_order(self._bid_order_id)
            self._bid_order_id = None
        if self._ask_order_id:
            exchange.cancel_order(self._ask_order_id)
            self._ask_order_id = None

        last = exchange.last_price
        if last <= 0:
            return

        # Inventory skew: shift midpoint to reduce inventory
        inv_lots = self._inventory / LOT_SIZE
        max_inv = max(1.0, float(self.max_inventory_lots))
        skew = -(inv_lots / max_inv) * self.half_spread_pct * last

        mid = last + skew
        half = last * self.half_spread_pct

        bid_price = round_tick(mid - half)
        ask_price = round_tick(mid + half)
        qty = float(self.quote_size_lots * LOT_SIZE)

        # Place bid
        bid_result = exchange.submit_order(
            agent_id=self.agent_id,
            persona_id=self.persona_id,
            side="buy",
            order_type="limit",
            price=bid_price,
            quantity=qty,
            agent_holdings=abs(self._inventory) if self._inventory < 0 else 0.0,
            agent_cash=self.cash,
        )
        if bid_result.is_resting:
            self._bid_order_id = bid_result.order.order_id
        self._inventory += bid_result.filled_quantity

        # Place ask
        ask_result = exchange.submit_order(
            agent_id=self.agent_id,
            persona_id=self.persona_id,
            side="sell",
            order_type="limit",
            price=ask_price,
            quantity=qty,
            agent_holdings=qty + 1e6,  # MM has unlimited inventory
            agent_cash=self.cash,
        )
        if ask_result.is_resting:
            self._ask_order_id = ask_result.order.order_id
        self._inventory -= ask_result.filled_quantity

        self._last_quote_tick = tick


@dataclass
class MeanReversion:
    """Mean-reversion agent: trades against deviations from an anchor price.

    When the price drops below the anchor by more than the threshold,
    places buy orders. When it rises above, places sell orders. Size
    increases with the deviation magnitude.

    Models institutional value investors and index arbitrageurs who
    anchor to fundamentals and provide stabilizing flow.

    Parameters:
        anchor_price: fair value estimate (typically prev_close or VWAP)
        threshold_pct: minimum deviation before acting
        aggression: how much of the deviation to trade (0-1)
        max_size_lots: maximum order size per tick
    """

    agent_id: str = "MR_001"
    persona_id: str = "__mean_reversion__"
    anchor_price: float = 0.0
    threshold_pct: float = 0.01    # 1% deviation before acting
    aggression: float = 0.5        # trade half the deviation
    max_size_lots: int = 100       # 10,000 shares max per order
    cooldown_ticks: int = 10       # don't trade more often than this
    cash: float = float("inf")
    holdings: float = float("inf")

    _last_trade_tick: int = -999
    _rng: random.Random = field(default_factory=lambda: random.Random(123))

    def step(self, exchange: Exchange) -> None:
        tick = exchange._tick

        if tick - self._last_trade_tick < self.cooldown_ticks:
            return

        last = exchange.last_price
        if last <= 0 or self.anchor_price <= 0:
            return

        deviation = (last - self.anchor_price) / self.anchor_price

        if abs(deviation) < self.threshold_pct:
            return

        # Trade against the deviation
        if deviation < 0:
            side = "buy"
            # Larger deviation -> larger size
            intensity = min(1.0, abs(deviation) / (self.threshold_pct * 5))
            price = round_tick(last + last * 0.001)  # slightly above market
        else:
            side = "sell"
            intensity = min(1.0, abs(deviation) / (self.threshold_pct * 5))
            price = round_tick(last - last * 0.001)  # slightly below market

        lots = max(1, int(intensity * self.max_size_lots * self.aggression))
        qty = float(lots * LOT_SIZE)

        exchange.submit_order(
            agent_id=self.agent_id,
            persona_id=self.persona_id,
            side=side,
            order_type="limit",
            price=price,
            quantity=qty,
            agent_holdings=self.holdings,
            agent_cash=self.cash,
        )

        self._last_trade_tick = tick

    def update_anchor(self, new_anchor: float) -> None:
        """Update the fair value anchor (e.g., after earnings revision)."""
        self.anchor_price = new_anchor

    def seed(self, s: int) -> None:
        self._rng = random.Random(s)


@dataclass
class BackgroundAgentPool:
    """Manages a pool of background agents for one exchange.

    Usage:
        pool = BackgroundAgentPool.default(exchange)
        # each tick:
        pool.step(exchange)
    """

    agents: list[ZeroIntelligence | MarketMaker | MeanReversion] = field(
        default_factory=list
    )

    def step(self, exchange: Exchange) -> None:
        """Run one tick for all background agents."""
        for agent in self.agents:
            agent.step(exchange)

    @classmethod
    def default(
        cls,
        exchange: Exchange,
        *,
        n_zi: int = 5,
        n_mm: int = 1,
        n_mr: int = 2,
        seed: int = 42,
    ) -> BackgroundAgentPool:
        """Create a default pool calibrated for A-share simulation.

        Args:
            exchange: the exchange these agents will trade on.
            n_zi: number of ZI agents (more = deeper book).
            n_mm: number of market makers.
            n_mr: number of mean-reversion agents.
            seed: random seed for reproducibility.
        """
        agents: list[ZeroIntelligence | MarketMaker | MeanReversion] = []

        for i in range(n_zi):
            zi = ZeroIntelligence(
                agent_id=f"ZI_{i:03d}",
                arrival_rate=0.2 + 0.1 * (i % 3),  # stagger arrival rates
                price_range_pct=0.01 + 0.005 * i,   # wider range for later agents
                order_size_range=(1, 10 + 5 * i),
            )
            zi.seed(seed + i)
            agents.append(zi)

        for i in range(n_mm):
            mm = MarketMaker(
                agent_id=f"MM_{i:03d}",
                half_spread_pct=0.001 + 0.0005 * i,
                quote_size_lots=30 + 20 * i,
                refresh_interval=3 + 2 * i,
            )
            agents.append(mm)

        for i in range(n_mr):
            mr = MeanReversion(
                agent_id=f"MR_{i:03d}",
                anchor_price=exchange.prev_close,
                threshold_pct=0.01 + 0.005 * i,
                aggression=0.3 + 0.2 * i,
                max_size_lots=50 + 50 * i,
                cooldown_ticks=8 + 4 * i,
            )
            mr.seed(seed + 100 + i)
            agents.append(mr)

        return cls(agents=agents)


__all__ = [
    "BackgroundAgentPool",
    "MarketMaker",
    "MeanReversion",
    "ZeroIntelligence",
]
