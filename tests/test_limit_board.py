"""Tests for limit_board.py — A-share limit-board engine.

Covers:
  - Daily price limits: ±10% (normal), ±20% (ChiNext/STAR), ±5% (ST)
  - IPO rules: +44%/-36% (main board), no limit (reform board first 5 days)
  - Limit-board state detection (NORMAL, LIMIT_UP, LIMIT_DOWN)
  - One-word board detection (ONE_WORD_UP, ONE_WORD_DOWN)
  - Unfilled order queue (封单) tracking
  - T+1 sell constraint (T1Ledger)
  - Gap-open within bounds
  - Seal strength calculation
  - State transitions across rounds
  - Board type inference from ticker
"""

from __future__ import annotations

import math

import pytest

from ssflow.limit_board import (
    BoardState,
    BoardType,
    LIMIT_PCT,
    LimitBoard,
    T1Ledger,
    gap_open,
    infer_board_type,
    resolve_board_type,
)


# ─────────────────────── Board type resolution ───────────────────────


class TestResolveBoardType:
    def test_normal_aliases(self):
        assert resolve_board_type("normal") == BoardType.NORMAL
        assert resolve_board_type("main") == BoardType.NORMAL
        assert resolve_board_type("主板") == BoardType.NORMAL
        assert resolve_board_type("NORMAL") == BoardType.NORMAL

    def test_chinext_aliases(self):
        assert resolve_board_type("chinext") == BoardType.CHINEXT
        assert resolve_board_type("创业板") == BoardType.CHINEXT
        assert resolve_board_type("gem") == BoardType.CHINEXT

    def test_star_aliases(self):
        assert resolve_board_type("star") == BoardType.STAR
        assert resolve_board_type("科创板") == BoardType.STAR
        assert resolve_board_type("star_market") == BoardType.STAR

    def test_st(self):
        assert resolve_board_type("st") == BoardType.ST
        assert resolve_board_type("*st") == BoardType.ST

    def test_ipo_types(self):
        assert resolve_board_type("ipo_main") == BoardType.IPO_MAIN
        assert resolve_board_type("ipo_reform") == BoardType.IPO_REFORM
        assert resolve_board_type("ipo_chinext") == BoardType.IPO_REFORM
        assert resolve_board_type("ipo_star") == BoardType.IPO_REFORM

    def test_unknown_defaults_to_normal(self):
        assert resolve_board_type("crypto") == BoardType.NORMAL

    def test_whitespace_handling(self):
        assert resolve_board_type("  chinext  ") == BoardType.CHINEXT


class TestInferBoardType:
    def test_star_688(self):
        assert infer_board_type("688001.SH") == BoardType.STAR
        assert infer_board_type("688981.SH") == BoardType.STAR

    def test_chinext_300(self):
        assert infer_board_type("300750.SZ") == BoardType.CHINEXT
        assert infer_board_type("300001.SZ") == BoardType.CHINEXT

    def test_main_board(self):
        assert infer_board_type("600519.SH") == BoardType.NORMAL
        assert infer_board_type("000001.SZ") == BoardType.NORMAL
        assert infer_board_type("601318.SH") == BoardType.NORMAL

    def test_bare_code(self):
        """Works without exchange suffix."""
        assert infer_board_type("688001") == BoardType.STAR
        assert infer_board_type("300750") == BoardType.CHINEXT
        assert infer_board_type("600519") == BoardType.NORMAL


# ─────────────────────── Limit percentages ───────────────────────


class TestLimitPct:
    def test_normal_10pct(self):
        up, down = LIMIT_PCT[BoardType.NORMAL]
        assert up == pytest.approx(0.10)
        assert down == pytest.approx(-0.10)

    def test_chinext_20pct(self):
        up, down = LIMIT_PCT[BoardType.CHINEXT]
        assert up == pytest.approx(0.20)
        assert down == pytest.approx(-0.20)

    def test_star_20pct(self):
        up, down = LIMIT_PCT[BoardType.STAR]
        assert up == pytest.approx(0.20)
        assert down == pytest.approx(-0.20)

    def test_st_5pct(self):
        up, down = LIMIT_PCT[BoardType.ST]
        assert up == pytest.approx(0.05)
        assert down == pytest.approx(-0.05)

    def test_ipo_main_asymmetric(self):
        up, down = LIMIT_PCT[BoardType.IPO_MAIN]
        assert up == pytest.approx(0.44)
        assert down == pytest.approx(-0.36)

    def test_ipo_reform_no_limit(self):
        up, down = LIMIT_PCT[BoardType.IPO_REFORM]
        assert math.isinf(up)
        assert math.isinf(down)


