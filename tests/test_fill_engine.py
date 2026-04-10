"""Tests for limit-board fill-rate logic and T+1 sell constraint in trading_layer.

Covers the two critical gaps identified by the external reviewer:
  1. Orders should NOT fill when the limit board says limit — fill rate
     must depend on contra-side volume.
  2. T+1 sell constraint must be enforced: shares bought in round R cannot
     be sold until round R+1.
"""

import random

import pytest

from ssflow.limit_board import BoardState, LimitBoard, T1Ledger
from ssflow.persona import Persona, SandboxConfig, MarketShare
from ssflow.trading_layer import (
    Agent,
    ClassFlowResult,
    _compute_fill_rate,
    _DEFAULT_TICKER,
    apply_action,
    apply_distribution_to_agent_pop,
    spawn_agents,
)


# ─────────────────────── Test helpers ───────────────────────


def _make_persona(
    instance_count: int = 20,
    max_position_pct: float = 0.95,
) -> Persona:
    return Persona(
        id="fill_test",
        archetype="test_retail",
        display_name="Fill Test",
        voice_prompt="test",
        market_share=MarketShare(by_volume=0.5),
        decision_mode="discretionary",
        role="directional_speculator",
        sandbox=SandboxConfig(
            instance_count=instance_count,
            capital_distribution={"type": "fixed", "value_cny": 100000},
            initial_position_distribution={
                "type": "bernoulli",
                "prob_holding": 0.5,
                "position_size_pct_when_holding": {"type": "fixed", "value": 0.3},
            },
            risk={"max_position_pct": max_position_pct},
            action_space=[
                {"name": "hold", "side": "none", "pool": "none", "fraction": 0.0},
                {"name": "buy_30pct", "side": "buy", "pool": "cash", "fraction": 0.30},
                {"name": "sell_50pct", "side": "sell", "pool": "holdings_in_target", "fraction": 0.50},
            ],
        ),
    )


def _make_buyer_agents(n: int = 10, cash: float = 100000.0) -> list[Agent]:
    """Create agents with only cash (no holdings) — pure buyers."""
    return [
        Agent(
            persona_id="fill_test",
            capital=cash,
            cash=cash,
            holdings={_DEFAULT_TICKER: 0.0},
            max_holdings_value=cash * 0.95,
            agent_id=f"fill_test_{i}",
        )
        for i in range(n)
    ]


def _make_seller_agents(
    n: int = 10, cash: float = 0.0, shares: float = 1000.0,
) -> list[Agent]:
    """Create agents with holdings and minimal cash — pure sellers."""
    return [
        Agent(
            persona_id="fill_test",
            capital=shares * 100.0,
            cash=cash,
            holdings={_DEFAULT_TICKER: shares},
            max_holdings_value=shares * 100.0 * 0.95,
            agent_id=f"fill_test_{i}",
        )
        for i in range(n)
    ]


def _make_mixed_agents(
    n: int = 10, cash: float = 50000.0, shares: float = 500.0,
) -> list[Agent]:
    """Agents with both cash and holdings."""
    return [
        Agent(
            persona_id="fill_test",
            capital=100000.0,
            cash=cash,
            holdings={_DEFAULT_TICKER: shares},
            max_holdings_value=95000.0,
            agent_id=f"fill_test_{i}",
        )
        for i in range(n)
    ]


def _make_limit_up_board(prev_close: float = 100.0) -> LimitBoard:
    """Create a LimitBoard at LIMIT_UP state."""
    board = LimitBoard(prev_close=prev_close, board_type="normal")
    # Move price to upper limit (+10%)
    board.update(0.10, buy_volume=1e9, sell_volume=3e8)
    assert board.at_limit_up
    return board


def _make_limit_down_board(prev_close: float = 100.0) -> LimitBoard:
    """Create a LimitBoard at LIMIT_DOWN state."""
    board = LimitBoard(prev_close=prev_close, board_type="normal")
    # Move price to lower limit (-10%)
    board.update(-0.10, buy_volume=2e8, sell_volume=1e9)
    assert board.at_limit_down
    return board


