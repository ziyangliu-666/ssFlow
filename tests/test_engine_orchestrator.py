"""Tests for the LOB-based orchestrator (Phase 2).

Covers:
  - AShareClock: session schedule, tick generation
  - RoundAdapter: micro-step to round mapping
  - Orchestrator: end-to-end simulation with background + LLM agents
  - Event callback streaming
  - Full pipeline: intent -> constraint -> order -> fill -> P&L
"""

import pytest

from ssflow.engine.clock import AShareClock, SessionEvent
from ssflow.engine.round_adapter import RoundAdapter, RoundRecord
from ssflow.engine.orchestrator import (
    SimulationResult,
    run_simulation_lob,
)
from ssflow.market.ashare_rules import BoardType, SessionPhase
from ssflow.cognition.intent_resolver import StructuredIntent, Urgency
from ssflow.cognition.agent_memory import AgentMemory


# ─── Clock Tests ───


class TestAShareClock:
    def test_generates_events(self):
        clock = AShareClock(n_days=1, ticks_per_session=20)
        events = list(clock)
        assert len(events) > 0

    def test_phases_in_order(self):
        clock = AShareClock(n_days=1, ticks_per_session=10)
        events = list(clock)

        # Find all phase transitions
        phase_starts = [e for e in events if e.is_phase_start]
        phases = [e.phase for e in phase_starts]

        # Should follow the A-share session schedule
        assert SessionPhase.PRE_OPEN in phases
        assert SessionPhase.CALL_AUCTION in phases
        assert SessionPhase.CONTINUOUS_AM in phases
        assert SessionPhase.CONTINUOUS_PM in phases
        assert SessionPhase.CLOSED in phases

    def test_multi_day(self):
        clock = AShareClock(n_days=3, ticks_per_session=10)
        events = list(clock)

        days = set(e.day_idx for e in events)
        assert days == {0, 1, 2}

        # Each day has a start and end
        day_starts = [e for e in events if e.is_day_start]
        day_ends = [e for e in events if e.is_day_end]
        assert len(day_starts) == 3
        assert len(day_ends) == 3

    def test_ticks_monotonic(self):
        clock = AShareClock(n_days=2, ticks_per_session=20)
        events = list(clock)
        ticks = [e.tick for e in events if e.phase in AShareClock.ACTIVE_PHASES]
        for i in range(1, len(ticks)):
            assert ticks[i] >= ticks[i - 1], f"Non-monotonic at {i}: {ticks[i]} < {ticks[i-1]}"


# ─── Round Adapter Tests ───


class TestRoundAdapter:
    def test_basic_round(self):
        adapter = RoundAdapter()
        adapter.on_day_start(0, 100.0)
        adapter.on_phase_start("continuous_am", 100.0)
        adapter.on_fill(5000, 50, is_buy=True)
        adapter.on_fill(3000, 30, is_buy=False)
        adapter.on_phase_end(101.0)
        record = adapter.finalize_round(101.0)

        assert record is not None
        assert record.round_idx == 0
        assert record.price_before == 100.0
        assert record.price_after == 101.0
        assert record.delta_pct == pytest.approx(0.01)
        assert record.n_fills == 2
        assert len(record.phase_results) == 1
        assert record.phase_results[0].net_flow == pytest.approx(2000)  # 5000 - 3000

    def test_multi_phase(self):
        adapter = RoundAdapter()
        adapter.on_day_start(0, 100.0)

        adapter.on_phase_start("continuous_am", 100.0)
        adapter.on_fill(1000, 10, is_buy=True)
        adapter.on_phase_end(100.5)

        adapter.on_phase_start("continuous_pm", 100.5)
        adapter.on_fill(2000, 20, is_buy=False)
        adapter.on_phase_end(99.5)

        record = adapter.finalize_round(99.5)
        assert len(record.phase_results) == 2
        assert record.net_flow == pytest.approx(-1000)  # 1000 - 2000

    def test_multi_round(self):
        adapter = RoundAdapter()

        adapter.on_day_start(0, 100.0)
        adapter.on_phase_start("am", 100.0)
        adapter.on_phase_end(101.0)
        adapter.finalize_round(101.0)

        adapter.on_day_start(1, 101.0)
        adapter.on_phase_start("am", 101.0)
        adapter.on_phase_end(102.0)
        adapter.finalize_round(102.0)

        assert len(adapter.rounds) == 2
        assert adapter.rounds[0].round_idx == 0
        assert adapter.rounds[1].round_idx == 1


# ─── Orchestrator Tests ───


