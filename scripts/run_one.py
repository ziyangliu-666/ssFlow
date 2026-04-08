#!/usr/bin/env python
"""CLI runner for one simulation. The founder's primary daily-use loop.

Example:
    python scripts/run_one.py \\
        --event ./predictions/event-1.txt \\
        --ticker 002594 \\
        --event-type earnings \\
        --event-date 2026-04-09 \\
        --prior-consensus ./predictions/consensus-1.txt
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Ensure src/ is on the path when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ssfish.aggregation import aggregate
from ssfish.config import settings
from ssfish.event import VALID_EVENT_TYPES, Event
from ssfish.llm_client import cost_tracker
from ssfish.persona import load_personas, persona_set_hash
from ssfish.report import (
    render_safe_or_quarantine,
    render_sandbox_safe_or_quarantine,
    save_report,
)
from ssfish.sandbox import run_sandbox_simulation
from ssfish.scorecard import init_db, insert_sandbox_simulation, insert_simulation
from ssfish.simulation import run_simulation


def _read_or_blank(path: str | None) -> str:
    if not path:
        return ""
    p = Path(path)
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8").strip()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ssFish — run one A-share event simulation")
    p.add_argument("--event", required=True, help="Path to event text file")
    p.add_argument("--ticker", required=True, help="A-share ticker code (6 digits)")
    p.add_argument(
        "--event-type",
        default="other",
        choices=sorted(VALID_EVENT_TYPES),
        help="Event type label",
    )
    p.add_argument("--event-date", required=True, help="Event date (YYYY-MM-DD)")
    p.add_argument("--prior-consensus", default=None, help="Path to prior consensus text file")
    p.add_argument("--recent-price-action", default=None, help="Path to recent price text file")
    p.add_argument("--sector-context", default=None, help="Path to sector context text file")
    p.add_argument(
        "--personas",
        default=None,
        help="Path to personas YAML (defaults to personas/ashare.yaml)",
    )
    p.add_argument(
        "--rounds", type=int, default=None, help="Override n_rounds (defaults to settings)"
    )
    p.add_argument(
        "--mode",
        choices=["sentiment", "sandbox"],
        default="sentiment",
        help="Simulation mode: 'sentiment' (legacy heuristic) or 'sandbox' "
             "(agent-based market model). sandbox requires --current-price + --adv",
    )
    p.add_argument(
        "--current-price",
        type=float,
        default=None,
        help="Current price (CNY) — required for --mode sandbox",
    )
    p.add_argument(
        "--adv",
        type=float,
        default=None,
        help="Average daily volume (CNY) — required for --mode sandbox. "
             "Trailing 30d is conventional. Will be filled by Tushare in B8 v2.",
    )
    return p.parse_args()


async def amain() -> int:
    args = parse_args()

    event_text = _read_or_blank(args.event)
    if not event_text:
        print(f"ERROR: event file empty or missing: {args.event}", file=sys.stderr)
        return 2

    try:
        event = Event(
            ticker=args.ticker,
            event_text=event_text,
            event_type=args.event_type,
            event_date=args.event_date,
            prior_consensus=_read_or_blank(args.prior_consensus),
            recent_price_action=_read_or_blank(args.recent_price_action),
            sector_context=_read_or_blank(args.sector_context),
            current_price=args.current_price,
            adv_cny=args.adv,
        )
    except ValueError as exc:
        print(f"ERROR: invalid event: {exc}", file=sys.stderr)
        return 2

    if args.mode == "sandbox" and not event.is_sandbox_ready:
        print(
            "ERROR: --mode sandbox requires --current-price and --adv. "
            "Sandbox mode runs an agent-based market model that needs the "
            "ticker's current price and average daily volume in CNY.",
            file=sys.stderr,
        )
        return 2

    personas_path = Path(args.personas) if args.personas else settings.personas_dir / "ashare.yaml"
    personas = load_personas(personas_path)

    init_db()

    print(f"# ssFish run: {event.ticker} {event.event_type} {event.event_date} (mode={args.mode})", file=sys.stderr)
    print(f"#   {len(personas)} personas, {args.rounds or settings.n_rounds} rounds, "
          f"model={settings.default_model}", file=sys.stderr)
    print(f"#   context completeness: {event.context_completeness:.0%}", file=sys.stderr)
    print(f"#   running...", file=sys.stderr, flush=True)

    if args.mode == "sandbox":
        return await _run_sandbox(event, personas, args)
    return await _run_sentiment(event, personas, args)


async def _run_sentiment(event: Event, personas, args) -> int:
    result = await run_simulation(event, personas, n_rounds=args.rounds)

    report = aggregate(result)
    display, ok = render_safe_or_quarantine(result, report)
    report_path = save_report(display, result.simulation_id)

    insert_simulation(
        event_ticker=event.ticker,
        event_date=event.event_date,
        event_type=event.event_type,
        event_text_hash=event.text_hash,
        persona_set_hash=persona_set_hash(personas),
        model_default=settings.default_model,
        seed=settings.seed,
        n_personas=result.n_personas,
        n_rounds=result.n_rounds,
        sentiment_mean=report.sentiment_mean,
        sentiment_std=report.sentiment_std,
        implied_move_low=report.implied_move_low,
        implied_move_high=report.implied_move_high,
        implied_move_confidence=report.implied_move_confidence,
        blind_spots=report.blind_spots,
        full_report_path=str(report_path),
        cost_usd=result.cost_usd,
        elapsed_seconds=result.elapsed_seconds,
        simulation_id=result.simulation_id,
        round_fingerprints=result.round_fingerprints,
        llm_seed=result.llm_seed,
    )

    print(f"# done in {result.elapsed_seconds:.1f}s, "
          f"cost ${result.cost_usd:.4f} (session total ${cost_tracker.total_cost_usd:.4f})",
          file=sys.stderr)
    print(f"# report: {report_path}", file=sys.stderr)
    print(f"# compliance: {'PASS' if ok else 'QUARANTINED'}", file=sys.stderr)
    print(f"# simulation_id: {result.simulation_id}", file=sys.stderr)
    print(file=sys.stderr)

    print(display)
    return 0 if ok else 1


async def _run_sandbox(event: Event, personas, args) -> int:
    result = await run_sandbox_simulation(event, personas, n_rounds=args.rounds)

    display, ok = render_sandbox_safe_or_quarantine(result)
    report_path = save_report(display, result.simulation_id)

    # Collect strategic signals from the last round
    strategic_signals = {}
    by_id = {p.id: p for p in personas}
    if result.rounds:
        last = result.rounds[-1]
        for cid, cf in last.class_flows.items():
            p = by_id.get(cid)
            if p and p.contributes_to_strategic_signal and cf.strategic_signal:
                strategic_signals[cid] = cf.strategic_signal

    fingerprints_per_round = [r.fingerprints for r in result.rounds]

    insert_sandbox_simulation(
        event_ticker=event.ticker,
        event_date=event.event_date,
        event_type=event.event_type,
        event_text_hash=event.text_hash,
        persona_set_hash=persona_set_hash(personas),
        model_default=settings.default_model,
        seed=settings.seed,
        n_personas=result.n_personas,
        n_rounds=result.n_rounds,
        initial_price=result.initial_price,
        final_price=result.final_price,
        cumulative_delta_pct=result.cumulative_delta_pct,
        price_trajectory=result.price_trajectory,
        class_pnl=result.compute_class_pnl(),
        strategic_signals=strategic_signals or None,
        lambda_used=result.lambda_used,
        adv_used=result.adv_cny_used,
        full_report_path=str(report_path),
        cost_usd=result.cost_usd,
        elapsed_seconds=result.elapsed_seconds,
        simulation_id=result.simulation_id,
        round_fingerprints=fingerprints_per_round,
        llm_seed=result.llm_seed,
    )

    print(f"# done in {result.elapsed_seconds:.1f}s, "
          f"cost ${result.cost_usd:.4f} (session total ${cost_tracker.total_cost_usd:.4f})",
          file=sys.stderr)
    print(f"# price: ¥{result.initial_price:.2f} → ¥{result.final_price:.2f} "
          f"({result.cumulative_delta_pct * 100:+.2f}%)", file=sys.stderr)
    print(f"# report: {report_path}", file=sys.stderr)
    print(f"# compliance: {'PASS' if ok else 'QUARANTINED'}", file=sys.stderr)
    print(f"# simulation_id: {result.simulation_id}", file=sys.stderr)
    print(file=sys.stderr)

    print(display)
    return 0 if ok else 1


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