# ─────────────────────── LimitBoard — clamping ───────────────────────


class TestLimitBoardClamping:
    """Test that clamp_delta enforces price limits correctly."""

    def test_normal_clamp_up(self):
        """Raw +15% should be clamped to +10% for a normal stock."""
        board = LimitBoard(prev_close=50.0, board_type="normal")
        clamped = board.clamp_delta(0.15)
        new_price = board.current_price * (1.0 + clamped)
        assert new_price == pytest.approx(board.upper_limit, abs=0.01)

    def test_normal_clamp_down(self):
        """Raw -15% should be clamped to -10% for a normal stock."""
        board = LimitBoard(prev_close=50.0, board_type="normal")
        clamped = board.clamp_delta(-0.15)
        new_price = board.current_price * (1.0 + clamped)
        assert new_price == pytest.approx(board.lower_limit, abs=0.01)

    def test_normal_no_clamp_within_bounds(self):
        """A +5% move should pass through unclamped for a normal stock."""
        board = LimitBoard(prev_close=50.0, board_type="normal")
        clamped = board.clamp_delta(0.05)
        assert clamped == pytest.approx(0.05, abs=1e-9)

    def test_chinext_clamp_at_20pct(self):
        """ChiNext allows ±20%: +25% should be clamped to +20%."""
        board = LimitBoard(prev_close=30.0, board_type="chinext")
        clamped = board.clamp_delta(0.25)
        new_price = board.current_price * (1.0 + clamped)
        assert new_price == pytest.approx(board.upper_limit, abs=0.01)

    def test_chinext_15pct_unclamped(self):
        """ChiNext: +15% is within ±20%, should not be clamped."""
        board = LimitBoard(prev_close=30.0, board_type="chinext")
        clamped = board.clamp_delta(0.15)
        assert clamped == pytest.approx(0.15, abs=1e-9)

    def test_star_clamp_at_20pct(self):
        """STAR market: same ±20% as ChiNext."""
        board = LimitBoard(prev_close=100.0, board_type="star")
        clamped = board.clamp_delta(-0.30)
        new_price = board.current_price * (1.0 + clamped)
        assert new_price == pytest.approx(board.lower_limit, abs=0.01)

    def test_st_clamp_at_5pct(self):
        """ST stock: ±5% limit. +8% should be clamped."""
        board = LimitBoard(prev_close=3.50, board_type="st")
        clamped = board.clamp_delta(0.08)
        new_price = board.current_price * (1.0 + clamped)
        assert new_price == pytest.approx(board.upper_limit, abs=0.01)

    def test_st_minus_3pct_unclamped(self):
        """ST stock: -3% is within ±5%, should not be clamped."""
        board = LimitBoard(prev_close=3.50, board_type="st")
        clamped = board.clamp_delta(-0.03)
        assert clamped == pytest.approx(-0.03, abs=1e-9)

    def test_ipo_main_asymmetric_limits(self):
        """IPO main board: +44% / -36% asymmetric."""
        board = LimitBoard(prev_close=20.0, board_type="ipo_main")
        # Test upper: +50% should clamp to +44%
        clamped_up = board.clamp_delta(0.50)
        new_price_up = board.current_price * (1.0 + clamped_up)
        assert new_price_up == pytest.approx(board.upper_limit, abs=0.01)
        assert board.upper_limit == pytest.approx(20.0 * 1.44, abs=0.01)
        # Test lower: -40% should clamp to -36%
        clamped_down = board.clamp_delta(-0.40)
        new_price_down = board.current_price * (1.0 + clamped_down)
        assert new_price_down == pytest.approx(board.lower_limit, abs=0.01)
        assert board.lower_limit == pytest.approx(20.0 * 0.64, abs=0.01)

    def test_ipo_reform_no_clamp(self):
        """IPO reform (ChiNext/STAR first 5 days): no limit applied."""
        board = LimitBoard(prev_close=50.0, board_type="ipo_reform")
        clamped = board.clamp_delta(0.80)
        assert clamped == pytest.approx(0.80, abs=1e-9)
        clamped_neg = board.clamp_delta(-0.60)
        assert clamped_neg == pytest.approx(-0.60, abs=1e-9)

    def test_limit_prices_rounded_to_tick(self):
        """A-share prices are rounded to 0.01 (tick size)."""
        board = LimitBoard(prev_close=33.33, board_type="normal")
        # 33.33 * 1.10 = 36.663 → rounded to 36.66
        assert board.upper_limit == pytest.approx(36.66, abs=0.005)
        # 33.33 * 0.90 = 29.997 → rounded to 30.00
        assert board.lower_limit == pytest.approx(30.00, abs=0.005)

    def test_zero_delta_unclamped(self):
        board = LimitBoard(prev_close=50.0, board_type="normal")
        assert board.clamp_delta(0.0) == 0.0

    def test_clamp_after_intraday_move(self):
        """After an intraday move, the limits still reference prev_close.

        If the stock opened at 50 and moved to 53 (6%), a further +6% delta
        would push to 56.18, which exceeds the upper limit of 55 — so it
        should be clamped.
        """
        board = LimitBoard(prev_close=50.0, board_type="normal")
        # Simulate first round: +6%
        delta1 = board.clamp_delta(0.06)
        board.update(delta1)
        assert board.current_price == pytest.approx(53.0, abs=0.01)
        # Second round: another +6% would take price to ~56.18, beyond 55.0 limit
        delta2 = board.clamp_delta(0.06)
        proposed = 53.0 * (1.0 + delta2)
        assert proposed <= board.upper_limit + 0.01


