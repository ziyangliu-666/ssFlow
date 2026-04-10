"""Tests for conditional actions (Fix 2: Profit-Taking, Stop-Loss, Time-Exit)."""

import pytest

from ssflow.trading_layer import Agent, apply_action
from ssflow.policy import Policy, TradeAction
from ssflow.world import SimAgent, SimGraph
from ssflow.sim_graph_builder import _risk_config_to_policies


class TestFirstPositionRound:
    def test_set_on_first_buy(self):
        agent = Agent(
            persona_id="test", capital=100000, cash=100000,
            holdings={"_default": 0}, max_holdings_value=95000,
        )
        assert agent.first_position_round == -1
        apply_action(
            agent,
            {"side": "buy", "pool": "cash", "fraction": 0.3},
            current_price=100.0,
            round_idx=2,
        )
        assert agent.first_position_round == 2
        assert agent.holdings["_default"] > 0

    def test_not_reset_on_subsequent_buy(self):
        agent = Agent(
            persona_id="test", capital=100000, cash=50000,
            holdings={"_default": 500}, max_holdings_value=95000,
            first_position_round=1,
        )
        apply_action(
            agent,
            {"side": "buy", "pool": "cash", "fraction": 0.2},
            current_price=100.0,
            round_idx=3,
        )
        assert agent.first_position_round == 1  # unchanged

    def test_spawned_with_holdings_gets_round_0(self):
        """Agents spawned with initial holdings should have first_position_round=0."""
        agent = Agent(
            persona_id="test", capital=100000, cash=50000,
            holdings={"_default": 500}, max_holdings_value=95000,
            first_position_round=0,
        )
        assert agent.first_position_round == 0

    def test_hold_does_not_set(self):
        agent = Agent(
            persona_id="test", capital=100000, cash=100000,
            holdings={"_default": 0}, max_holdings_value=95000,
        )
        apply_action(
            agent,
            {"side": "none", "pool": "none", "fraction": 0.0},
            current_price=100.0,
            round_idx=5,
        )
        assert agent.first_position_round == -1

    def test_backward_compat_no_round_idx(self):
        """Calling apply_action without round_idx should work (defaults to 0)."""
        agent = Agent(
            persona_id="test", capital=100000, cash=100000,
            holdings={"_default": 0}, max_holdings_value=95000,
        )
        amount = apply_action(
            agent,
            {"side": "buy", "pool": "cash", "fraction": 0.1},
            current_price=100.0,
        )
        assert amount > 0
        assert agent.first_position_round == 0


class TestSyncPnl:
    def _make_graph_with_pop(self, agents, round_idx=3):
        """Helper to build a SimGraph + population dict and sync."""
        graph = SimGraph()
        agent_node = SimAgent(
            id="trader_test", kind="trader", display_name="Test",
            state={}, persona_id="test",
        )
        graph.add_agent(agent_node)
        pops = {"test": agents}
        graph.sync_population_state(pops, current_price=110.0, round_idx=round_idx)
        return graph.agents["trader_test"]

    def test_unrealized_pnl_pct_positive(self):
        agents = [
            Agent(persona_id="test", capital=100000, cash=0,
                  holdings={"_default": 1000}, max_holdings_value=100000,
                  first_position_round=0),
        ]
        node = self._make_graph_with_pop(agents)
        # NAV = 1000 * 110 = 110000, capital = 100000
        # PnL = (110000 - 100000) / 100000 * 100 = 10%
        assert node.get("unrealized_pnl_pct") == pytest.approx(10.0, rel=0.01)

    def test_unrealized_pnl_pct_negative(self):
        agents = [
            Agent(persona_id="test", capital=100000, cash=0,
                  holdings={"_default": 800}, max_holdings_value=100000,
                  first_position_round=0),
        ]
        node = self._make_graph_with_pop(agents)
        # NAV = 800 * 110 = 88000
        # PnL = (88000 - 100000) / 100000 * 100 = -12%
        assert node.get("unrealized_pnl_pct") == pytest.approx(-12.0, rel=0.01)

    def test_avg_rounds_held(self):
        agents = [
            Agent(persona_id="test", capital=100000, cash=0,
                  holdings={"_default": 1000}, max_holdings_value=100000,
                  first_position_round=1),
            Agent(persona_id="test", capital=100000, cash=0,
                  holdings={"_default": 500}, max_holdings_value=100000,
                  first_position_round=2),
        ]
        node = self._make_graph_with_pop(agents, round_idx=5)
        # Agent 1: 5-1=4 rounds, Agent 2: 5-2=3 rounds, avg = 3.5
        assert node.get("avg_rounds_held") == pytest.approx(3.5, rel=0.01)


class TestRiskConfigToPolicies:
    def test_stop_loss_generated(self):
        risk = {"stop_loss_threshold": -0.15, "stop_loss_discipline": 0.30}
        policies = _risk_config_to_policies("retail", risk)
        assert len(policies) == 1
        p = policies[0]
        assert "unrealized_pnl_pct < -15.0" in p.trigger_expr
        assert isinstance(p.action, TradeAction)
        assert p.action.side == "sell"
        assert p.action.quantity_pct == 0.30

    def test_profit_take_generated(self):
        risk = {"profit_take_threshold": 0.10, "profit_take_fraction": 0.50}
        policies = _risk_config_to_policies("retail", risk)
        assert len(policies) == 1
        p = policies[0]
        assert "unrealized_pnl_pct > 10.0" in p.trigger_expr
        assert p.action.side == "sell"
        assert p.action.quantity_pct == 0.50

    def test_time_exit_generated(self):
        risk = {"max_hold_rounds": 4}
        policies = _risk_config_to_policies("retail", risk)
        assert len(policies) == 1
        p = policies[0]
        assert "avg_rounds_held > 4.0" in p.trigger_expr
        assert p.action.side == "sell"

    def test_all_three_combined(self):
        risk = {
            "stop_loss_threshold": -0.15,
            "stop_loss_discipline": 0.30,
            "profit_take_threshold": 0.10,
            "profit_take_fraction": 0.50,
            "max_hold_rounds": 4,
        }
        policies = _risk_config_to_policies("retail", risk)
        assert len(policies) == 3

    def test_no_policies_when_keys_missing(self):
        risk = {"max_position_pct": 0.95}
        policies = _risk_config_to_policies("retail", risk)
        assert len(policies) == 0

    def test_stop_loss_not_generated_without_discipline(self):
        risk = {"stop_loss_threshold": -0.15, "stop_loss_discipline": 0.0}
        policies = _risk_config_to_policies("retail", risk)
        assert len(policies) == 0

    def test_priority_ordering(self):
        risk = {
            "stop_loss_threshold": -0.15,
            "stop_loss_discipline": 0.30,
            "profit_take_threshold": 0.10,
            "profit_take_fraction": 0.50,
            "max_hold_rounds": 4,
        }
        policies = _risk_config_to_policies("retail", risk)
        priorities = {p.id: p.priority for p in policies}
        assert priorities[f"auto_stop_loss_retail"] == 20
        assert priorities[f"auto_profit_take_retail"] == 15
        assert priorities[f"auto_time_exit_retail"] == 10
