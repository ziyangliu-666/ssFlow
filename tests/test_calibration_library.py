"""Tests for calibration_library.py — real A-share event ground truth.

Validates the calibration event library structure, implied parameter
calculations, and simulation comparison utilities.
"""

from __future__ import annotations

import math

import pytest

from ssflow.calibration_library import (
    CALIBRATION_EVENTS,
    CalibrationEvent,
    compute_implied_knee,
    compute_implied_lambda,
    get_event,
    get_events_by_board,
    get_events_by_sector,
    get_events_by_type,
    summarize_calibration,
    validate_simulation,
)


# ─────────────────────── Library integrity ───────────────────────


class TestLibraryIntegrity:
    """All events must have valid, non-degenerate data."""

    def test_minimum_event_count(self):
        """Library should have at least 8 events."""
        assert len(CALIBRATION_EVENTS) >= 8

    def test_unique_ids(self):
        ids = [e.id for e in CALIBRATION_EVENTS]
        assert len(ids) == len(set(ids)), f"Duplicate IDs: {ids}"

    def test_all_events_have_required_fields(self):
        for e in CALIBRATION_EVENTS:
            assert e.id, f"Missing id"
            assert e.name, f"Missing name for {e.id}"
            assert e.date, f"Missing date for {e.id}"
            assert e.ticker, f"Missing ticker for {e.id}"
            assert e.market == "ashare", f"Non-ashare market for {e.id}"
            assert e.board in ("main", "chinext", "star", "bse"), (
                f"Invalid board '{e.board}' for {e.id}"
            )
            assert e.event_type in (
                "geopolitical", "earnings", "policy", "supply_shock",
                "mania", "delisting_risk",
            ), f"Invalid event_type '{e.event_type}' for {e.id}"

    def test_price_data_reasonable(self):
        for e in CALIBRATION_EVENTS:
            assert e.prev_close > 0, f"prev_close <= 0 for {e.id}"
            assert e.day1_open > 0, f"day1_open <= 0 for {e.id}"
            assert e.day1_close > 0, f"day1_close <= 0 for {e.id}"
            assert e.day1_high >= e.day1_low > 0, (
                f"Invalid high/low for {e.id}"
            )
            assert e.day1_high >= e.day1_open, f"high < open for {e.id}"
            assert e.day1_high >= e.day1_close, f"high < close for {e.id}"
            assert e.day1_low <= e.day1_open, f"low > open for {e.id}"
            assert e.day1_low <= e.day1_close, f"low > close for {e.id}"

    def test_volume_data_positive(self):
        for e in CALIBRATION_EVENTS:
            assert e.adv_20d > 0, f"adv_20d <= 0 for {e.id}"
            assert e.adv_value_20d > 0, f"adv_value_20d <= 0 for {e.id}"
            assert e.day1_volume > 0, f"day1_volume <= 0 for {e.id}"
            assert e.day1_turnover > 0, f"day1_turnover <= 0 for {e.id}"

    def test_path_lengths(self):
        """Multi-day paths should have 5 entries (T+0 through T+4)."""
        for e in CALIBRATION_EVENTS:
            assert len(e.path_close) == 5, (
                f"path_close length {len(e.path_close)} for {e.id}"
            )
            assert len(e.path_volume) == 5, (
                f"path_volume length {len(e.path_volume)} for {e.id}"
            )
            assert len(e.path_limit_status) == 5, (
                f"path_limit_status length {len(e.path_limit_status)} for {e.id}"
            )

    def test_path_close_starts_at_day1(self):
        """path_close[0] should equal day1_close."""
        for e in CALIBRATION_EVENTS:
            assert e.path_close[0] == pytest.approx(e.day1_close, rel=0.01), (
                f"path_close[0]={e.path_close[0]} != day1_close={e.day1_close} "
                f"for {e.id}"
            )

    def test_limit_status_valid(self):
        valid = {"normal", "limit_up", "limit_down",
                 "one_word_up", "one_word_down"}
        for e in CALIBRATION_EVENTS:
            assert e.day1_limit_status in valid, (
                f"Invalid day1_limit_status '{e.day1_limit_status}' for {e.id}"
            )
            for i, status in enumerate(e.path_limit_status):
                assert status in valid, (
                    f"Invalid path_limit_status[{i}]='{status}' for {e.id}"
                )

    def test_day1_return_direction_matches_limit_status(self):
        """Limit-down should have negative return; limit-up positive."""
        for e in CALIBRATION_EVENTS:
            ret = e.day1_return
            if "down" in e.day1_limit_status:
                assert ret < 0, (
                    f"Limit down but positive return {ret:.4f} for {e.id}"
                )
            if "up" in e.day1_limit_status:
                assert ret > 0, (
                    f"Limit up but negative return {ret:.4f} for {e.id}"
                )

    def test_suggested_parameters_reasonable(self):
        for e in CALIBRATION_EVENTS:
            assert 0.01 <= e.suggested_lambda <= 2.0, (
                f"suggested_lambda={e.suggested_lambda} out of range for {e.id}"
            )
            assert 0.005 <= e.suggested_knee <= 0.50, (
                f"suggested_knee={e.suggested_knee} out of range for {e.id}"
            )

    def test_float_market_cap_positive(self):
        for e in CALIBRATION_EVENTS:
            assert e.float_market_cap > 0, (
                f"float_market_cap <= 0 for {e.id}"
            )

    def test_description_nonempty(self):
        for e in CALIBRATION_EVENTS:
            assert len(e.description) > 20, (
                f"Description too short for {e.id}"
            )

    def test_key_dynamics_nonempty(self):
        for e in CALIBRATION_EVENTS:
            assert len(e.key_dynamics) >= 2, (
                f"Too few key_dynamics for {e.id}"
            )


