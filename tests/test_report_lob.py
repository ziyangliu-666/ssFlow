"""Tests for LOB report rendering and calibration (Phase 3).

Covers:
  - LOB report generation from SimulationResult
  - Report sections: header, rounds, tape attribution, book state, P&L
  - Calibration against historical events
  - Calibration scoring
"""

import pytest

from ssflow.engine.orchestrator import SimulationResult, run_simulation_lob, AgentState
from ssflow.engine.round_adapter import RoundRecord, PhaseResult
from ssflow.market.ashare_rules import BoardType
from ssflow.report_lob import render_lob_report
from ssflow.calibration_lob import (
    CalibrationResult,
    calibrate_single,
    render_calibration_report,
)
from ssflow.calibration_library import CALIBRATION_EVENTS


# ─── Report Tests ───


class TestLobReport:
    def _run_sim(self, **kwargs) -> SimulationResult:
        defaults = dict(
            ticker="TEST",
            prev_close=100.0,
            board_type=BoardType.NORMAL,
            n_days=2,
            ticks_per_session=15,
            background_seed=42,
        )
        defaults.update(kwargs)
        return run_simulation_lob(**defaults)

    def test_report_renders(self):
        result = self._run_sim()
        md = render_lob_report(result)
        assert isinstance(md, str)
        assert len(md) > 100

    def test_report_has_header(self):
        result = self._run_sim()
        md = render_lob_report(result)
        assert "ssFlow Market Simulation Report" in md
        assert "LOB Engine" in md
        assert "TEST" in md

    def test_report_has_price_info(self):
        result = self._run_sim()
        md = render_lob_report(result)
        assert "¥100.00" in md  # initial price
        assert "Total fills" in md

    def test_report_has_rounds(self):
        result = self._run_sim()
        md = render_lob_report(result)
        assert "Day 0" in md or "Round 0" in md

    def test_report_has_tape_summary(self):
        result = self._run_sim()
        md = render_lob_report(result)
        assert "Trade Tape Summary" in md
        assert "VWAP" in md

    def test_report_has_book_state(self):
        result = self._run_sim()
        md = render_lob_report(result)
        assert "Order Book" in md

    def test_report_has_mechanism_note(self):
        result = self._run_sim()
        md = render_lob_report(result)
        assert "How This Report Works" in md
        assert "trade tape" in md
        assert "no formula" in md.lower() or "no formula" in md

    def test_report_with_agents(self):
        from ssflow.cognition.intent_resolver import StructuredIntent, Urgency

        def buyer(round_idx, day_idx, memories, snapshot):
            return [
                StructuredIntent(
                    persona_id=m.persona_id,
                    agent_id=m.agent_id,
                    side="buy" if day_idx == 0 else "hold",
                    size_fraction=0.2 if day_idx == 0 else 0.0,
                    urgency=Urgency.PATIENT if day_idx == 0 else Urgency.NONE,
                )
                for m in memories
            ]

        result = self._run_sim(
            agent_configs=[
                {"agent_id": "a1", "persona_id": "retail", "cash": 50_000},
            ],
            agent_decisions=buyer,
        )
        md = render_lob_report(result)
        assert "Per-Agent P&L" in md


# ─── Calibration Tests ───


class TestCalibration:
    def test_calibration_library_exists(self):
        assert len(CALIBRATION_EVENTS) > 0

    def test_calibrate_single(self):
        if not CALIBRATION_EVENTS:
            pytest.skip("No calibration events")

        event = CALIBRATION_EVENTS[0]
        result = calibrate_single(
            event,
            n_days=2,
            ticks_per_session=10,
            seed=42,
        )
        assert isinstance(result, CalibrationResult)
        assert result.event_id == event.id
        assert result.sim_n_fills > 0
        assert 0.0 <= result.score <= 1.0

    def test_calibration_result_structure(self):
        cr = CalibrationResult(
            event_id="test",
            event_name="Test Event",
            ticker="TEST",
            actual_day1_return=-0.10,
            actual_path_returns=[-0.10, -0.15, -0.12],
            sim_cumulative_return=-0.05,
            sim_round_returns=[-0.03, -0.02],
            sim_n_fills=100,
            direction_correct=True,
            magnitude_error=0.05,
            path_rmse=0.03,
        )
        assert cr.direction_correct
        assert cr.score > 0  # should be positive since direction is correct

    def test_calibration_score_direction_miss(self):
        cr = CalibrationResult(
            event_id="test",
            event_name="Test",
            ticker="TEST",
            actual_day1_return=-0.10,
            actual_path_returns=[],
            sim_cumulative_return=0.05,  # wrong direction
            sim_round_returns=[],
            sim_n_fills=100,
            direction_correct=False,
            magnitude_error=0.15,
            path_rmse=0.10,
        )
        assert cr.score < 0.5  # penalized for wrong direction

    def test_render_calibration_report(self):
        results = [
            CalibrationResult(
                event_id="e1", event_name="Event One", ticker="000001.SZ",
                actual_day1_return=-0.10, actual_path_returns=[-0.10],
                sim_cumulative_return=-0.08, sim_round_returns=[-0.08],
                sim_n_fills=50, direction_correct=True,
                magnitude_error=0.02, path_rmse=0.02,
            ),
            CalibrationResult(
                event_id="e2", event_name="Event Two", ticker="600000.SH",
                actual_day1_return=0.05, actual_path_returns=[0.05],
                sim_cumulative_return=-0.03, sim_round_returns=[-0.03],
                sim_n_fills=30, direction_correct=False,
                magnitude_error=0.08, path_rmse=0.08,
            ),
        ]
        md = render_calibration_report(results)
        assert "Calibration Report" in md
        assert "Event One" in md
        assert "Direction accuracy" in md
        assert "1/2" in md  # 1 out of 2 correct direction