# ─────────────────────── LimitBoard — state detection ───────────────────────


class TestLimitBoardState:
    """Test board state transitions."""

    def test_initial_state_is_normal(self):
        board = LimitBoard(prev_close=50.0)
        assert board.state == BoardState.NORMAL

    def test_limit_up_detected(self):
        """Price rising to upper limit after a normal open → LIMIT_UP state."""
        board = LimitBoard(prev_close=50.0, board_type="normal")
        # Round 1: normal open at +3%
        board.update(0.03)
        assert board.state == BoardState.NORMAL
        # Round 2: surge to limit-up
        delta = board.clamp_delta(0.10)
        board.update(delta, buy_volume=2e9, sell_volume=5e8)
        assert board.state == BoardState.LIMIT_UP
        assert board.at_limit_up
        assert board.at_limit

    def test_limit_down_detected(self):
        """Price falling to lower limit after a normal open → LIMIT_DOWN state."""
        board = LimitBoard(prev_close=50.0, board_type="normal")
        # Round 1: normal open at -2%
        board.update(-0.02)
        assert board.state == BoardState.NORMAL
        # Round 2: drop to limit-down
        delta = board.clamp_delta(-0.12)
        board.update(delta, buy_volume=3e8, sell_volume=2e9)
        assert board.state == BoardState.LIMIT_DOWN
        assert board.at_limit_down
        assert board.at_limit

    def test_normal_state_when_within_bounds(self):
        board = LimitBoard(prev_close=50.0, board_type="normal")
        board.update(0.05, buy_volume=1e9, sell_volume=1e9)
        assert board.state == BoardState.NORMAL
        assert not board.at_limit

    def test_one_word_up(self):
        """Open at limit-up and never trade below → ONE_WORD_UP (一字涨停)."""
        board = LimitBoard(prev_close=50.0, board_type="normal")
        # Round 1: open at limit-up
        delta = board.clamp_delta(0.15)  # clamped to +10%
        board.update(delta, buy_volume=5e9, sell_volume=2e8)
        assert board.state == BoardState.ONE_WORD_UP
        # Round 2: still at limit-up
        delta2 = board.clamp_delta(0.05)  # would only go up, but already at limit
        board.update(delta2, buy_volume=4e9, sell_volume=3e8)
        assert board.state == BoardState.ONE_WORD_UP

    def test_one_word_down(self):
        """Open at limit-down and never trade above → ONE_WORD_DOWN (一字跌停)."""
        board = LimitBoard(prev_close=50.0, board_type="normal")
        # Round 1: open at limit-down
        delta = board.clamp_delta(-0.20)  # clamped to -10%
        board.update(delta, buy_volume=1e8, sell_volume=3e9)
        assert board.state == BoardState.ONE_WORD_DOWN
        # Round 2: still at limit-down
        delta2 = board.clamp_delta(-0.05)
        board.update(delta2, buy_volume=2e8, sell_volume=2e9)
        assert board.state == BoardState.ONE_WORD_DOWN

    def test_limit_up_not_one_word_if_opened_normal(self):
        """If price opened normal then hit limit-up, it's LIMIT_UP not ONE_WORD_UP."""
        board = LimitBoard(prev_close=50.0, board_type="normal")
        # Round 1: small move
        board.update(0.03)
        assert board.state == BoardState.NORMAL
        # Round 2: jump to limit
        delta = board.clamp_delta(0.10)  # from 51.50 → clamped to upper limit
        board.update(delta, buy_volume=3e9, sell_volume=5e8)
        assert board.state == BoardState.LIMIT_UP
        # NOT one-word because it didn't open at limit
        assert board.state != BoardState.ONE_WORD_UP

    def test_one_word_breaks_to_limit_up(self):
        """If a one-word board briefly drops below limit then returns,
        it downgrades from ONE_WORD_UP to LIMIT_UP."""
        board = LimitBoard(prev_close=50.0, board_type="normal")
        # Round 1: open at limit-up
        delta = board.clamp_delta(0.20)
        board.update(delta, buy_volume=5e9, sell_volume=1e8)
        assert board.state == BoardState.ONE_WORD_UP
        # Round 2: price briefly drops below limit (board opens, 开板)
        board.update(-0.02)  # drops from 55.0 to 53.9
        assert board.state == BoardState.NORMAL
        # Round 3: back to limit-up
        # Need to recalculate: current_price is now ~53.9, upper_limit is 55.0
        needed_delta = (board.upper_limit / board.current_price) - 1.0
        board.update(needed_delta, buy_volume=4e9, sell_volume=5e8)
        # Should be LIMIT_UP, not ONE_WORD_UP (because it traded below)
        assert board.state == BoardState.LIMIT_UP

    def test_transition_limit_up_to_normal_next_round(self):
        """Limit-up in round 2 → price drops → NORMAL in round 3."""
        board = LimitBoard(prev_close=50.0, board_type="normal")
        # Round 1: normal open at +2%
        board.update(0.02)
        assert board.state == BoardState.NORMAL
        # Round 2: surge to limit-up
        delta = board.clamp_delta(0.12)
        board.update(delta, buy_volume=3e9, sell_volume=2e8)
        assert board.state == BoardState.LIMIT_UP
        # Round 3: sell pressure brings price back
        board.update(-0.05, buy_volume=5e8, sell_volume=2e9)
        assert board.state == BoardState.NORMAL
        assert board.unfilled_volume == 0.0