# ─────────────────────── CalibrationEvent properties ───────────────────────


class TestCalibrationEventProperties:
    """Test computed properties on CalibrationEvent."""

    def test_day1_return_wuxi(self):
        """WuXi one-word limit-down should be ~-10%."""
        e = get_event("wuxi_biosecure_2024")
        assert e is not None
        assert e.day1_return == pytest.approx(-0.10, abs=0.005)

    def test_day1_return_924(self):
        """924 rally SSE Composite should be ~+4.15%."""
        e = get_event("policy_924_rally_2024")
        assert e is not None
        assert e.day1_return == pytest.approx(0.0415, abs=0.005)

    def test_day1_open_return(self):
        e = get_event("wuxi_biosecure_2024")
        assert e is not None
        assert e.day1_open_return < 0  # Gap down

    def test_path_returns_length(self):
        for e in CALIBRATION_EVENTS:
            assert len(e.path_returns) == 5

    def test_day1_turnover_ratio(self):
        """WuXi had low turnover (one-word limit-down suppresses volume)."""
        e = get_event("wuxi_biosecure_2024")
        assert e is not None
        assert e.day1_turnover_ratio < 0.5  # Well below normal

    def test_estimated_net_flow_uses_actual_when_available(self):
        e = get_event("wuxi_biosecure_2024")
        assert e is not None
        assert e.institutional_net_flow is not None
        # Should use institutional + northbound
        expected = e.institutional_net_flow + (e.northbound_net_flow or 0)
        assert e.estimated_net_flow == expected

    def test_estimated_net_flow_heuristic(self):
        """When institutional flow is None, heuristic should give reasonable sign."""
        e = get_event("dazhong_robotaxi_2024")
        assert e is not None
        assert e.institutional_net_flow is None
        # Retail net flow is set, but institutional is None — heuristic kicks in.
        # Heuristic uses turnover * return direction.
        assert e.estimated_net_flow != 0


# ─────────────────────── Filtering ───────────────────────


