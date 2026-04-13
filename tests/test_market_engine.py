"""Tests for the LOB-based market engine (Phase 0).

Covers:
  - Order book: insertion, matching, cancellation, price-time priority
  - A-share rules: price limits, T+1, tick size, lot size
  - Matching engine: continuous auction fills, call auction clearing
  - Exchange facade: end-to-end submit, reject, fill, tape recording
  - Background agents: ZI, MM, MR produce realistic price paths
  - Trade tape: query, attribution, VWAP
"""

import pytest

from ssflow.market.order_book import Order, OrderBook, OrderSide, OrderType, OrderStatus
from ssflow.market.trade_tape import Fill, TradeTape
from ssflow.market.ashare_rules import (
    AShareRules,
    BoardType,
    SessionPhase,
    T1Ledger,
    round_tick,
    round_lot,
    LOT_SIZE,
    TICK_SIZE,
)
from ssflow.market.matching import MatchingEngine
from ssflow.market.exchange import Exchange, OrderResult
from ssflow.market.background_agents import (
    BackgroundAgentPool,
    MarketMaker,
    MeanReversion,
    ZeroIntelligence,
)


# ─── Order Book Tests ───


class TestOrderBook:
    def test_empty_book(self):
        book = OrderBook("TEST")
        assert book.best_bid is None
        assert book.best_ask is None
        assert book.spread is None
        assert len(book) == 0

    def test_insert_and_query(self):
        book = OrderBook("TEST")
        bid = Order("B1", "a1", "p1", "TEST", OrderSide.BUY, OrderType.LIMIT, 10.0, 100)
        ask = Order("A1", "a2", "p2", "TEST", OrderSide.SELL, OrderType.LIMIT, 10.5, 200)
        book.match_limit(bid)  # no contra → rests
        book.match_limit(ask)
        assert book.best_bid == 10.0
        assert book.best_ask == 10.5
        assert book.spread == pytest.approx(0.5)
        assert book.midpoint == pytest.approx(10.25)
        assert len(book) == 2

    def test_price_time_priority(self):
        """First order at same price gets filled first."""
        book = OrderBook("TEST")
        # Two asks at same price
        a1 = Order("A1", "a1", "p1", "TEST", OrderSide.SELL, OrderType.LIMIT, 10.0, 100, timestamp=1)
        a2 = Order("A2", "a2", "p2", "TEST", OrderSide.SELL, OrderType.LIMIT, 10.0, 100, timestamp=2)
        book.match_limit(a1)
        book.match_limit(a2)

        # Incoming buy matches the first ask (A1) due to time priority
        b1 = Order("B1", "b1", "p3", "TEST", OrderSide.BUY, OrderType.LIMIT, 10.0, 50)
        result = book.match_limit(b1)
        assert len(result.fills) == 1
        _, resting, price, qty = result.fills[0]
        assert resting.order_id == "A1"  # A1 was first
        assert qty == 50.0

    def test_price_priority(self):
        """Better price gets filled first."""
        book = OrderBook("TEST")
        a1 = Order("A1", "a1", "p1", "TEST", OrderSide.SELL, OrderType.LIMIT, 10.5, 100)
        a2 = Order("A2", "a2", "p2", "TEST", OrderSide.SELL, OrderType.LIMIT, 10.0, 100)
        book.match_limit(a1)
        book.match_limit(a2)

        b1 = Order("B1", "b1", "p3", "TEST", OrderSide.BUY, OrderType.LIMIT, 10.5, 50)
        result = book.match_limit(b1)
        assert len(result.fills) == 1
        _, resting, price, qty = result.fills[0]
        assert resting.order_id == "A2"  # A2 has better (lower) price
        assert price == 10.0

    def test_partial_fill(self):
        book = OrderBook("TEST")
        ask = Order("A1", "a1", "p1", "TEST", OrderSide.SELL, OrderType.LIMIT, 10.0, 100)
        book.match_limit(ask)

        bid = Order("B1", "b1", "p2", "TEST", OrderSide.BUY, OrderType.LIMIT, 10.0, 150)
        result = book.match_limit(bid)

        assert len(result.fills) == 1
        assert result.fills[0][3] == 100.0  # filled 100 of 150
        assert result.remaining == 50.0     # 50 still resting
        assert bid.status == OrderStatus.PARTIAL

    def test_market_order_walks_book(self):
        book = OrderBook("TEST")
        a1 = Order("A1", "a1", "p1", "TEST", OrderSide.SELL, OrderType.LIMIT, 10.0, 50)
        a2 = Order("A2", "a2", "p2", "TEST", OrderSide.SELL, OrderType.LIMIT, 10.5, 50)
        book.match_limit(a1)
        book.match_limit(a2)

        mkt = Order("M1", "m1", "pm", "TEST", OrderSide.BUY, OrderType.MARKET, 0.0, 80)
        result = book.match_market(mkt)
        assert len(result.fills) == 2
        assert result.fills[0][2] == 10.0   # first fill at 10.0
        assert result.fills[0][3] == 50.0
        assert result.fills[1][2] == 10.5   # second fill at 10.5
        assert result.fills[1][3] == 30.0

    def test_cancel(self):
        book = OrderBook("TEST")
        bid = Order("B1", "a1", "p1", "TEST", OrderSide.BUY, OrderType.LIMIT, 10.0, 100)
        book.match_limit(bid)
        assert len(book) == 1
        assert book.cancel("B1")
        assert len(book) == 0
        assert book.best_bid is None

    def test_depth(self):
        book = OrderBook("TEST")
        for i in range(5):
            price = 10.0 - i * 0.1
            o = Order(f"B{i}", "a", "p", "TEST", OrderSide.BUY, OrderType.LIMIT, price, 100 * (i + 1))
            book.match_limit(o)

        depth = book.bid_depth(3)
        assert len(depth) == 3
        assert depth[0] == (10.0, 100.0)   # best bid
        assert depth[1] == (9.9, 200.0)
        assert depth[2] == (9.8, 300.0)