# ─────────────────────── Unfilled volume (封单) ───────────────────────


class TestUnfilledVolume:
    def test_unfilled_at_limit_up(self):
        """At limit-up, unfilled = buy_volume - sell_volume (excess demand)."""
        board = LimitBoard(prev_close=50.0, board_type="normal")
        delta = board.clamp_delta(0.20)
        board.update(delta, buy_volume=3e9, sell_volume=5e8)
        assert board.unfilled_volume == pytest.approx(2.5e9)

    def test_unfilled_at_limit_down(self):
        """At limit-down, unfilled = sell_volume - buy_volume (excess supply)."""
        board = LimitBoard(prev_close=50.0, board_type="normal")
        delta = board.clamp_delta(-0.20)
        board.update(delta, buy_volume=2e8, sell_volume=4e9)
        assert board.unfilled_volume == pytest.approx(3.8e9)

    def test_no_unfilled_when_normal(self):
        """No unfilled volume when price is within bounds."""
        board = LimitBoard(prev_close=50.0, board_type="normal")
        board.update(0.03, buy_volume=1e9, sell_volume=1.2e9)
        assert board.unfilled_volume == 0.0

    def test_unfilled_non_negative(self):
        """Even at limit, if sell > buy at limit-up, unfilled = 0 (not negative)."""
        board = LimitBoard(prev_close=50.0, board_type="normal")
        delta = board.clamp_delta(0.20)
        # Unusual: more sell than buy at limit-up (board about to break)
        board.update(delta, buy_volume=3e8, sell_volume=5e8)
        assert board.unfilled_volume == 0.0