class TestOrchestrator:
    def test_background_only_simulation(self):
        """Run with only background agents — no LLM decisions."""
        result = run_simulation_lob(
            ticker="002594.SZ",
            prev_close=218.50,
            board_type=BoardType.NORMAL,
            n_days=2,
            ticks_per_session=20,
            background_seed=42,
        )

        assert result.ticker == "002594.SZ"
        assert result.initial_price == 218.50
        # Price should stay within A-share limits
        assert 196.65 <= result.final_price <= 240.35  # ±10%
        # Should have rounds
        assert len(result.rounds) >= 1
        # Background agents should generate fills
        assert result.n_total_fills > 0

    def test_with_agent_decisions(self):
        """Run with LLM agents that make decisions."""
        # Create a simple buy decision callback
        def simple_buyer(round_idx, day_idx, memories, snapshot):
            intents = []
            for mem in memories:
                if day_idx == 0 and mem.current_cash > 0:
                    intents.append(StructuredIntent(
                        persona_id=mem.persona_id,
                        agent_id=mem.agent_id,
                        side="buy",
                        size_fraction=0.3,
                        urgency=Urgency.PATIENT,
                        confidence=0.7,
                        rationale="Test buy",
                    ))
                else:
                    intents.append(StructuredIntent(
                        persona_id=mem.persona_id,
                        agent_id=mem.agent_id,
                        side="hold",
                        size_fraction=0.0,
                        urgency=Urgency.NONE,
                    ))
            return intents

        result = run_simulation_lob(
            ticker="TEST",
            prev_close=100.0,
            board_type=BoardType.NORMAL,
            n_days=2,
            ticks_per_session=20,
            agent_configs=[
                {"agent_id": "a1", "persona_id": "retail", "cash": 50_000, "holdings": {}},
                {"agent_id": "a2", "persona_id": "inst", "cash": 200_000, "holdings": {}},
            ],
            agent_decisions=simple_buyer,
            background_seed=42,
        )

        assert result.n_agents == 2
        # Agents may or may not have traded depending on liquidity
        # But the simulation should complete without errors
        assert len(result.rounds) >= 1

    def test_sell_agent_with_holdings(self):
        """Agent with initial holdings can sell."""
        def seller(round_idx, day_idx, memories, snapshot):
            intents = []
            for mem in memories:
                if mem.holdings.get("_default", 0) > 0 and day_idx > 0:
                    # Sell on day 2 (day 0 buys are T+1 locked if they were buys)
                    intents.append(StructuredIntent(
                        persona_id=mem.persona_id,
                        agent_id=mem.agent_id,
                        side="sell",
                        size_fraction=0.5,
                        urgency=Urgency.PATIENT,
                        confidence=0.6,
                    ))
                else:
                    intents.append(StructuredIntent(
                        persona_id=mem.persona_id,
                        agent_id=mem.agent_id,
                        side="hold",
                        size_fraction=0.0,
                        urgency=Urgency.NONE,
                    ))
            return intents

        result = run_simulation_lob(
            ticker="TEST",
            prev_close=100.0,
            n_days=3,
            ticks_per_session=15,
            agent_configs=[
                {
                    "agent_id": "holder",
                    "persona_id": "institutional",
                    "cash": 10_000,
                    "holdings": {"_default": 500},
                },
            ],
            agent_decisions=seller,
            background_seed=42,
        )

        assert result.n_agents == 1
        assert len(result.rounds) >= 1

    def test_event_callback_streaming(self):
        """Event callback receives simulation events."""
        events_received = []

        def on_event(event_type, data):
            events_received.append((event_type, data))

        result = run_simulation_lob(
            ticker="TEST",
            prev_close=100.0,
            n_days=1,
            ticks_per_session=10,
            event_callback=on_event,
            background_seed=42,
        )

        # Should have received day_start and round_complete events
        event_types = [e[0] for e in events_received]
        assert "day_start" in event_types

    def test_price_within_limits(self):
        """Price should never breach A-share limits."""
        result = run_simulation_lob(
            ticker="TEST",
            prev_close=50.0,
            board_type=BoardType.NORMAL,
            n_days=3,
            ticks_per_session=30,
            background_seed=123,
        )

        # All round prices should be within ±10% of prev_close
        for r in result.rounds:
            assert r.price_after >= 45.0, f"Price {r.price_after} below limit"
            assert r.price_after <= 55.0, f"Price {r.price_after} above limit"

    def test_tape_is_audit_trail(self):
        """Tape should have fills from both warm-up and simulation."""
        result = run_simulation_lob(
            ticker="TEST",
            prev_close=100.0,
            n_days=2,
            ticks_per_session=20,
            background_seed=42,
        )

        # Tape includes warm-up + sim fills; n_total_fills is sim-only
        assert result.tape_summary["n_fills"] >= result.n_total_fills
        assert result.n_total_fills > 0

    def test_result_structure(self):
        """Result has all expected fields."""
        result = run_simulation_lob(
            ticker="TEST",
            prev_close=100.0,
            n_days=1,
            ticks_per_session=10,
        )

        assert isinstance(result, SimulationResult)
        assert result.ticker == "TEST"
        assert result.initial_price == 100.0
        assert isinstance(result.rounds, list)
        assert isinstance(result.agent_states, list)
        assert isinstance(result.tape_summary, dict)
        assert isinstance(result.book_snapshot, dict)