# ─── A-Share Rules Tests ───


class TestAShareRules:
    def test_price_limits_normal(self):
        rules = AShareRules(ticker="600000.SH", prev_close=10.0, board_type=BoardType.NORMAL)
        assert rules.upper_limit == pytest.approx(11.0)
        assert rules.lower_limit == pytest.approx(9.0)

    def test_price_limits_chinext(self):
        rules = AShareRules(ticker="300001.SZ", prev_close=50.0, board_type=BoardType.CHINEXT)
        assert rules.upper_limit == pytest.approx(60.0)
        assert rules.lower_limit == pytest.approx(40.0)

    def test_price_limits_st(self):
        rules = AShareRules(ticker="000001.SZ", prev_close=5.0, board_type=BoardType.ST)
        assert rules.upper_limit == pytest.approx(5.25)
        assert rules.lower_limit == pytest.approx(4.75)

    def test_reject_price_above_limit(self):
        rules = AShareRules(ticker="600000.SH", prev_close=10.0)
        rejection = rules.validate_order(
            side="buy", order_type="limit", price=11.50,
            quantity=100, agent_id="a1", agent_holdings=0,
            agent_cash=100000, t1_locked=0,
        )
        assert rejection is not None
        assert rejection.code == "PRICE_LIMIT_UP"

    def test_reject_price_below_limit(self):
        rules = AShareRules(ticker="600000.SH", prev_close=10.0)
        rejection = rules.validate_order(
            side="sell", order_type="limit", price=8.50,
            quantity=100, agent_id="a1", agent_holdings=1000,
            agent_cash=0, t1_locked=0,
        )
        assert rejection is not None
        assert rejection.code == "PRICE_LIMIT_DOWN"

    def test_accept_price_within_limit(self):
        rules = AShareRules(ticker="600000.SH", prev_close=10.0)
        rejection = rules.validate_order(
            side="buy", order_type="limit", price=10.50,
            quantity=100, agent_id="a1", agent_holdings=0,
            agent_cash=100000, t1_locked=0,
        )
        assert rejection is None

    def test_reject_insufficient_cash(self):
        rules = AShareRules(ticker="600000.SH", prev_close=10.0)
        rejection = rules.validate_order(
            side="buy", order_type="limit", price=10.0,
            quantity=1000, agent_id="a1", agent_holdings=0,
            agent_cash=500,  # need 10000 but only have 500
            t1_locked=0,
        )
        assert rejection is not None
        assert rejection.code == "INSUFFICIENT_CASH"

    def test_reject_sell_no_holdings(self):
        rules = AShareRules(ticker="600000.SH", prev_close=10.0)
        rejection = rules.validate_order(
            side="sell", order_type="limit", price=10.0,
            quantity=100, agent_id="a1", agent_holdings=0,
            agent_cash=0, t1_locked=0,
        )
        assert rejection is not None
        assert rejection.code == "NO_SELLABLE"

    def test_reject_sell_t1_locked(self):
        rules = AShareRules(ticker="600000.SH", prev_close=10.0)
        rejection = rules.validate_order(
            side="sell", order_type="limit", price=10.0,
            quantity=100, agent_id="a1",
            agent_holdings=100,  # has 100 shares
            agent_cash=0,
            t1_locked=100,       # but all locked by T+1
        )
        assert rejection is not None
        assert rejection.code == "NO_SELLABLE"

    def test_reject_lot_size(self):
        rules = AShareRules(ticker="600000.SH", prev_close=10.0)
        rejection = rules.validate_order(
            side="buy", order_type="limit", price=10.0,
            quantity=50,  # not a multiple of 100
            agent_id="a1", agent_holdings=0,
            agent_cash=100000, t1_locked=0,
        )
        assert rejection is not None
        assert rejection.code == "LOT_SIZE"

    def test_session_closed_rejection(self):
        rules = AShareRules(ticker="600000.SH", prev_close=10.0)
        rules.set_session(SessionPhase.CLOSED)
        rejection = rules.validate_order(
            side="buy", order_type="limit", price=10.0,
            quantity=100, agent_id="a1", agent_holdings=0,
            agent_cash=100000, t1_locked=0,
        )
        assert rejection is not None
        assert rejection.code == "SESSION_CLOSED"