def _make_normal_board(prev_close: float = 100.0) -> LimitBoard:
    """Create a LimitBoard in NORMAL state."""
    board = LimitBoard(prev_close=prev_close, board_type="normal")
    board.update(0.03, buy_volume=5e8, sell_volume=5e8)
    assert board.state == BoardState.NORMAL
    return board


# ─────────────────────── _compute_fill_rate unit tests ───────────────────────


class TestComputeFillRate:
    def test_no_board_returns_full_fill(self):
        buy_fill, sell_fill, ub, us = _compute_fill_rate(None, 1e9, 5e8)
        assert buy_fill == 1.0
        assert sell_fill == 1.0
        assert ub == 0.0
        assert us == 0.0

    def test_normal_board_returns_full_fill(self):
        board = _make_normal_board()
        buy_fill, sell_fill, ub, us = _compute_fill_rate(board, 1e9, 5e8)
        assert buy_fill == 1.0
        assert sell_fill == 1.0

    def test_limit_up_sell_fills_fully(self):
        board = _make_limit_up_board()
        buy_fill, sell_fill, _, _ = _compute_fill_rate(board, 1e9, 3e8)
        assert sell_fill == 1.0

    def test_limit_up_buy_partially_fills(self):
        board = _make_limit_up_board()
        buy_fill, sell_fill, unfilled_buy, _ = _compute_fill_rate(
            board, 1e9, 3e8,
        )
        assert 0 < buy_fill < 1.0
        # fill rate = sell_volume / buy_volume = 3e8 / 1e9 = 0.3
        assert abs(buy_fill - 0.3) < 1e-9
        assert abs(unfilled_buy - 7e8) < 1e-3

    def test_limit_up_equal_volume_full_fill(self):
        board = _make_limit_up_board()
        buy_fill, sell_fill, _, _ = _compute_fill_rate(board, 5e8, 5e8)
        assert buy_fill == 1.0
        assert sell_fill == 1.0

    def test_limit_down_buy_fills_fully(self):
        board = _make_limit_down_board()
        buy_fill, sell_fill, _, _ = _compute_fill_rate(board, 2e8, 1e9)
        assert buy_fill == 1.0

    def test_limit_down_sell_partially_fills(self):
        board = _make_limit_down_board()
        buy_fill, sell_fill, _, unfilled_sell = _compute_fill_rate(
            board, 2e8, 1e9,
        )
        assert 0 < sell_fill < 1.0
        # fill rate = buy_volume / sell_volume = 2e8 / 1e9 = 0.2
        assert abs(sell_fill - 0.2) < 1e-9
        assert abs(unfilled_sell - 8e8) < 1e-3

    def test_limit_down_equal_volume_full_fill(self):
        board = _make_limit_down_board()
        buy_fill, sell_fill, _, _ = _compute_fill_rate(board, 5e8, 5e8)
        assert buy_fill == 1.0
        assert sell_fill == 1.0

    def test_zero_buy_volume_at_limit_up(self):
        board = _make_limit_up_board()
        buy_fill, sell_fill, _, _ = _compute_fill_rate(board, 0, 1e8)
        # No buys → buy fill rate defaults to 1.0 (irrelevant, no orders)
        assert buy_fill == 1.0
        assert sell_fill == 1.0

    def test_zero_sell_volume_at_limit_down(self):
        board = _make_limit_down_board()
        buy_fill, sell_fill, _, _ = _compute_fill_rate(board, 1e8, 0)
        assert buy_fill == 1.0
        assert sell_fill == 1.0


# ─────────────────────── apply_action T+1 tests ───────────────────────