# ─────────────────────── Seal strength ───────────────────────


class TestSealStrength:
    def test_strong_seal(self):
        """Large unfilled relative to ADV → high seal strength."""
        board = LimitBoard(prev_close=50.0, board_type="normal")
        delta = board.clamp_delta(0.20)
        board.update(delta, buy_volume=5e9, sell_volume=1e8)
        # unfilled = 4.9e9, adv = 2e9 → strength = 2.45
        strength = board.seal_strength(adv=2e9)
        assert strength == pytest.approx(4.9e9 / 2e9, rel=0.01)
        assert strength > 2.0  # very strong

    def test_weak_seal(self):
        """Small unfilled relative to ADV → low seal strength."""
        board = LimitBoard(prev_close=50.0, board_type="normal")
        delta = board.clamp_delta(0.20)
        board.update(delta, buy_volume=1.1e9, sell_volume=1e9)
        strength = board.seal_strength(adv=5e9)
        assert strength < 0.5  # weak

    def test_zero_when_normal(self):
        """Seal strength is 0 when not at limit."""
        board = LimitBoard(prev_close=50.0, board_type="normal")
        board.update(0.03)
        assert board.seal_strength(adv=5e9) == 0.0

    def test_zero_when_adv_zero(self):
        board = LimitBoard(prev_close=50.0, board_type="normal")
        delta = board.clamp_delta(0.20)
        board.update(delta, buy_volume=3e9, sell_volume=1e8)
        assert board.seal_strength(adv=0) == 0.0

    def test_zero_when_adv_negative(self):
        board = LimitBoard(prev_close=50.0, board_type="normal")
        delta = board.clamp_delta(0.20)
        board.update(delta, buy_volume=3e9, sell_volume=1e8)
        assert board.seal_strength(adv=-1e9) == 0.0


# ─────────────────────── T+1 sell constraint ───────────────────────


