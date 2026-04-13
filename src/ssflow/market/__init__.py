"""LOB-based market engine for A-share microstructure simulation.

Replaces Kyle net-flow pricing with a real limit order book, price-time
priority matching, and A-share regulatory rules (涨跌停, T+1, tick size).

Every price tick is traceable to a specific fill on the trade tape.
"""

from .ashare_rules import AShareRules, BoardType, SessionPhase, TICK_SIZE
from .exchange import Exchange
from .order_book import Order, OrderBook, OrderSide, OrderType, OrderStatus
from .trade_tape import Fill, TradeTape

__all__ = [
    "AShareRules",
    "BoardType",
    "Exchange",
    "Fill",
    "Order",
    "OrderBook",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "SessionPhase",
    "TICK_SIZE",
    "TradeTape",
]