class TestApplyActionT1:
    def test_sell_blocked_same_round(self):
        """Agent buys in round 3, cannot sell those shares in round 3."""
        agent = Agent(
            persona_id="t1_test",
            capital=100000.0,
            cash=50000.0,
            holdings={_DEFAULT_TICKER: 500.0},
            max_holdings_value=95000.0,
            agent_id="t1_agent_0",
        )
        ledger = T1Ledger()

        # Buy 200 shares in round 3
        buy_spec = {"side": "buy", "pool": "cash", "fraction": 0.40}
        buy_amount = apply_action(
            agent, buy_spec, 100.0,
            instrument=_DEFAULT_TICKER, round_idx=3, t1_ledger=ledger,
        )
        assert buy_amount > 0
        total_shares_after_buy = agent.holdings[_DEFAULT_TICKER]
        assert total_shares_after_buy > 500.0  # bought more

        # Try to sell all holdings in same round 3 — should be capped
        sell_spec = {"side": "sell", "pool": "holdings_in_target", "fraction": 1.0}
        sell_amount = apply_action(
            agent, sell_spec, 100.0,
            instrument=_DEFAULT_TICKER, round_idx=3, t1_ledger=ledger,
        )
        # Should only sell the original 500 shares (pre-buy), not the new ones
        shares_after_sell = agent.holdings[_DEFAULT_TICKER]
        # The locked shares (bought in round 3) should remain
        locked = ledger.locked_shares("t1_agent_0", _DEFAULT_TICKER, 3)
        assert locked > 0
        assert shares_after_sell >= locked - 1e-9  # at least locked shares remain

    def test_sell_allowed_next_round(self):
        """Agent buys in round 3, can sell everything in round 4."""
        agent = Agent(
            persona_id="t1_test",
            capital=100000.0,
            cash=100000.0,
            holdings={_DEFAULT_TICKER: 0.0},
            max_holdings_value=95000.0,
            agent_id="t1_agent_1",
        )
        ledger = T1Ledger()

        # Buy in round 3
        buy_spec = {"side": "buy", "pool": "cash", "fraction": 0.50}
        apply_action(
            agent, buy_spec, 100.0,
            instrument=_DEFAULT_TICKER, round_idx=3, t1_ledger=ledger,
        )
        shares_owned = agent.holdings[_DEFAULT_TICKER]
        assert shares_owned > 0

        # Sell in round 4 — all shares should be sellable
        sell_spec = {"side": "sell", "pool": "holdings_in_target", "fraction": 1.0}
        sell_amount = apply_action(
            agent, sell_spec, 100.0,
            instrument=_DEFAULT_TICKER, round_idx=4, t1_ledger=ledger,
        )
        assert sell_amount < 0  # negative = sell
        assert agent.holdings[_DEFAULT_TICKER] < 1e-9  # sold everything

    def test_no_ledger_backward_compat(self):
        """Without t1_ledger, sell is unconstrained (backward compatible)."""
        agent = Agent(
            persona_id="t1_test",
            capital=100000.0,
            cash=50000.0,
            holdings={_DEFAULT_TICKER: 500.0},
            max_holdings_value=95000.0,
            agent_id="t1_agent_2",
        )

        # Buy without ledger
        buy_spec = {"side": "buy", "pool": "cash", "fraction": 0.50}
        apply_action(agent, buy_spec, 100.0, round_idx=3)
        total = agent.holdings[_DEFAULT_TICKER]

        # Sell all in same round — no T+1 constraint
        sell_spec = {"side": "sell", "pool": "holdings_in_target", "fraction": 1.0}
        apply_action(agent, sell_spec, 100.0, round_idx=3)
        assert agent.holdings[_DEFAULT_TICKER] < 1e-9  # sold everything

    def test_sell_zero_holdings_returns_zero(self):
        """Selling with zero holdings returns zero even with T1 ledger."""
        agent = Agent(
            persona_id="t1_test",
            capital=100000.0,
            cash=100000.0,
            holdings={_DEFAULT_TICKER: 0.0},
            max_holdings_value=95000.0,
            agent_id="t1_agent_3",
        )
        ledger = T1Ledger()

        sell_spec = {"side": "sell", "pool": "holdings_in_target", "fraction": 1.0}
        result = apply_action(
            agent, sell_spec, 100.0,
            instrument=_DEFAULT_TICKER, round_idx=0, t1_ledger=ledger,
        )
        assert result == 0.0


# ─────────────────────── Distribution + LimitBoard tests ───────────────────────


