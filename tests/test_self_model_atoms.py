"""Unit tests for ssflow.self_model.atoms — atom library.

Each atom is a (init, update) pair. These tests pin down the expected
numerics for:
  - universal financial (cash / nav / unrealized_pnl_pct / max_drawdown)
  - trajectory (last_action_* / rounds_since_last_trade)
  - emotional (conviction / stress / regret / consecutive_loss_rounds)
  - accountability (client_net_redemption / career_risk_pressure)
  - benchmark (benchmark_gap_pct / peer_rank_percentile using world_public_state)
  - mandate (risk_budget_used_pct / compliance_breaches_this_sim)
  - company side (cash_runway_months / board_pressure_index)

Library integrity:
  - STATE_ATOMS has the expected count + no key typos
  - ``atom_keys_by_category`` groups everything correctly
"""

from __future__ import annotations

import pytest

from ssflow.self_model.atoms import STATE_ATOMS, atom_keys_by_category
from ssflow.self_model.schema import RoundCtx, TradeResult


# ────────────────────── Library integrity ──────────────────────


class TestAtomLibrary:
    def test_atom_count_positive(self):
        assert len(STATE_ATOMS) >= 20

    def test_all_atoms_have_required_fields(self):
        for key, atom in STATE_ATOMS.items():
            assert atom.key == key
            assert callable(atom.init_rule)
            assert callable(atom.update_rule)
            assert isinstance(atom.category, str)
            assert atom.category in {
                "financial", "trajectory", "emotional",
                "accountability", "benchmark", "mandate", "company",
                "universal",
            }

    def test_category_index_contains_everything(self):
        cat = atom_keys_by_category()
        flat = set()
        for keys in cat.values():
            flat.update(keys)
        assert flat == set(STATE_ATOMS.keys())


# ────────────────────── Atom update-rule tests ──────────────────────


def _sandbox_state():
    """Mock sandbox state for init rules."""
    return {
        "capital_distribution": {"type": "lognormal", "median_cny": 1_000_000},
        "initial_position_distribution": {
            "type": "bernoulli",
            "prob_holding": 0.5,
            "position_size_pct_when_holding": {"type": "uniform", "min": 0.1, "max": 0.3},
        },
        "risk": {"max_position_pct": 0.5},
    }


def _round_ctx(round_idx=0, current_price=100.0, initial_price=100.0, delta=0.0, prev_delta=0.0, event_type=""):
    return RoundCtx(
        round_idx=round_idx,
        round_hours=float(round_idx * 24),
        n_rounds=6,
        current_price=current_price,
        initial_price=initial_price,
        cumulative_delta_pct=delta,
        prev_round_delta_pct=prev_delta,
        event_type=event_type,
    )


def _trade_result(side="hold", qty=0.0, nav=1e6, cash=8e5, holdings=2e5, pnl=0.0):
    return TradeResult(
        persona_id="test_persona",
        side=side,
        quantity_pct=qty,
        nav=nav,
        cash=cash,
        holdings_value=holdings,
        unrealized_pnl_pct=pnl,
        avg_position_pct=holdings / nav if nav > 0 else 0,
        net_flow=0.0,
    )


class TestFinancialAtoms:
    def test_cash_init_from_capital_distribution(self):
        atom = STATE_ATOMS["cash"]
        val = atom.init_rule(_sandbox_state(), None, None)
        assert val == 1_000_000.0

    def test_nav_init_includes_holdings(self):
        atom = STATE_ATOMS["nav"]
        val = atom.init_rule(_sandbox_state(), None, None)
        # prob_holding=0.5 × avg_size=0.2 × cash=1e6 = 1e5
        # init_nav = cash + holdings = 1e6 + 1e5 = 1.1e6
        assert val == pytest.approx(1.1e6, rel=0.01)

    def test_unrealized_pnl_pct_tracks_nav_drift(self):
        atom = STATE_ATOMS["unrealized_pnl_pct"]
        state = {"nav": 1.1e6, "initial_nav": 1.0e6}
        val = atom.update_rule(state, _round_ctx(), _trade_result())
        assert val == pytest.approx(10.0, abs=0.01)

    def test_max_drawdown_ratchets(self):
        atom = STATE_ATOMS["max_drawdown_pct"]
        state = {"peak_nav": 1.2e6, "nav": 1.0e6, "max_drawdown_pct": 5.0}
        val = atom.update_rule(state, _round_ctx(), _trade_result())
        # current_dd = (1.2 - 1.0) / 1.2 * 100 = 16.67%, prev was 5 → ratchets up
        assert val == pytest.approx(16.67, abs=0.1)

    def test_max_drawdown_stays_when_new_high(self):
        atom = STATE_ATOMS["max_drawdown_pct"]
        state = {"peak_nav": 1.0e6, "nav": 1.1e6, "max_drawdown_pct": 12.0}
        val = atom.update_rule(state, _round_ctx(), _trade_result())
        # current nav > peak, current_dd < 0 → stays at prev 12.0
        assert val == 12.0

    def test_peak_nav_tracks_high_water(self):
        atom = STATE_ATOMS["peak_nav"]
        state = {"nav": 1.3e6, "peak_nav": 1.1e6}
        val = atom.update_rule(state, _round_ctx(), _trade_result())
        assert val == 1.3e6