class TestT1Ledger:
    def test_basic_lockup(self):
        """Shares bought in round 3 are locked in round 3, free in round 4."""
        ledger = T1Ledger()
        ledger.record_buy("agent_1", "000001.SZ", shares=1000, round_idx=3)

        locked = ledger.locked_shares("agent_1", "000001.SZ", current_round=3)
        assert locked == 1000.0

        locked_next = ledger.locked_shares("agent_1", "000001.SZ", current_round=4)
        assert locked_next == 0.0

    def test_sellable_with_total_holdings(self):
        """With total_holdings, sellable = total - locked."""
        ledger = T1Ledger()
        ledger.record_buy("agent_1", "000001.SZ", shares=500, round_idx=2)
        # Agent holds 2000 shares total (1500 from before + 500 just bought)
        sellable = ledger.sellable_shares(
            "agent_1", "000001.SZ", current_round=2, total_holdings=2000,
        )
        assert sellable == pytest.approx(1500.0)

    def test_sellable_without_total_holdings(self):
        """Without total_holdings, returns shares bought in prior rounds."""
        ledger = T1Ledger()
        ledger.record_buy("agent_1", "000001.SZ", shares=300, round_idx=1)
        ledger.record_buy("agent_1", "000001.SZ", shares=200, round_idx=3)

        # Round 3: only round 1's 300 shares are sellable
        sellable = ledger.sellable_shares("agent_1", "000001.SZ", current_round=3)
        assert sellable == pytest.approx(300.0)

        # Round 4: both batches are sellable
        sellable = ledger.sellable_shares("agent_1", "000001.SZ", current_round=4)
        assert sellable == pytest.approx(500.0)

    def test_sellable_is_zero_same_round(self):
        """Shares bought this round cannot be sold at all."""
        ledger = T1Ledger()
        ledger.record_buy("agent_1", "600519.SH", shares=100, round_idx=0)
        sellable = ledger.sellable_shares(
            "agent_1", "600519.SH", current_round=0, total_holdings=100,
        )
        assert sellable == 0.0

    def test_multiple_buys_same_round(self):
        """Multiple buys in the same round are all locked."""
        ledger = T1Ledger()
        ledger.record_buy("agent_1", "000001.SZ", shares=100, round_idx=5)
        ledger.record_buy("agent_1", "000001.SZ", shares=200, round_idx=5)
        locked = ledger.locked_shares("agent_1", "000001.SZ", current_round=5)
        assert locked == 300.0

    def test_different_agents_independent(self):
        """T+1 is tracked per agent."""
        ledger = T1Ledger()
        ledger.record_buy("agent_1", "000001.SZ", shares=500, round_idx=3)
        ledger.record_buy("agent_2", "000001.SZ", shares=800, round_idx=3)

        assert ledger.locked_shares("agent_1", "000001.SZ", 3) == 500.0
        assert ledger.locked_shares("agent_2", "000001.SZ", 3) == 800.0
        # Agent 3 never bought — nothing locked
        assert ledger.locked_shares("agent_3", "000001.SZ", 3) == 0.0

    def test_different_tickers_independent(self):
        """T+1 is tracked per ticker."""
        ledger = T1Ledger()
        ledger.record_buy("agent_1", "000001.SZ", shares=500, round_idx=2)
        ledger.record_buy("agent_1", "600519.SH", shares=100, round_idx=2)

        assert ledger.locked_shares("agent_1", "000001.SZ", 2) == 500.0
        assert ledger.locked_shares("agent_1", "600519.SH", 2) == 100.0
        assert ledger.locked_shares("agent_1", "300750.SZ", 2) == 0.0

    def test_advance_day_clears_all(self):
        """advance_day() makes all previously locked shares available."""
        ledger = T1Ledger()
        ledger.record_buy("agent_1", "000001.SZ", shares=1000, round_idx=5)
        assert ledger.locked_shares("agent_1", "000001.SZ", 5) == 1000.0

        ledger.advance_day()
        assert ledger.locked_shares("agent_1", "000001.SZ", 5) == 0.0

    def test_zero_shares_not_recorded(self):
        """record_buy with 0 or negative shares is a no-op."""
        ledger = T1Ledger()
        ledger.record_buy("agent_1", "000001.SZ", shares=0, round_idx=1)
        ledger.record_buy("agent_1", "000001.SZ", shares=-100, round_idx=1)
        assert ledger.locked_shares("agent_1", "000001.SZ", 1) == 0.0

    def test_sellable_never_negative(self):
        """sellable_shares should never go below 0."""
        ledger = T1Ledger()
        ledger.record_buy("agent_1", "000001.SZ", shares=500, round_idx=2)
        # Agent somehow has only 300 total (partial liquidation?)
        sellable = ledger.sellable_shares(
            "agent_1", "000001.SZ", current_round=2, total_holdings=300,
        )
        assert sellable == 0.0  # max(0, 300-500) = 0

    def test_serializable(self):
        """to_serializable returns a JSON-compatible dict."""
        ledger = T1Ledger()
        ledger.record_buy("agent_1", "000001.SZ", shares=100, round_idx=2)
        data = ledger.to_serializable()
        assert isinstance(data, dict)
        assert len(data) == 1


# ─────────────────────── Gap-open ───────────────────────