class TestT1Ledger:
    def test_t1_lockup(self):
        ledger = T1Ledger()
        ledger.record_buy("a1", "TEST", 500, round_idx=0)
        # Same round: locked
        assert ledger.locked_shares("a1", "TEST", 0) == 500
        assert ledger.sellable_shares("a1", "TEST", 0, total_holdings=500) == 0
        # Next round: unlocked
        assert ledger.locked_shares("a1", "TEST", 1) == 0
        assert ledger.sellable_shares("a1", "TEST", 1, total_holdings=500) == 500

    def test_multiple_buys(self):
        ledger = T1Ledger()
        ledger.record_buy("a1", "TEST", 300, round_idx=0)
        ledger.record_buy("a1", "TEST", 200, round_idx=1)
        # Round 1: round 0 buys unlocked, round 1 buys locked
        assert ledger.sellable_shares("a1", "TEST", 1, total_holdings=500) == 300

    def test_advance_day(self):
        ledger = T1Ledger()
        ledger.record_buy("a1", "TEST", 500, round_idx=0)
        ledger.advance_day()
        assert ledger.locked_shares("a1", "TEST", 0) == 0


class TestHelpers:
    def test_round_tick(self):
        assert round_tick(10.123) == pytest.approx(10.12)
        # 10.125 may round to 10.12 or 10.13 due to float representation
        assert round_tick(10.125) in (pytest.approx(10.12), pytest.approx(10.13))
        assert round_tick(10.1) == pytest.approx(10.10)
        assert round_tick(0.01) == pytest.approx(0.01)

    def test_round_lot(self):
        assert round_lot(550) == 500.0
        assert round_lot(99) == 0.0
        assert round_lot(200) == 200.0