class TestTrajectoryAtoms:
    def test_last_action_side_numeric_encoding(self):
        atom = STATE_ATOMS["last_action_side"]
        assert atom.update_rule({}, _round_ctx(), _trade_result(side="buy")) == 1.0
        assert atom.update_rule({}, _round_ctx(), _trade_result(side="sell")) == -1.0
        assert atom.update_rule({}, _round_ctx(), _trade_result(side="hold")) == 0.0

    def test_rounds_since_last_trade_increments_on_hold(self):
        atom = STATE_ATOMS["rounds_since_last_trade"]
        state = {"rounds_since_last_trade": 3}
        val = atom.update_rule(state, _round_ctx(), _trade_result(side="hold"))
        assert val == 4.0

    def test_rounds_since_last_trade_resets_on_buy(self):
        atom = STATE_ATOMS["rounds_since_last_trade"]
        state = {"rounds_since_last_trade": 3}
        val = atom.update_rule(state, _round_ctx(), _trade_result(side="buy", qty=0.15))
        assert val == 0.0

    def test_last_action_outcome_computed_from_entry_price(self):
        atom = STATE_ATOMS["last_action_outcome_pct"]
        state = {"last_action_price": 100.0}
        val = atom.update_rule(state, _round_ctx(current_price=110.0), _trade_result())
        assert val == pytest.approx(10.0, abs=0.01)

    def test_last_action_price_preserved_on_hold(self):
        atom = STATE_ATOMS["last_action_price"]
        state = {"last_action_price": 73.29}
        val = atom.update_rule(state, _round_ctx(current_price=50.0), _trade_result(side="hold"))
        assert val == 73.29  # unchanged

    def test_last_action_price_updated_on_buy(self):
        atom = STATE_ATOMS["last_action_price"]
        state = {"last_action_price": 73.29}
        val = atom.update_rule(
            state, _round_ctx(current_price=80.0),
            _trade_result(side="buy", qty=0.15),
        )
        assert val == 80.0


class TestEmotionalAtoms:
    def test_conviction_rewards_winning_long(self):
        atom = STATE_ATOMS["conviction"]
        state = {
            "conviction": 0.5,
            "last_action_side": 1.0,  # was long
            "last_action_outcome_pct": 5.0,  # won
            "stress": 0.0,
        }
        val = atom.update_rule(state, _round_ctx(), _trade_result())
        assert val > 0.5  # rewarded

    def test_conviction_punishes_losing_long(self):
        atom = STATE_ATOMS["conviction"]
        state = {
            "conviction": 0.5,
            "last_action_side": 1.0,
            "last_action_outcome_pct": -5.0,
            "stress": 0.0,
        }
        val = atom.update_rule(state, _round_ctx(), _trade_result())
        assert val < 0.5

    def test_conviction_clamped_to_unit_interval(self):
        atom = STATE_ATOMS["conviction"]
        state = {"conviction": 0.98, "last_action_side": 1, "last_action_outcome_pct": 10, "stress": 0}
        val = atom.update_rule(state, _round_ctx(), _trade_result())
        assert 0 <= val <= 1

    def test_stress_builds_on_drawdown(self):
        atom = STATE_ATOMS["stress"]
        state = {"stress": 0, "unrealized_pnl_pct": -15.0, "max_drawdown_pct": 15.0}
        val = atom.update_rule(state, _round_ctx(), _trade_result())
        assert val > 0

    def test_stress_decays_on_positive_pnl(self):
        atom = STATE_ATOMS["stress"]
        state = {"stress": 0.5, "unrealized_pnl_pct": 5.0, "max_drawdown_pct": 0}
        val = atom.update_rule(state, _round_ctx(), _trade_result())
        assert val == pytest.approx(0.4, abs=0.01)  # 0.5 - 0.1 decay

    def test_consecutive_loss_rounds_increment_on_loss(self):
        atom = STATE_ATOMS["consecutive_loss_rounds"]
        state = {"consecutive_loss_rounds": 2, "last_action_outcome_pct": -3}
        val = atom.update_rule(state, _round_ctx(), _trade_result())
        assert val == 3

    def test_consecutive_loss_rounds_resets_on_win(self):
        atom = STATE_ATOMS["consecutive_loss_rounds"]
        state = {"consecutive_loss_rounds": 2, "last_action_outcome_pct": 5}
        val = atom.update_rule(state, _round_ctx(), _trade_result())
        assert val == 0


