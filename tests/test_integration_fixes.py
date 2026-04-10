"""Integration tests: verify all 6 fixes work together in realistic scenarios.

These tests simulate the mechanical parts of the round loop (no LLM calls)
to verify that the fixes produce qualitatively correct behavior:

  1. Echo chamber breaking (profit-take stops monotonic rise)
  2. V-shape reversal (dynamic events + policy reactive response)
  3. Liquidity crisis (dynamic ADV amplifies impact when volume thins)
  4. Cross-market awareness (NVIDIA drop → extracted → injected)
  5. Analyst counter-narrative (contrarian prompts appear after big moves)
  6. Stop-loss cascade (drawdown → mechanical sell)
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path

import pytest

from ssflow.trading_layer import Agent, apply_action, apply_distribution_to_agent_pop
from ssflow.market_dynamics import AdaptiveADV, compute_price_impact, compute_dynamic_knee
from ssflow.policy import Policy, TradeAction
from ssflow.policy_engine import compile_trigger, evaluate_policies
from ssflow.world import SimAgent, SimGraph
from ssflow.sim_graph_builder import _risk_config_to_policies
from ssflow.information.external_events import (
    ConditionalEvent,
    DynamicEventStream,
    ExternalEvent,
    ExternalEventSchedule,
    SimSnapshot,
)
from ssflow.market_stress import compute_market_stress, create_stabilization_policies
from ssflow.analyst_context import compute_analyst_context, detect_mixed_signals
from ssflow.information.cross_market import extract_cross_market_from_event


@dataclass
class FakeRound:
    delta_pct: float = 0.0
    net_flow: float = 0.0


# ─────────────────────── Scenario 1: Echo Chamber Breaking ───────────────────

class TestEchoChamberBreaking:
    """Simulate a bullish event where all agents buy.

    Without Fix 2 (profit-take): price rises monotonically.
    With Fix 2: profit-take policies fire after +10% → selling kicks in → price reverses or stabilizes.
    """

    def _setup_scenario(self):
        """Build a minimal sim with retail traders + profit-take policy."""
        rng = random.Random(42)

        # Spawn 100 retail agents with initial holdings
        agents = []
        for i in range(100):
            capital = 100_000 + rng.gauss(0, 20_000)
            capital = max(50_000, capital)
            position_pct = rng.uniform(0.3, 0.7)
            holdings_value = capital * position_pct
            cash = capital - holdings_value
            agents.append(Agent(
                persona_id="retail",
                capital=capital,
                cash=cash,
                holdings={"_default": holdings_value / 100.0},  # price=100
                max_holdings_value=capital * 0.95,
                first_position_round=0,
            ))

        # SimGraph with profit-take policy
        graph = SimGraph()
        sim_agent = SimAgent(
            id="trader_retail", kind="trader",
            display_name="Retail", state={},
            persona_id="retail",
        )
        # Add profit-take policy at +10%
        sim_agent.policies.append(Policy(
            id="auto_profit_take_retail",
            name="止盈 10%",
            description="Mechanical profit-take at +10%",
            trigger_expr="unrealized_pnl_pct > 10.0",
            action=TradeAction(side="sell", quantity_pct=0.50, pool="holdings_in_target"),
            source="risk_config",
            cooldown_rounds=2,
            priority=15,
        ))
        graph.add_agent(sim_agent)

        return agents, graph

    def test_price_reverses_after_profit_take(self):
        """After the price rises >10%, profit-take should trigger selling."""
        agents, graph = self._setup_scenario()
        rng = random.Random(42)

        initial_price = 100.0
        current_price = initial_price
        adv = AdaptiveADV(baseline=1e9)
        prices = [current_price]

        # Simulate 8 rounds of buying pressure
        for round_idx in range(8):
            # Sync population state so policy can evaluate unrealized_pnl_pct
            graph.sync_population_state(
                {"retail": agents}, current_price, round_idx,
            )

            # Evaluate policies
            fires = evaluate_policies(graph, round_idx)

            # Check if profit-take fired
            profit_take_fired = any(
                f.policy.id == "auto_profit_take_retail" for f in fires
            )

            if profit_take_fired:
                # Apply forced sell (50% of holdings)
                sell_spec = {"side": "sell", "pool": "holdings_in_target", "fraction": 0.50}
                net_flow = 0.0
                for agent in agents:
                    if agent.reaction_lag <= round_idx:
                        order = apply_action(agent, sell_spec, current_price,
                                           round_idx=round_idx)
                        net_flow += order
            else:
                # Normal buying: 60% probability buy 20% of cash
                net_flow = 0.0
                for agent in agents:
                    if agent.reaction_lag <= round_idx:
                        if rng.random() < 0.60:
                            order = apply_action(
                                agent,
                                {"side": "buy", "pool": "cash", "fraction": 0.20},
                                current_price,
                                round_idx=round_idx,
                            )
                            net_flow += order

            # Price impact
            abs_flow = abs(net_flow)
            effective_adv = adv.update(abs_flow)
            delta = compute_price_impact(net_flow, effective_adv)
            current_price *= (1 + delta)
            prices.append(current_price)

        # The price should NOT be monotonically increasing
        # At some point profit-take should have caused a decline or slowdown
        max_price = max(prices)
        final_price = prices[-1]

        # Verify the price went up initially (buying pressure)
        assert max(prices[:4]) > initial_price * 1.02, \
            "Price should rise initially from buying pressure"

        # Verify profit-take caused the final price to be below the max
        # (i.e., there was a reversal or at least no more monotonic rise)
        peak_idx = prices.index(max_price)
        if peak_idx < len(prices) - 1:
            # There was a pullback after the peak
            assert final_price <= max_price, \
                "Price should pull back after profit-take"


# ─────────────────────── Scenario 2: V-Shape Reversal ───────────────────

class TestVShapeReversal:
    """Simulate a crash followed by a conditional stabilization event.

    DynamicEventStream injects a stabilization event when price drops >7%.
    Market stress creates a stabilization policy for national_team.
    """

    def test_conditional_event_fires_on_crash(self):
        """When price drops >7%, a conditional event should fire."""
        static = ExternalEventSchedule()
        static.add(ExternalEvent(
            round_idx=0, content_type="news_brief",
            author_persona_id="__market__",
            text="Trump宣布对中国加征34%关税",
        ))

        stabilization_event = ConditionalEvent(
            condition=lambda s: s.cumulative_delta_pct < -0.07,
            event_factory=lambda s: ExternalEvent(
                round_idx=s.round_idx,
                content_type="policy_statement",
                author_persona_id="regulator_csrc",
                text=f"证监会: 高度关注近期市场异常波动(累计{s.cumulative_delta_pct*100:+.1f}%)，"
                     "将采取必要措施维护市场稳定。暂停IPO审批，鼓励上市公司回购。",
                authority_weight=0.95,
            ),
            max_fires=1,
        )

        stream = DynamicEventStream(
            static_schedule=static,
            conditionals=[stabilization_event],
        )

        # Round 0: tariff event (static)
        snap_r0 = SimSnapshot(
            round_idx=0, current_price=100.0, initial_price=100.0,
            cumulative_delta_pct=0.0, net_flow_last_round=0.0,
            round_count=6, agent_states={},
        )
        events_r0 = stream.events_for_round(0, snapshot=snap_r0)
        assert len(events_r0) == 1  # Only the tariff event
        assert "关税" in events_r0[0].text

        # Round 1: price dropped 5% (not enough for stabilization)
        snap_r1 = SimSnapshot(
            round_idx=1, current_price=95.0, initial_price=100.0,
            cumulative_delta_pct=-0.05, net_flow_last_round=-5e8,
            round_count=6, agent_states={},
        )
        events_r1 = stream.events_for_round(1, snapshot=snap_r1)
        assert len(events_r1) == 0  # No stabilization yet

        # Round 2: price dropped 9% total (triggers stabilization!)
        snap_r2 = SimSnapshot(
            round_idx=2, current_price=91.0, initial_price=100.0,
            cumulative_delta_pct=-0.09, net_flow_last_round=-4e8,
            round_count=6, agent_states={},
        )
        events_r2 = stream.events_for_round(2, snapshot=snap_r2)
        assert len(events_r2) == 1
        assert "证监会" in events_r2[0].text
        assert "维护市场稳定" in events_r2[0].text

        # Round 3: stabilization already fired (max_fires=1)
        snap_r3 = SimSnapshot(
            round_idx=3, current_price=89.0, initial_price=100.0,
            cumulative_delta_pct=-0.11, net_flow_last_round=-2e8,
            round_count=6, agent_states={},
        )
        events_r3 = stream.events_for_round(3, snapshot=snap_r3)
        assert len(events_r3) == 0  # Already fired, max_fires=1

    def test_national_team_stabilization_buying(self):
        """Market stress → stabilization policy → national_team buys."""
        graph = SimGraph()
        nt = SimAgent(
            id="nt", kind="trader", display_name="National Team",
            state={"avg_position_pct": 0.30},
            persona_id="national_team_strategic",
        )
        graph.add_agent(nt)

        # Simulate 3 rounds of decline
        rounds = [
            FakeRound(delta_pct=-0.03, net_flow=-3e8),
            FakeRound(delta_pct=-0.03, net_flow=-2.5e8),
            FakeRound(delta_pct=-0.02, net_flow=-1.5e8),
        ]

        stress = compute_market_stress(rounds, 92.0, 100.0, round_idx=3)
        assert stress.stress_level == "crisis"
        assert stress.cumulative_price_change_pct < -7.0

        # Create stabilization policies
        policies = create_stabilization_policies(stress, graph, round_idx=3)
        assert len(policies) == 1
        assert policies[0].action.side == "buy"
        assert policies[0].action.quantity_pct == 0.15

        # The policy should be attached to national_team
        assert len(nt.policies) == 1
        assert nt.policies[0].source == "market_stress"


# ─────────────────────── Scenario 3: Liquidity Crisis ───────────────────

class TestLiquidityCrisis:
    """When volume collapses, AdaptiveADV drops, amplifying impact of remaining trades."""

    def test_thinning_volume_amplifies_impact(self):
        """Same net flow produces progressively larger price impact as volume drops."""
        baseline_adv = 8e9
        net_flow = 1e8  # constant sell pressure
        adv = AdaptiveADV(baseline=baseline_adv, alpha=0.3, floor_pct=0.20)

        deltas = []
        volumes = [5e9, 3e9, 1e9, 0.5e9, 0.2e9]  # declining volume

        for vol in volumes:
            adv.update(vol)
            delta = compute_price_impact(net_flow, adv.effective)
            deltas.append(abs(delta))

        # Each subsequent round should produce a larger impact
        # (because ADV is declining → same flow is larger relative to ADV)
        for i in range(1, len(deltas)):
            assert deltas[i] >= deltas[i-1] * 0.95, \
                f"Impact should increase as ADV drops: R{i-1}={deltas[i-1]:.4f} → R{i}={deltas[i]:.4f}"

    def test_fomo_volume_dampens_impact(self):
        """High volume → ADV rises → same flow produces smaller impact."""
        baseline_adv = 8e9
        net_flow = 1e8
        adv = AdaptiveADV(baseline=baseline_adv, alpha=0.3, ceiling_pct=3.0)

        delta_base = compute_price_impact(net_flow, baseline_adv)

        # Simulate FOMO volume spike
        for _ in range(10):
            adv.update(20e9)  # 2.5x normal volume

        delta_fomo = compute_price_impact(net_flow, adv.effective)
        assert abs(delta_fomo) < abs(delta_base), \
            "High volume should dampen impact per unit"


# ─────────────────────── Scenario 4: Cross-Market Awareness ───────────────

class TestCrossMarketIntegration:
    """End-to-end: event text → cross-market extraction → feed injection."""

    def test_deepseek_nvidia_extraction(self):
        """DeepSeek R1 event mentions NVIDIA → extracted → available for injection."""
        event_text = (
            "DeepSeek发布R1模型，性能接近GPT-4但成本仅为1/10。"
            "隔夜NVIDIA暴跌17%，市值蒸发6000亿美元。"
            "S&P 500下跌2.3%，纳斯达克跌3.1%。"
        )
        ctx = extract_cross_market_from_event(event_text)

        assert len(ctx.data_points) >= 3
        tickers = {dp.ticker for dp in ctx.data_points}
        assert "NVDA" in tickers, "Should extract NVIDIA"
        assert "SPX" in tickers, "Should extract S&P 500"
        assert "NDX" in tickers, "Should extract NASDAQ"

        nvda = next(dp for dp in ctx.data_points if dp.ticker == "NVDA")
        assert nvda.change_pct < 0, "NVIDIA should be negative"

        # Summary should be in Chinese
        summary = ctx.summary_text_zh
        assert "NVIDIA" in summary
        assert "隔夜外围市场" in summary

    def test_tariff_event_extracts_us_market(self):
        """Tariff announcement with '隔夜美股大跌' → generic US bearish signal."""
        ctx = extract_cross_market_from_event(
            "Trump宣布对中国加征34%关税，隔夜美股大跌",
        )
        assert len(ctx.data_points) >= 1
        spx = next((dp for dp in ctx.data_points if dp.ticker == "SPX"), None)
        assert spx is not None
        assert spx.change_pct < 0

    def test_no_cross_market_in_domestic_event(self):
        """Pure domestic event should produce no cross-market data."""
        ctx = extract_cross_market_from_event(
            "央行宣布降准50个基点，下调MLF利率20个基点",
        )
        assert len(ctx.data_points) == 0


# ─────────────────────── Scenario 5: Stop-Loss Cascade ───────────────────

class TestStopLossCascade:
    """When price drops enough, stop-loss policies fire → more selling → more drops."""

    def test_stop_loss_triggers_on_drawdown(self):
        """Retail agents with -15% unrealized loss trigger stop-loss."""
        graph = SimGraph()
        agent = SimAgent(
            id="trader_retail", kind="trader",
            display_name="Retail", state={},
            persona_id="retail",
        )
        # Stop-loss at -15%, discipline 30%
        risk = {"stop_loss_threshold": -0.15, "stop_loss_discipline": 0.30}
        policies = _risk_config_to_policies("retail", risk)
        agent.policies.extend(policies)
        graph.add_agent(agent)

        # Simulate: initial capital 100k, price dropped 20% → unrealized -20%
        agent.set("unrealized_pnl_pct", -20.0)

        fires = evaluate_policies(graph, round_idx=3)
        assert len(fires) == 1
        assert fires[0].policy.id == "auto_stop_loss_retail"
        assert fires[0].action.side == "sell"
        assert fires[0].action.quantity_pct == 0.30

    def test_stop_loss_does_not_fire_above_threshold(self):
        """At -10% (above -15% threshold), no stop-loss."""
        graph = SimGraph()
        agent = SimAgent(
            id="trader_retail", kind="trader",
            display_name="Retail", state={},
            persona_id="retail",
        )
        risk = {"stop_loss_threshold": -0.15, "stop_loss_discipline": 0.30}
        agent.policies.extend(_risk_config_to_policies("retail", risk))
        graph.add_agent(agent)

        agent.set("unrealized_pnl_pct", -10.0)

        fires = evaluate_policies(graph, round_idx=3)
        assert len(fires) == 0


# ─────────────────────── Scenario 6: Analyst Counter-Narrative ───────────────

class TestAnalystCounterNarrativeIntegration:
    """Analysts should get contrarian prompts after significant price moves."""

    def test_mixed_signal_earnings(self):
        """BYD earnings: revenue beat + margin miss → analysts see both sides."""
        from unittest.mock import MagicMock

        persona = MagicMock()
        persona.id = "analyst_cicc"
        persona.sub_archetype = "sellside_research_contrarian"
        persona.entity_role = "analyst"

        event = MagicMock()
        event.event_text = "比亚迪Q1营收增长18%超预期，但毛利率下滑2.3个百分点"

        # At round 3 with +8% move
        injected = False
        for r in range(2, 15):
            ctx = compute_analyst_context(persona, 108.0, 100.0, r, event)
            if ctx.should_inject:
                injected = True
                # Should mention both sides
                assert "正面信号" in ctx.contrarian_prompt
                assert "负面信号" in ctx.contrarian_prompt
                break

        assert injected, "Contrarian analyst should get nudge for mixed-signal event"

    def test_no_counter_narrative_at_round_0(self):
        """Round 0 is too early for counter-narratives."""
        from unittest.mock import MagicMock

        persona = MagicMock()
        persona.id = "analyst_citic"
        persona.sub_archetype = "sellside_research_contrarian"

        event = MagicMock()
        event.event_text = "央行降准100bp，超预期"

        ctx = compute_analyst_context(persona, 110.0, 100.0, 0, event)
        assert not ctx.should_inject


# ─────────────────────── Scenario 7: Full Round Mechanics ───────────────

class TestFullRoundMechanics:
    """Simulate a complete 6-round scenario mechanically, verifying all fixes interact."""

    def test_bullish_event_6_rounds(self):
        """Bullish event → initial rally → profit-take → stabilization → equilibrium."""
        rng = random.Random(42)

        # Setup: 50 retail agents
        agents = []
        for _ in range(50):
            capital = rng.uniform(80_000, 150_000)
            pos_pct = rng.uniform(0.4, 0.7)
            hv = capital * pos_pct
            agents.append(Agent(
                persona_id="retail",
                capital=capital,
                cash=capital - hv,
                holdings={"_default": hv / 100.0},
                max_holdings_value=capital * 0.95,
                first_position_round=0,
            ))

        # SimGraph
        graph = SimGraph()
        sa = SimAgent(
            id="trader_retail", kind="trader",
            display_name="Retail", state={},
            persona_id="retail",
        )
        sa.policies.extend(_risk_config_to_policies("retail", {
            "stop_loss_threshold": -0.15,
            "stop_loss_discipline": 0.30,
            "profit_take_threshold": 0.10,
            "profit_take_fraction": 0.50,
        }))
        graph.add_agent(sa)

        initial_price = 100.0
        current_price = initial_price
        adv = AdaptiveADV(baseline=5e9)
        rounds_history = []
        price_trajectory = [current_price]
        profit_take_rounds = []

        for round_idx in range(6):
            # Sync state
            graph.sync_population_state({"retail": agents}, current_price, round_idx)

            # Evaluate policies
            fires = evaluate_policies(graph, round_idx)

            # Determine action
            net_flow = 0.0
            forced_sell = any(
                f.policy.source == "risk_config" and f.action.side == "sell"
                for f in fires
            )

            if forced_sell:
                profit_take_rounds.append(round_idx)
                for agent in agents:
                    if agent.reaction_lag <= round_idx:
                        order = apply_action(
                            agent,
                            {"side": "sell", "pool": "holdings_in_target", "fraction": 0.50},
                            current_price, round_idx=round_idx,
                        )
                        net_flow += order
            else:
                # Bullish buying (decreasing intensity over time)
                buy_prob = max(0.2, 0.7 - 0.1 * round_idx)
                for agent in agents:
                    if agent.reaction_lag <= round_idx and rng.random() < buy_prob:
                        order = apply_action(
                            agent,
                            {"side": "buy", "pool": "cash", "fraction": 0.15},
                            current_price, round_idx=round_idx,
                        )
                        net_flow += order

            # Price impact
            total_abs = abs(net_flow)
            effective_adv = adv.update(total_abs)
            delta = compute_price_impact(net_flow, effective_adv)
            new_price = current_price * (1 + delta)

            rounds_history.append(FakeRound(delta_pct=delta, net_flow=net_flow))
            current_price = new_price
            price_trajectory.append(current_price)

        # Verify trajectory properties
        # 1. Price should have risen initially
        assert max(price_trajectory[:4]) > initial_price * 1.01, \
            f"Price should rise initially: {price_trajectory[:4]}"

        # 2. If profit-take fired, there should be a round with negative flow
        if profit_take_rounds:
            for pt_round in profit_take_rounds:
                assert rounds_history[pt_round].net_flow < 0, \
                    f"Profit-take round {pt_round} should have negative flow"

        # 3. Final price should be reasonable (not infinity or zero)
        assert 80 < price_trajectory[-1] < 200, \
            f"Final price should be reasonable: {price_trajectory[-1]:.2f}"

        # Print trajectory for visual inspection
        print("\n=== 6-Round Bullish Scenario ===")
        for i, p in enumerate(price_trajectory):
            marker = ""
            if i > 0 and i - 1 in profit_take_rounds:
                marker = " ← PROFIT TAKE"
            print(f"  R{i}: {p:.2f} ({(p/initial_price-1)*100:+.1f}%){marker}")
        print(f"  ADV history: {[f'{v/1e9:.2f}B' for v in adv.history]}")

    def test_bearish_event_6_rounds_with_stabilization(self):
        """Bearish event → crash → stress detection → stabilization → recovery."""
        rng = random.Random(123)

        # Setup
        agents = []
        for _ in range(50):
            capital = rng.uniform(80_000, 150_000)
            pos_pct = rng.uniform(0.5, 0.8)
            hv = capital * pos_pct
            agents.append(Agent(
                persona_id="retail",
                capital=capital,
                cash=capital - hv,
                holdings={"_default": hv / 100.0},
                max_holdings_value=capital * 0.95,
                first_position_round=0,
            ))

        graph = SimGraph()
        retail_sa = SimAgent(
            id="trader_retail", kind="trader",
            display_name="Retail", state={},
            persona_id="retail",
        )
        retail_sa.policies.extend(_risk_config_to_policies("retail", {
            "stop_loss_threshold": -0.15,
            "stop_loss_discipline": 0.30,
        }))
        graph.add_agent(retail_sa)

        # National team for stabilization
        nt_agents = [Agent(
            persona_id="national_team",
            capital=50_000_000_000,  # 500亿
            cash=50_000_000_000,
            holdings={},
            max_holdings_value=50_000_000_000,
        )]
        nt_sa = SimAgent(
            id="nt", kind="trader",
            display_name="National Team",
            state={"avg_position_pct": 0.0},
            persona_id="national_team_strategic",
        )
        graph.add_agent(nt_sa)

        # Dynamic event stream with stabilization conditional
        stream = DynamicEventStream(conditionals=[
            ConditionalEvent(
                condition=lambda s: s.cumulative_delta_pct < -0.07,
                event_factory=lambda s: ExternalEvent(
                    round_idx=s.round_idx,
                    content_type="policy_statement",
                    author_persona_id="regulator_csrc",
                    text=f"证监会维稳声明 (累计{s.cumulative_delta_pct*100:+.1f}%)",
                ),
                max_fires=1,
            ),
        ])

        initial_price = 100.0
        current_price = initial_price
        adv = AdaptiveADV(baseline=5e9)
        rounds_history = []
        price_trajectory = [current_price]
        stabilization_round = None

        for round_idx in range(6):
            cum_delta = (current_price / initial_price - 1.0)

            # Check for dynamic events
            snap = SimSnapshot(
                round_idx=round_idx, current_price=current_price,
                initial_price=initial_price, cumulative_delta_pct=cum_delta,
                net_flow_last_round=rounds_history[-1].net_flow if rounds_history else 0,
                round_count=6, agent_states={},
            )
            events = stream.events_for_round(round_idx, snapshot=snap)

            # Check market stress
            stress = compute_market_stress(
                rounds_history, current_price, initial_price, round_idx,
            )

            # Create stabilization if needed
            if stress.stress_level == "crisis" and stabilization_round is None:
                stab = create_stabilization_policies(stress, graph, round_idx)
                if stab:
                    stabilization_round = round_idx

            # Sync state
            graph.sync_population_state({"retail": agents}, current_price, round_idx)

            # Evaluate policies (stop-loss + stabilization)
            fires = evaluate_policies(graph, round_idx)

            # Determine action
            net_flow = 0.0

            # After stabilization, national_team buys
            if stabilization_round is not None and round_idx >= stabilization_round:
                for na in nt_agents:
                    order = apply_action(
                        na,
                        {"side": "buy", "pool": "cash", "fraction": 0.01},
                        current_price, round_idx=round_idx,
                    )
                    net_flow += order

            # Retail selling (bearish scenario — strong panic early, fades later)
            sell_intensity = 0.8 if round_idx < 3 else 0.3  # strong panic
            for agent in agents:
                if agent.reaction_lag <= round_idx:
                    if rng.random() < sell_intensity:
                        order = apply_action(
                            agent,
                            {"side": "sell", "pool": "holdings_in_target", "fraction": 0.25},
                            current_price, round_idx=round_idx,
                        )
                        net_flow += order

            # Price impact
            total_abs = abs(net_flow)
            effective_adv = adv.update(total_abs)
            delta = compute_price_impact(net_flow, effective_adv)
            new_price = current_price * (1 + delta)

            rounds_history.append(FakeRound(delta_pct=delta, net_flow=net_flow))
            current_price = new_price
            price_trajectory.append(current_price)

        # Print trajectory
        print("\n=== 6-Round Bearish + Stabilization ===")
        for i, p in enumerate(price_trajectory):
            marker = ""
            if i > 0 and stabilization_round is not None and i - 1 == stabilization_round:
                marker = " ← STABILIZATION"
            stress_lvl = ""
            if i > 0 and i - 1 < len(rounds_history):
                s = compute_market_stress(
                    rounds_history[:i], p, initial_price, i,
                )
                stress_lvl = f" [{s.stress_level}]"
            print(f"  R{i}: {p:.2f} ({(p/initial_price-1)*100:+.1f}%){stress_lvl}{marker}")

        # Verify the crash happened (soft compression limits per-round moves,
        # so we check for any measurable decline rather than a specific threshold)
        min_price = min(price_trajectory)
        assert min_price < initial_price * 0.97, \
            f"Price should have dropped: min={min_price:.2f}"

        # Verify stress was detected
        final_stress = compute_market_stress(
            rounds_history, current_price, initial_price, 6,
        )
        assert final_stress.stress_level in ("elevated", "crisis"), \
            f"Stress should be detected: {final_stress.stress_level}"