class TestGapOpen:
    def test_neutral_sentiment_opens_at_close(self):
        """Zero sentiment → open at previous close."""
        price = gap_open(50.0, overnight_sentiment=0.0)
        assert price == pytest.approx(50.0, abs=0.01)

    def test_positive_sentiment_gaps_up(self):
        price = gap_open(50.0, overnight_sentiment=0.8, volatility=0.03)
        assert price > 50.0
        expected = 50.0 * (1.0 + 0.8 * 0.03)
        assert price == pytest.approx(expected, abs=0.01)

    def test_negative_sentiment_gaps_down(self):
        price = gap_open(50.0, overnight_sentiment=-0.5, volatility=0.04)
        assert price < 50.0
        expected = 50.0 * (1.0 + (-0.5) * 0.04)
        assert price == pytest.approx(expected, abs=0.01)

    def test_gap_clamped_to_upper_limit(self):
        """Extreme positive sentiment can't gap beyond 涨停板."""
        # Normal board: +10% max. sentiment=1.0, volatility=0.15 → +15% gap
        price = gap_open(50.0, overnight_sentiment=1.0, volatility=0.15,
                         board_type="normal")
        assert price <= 50.0 * 1.10 + 0.01

    def test_gap_clamped_to_lower_limit(self):
        """Extreme negative sentiment can't gap beyond 跌停板."""
        price = gap_open(50.0, overnight_sentiment=-1.0, volatility=0.15,
                         board_type="normal")
        assert price >= 50.0 * 0.90 - 0.01

    def test_st_gap_clamped_to_5pct(self):
        """ST stock gaps are limited to ±5%."""
        price = gap_open(3.50, overnight_sentiment=1.0, volatility=0.10,
                         board_type="st")
        assert price <= 3.50 * 1.05 + 0.01

    def test_chinext_gap_allows_wider(self):
        """ChiNext allows ±20% gaps."""
        price = gap_open(30.0, overnight_sentiment=1.0, volatility=0.15,
                         board_type="chinext")
        # 15% gap is within ChiNext's 20% limit → should pass through
        expected = 30.0 * (1.0 + 0.15)
        assert price == pytest.approx(expected, abs=0.01)

    def test_ipo_reform_no_clamp(self):
        """IPO reform board (no limits): any gap is allowed."""
        price = gap_open(50.0, overnight_sentiment=1.0, volatility=0.50,
                         board_type="ipo_reform")
        expected = 50.0 * 1.50
        assert price == pytest.approx(expected, abs=0.01)

    def test_sentiment_clamped_to_range(self):
        """Sentiment > 1 is clamped to 1."""
        p1 = gap_open(50.0, overnight_sentiment=1.0, volatility=0.02)
        p2 = gap_open(50.0, overnight_sentiment=5.0, volatility=0.02)
        assert p1 == pytest.approx(p2, abs=0.01)

    def test_gap_price_positive(self):
        """Gap should never produce a negative or zero price."""
        price = gap_open(1.0, overnight_sentiment=-1.0, volatility=0.99)
        assert price > 0

    def test_gap_rounded_to_tick(self):
        """Gap-open price should be rounded to 0.01."""
        price = gap_open(33.33, overnight_sentiment=0.3, volatility=0.02)
        # Check it's rounded to nearest 0.01
        assert price == pytest.approx(round(price, 2), abs=0.001)


# ─────────────────────── Serialization ───────────────────────


class TestLimitBoardSerialization:
    def test_to_serializable_structure(self):
        board = LimitBoard(prev_close=50.0, board_type="normal")
        board.update(0.05, buy_volume=1e9, sell_volume=8e8)
        data = board.to_serializable()

        assert data["state"] == "normal"
        assert data["board_type"] == "normal"
        assert data["prev_close"] == 50.0
        assert "upper_limit" in data
        assert "lower_limit" in data
        assert "unfilled_volume" in data

    def test_to_serializable_at_limit(self):
        """First-round surge to limit → ONE_WORD_UP (opened at limit)."""
        board = LimitBoard(prev_close=50.0, board_type="normal")
        delta = board.clamp_delta(0.20)
        board.update(delta, buy_volume=3e9, sell_volume=2e8)
        data = board.to_serializable()
        assert data["state"] == "one_word_up"

    def test_to_serializable_limit_up_after_normal_open(self):
        """Normal open then surge to limit → LIMIT_UP."""
        board = LimitBoard(prev_close=50.0, board_type="normal")
        board.update(0.02)  # normal open
        delta = board.clamp_delta(0.12)
        board.update(delta, buy_volume=3e9, sell_volume=2e8)
        data = board.to_serializable()
        assert data["state"] == "limit_up"