class TestDistributionWithLimitBoard:
    """Test apply_distribution_to_agent_pop with limit_board parameter."""

    def test_normal_board_full_fill(self):
        """NORMAL board state: all orders fill (fill_rate = 1.0)."""
        persona = _make_persona(instance_count=10)
        agents = _make_mixed_agents(n=10)
        board = _make_normal_board()
        rng = random.Random(42)

        result = apply_distribution_to_agent_pop(
            persona=persona,
            agents=agents,
            distribution={"__freeform__": 1.0},
            current_price=103.0,  # within normal range
            rng=rng,
            raw_distribution={"side": "buy", "quantity_pct": 0.40, "pool": "cash"},
            limit_board=board,
        )

        assert isinstance(result, ClassFlowResult)
        assert result.fill_rate == pytest.approx(1.0)
        assert result.unfilled_volume == pytest.approx(0.0)

    def test_limit_up_buy_partially_fills(self):
        """At LIMIT_UP, buy orders partially fill, result.fill_rate < 1.0."""
        persona = _make_persona(instance_count=10)
        # Pure buyers — all will try to buy
        agents = _make_buyer_agents(n=10, cash=100000.0)
        board = _make_limit_up_board()
        rng = random.Random(42)

        result = apply_distribution_to_agent_pop(
            persona=persona,
            agents=agents,
            distribution={"__freeform__": 1.0},
            current_price=110.0,  # at limit up
            rng=rng,
            raw_distribution={"side": "buy", "quantity_pct": 0.50, "pool": "cash"},
            limit_board=board,
        )

        # Mostly buy orders, at limit-up with no sellers providing contra volume
        # → buy fill rate should be low (close to 0)
        assert result.fill_rate < 1.0
        assert result.unfilled_volume > 0

    def test_limit_up_sell_fills_fully(self):
        """At LIMIT_UP, sell orders fill fully."""
        persona = _make_persona(instance_count=10)
        agents = _make_seller_agents(n=10, shares=1000.0)
        board = _make_limit_up_board()
        rng = random.Random(42)

        result = apply_distribution_to_agent_pop(
            persona=persona,
            agents=agents,
            distribution={"__freeform__": 1.0},
            current_price=110.0,
            rng=rng,
            raw_distribution={"side": "sell", "quantity_pct": 0.50, "pool": "holdings_in_target"},
            limit_board=board,
        )

        # At limit-up, sell fills fully → fill_rate ≈ 1.0
        # (some agents may noise into buy, but the sell side is unconstrained)
        assert result.fill_rate >= 0.9  # mostly sells, all fill
        assert result.net_flow < 0  # net selling

    def test_limit_down_sell_partially_fills(self):
        """At LIMIT_DOWN, sell orders partially fill."""
        persona = _make_persona(instance_count=10)
        agents = _make_seller_agents(n=10, shares=1000.0)
        board = _make_limit_down_board()
        rng = random.Random(42)

        result = apply_distribution_to_agent_pop(
            persona=persona,
            agents=agents,
            distribution={"__freeform__": 1.0},
            current_price=90.0,  # at limit down
            rng=rng,
            raw_distribution={"side": "sell", "quantity_pct": 0.50, "pool": "holdings_in_target"},
            limit_board=board,
        )

        # Mostly sell orders, at limit-down → sell fill rate should be low
        assert result.fill_rate < 1.0
        assert result.unfilled_volume > 0

    def test_limit_down_buy_fills_fully(self):
        """At LIMIT_DOWN, buy orders fill fully."""
        persona = _make_persona(instance_count=10)
        agents = _make_buyer_agents(n=10, cash=100000.0)
        board = _make_limit_down_board()
        rng = random.Random(42)

        result = apply_distribution_to_agent_pop(
            persona=persona,
            agents=agents,
            distribution={"__freeform__": 1.0},
            current_price=90.0,
            rng=rng,
            raw_distribution={"side": "buy", "quantity_pct": 0.50, "pool": "cash"},
            limit_board=board,
        )

        # At limit-down, buy fills fully
        assert result.fill_rate >= 0.9
        assert result.net_flow > 0

    def test_no_limit_board_backward_compat(self):
        """Without limit_board, all orders fill (backward compatible)."""
        persona = _make_persona(instance_count=5)
        agents = _make_mixed_agents(n=5)
        rng = random.Random(42)

        result = apply_distribution_to_agent_pop(
            persona=persona,
            agents=agents,
            distribution={"__freeform__": 1.0},
            current_price=100.0,
            rng=rng,
            raw_distribution={"side": "buy", "quantity_pct": 0.30, "pool": "cash"},
        )

        assert result.fill_rate == pytest.approx(1.0)
        assert result.unfilled_volume == pytest.approx(0.0)
        assert result.t1_blocked_sells == 0

    def test_legacy_path_with_limit_board(self):
        """Legacy distribution path also respects limit board."""
        persona = _make_persona(instance_count=10)
        agents = _make_mixed_agents(n=10)
        board = _make_limit_down_board()
        rng = random.Random(42)

        result = apply_distribution_to_agent_pop(
            persona=persona,
            agents=agents,
            distribution={"hold": 0.1, "buy_30pct": 0.1, "sell_50pct": 0.8},
            current_price=90.0,
            rng=rng,
            limit_board=board,
        )

        # Heavy sell distribution at limit-down → reduced fill rate
        assert result.fill_rate < 1.0 or result.unfilled_volume > 0