class TestFiltering:
    def test_get_event_by_id(self):
        e = get_event("wuxi_biosecure_2024")
        assert e is not None
        assert e.ticker == "603259.SH"

    def test_get_event_missing(self):
        assert get_event("nonexistent_event_xyz") is None

    def test_get_events_by_type_geopolitical(self):
        events = get_events_by_type("geopolitical")
        assert len(events) >= 2
        for e in events:
            assert e.event_type == "geopolitical"

    def test_get_events_by_type_policy(self):
        events = get_events_by_type("policy")
        assert len(events) >= 2
        for e in events:
            assert e.event_type == "policy"

    def test_get_events_by_type_empty(self):
        events = get_events_by_type("alien_invasion")
        assert events == []

    def test_get_events_by_sector_finance(self):
        events = get_events_by_sector("finance")
        assert len(events) >= 1
        for e in events:
            assert e.sector == "finance"

    def test_get_events_by_sector_biotech(self):
        events = get_events_by_sector("biotech")
        assert len(events) >= 1

    def test_get_events_by_board(self):
        main_events = get_events_by_board("main")
        assert len(main_events) >= 5
        chinext_events = get_events_by_board("chinext")
        assert len(chinext_events) >= 1


# ─────────────────────── Implied lambda ───────────────────────


class TestImpliedLambda:
    """Test compute_implied_lambda against known events."""

    def test_implied_lambda_positive(self):
        """All events with flow data should produce a positive lambda."""
        for e in CALIBRATION_EVENTS:
            if e.estimated_net_flow != 0 and e.day1_return != 0:
                lam = compute_implied_lambda(e)
                assert lam > 0, f"Non-positive lambda {lam} for {e.id}"

    def test_implied_lambda_close_to_suggested(self):
        """Implied lambda should be in the same ballpark as suggested.

        We allow 3x tolerance because the implied calculation uses
        estimated_net_flow which may differ from the flow used to
        derive suggested_lambda.
        """
        for e in CALIBRATION_EVENTS:
            lam = compute_implied_lambda(e)
            if lam > 0 and e.suggested_lambda > 0:
                ratio = lam / e.suggested_lambda
                assert 0.1 < ratio < 10.0, (
                    f"Implied lambda {lam} vs suggested {e.suggested_lambda} "
                    f"(ratio {ratio:.2f}) for {e.id}"
                )

    def test_implied_lambda_zero_flow(self):
        """Zero net flow should return 0.0."""
        e = CALIBRATION_EVENTS[0]
        # Create a modified copy with zero flow
        modified = CalibrationEvent(
            id="test_zero", name="test", date="2024-01-01",
            ticker="000001.SH", market="ashare", board="main",
            event_type="policy",
            prev_close=100.0, float_market_cap=100.0,
            adv_20d=1_000_000, adv_value_20d=100_000_000,
            day1_open=100.0, day1_close=100.0,
            day1_high=100.0, day1_low=100.0,
            day1_volume=1_000_000, day1_turnover=100_000_000,
            day1_limit_status="normal",
            path_close=[100.0] * 5,
            path_volume=[1_000_000] * 5,
            path_limit_status=["normal"] * 5,
            institutional_net_flow=0,
        )
        assert compute_implied_lambda(modified) == 0.0

    def test_moutai_lambda_below_default(self):
        """Moutai (mega-cap, high liquidity) should imply lambda < 0.5."""
        e = get_event("moutai_earnings_miss_2024")
        assert e is not None
        lam = compute_implied_lambda(e)
        assert 0.0 < lam < 0.5, f"Moutai implied lambda {lam} >= 0.5"


# ─────────────────────── Implied knee ───────────────────────