class TestAccountabilityAtoms:
    def test_client_redemption_triggers_above_10pct_drawdown(self):
        atom = STATE_ATOMS["client_net_redemption"]
        state = {"max_drawdown_pct": 15.0}
        val = atom.update_rule(state, _round_ctx(), _trade_result())
        assert val == pytest.approx(0.05, abs=0.001)

    def test_client_redemption_zero_at_low_drawdown(self):
        atom = STATE_ATOMS["client_net_redemption"]
        state = {"max_drawdown_pct": 5.0}
        val = atom.update_rule(state, _round_ctx(), _trade_result())
        assert val == 0.0

    def test_career_risk_pressure_accumulates(self):
        atom = STATE_ATOMS["career_risk_pressure"]
        state = {"consecutive_loss_rounds": 3, "client_net_redemption": 0.05}
        val = atom.update_rule(state, _round_ctx(), _trade_result())
        # 3 * 0.15 + 0.05 * 2.0 = 0.45 + 0.1 = 0.55
        assert val == pytest.approx(0.55, abs=0.001)


class TestBenchmarkAtoms:
    def test_benchmark_gap_uses_peer_mean(self):
        atom = STATE_ATOMS["benchmark_gap_pct"]
        state = {"unrealized_pnl_pct": 10.0}
        world_state = {
            "peer1": {"unrealized_pnl_pct": 5.0},
            "peer2": {"unrealized_pnl_pct": 15.0},
        }
        ctx = _round_ctx()
        # RoundCtx is frozen; construct a fresh one with world_public_state
        ctx2 = RoundCtx(
            round_idx=0, round_hours=0, n_rounds=6,
            current_price=100, initial_price=100,
            cumulative_delta_pct=0.0,
            world_public_state=world_state,
        )
        val = atom.update_rule(state, ctx2, _trade_result())
        # peer mean = 10, my = 10 → gap = 0
        assert val == 0.0

    def test_peer_rank_percentile_best_when_top(self):
        atom = STATE_ATOMS["peer_rank_percentile"]
        state = {"unrealized_pnl_pct": 20.0}
        world_state = {
            "a": {"unrealized_pnl_pct": 5},
            "b": {"unrealized_pnl_pct": 10},
            "c": {"unrealized_pnl_pct": 15},
        }
        ctx = RoundCtx(
            round_idx=0, round_hours=0, n_rounds=6,
            current_price=100, initial_price=100, cumulative_delta_pct=0.0,
            world_public_state=world_state,
        )
        val = atom.update_rule(state, ctx, _trade_result())
        # my return 20 beats all 3, rank = 0 (best)
        assert val == 0.0


class TestCompanySideAtoms:
    def test_cash_runway_decays_by_round_hours(self):
        atom = STATE_ATOMS["cash_runway_months"]
        state = {"cash_runway_months": 12}
        ctx = _round_ctx(round_idx=1)  # round_hours=24
        val = atom.update_rule(state, ctx, _trade_result())
        # 12 - 24/720 = 12 - 0.033... ≈ 11.97
        assert val == pytest.approx(11.967, abs=0.01)

    def test_cash_runway_accelerates_under_stress(self):
        atom = STATE_ATOMS["cash_runway_months"]
        state = {"cash_runway_months": 12}
        ctx = _round_ctx(round_idx=1, delta=-0.20)  # -20% drop
        val = atom.update_rule(state, ctx, _trade_result())
        assert val < 12 - 0.033  # penalty applied

    def test_board_pressure_builds_on_drop(self):
        atom = STATE_ATOMS["board_pressure_index"]
        state = {"board_pressure_index": 0}
        ctx = _round_ctx(round_idx=2, delta=-0.15)
        val = atom.update_rule(state, ctx, _trade_result())
        assert val > 0
        assert val <= 1.0

    def test_customer_concentration_rises_on_regulatory(self):
        atom = STATE_ATOMS["customer_concentration_risk"]
        state = {"customer_concentration_risk": 0.3}
        ctx = _round_ctx(round_idx=1, event_type="regulatory")
        val = atom.update_rule(state, ctx, _trade_result())
        assert val > 0.3