# ─────────────────────── Distribution + T1Ledger tests ───────────────────────


class TestDistributionWithT1Ledger:
    """Test apply_distribution_to_agent_pop with t1_ledger parameter."""

    def test_t1_blocks_same_round_sell(self):
        """Agents that buy in round 3 cannot sell those shares in round 3."""
        persona = _make_persona(instance_count=5)
        agents = _make_buyer_agents(n=5, cash=100000.0)
        ledger = T1Ledger()
        rng = random.Random(42)

        # Round 3: everyone buys
        result_buy = apply_distribution_to_agent_pop(
            persona=persona,
            agents=agents,
            distribution={"__freeform__": 1.0},
            current_price=100.0,
            rng=rng,
            raw_distribution={"side": "buy", "quantity_pct": 0.50, "pool": "cash"},
            round_idx=3,
            t1_ledger=ledger,
        )
        assert result_buy.net_flow > 0  # bought
        assert all(a.holdings[_DEFAULT_TICKER] > 0 for a in agents)

        # Round 3: try to sell in the same round
        rng2 = random.Random(99)
        result_sell = apply_distribution_to_agent_pop(
            persona=persona,
            agents=agents,
            distribution={"__freeform__": 1.0},
            current_price=100.0,
            rng=rng2,
            raw_distribution={"side": "sell", "quantity_pct": 1.0, "pool": "holdings_in_target"},
            round_idx=3,
            t1_ledger=ledger,
        )
        # T+1: sell should be blocked because all holdings were bought this round
        assert result_sell.t1_blocked_sells > 0

    def test_t1_allows_next_round_sell(self):
        """Agents that buy in round 3 can sell in round 4."""
        persona = _make_persona(instance_count=5)
        agents = _make_buyer_agents(n=5, cash=100000.0)
        ledger = T1Ledger()
        rng = random.Random(42)

        # Round 3: buy
        apply_distribution_to_agent_pop(
            persona=persona,
            agents=agents,
            distribution={"__freeform__": 1.0},
            current_price=100.0,
            rng=rng,
            raw_distribution={"side": "buy", "quantity_pct": 0.50, "pool": "cash"},
            round_idx=3,
            t1_ledger=ledger,
        )

        # Round 4: sell — should be allowed
        rng2 = random.Random(99)
        result_sell = apply_distribution_to_agent_pop(
            persona=persona,
            agents=agents,
            distribution={"__freeform__": 1.0},
            current_price=100.0,
            rng=rng2,
            raw_distribution={"side": "sell", "quantity_pct": 1.0, "pool": "holdings_in_target"},
            round_idx=4,
            t1_ledger=ledger,
        )
        # T+1 should not block: all shares are from prior round
        assert result_sell.t1_blocked_sells == 0
        assert result_sell.net_flow < 0  # actually sold


# ─────────────────────── Combined: Limit Board + T+1 ───────────────────────


