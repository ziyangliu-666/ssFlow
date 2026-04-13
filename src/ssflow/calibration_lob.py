"""LOB engine calibration — compare simulated vs actual price paths.

Runs the LOB engine against calibration events from calibration_library.py
and compares the simulated price path against actual market data. This is
the LOB-specific replacement for the Kyle formula calibration.

Key difference: instead of tuning lambda/knee parameters, we tune
background agent parameters (ZI arrival rate, MM spread, MR anchor/threshold)
to reproduce realistic price dynamics.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .calibration_library import CalibrationEvent, CALIBRATION_EVENTS
from .engine.orchestrator import SimulationResult, run_simulation_lob
from .market.ashare_rules import BoardType, infer_board_type

log = logging.getLogger(__name__)


@dataclass
class CalibrationResult:
    """Result of comparing LOB simulation against actual market data."""

    event_id: str
    event_name: str
    ticker: str

    # Actual market data
    actual_day1_return: float
    actual_path_returns: list[float]

    # Simulated data
    sim_cumulative_return: float
    sim_round_returns: list[float]
    sim_n_fills: int

    # Comparison metrics
    direction_correct: bool           # did sim get the sign right?
    magnitude_error: float            # |sim_return - actual_return|
    path_rmse: float                  # RMSE of simulated vs actual path

    @property
    def score(self) -> float:
        """Calibration score: 0 (worst) to 1 (perfect).

        Weighted: 40% direction, 30% magnitude, 30% path similarity.
        """
        dir_score = 1.0 if self.direction_correct else 0.0
        mag_score = max(0.0, 1.0 - self.magnitude_error * 5)  # 20% error -> 0
        path_score = max(0.0, 1.0 - self.path_rmse * 10)      # 10% RMSE -> 0
        return 0.4 * dir_score + 0.3 * mag_score + 0.3 * path_score


def calibrate_single(
    event: CalibrationEvent,
    *,
    n_days: int = 5,
    ticks_per_session: int = 30,
    n_zi: int = 5,
    n_mm: int = 1,
    n_mr: int = 2,
    seed: int = 42,
) -> CalibrationResult:
    """Run the LOB engine against one calibration event and score the result."""

    board = infer_board_type(event.ticker)

    result = run_simulation_lob(
        ticker=event.ticker,
        prev_close=event.prev_close,
        board_type=board,
        n_days=min(n_days, len(event.path_close)),
        ticks_per_session=ticks_per_session,
        n_zi=n_zi,
        n_mm=n_mm,
        n_mr=n_mr,
        background_seed=seed,
    )

    # Compute simulated per-round returns
    sim_returns = []
    for r in result.rounds:
        if r.price_before > 0:
            sim_returns.append((r.price_after / r.price_before) - 1.0)

    # Compare against actual
    actual_returns = event.path_returns[:len(sim_returns)]

    # Direction check
    direction_correct = (
        (result.cumulative_delta_pct >= 0 and event.day1_return >= 0) or
        (result.cumulative_delta_pct < 0 and event.day1_return < 0)
    )

    # Magnitude error
    magnitude_error = abs(result.cumulative_delta_pct - event.day1_return)

    # Path RMSE
    if actual_returns and sim_returns:
        n = min(len(actual_returns), len(sim_returns))
        mse = sum((a - s) ** 2 for a, s in zip(actual_returns[:n], sim_returns[:n])) / n
        path_rmse = mse ** 0.5
    else:
        path_rmse = 1.0

    return CalibrationResult(
        event_id=event.id,
        event_name=event.name,
        ticker=event.ticker,
        actual_day1_return=event.day1_return,
        actual_path_returns=actual_returns,
        sim_cumulative_return=result.cumulative_delta_pct,
        sim_round_returns=sim_returns,
        sim_n_fills=result.n_total_fills,
        direction_correct=direction_correct,
        magnitude_error=magnitude_error,
        path_rmse=path_rmse,
    )


def calibrate_all(
    *,
    n_days: int = 5,
    ticks_per_session: int = 30,
    seed: int = 42,
) -> list[CalibrationResult]:
    """Run calibration against all events in the library."""
    results = []
    for event in CALIBRATION_EVENTS:
        try:
            result = calibrate_single(
                event,
                n_days=n_days,
                ticks_per_session=ticks_per_session,
                seed=seed,
            )
            results.append(result)
            log.info(
                "Calibration %s: direction=%s, magnitude_error=%.2f%%, score=%.2f",
                event.id,
                "OK" if result.direction_correct else "WRONG",
                result.magnitude_error * 100,
                result.score,
            )
        except Exception:
            log.exception("Calibration failed for %s", event.id)
    return results


def render_calibration_report(results: list[CalibrationResult]) -> str:
    """Render calibration results as a markdown table."""
    if not results:
        return "No calibration results."

    lines = [
        "# LOB Engine Calibration Report",
        "",
        "| Event | Ticker | Actual D1 | Sim Cumul | Direction | Mag Error | Score |",
        "|-------|--------|-----------|----------|-----------|-----------|-------|",
    ]

    total_score = 0.0
    for r in results:
        dir_mark = "OK" if r.direction_correct else "MISS"
        lines.append(
            f"| {r.event_name[:30]} | `{r.ticker}` | "
            f"{r.actual_day1_return*100:+.1f}% | {r.sim_cumulative_return*100:+.1f}% | "
            f"{dir_mark} | {r.magnitude_error*100:.1f}% | {r.score:.2f} |"
        )
        total_score += r.score

    avg_score = total_score / len(results)
    lines.append("")
    lines.append(f"**Average score**: {avg_score:.2f} / 1.00")
    lines.append(f"**Direction accuracy**: {sum(1 for r in results if r.direction_correct)}/{len(results)}")

    return "\n".join(lines)


__all__ = [
    "CalibrationResult",
    "calibrate_all",
    "calibrate_single",
    "render_calibration_report",
]