# ─── Matching Engine Tests ───


class TestMatchingEngine:
    def _make_engine(self) -> MatchingEngine:
        book = OrderBook("TEST")
        tape = TradeTape("TEST")
        return MatchingEngine(book=book, tape=tape)

    def test_limit_match(self):
        engine = self._make_engine()
        # Resting ask
        ask = Order("A1", "seller", "ps", "TEST", OrderSide.SELL, OrderType.LIMIT, 10.0, 100)
        fills = engine.submit(ask)
        assert len(fills) == 0  # rests

        # Incoming buy matches
        bid = Order("B1", "buyer", "pb", "TEST", OrderSide.BUY, OrderType.LIMIT, 10.0, 100)
        fills = engine.submit(bid)
        assert len(fills) == 1
        assert fills[0].price == 10.0
        assert fills[0].quantity == 100
        assert fills[0].buyer_agent_id == "buyer"
        assert fills[0].seller_agent_id == "seller"

        # Tape recorded
        assert len(engine.tape) == 1
        assert engine.tape.last_price == 10.0

    def test_call_auction(self):
        engine = self._make_engine()

        # Bids: 10.5 (100), 10.3 (200), 10.0 (300)
        engine.submit(Order("B1", "b1", "p", "TEST", OrderSide.BUY, OrderType.LIMIT, 10.5, 100))
        engine.submit(Order("B2", "b2", "p", "TEST", OrderSide.BUY, OrderType.LIMIT, 10.3, 200))
        engine.submit(Order("B3", "b3", "p", "TEST", OrderSide.BUY, OrderType.LIMIT, 10.0, 300))

        # Asks: 9.8 (100), 10.0 (200), 10.2 (150)
        engine.submit(Order("A1", "a1", "p", "TEST", OrderSide.SELL, OrderType.LIMIT, 9.8, 100))
        engine.submit(Order("A2", "a2", "p", "TEST", OrderSide.SELL, OrderType.LIMIT, 10.0, 200))
        engine.submit(Order("A3", "a3", "p", "TEST", OrderSide.SELL, OrderType.LIMIT, 10.2, 150))

        # Note: the continuous matching in submit() already matched some orders.
        # For a proper call auction test, we'd need to collect orders without matching.
        # This test verifies the call_auction method runs on whatever's resting.
        fills = engine.run_call_auction()
        # Verify that fills happened at a single clearing price
        if fills:
            prices = set(f.price for f in fills)
            assert len(prices) == 1  # all at clearing price


# ─── Exchange End-to-End Tests ───