class TestCombinedConstraints:
    """Test double constraints: limit board fill rate + T+1 lockup."""

    def test_limit_down_plus_t1(self):
        """At LIMIT_DOWN + T+1, sell is doubly constrained.

        Scenario: agents bought in round 2, try to sell in round 2 at limit-down.
        - T+1 blocks the sell entirely (bought this round).
        - Limit-down would also have reduced the fill rate.
        - The T+1 constraint is checked first via apply_action.
        """
        persona = _make_persona(instance_count=5)
        agents = _make_buyer_agents(n=5, cash=100000.0)
        board = _make_limit_down_board()
        ledger = T1Ledger()
        rng = random.Random(42)

        # Round 2: buy
        apply_distribution_to_agent_pop(
            persona=persona,
            agents=agents,
            distribution={"__freeform__": 1.0},
            current_price=90.0,
            rng=rng,
            raw_distribution={"side": "buy", "quantity_pct": 0.50, "pool": "cash"},
            round_idx=2,
            t1_ledger=ledger,
        )

        # Round 2: try to sell at limit-down
        rng2 = random.Random(99)
        result = apply_distribution_to_agent_pop(
            persona=persona,
            agents=agents,
            distribution={"__freeform__": 1.0},
            current_price=90.0,
            rng=rng2,
            raw_distribution={"side": "sell", "quantity_pct": 1.0, "pool": "holdings_in_target"},
            round_idx=2,
            limit_board=board,
            t1_ledger=ledger,
        )
        # Doubly constrained: T+1 blocks + limit-down reduces fill
        assert result.t1_blocked_sells > 0
        # Net flow should be very small (sells mostly blocked)
        assert abs(result.net_flow) < 1e3  # negligible

    def test_limit_down_t1_clears_next_round(self):
        """At LIMIT_DOWN in round 3, shares from round 2 can sell (T+1 passed)
        but sell fill rate is still reduced by the limit board."""
        persona = _make_persona(instance_count=5)
        agents = _make_buyer_agents(n=5, cash=100000.0)
        board = _make_limit_down_board()
        ledger = T1Ledger()
        rng = random.Random(42)

        # Round 2: buy
        apply_distribution_to_agent_pop(
            persona=persona,
            agents=agents,
            distribution={"__freeform__": 1.0},
            current_price=90.0,
            rng=rng,
            raw_distribution={"side": "buy", "quantity_pct": 0.50, "pool": "cash"},
            round_idx=2,
            t1_ledger=ledger,
        )

        # Round 3: sell at limit-down
        rng2 = random.Random(99)
        result = apply_distribution_to_agent_pop(
            persona=persona,
            agents=agents,
            distribution={"__freeform__": 1.0},
            current_price=90.0,
            rng=rng2,
            raw_distribution={"side": "sell", "quantity_pct": 1.0, "pool": "holdings_in_target"},
            round_idx=3,
            limit_board=board,
            t1_ledger=ledger,
        )
        # T+1 cleared (round 3 > round 2) but limit-down still constrains
        assert result.t1_blocked_sells == 0
        assert result.fill_rate < 1.0  # limit-down reduces sell fill


# ─────────────────────── ClassFlowResult field tests ───────────────────────


class TestClassFlowResultFields:
    """Verify the new fields on ClassFlowResult."""

    def test_default_values(self):
        result = ClassFlowResult(
            persona_id="test",
            net_flow=0.0,
            action_histogram={},
            n_agents=0,
        )
        assert result.fill_rate == 1.0
        assert result.unfilled_volume == 0.0
        assert result.t1_blocked_sells == 0

    def test_custom_values(self):
        result = ClassFlowResult(
            persona_id="test",
            net_flow=1000.0,
            action_histogram={"buy": 5},
            n_agents=5,
            fill_rate=0.3,
            unfilled_volume=7000.0,
            t1_blocked_sells=2,
        )
        assert result.fill_rate == 0.3
        assert result.unfilled_volume == 7000.0
        assert result.t1_blocked_sells == 2


# ─────────────────────── Agent agent_id field tests ───────────────────────


class TestAgentId:
    """Verify agent_id is assigned correctly."""

    def test_spawn_assigns_agent_id(self):
        persona = _make_persona(instance_count=3)
        rng = random.Random(42)
        agents = spawn_agents(persona, 100.0, rng)
        ids = [a.agent_id for a in agents]
        assert len(set(ids)) == 3  # all unique
        assert all(id_str.startswith("fill_test_") for id_str in ids)

    def test_agent_id_default_empty(self):
        agent = Agent(
            persona_id="test",
            capital=100000.0,
            cash=100000.0,
        )
        assert agent.agent_id == ""

    def test_agent_id_custom(self):
        agent = Agent(
            persona_id="test",
            capital=100000.0,
            cash=100000.0,
            agent_id="custom_42",
        )
        assert agent.agent_id == "custom_42"