# ─────────────────────── Integration-style: multi-round scenario ───────────────────────


class TestMultiRoundScenario:
    """Simulates a realistic multi-round day with limit board tracking."""

    def test_gradual_approach_to_limit_up(self):
        """Stock gradually approaches limit-up over 3 rounds, then hits it."""
        board = LimitBoard(prev_close=50.0, board_type="normal")

        # Round 1: +3%
        d1 = board.clamp_delta(0.03)
        board.update(d1)
        assert board.state == BoardState.NORMAL
        assert board.current_price == pytest.approx(51.50, abs=0.01)

        # Round 2: +4%
        d2 = board.clamp_delta(0.04)
        board.update(d2)
        assert board.state == BoardState.NORMAL
        assert board.current_price == pytest.approx(53.56, abs=0.01)

        # Round 3: +5% would take to 56.24, but limit is 55.0
        d3 = board.clamp_delta(0.05)
        board.update(d3, buy_volume=2e9, sell_volume=3e8)
        assert board.at_limit_up
        assert board.current_price == pytest.approx(55.0, abs=0.01)

    def test_limit_down_recovery(self):
        """Stock hits limit-down then recovers next round."""
        board = LimitBoard(prev_close=50.0, board_type="normal")

        # Round 1: crash to limit-down (first round → ONE_WORD_DOWN)
        d1 = board.clamp_delta(-0.20)
        board.update(d1, buy_volume=1e8, sell_volume=5e9)
        assert board.state == BoardState.ONE_WORD_DOWN
        assert board.unfilled_volume > 0

        # Round 2: buying comes in, lifts off limit
        board.update(0.03, buy_volume=3e9, sell_volume=1e9)
        assert board.state == BoardState.NORMAL
        assert board.unfilled_volume == 0.0

    def test_chinext_wider_range_scenario(self):
        """ChiNext stock moves +15% without hitting limit (±20% bounds)."""
        board = LimitBoard(prev_close=30.0, board_type="chinext")

        # Round 1: +8%
        board.update(0.08)
        assert board.state == BoardState.NORMAL

        # Round 2: another +7% (cumulative ~15.5%)
        d2 = board.clamp_delta(0.07)
        board.update(d2)
        assert board.state == BoardState.NORMAL
        # Should be around 34.9
        assert board.current_price == pytest.approx(34.65, abs=0.1)

    def test_board_type_from_enum(self):
        """LimitBoard accepts both string and BoardType enum."""
        board1 = LimitBoard(prev_close=50.0, board_type="normal")
        board2 = LimitBoard(prev_close=50.0, board_type=BoardType.NORMAL)
        assert board1.upper_limit == board2.upper_limit
        assert board1.lower_limit == board2.lower_limit


# ─────────────────────── Edge cases ───────────────────────


class TestEdgeCases:
    def test_very_low_price_stock(self):
        """Penny stocks near 0.01 should have sane limits."""
        board = LimitBoard(prev_close=1.00, board_type="st")
        # ±5% of 1.00 = [0.95, 1.05]
        assert board.lower_limit == pytest.approx(0.95, abs=0.005)
        assert board.upper_limit == pytest.approx(1.05, abs=0.005)

    def test_lower_limit_never_zero(self):
        """Lower limit should never be 0 (price floor of 0.01)."""
        board = LimitBoard(prev_close=0.10, board_type="normal")
        assert board.lower_limit >= 0.01

    def test_properties_at_limit(self):
        board = LimitBoard(prev_close=50.0, board_type="normal")
        delta = board.clamp_delta(0.20)
        board.update(delta, buy_volume=3e9, sell_volume=1e8)
        assert board.at_limit is True
        assert board.at_limit_up is True
        assert board.at_limit_down is False

    def test_properties_at_limit_down(self):
        board = LimitBoard(prev_close=50.0, board_type="normal")
        delta = board.clamp_delta(-0.20)
        board.update(delta, buy_volume=1e8, sell_volume=3e9)
        assert board.at_limit is True
        assert board.at_limit_up is False
        assert board.at_limit_down is True