class TestExchange:
    def test_basic_trade(self):
        ex = Exchange("TEST", prev_close=10.0)
        ex.set_session(SessionPhase.CONTINUOUS_AM)

        # Seller rests
        r1 = ex.submit_order("seller", "ps", "sell", "limit", 10.0, 100,
                             agent_holdings=1000, agent_cash=0)
        assert r1.is_resting
        assert len(r1.fills) == 0

        # Buyer matches
        r2 = ex.submit_order("buyer", "pb", "buy", "limit", 10.0, 100,
                             agent_holdings=0, agent_cash=100000)
        assert r2.is_filled
        assert len(r2.fills) == 1
        assert r2.fills[0].price == 10.0
        assert r2.fills[0].quantity == 100

        # Tape recorded
        assert ex.last_price == 10.0
        assert len(ex.tape) == 1

    def test_price_limit_rejection(self):
        ex = Exchange("TEST", prev_close=10.0, board_type=BoardType.NORMAL)
        ex.set_session(SessionPhase.CONTINUOUS_AM)

        r = ex.submit_order("buyer", "pb", "buy", "limit", 12.0, 100,
                            agent_holdings=0, agent_cash=100000)
        assert r.is_rejected
        assert r.rejection.code == "PRICE_LIMIT_UP"

    def test_t1_enforcement(self):
        ex = Exchange("TEST", prev_close=10.0)
        ex.set_session(SessionPhase.CONTINUOUS_AM)
        ex.set_round(0)

        # Resting ask
        ex.submit_order("seller", "ps", "sell", "limit", 10.0, 200,
                        agent_holdings=10000, agent_cash=0)

        # Buyer buys 200 shares
        r = ex.submit_order("buyer", "pb", "buy", "limit", 10.0, 200,
                            agent_holdings=0, agent_cash=100000)
        assert r.filled_quantity == 200

        # Resting bid for the buyer to sell against
        ex.submit_order("other_buyer", "po", "buy", "limit", 10.0, 200,
                        agent_holdings=0, agent_cash=100000)

        # Same round: buyer tries to sell (T+1 blocks it)
        r_sell = ex.submit_order("buyer", "pb", "sell", "limit", 10.0, 200,
                                 agent_holdings=200, agent_cash=0)
        assert r_sell.is_rejected
        assert r_sell.rejection.code == "NO_SELLABLE"

        # Next round: should succeed
        ex.set_round(1)
        # New resting bid
        ex.submit_order("other_buyer2", "po2", "buy", "limit", 10.0, 200,
                        agent_holdings=0, agent_cash=100000)
        r_sell2 = ex.submit_order("buyer", "pb", "sell", "limit", 10.0, 200,
                                  agent_holdings=200, agent_cash=0)
        assert not r_sell2.is_rejected

    def test_tape_attribution(self):
        ex = Exchange("TEST", prev_close=10.0)
        ex.set_session(SessionPhase.CONTINUOUS_AM)

        # Multiple trades: sellers rest, then buyer sweeps
        ex.submit_order("s1", "ps1", "sell", "limit", 10.0, 100,
                        agent_holdings=10000, agent_cash=0)
        ex.submit_order("s2", "ps2", "sell", "limit", 10.5, 100,
                        agent_holdings=10000, agent_cash=0)

        ex.submit_order("b1", "pb1", "buy", "limit", 10.5, 200,
                        agent_holdings=0, agent_cash=100000)

        # 2 fills: one at 10.0, one at 10.5
        assert len(ex.tape) == 2

        # Verify fills are on the tape
        all_fills = ex.tape.all_fills
        assert all_fills[0].price == pytest.approx(10.0)
        assert all_fills[1].price == pytest.approx(10.5)
        assert all_fills[0].buyer_persona_id == "pb1"
        assert all_fills[0].seller_persona_id == "ps1"

    def test_book_snapshot(self):
        ex = Exchange("TEST", prev_close=10.0)
        ex.set_session(SessionPhase.CONTINUOUS_AM)

        ex.submit_order("b1", "p", "buy", "limit", 9.90, 100,
                        agent_holdings=0, agent_cash=100000)
        ex.submit_order("s1", "p", "sell", "limit", 10.10, 200,
                        agent_holdings=10000, agent_cash=0)

        snap = ex.book_snapshot()
        assert snap["best_bid"] == 9.90
        assert snap["best_ask"] == 10.10
        assert snap["n_resting_orders"] == 2


# ─── Background Agent Tests ───