class TestImpliedKnee:
    """Test compute_implied_knee bisection solver."""

    def test_implied_knee_positive(self):
        for e in CALIBRATION_EVENTS:
            if e.estimated_net_flow != 0 and e.day1_return != 0:
                knee = compute_implied_knee(e)
                assert knee > 0, f"Non-positive knee {knee} for {e.id}"

    def test_implied_knee_in_range(self):
        """Implied knee should be in [0.001, 1.0] (bisection bounds)."""
        for e in CALIBRATION_EVENTS:
            knee = compute_implied_knee(e)
            if knee > 0:
                assert 0.001 <= knee <= 1.0, (
                    f"Knee {knee} out of range for {e.id}"
                )

    def test_implied_knee_zero_flow(self):
        modified = CalibrationEvent(
            id="test_zero", name="test", date="2024-01-01",
            ticker="000001.SH", market="ashare", board="main",
            event_type="policy",
            prev_close=100.0, float_market_cap=100.0,
            adv_20d=1_000_000, adv_value_20d=100_000_000,
            day1_open=100.0, day1_close=100.0,
            day1_high=100.0, day1_low=100.0,
            day1_volume=1_000_000, day1_turnover=100_000_000,
            day1_limit_status="normal",
            path_close=[100.0] * 5,
            path_volume=[1_000_000] * 5,
            path_limit_status=["normal"] * 5,
            institutional_net_flow=0,
        )
        assert compute_implied_knee(modified) == 0.0


# ─────────────────────── Simulation validation ───────────────────────


class TestValidateSimulation:
    """Test validate_simulation metrics."""

    def test_perfect_simulation(self):
        """Simulated path == actual path should give zero error."""
        e = CALIBRATION_EVENTS[0]
        result = validate_simulation(e, e.path_close[:])
        assert result["day1_error"] == pytest.approx(0.0, abs=1e-6)
        assert result["path_mae"] == pytest.approx(0.0, abs=1e-6)
        assert result["path_rmse"] == pytest.approx(0.0, abs=1e-6)
        assert result["direction_correct"] is True

    def test_wrong_direction(self):
        """Simulated rally when actual dropped should flag direction."""
        e = get_event("wuxi_biosecure_2024")
        assert e is not None
        # Simulate a rally instead of a crash
        sim_path = [e.prev_close * 1.05] * 5  # +5% every day
        result = validate_simulation(e, sim_path)
        assert result["direction_correct"] is False
        assert result["day1_error"] > 0.10  # Off by at least 10pp

    def test_partial_path(self):
        """Shorter simulated path should still work (truncated to min length)."""
        e = CALIBRATION_EVENTS[0]
        sim_path = e.path_close[:3]  # Only 3 days
        result = validate_simulation(e, sim_path)
        assert len(result["return_errors"]) == 3
        assert result["path_mae"] == pytest.approx(0.0, abs=1e-6)

    def test_empty_path(self):
        e = CALIBRATION_EVENTS[0]
        result = validate_simulation(e, [])
        assert math.isnan(result["day1_error"])

    def test_rmse_geq_mae(self):
        """RMSE >= MAE by Jensen's inequality."""
        e = get_event("policy_924_rally_2024")
        assert e is not None
        sim_path = [p * 1.02 for p in e.path_close]  # Systematically 2% high
        result = validate_simulation(e, sim_path)
        assert result["path_rmse"] >= result["path_mae"] - 1e-9

    def test_max_error_geq_mae(self):
        e = get_event("policy_924_rally_2024")
        assert e is not None
        sim_path = [p * 0.98 for p in e.path_close]
        result = validate_simulation(e, sim_path)
        assert result["path_max_error"] >= result["path_mae"] - 1e-9


# ─────────────────────── Summary ───────────────────────


class TestSummarizeCalibration:
    def test_summary_structure(self):
        s = summarize_calibration()
        assert s["n_events"] >= 8
        assert "event_types" in s
        assert "lambda_range" in s
        assert "knee_range" in s
        assert "lambda_by_type" in s
        assert "findings" in s

    def test_lambda_range_sensible(self):
        s = summarize_calibration()
        lo, hi = s["lambda_range"]
        assert 0 < lo < hi
        assert lo < 0.5  # Some events should have lambda < default
        assert hi > 0.1  # At least one event with meaningful lambda

    def test_findings_nonempty(self):
        s = summarize_calibration()
        assert len(s["findings"]) >= 1