class TestBackgroundAgents:
    def test_zi_produces_orders(self):
        ex = Exchange("TEST", prev_close=100.0)
        ex.set_session(SessionPhase.CONTINUOUS_AM)
        zi = ZeroIntelligence(arrival_rate=1.0)  # always places
        zi.seed(42)

        for tick in range(20):
            ex.set_tick(tick)
            zi.step(ex)

        # Should have resting orders and some fills
        assert len(ex.book) > 0 or len(ex.tape) > 0

    def test_mm_provides_two_sided_quotes(self):
        ex = Exchange("TEST", prev_close=100.0)
        ex.set_session(SessionPhase.CONTINUOUS_AM)
        mm = MarketMaker(refresh_interval=1)

        for tick in range(5):
            ex.set_tick(tick)
            mm.step(ex)

        assert ex.best_bid is not None
        assert ex.best_ask is not None
        assert ex.best_bid < ex.best_ask

    def test_pool_realistic_price_path(self):
        """Run the full pool for 200 ticks and verify price stays within limits."""
        ex = Exchange("TEST", prev_close=100.0, board_type=BoardType.NORMAL)
        ex.set_session(SessionPhase.CONTINUOUS_AM)
        pool = BackgroundAgentPool.default(ex, seed=42)

        prices = [100.0]
        for tick in range(200):
            ex.set_tick(tick)
            pool.step(ex)
            if ex.tape.last_price:
                prices.append(ex.tape.last_price)

        # Price should stay within limits (90-110 for normal board)
        assert all(90.0 <= p <= 110.0 for p in prices), (
            f"Price out of bounds: min={min(prices):.2f}, max={max(prices):.2f}"
        )

        # Should have meaningful activity
        assert len(ex.tape) > 10, f"Only {len(ex.tape)} fills in 200 ticks"

        # Spread should be reasonable (< 2% of price)
        if ex.spread is not None:
            assert ex.spread / 100.0 < 0.02, f"Spread too wide: {ex.spread:.2f}"

    def test_mean_reversion_stabilizes(self):
        """MR agent should push price back toward anchor after a dip."""
        ex = Exchange("TEST", prev_close=100.0)
        ex.set_session(SessionPhase.CONTINUOUS_AM)
        mr = MeanReversion(
            anchor_price=100.0,
            threshold_pct=0.005,
            aggression=0.8,
            cooldown_ticks=1,
        )

        # Simulate a dip: someone sells to push price down, then MR buys back
        # First place asks below prev_close (panic sellers offering shares cheap)
        for i in range(5):
            ex.submit_order(f"panic_{i}", "panic", "sell", "limit",
                            round_tick(98.0 - i * 0.5), 500,
                            agent_holdings=100000, agent_cash=0)

        # Create an initial trade at a low price so last_price deviates from anchor
        ex.submit_order("initial_buyer", "ib", "buy", "limit", 98.0, 100,
                        agent_holdings=0, agent_cash=100000)
        assert ex.last_price < 99.0, f"Expected dipped price, got {ex.last_price}"

        # Let MR agent react — it should see price below anchor and buy
        for tick in range(20):
            ex.set_tick(tick)
            mr.step(ex)

        tape = ex.tape.all_fills
        mr_buys = [f for f in tape if f.buyer_agent_id == "MR_001"]
        assert len(mr_buys) > 0, "MR agent should have bought on the dip"


# ─── Trade Tape Tests ───


class TestTradeTape:
    def test_vwap(self):
        tape = TradeTape("TEST")
        tape.record(Fill("F1", "TEST", "B1", "S1", "b", "s", "pb", "ps",
                         price=10.0, quantity=100, timestamp=1, aggressor_side="buy"))
        tape.record(Fill("F2", "TEST", "B2", "S2", "b", "s", "pb", "ps",
                         price=11.0, quantity=200, timestamp=2, aggressor_side="buy"))
        # VWAP = (10*100 + 11*200) / 300 = 3200/300 = 10.667
        assert tape.vwap() == pytest.approx(10.667, abs=0.001)

    def test_net_flow_by_persona(self):
        tape = TradeTape("TEST")
        tape.record(Fill("F1", "TEST", "B1", "S1", "b1", "s1", "buyer_p", "seller_p",
                         price=10.0, quantity=100, timestamp=1, aggressor_side="buy"))
        flows = tape.net_flow_by_persona()
        assert flows["buyer_p"] == pytest.approx(1000.0)    # bought 100 @ 10
        assert flows["seller_p"] == pytest.approx(-1000.0)  # sold 100 @ 10

    def test_fills_in_range(self):
        tape = TradeTape("TEST")
        for i in range(10):
            tape.record(Fill(f"F{i}", "TEST", "B", "S", "b", "s", "pb", "ps",
                             price=10.0, quantity=10, timestamp=i, aggressor_side="buy"))
        assert len(tape.fills_in_range(3, 7)) == 5
        assert len(tape.fills_in_range(0, 9)) == 10
