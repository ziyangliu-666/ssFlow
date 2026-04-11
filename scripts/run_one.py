#!/usr/bin/env python
"""CLI runner for one ssFlow sandbox simulation.

ssFlow runs an agent-based market model: each persona class spawns N
stochastic agents with real capital + holdings, an LLM call per class
returns an action distribution, the sandbox samples agents and aggregates
into net order flow, then applies the Kyle square-root price impact
formula to derive a price trajectory.

There is only one execution mode: `sandbox`. The legacy `sentiment`
heuristic mode was removed in the G2 cleanup — sandbox supersedes it.

Two ways to invoke:

1. Explicit fields (legacy / scriptable):

    uv run python scripts/run_one.py \\
      --event ./predictions/event-1.txt \\
      --ticker 002594 \\
      --event-type earnings \\
      --event-date 2026-04-29 \\
      --current-price 218.50 \\
      --adv 8000000000 \\
      --personas personas/ashare.yaml

2. Free-form input via the event extractor (G5, recommended):

    uv run python scripts/run_one.py \\
      --input "BYD Q1 2026 earnings: 营收 +18% beat, 毛利率 -2.3pp miss" \\
      --confirm  # auto-confirm extractor output, no interactive prompt

The free-form mode uses event_extractor to identify the market, instrument,
event type, fetches context, and pre-fills all other fields. The user
sees what was extracted and confirms (or edits with --no-confirm).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# Ensure src/ is on the path when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Configure logging so ssflow module output is visible.
# LOG_LEVEL env var controls verbosity (default INFO).
logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stderr,
)

from ssflow.config import settings
from ssflow.distillation import distill
from ssflow.event import VALID_EVENT_TYPES, Event
from ssflow.event_extractor import extract_event
from ssflow.llm_client import cost_tracker
from ssflow.oasis_engine import run_simulation
from ssflow.persona import load_personas, persona_set_hash
from ssflow.report import render_simulation_safe_or_quarantine, save_report
from ssflow.round_schedule import PRESET_NAMES, make_schedule
from ssflow.sandbox_generator import build_from_template
from ssflow.scorecard import init_db, insert_sandbox_simulation


def _read_or_blank(path: str | None) -> str:
    if not path:
        return ""
    p = Path(path)
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8").strip()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="ssFlow — run one market sandbox simulation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Two ways to invoke:

  1. Free-form input (recommended, ssFlow auto-extracts everything):
       --input "NVIDIA Q1 2026 earnings beat, data center revenue +75% YoY"
       --confirm                  (skip interactive confirm prompt)

  2. Explicit fields (legacy, scriptable):
       --event ./byd.txt --ticker 002594 --event-type earnings ...
        """,
    )
    # Free-form input mode (Stage 0 extractor)
    p.add_argument(
        "--input",
        default=None,
        help="Free-form input string. ssFlow will run the event extractor "
             "to identify market / instrument / event_type / current price / "
             "ADV automatically. Mutually exclusive with --event.",
    )
    p.add_argument(
        "--confirm",
        action="store_true",
        help="Skip the interactive confirmation step (use auto-extracted values as-is)",
    )

    # Explicit field mode (legacy)
    p.add_argument("--event", default=None, help="Path to event text file")
    p.add_argument("--ticker", default=None, help="Instrument ticker (e.g. 002594, NVDA, BTC-USD)")
    p.add_argument(
        "--event-type",
        default="other",
        choices=sorted(VALID_EVENT_TYPES),
        help="Event type label",
    )
    p.add_argument("--event-date", default=None, help="Event date (YYYY-MM-DD)")
    p.add_argument("--prior-consensus", default=None, help="Path to prior consensus text file")
    p.add_argument("--recent-price-action", default=None, help="Path to recent price text file")
    p.add_argument("--sector-context", default=None, help="Path to sector context text file")
    p.add_argument(
        "--personas",
        default=None,
        help="Path to personas YAML (auto-suggested in --input mode, "
             "required in explicit mode)",
    )
    p.add_argument(
        "--rounds", type=int, default=None,
        help="Override n_rounds (defaults to schedule's round count)",
    )
    p.add_argument(
        "--schedule",
        default="earnings-5d",
        help=f"Round schedule preset or 'custom'. "
             f"Presets: {', '.join(PRESET_NAMES)}. "
             f"Default: earnings-5d",
    )
    p.add_argument(
        "--current-price",
        type=float,
        default=None,
        help="Current price of the instrument (auto-extracted in --input mode)",
    )
    p.add_argument(
        "--adv",
        type=float,
        default=None,
        help="Average daily volume in the same currency as --current-price "
             "(auto-extracted in --input mode)",
    )
    p.add_argument(
        "--no-universe",
        action="store_true",
        help="Skip distillation — run in single-instrument mode. By default "
             "run_one.py builds a 3-6 instrument universe with real K-line "
             "history for each so agents see sector context.",
    )
    return p.parse_args()


async def _build_event_from_input(args: argparse.Namespace) -> tuple[Event, str | None]:
    """Free-form input mode: run the event extractor + confirm with user.

    Returns (event, suggested_personas_path).
    """
    print(f"# Extracting event from input: {args.input[:120]}{'...' if len(args.input) > 120 else ''}",
          file=sys.stderr)
    print(f"# (this takes ~20-40s, runs LLM classification + web research + synthesis)",
          file=sys.stderr, flush=True)

    proposal = await extract_event(args.input)

    # Show the extracted proposal
    print(f"\n=== Auto-extracted EventProposal ===", file=sys.stderr)
    print(f"  Market:        {proposal.market}", file=sys.stderr)
    print(f"  Instrument:    {proposal.instrument} ({proposal.ticker})", file=sys.stderr)
    print(f"  Event type:    {proposal.event_type}", file=sys.stderr)
    print(f"  Event date:    {proposal.event_date}", file=sys.stderr)
    print(f"  Currency:      {proposal.price_currency}", file=sys.stderr)
    print(f"  Current price: {proposal.current_price}", file=sys.stderr)
    print(f"  ADV:           {proposal.adv_value}", file=sys.stderr)
    print(f"  Pack:          {proposal.suggested_personas_path}", file=sys.stderr)
    print(f"  Confidence:    {proposal.confidence}", file=sys.stderr)
    if proposal.extraction_warnings:
        print(f"  Warnings:      {proposal.extraction_warnings}", file=sys.stderr)
    print(f"  Sources:       {len(proposal.sources_consulted)} URLs consulted", file=sys.stderr)
    print(file=sys.stderr)

    if not args.confirm:
        try:
            response = input("Proceed with these values? (y/n/edit): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("Cancelled", file=sys.stderr)
            sys.exit(1)
        if response not in ("y", "yes", ""):
            print("Aborted by user. Re-run with --confirm to skip this prompt.",
                  file=sys.stderr)
            sys.exit(0)

    if proposal.current_price is None or proposal.adv_value is None:
        print(
            "ERROR: extraction did not produce current_price + adv_value. "
            "Either edit the proposal manually or pass --current-price + --adv "
            "explicitly.",
            file=sys.stderr,
        )
        sys.exit(2)

    return proposal.to_event(), proposal.suggested_personas_path


async def amain() -> int:
    args = parse_args()
    suggested_path: str | None = None

    if args.input:
        # Free-form input mode (Stage 0 extractor)
        event, suggested_path = await _build_event_from_input(args)
    else:
        # Explicit field mode (legacy)
        if not args.event or not args.ticker or not args.event_date:
            print(
                "ERROR: explicit mode requires --event, --ticker, --event-date. "
                "Or use --input for free-form input.",
                file=sys.stderr,
            )
            return 2
        if args.current_price is None or args.adv is None:
            print(
                "ERROR: explicit mode requires --current-price and --adv.",
                file=sys.stderr,
            )
            return 2
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
                adv_value=args.adv,
            )
        except ValueError as exc:
            print(f"ERROR: invalid event: {exc}", file=sys.stderr)
            return 2

    personas_path = args.personas or suggested_path
    if not personas_path:
        print(
            "ERROR: no personas pack specified and event extractor did not "
            "suggest one. Pass --personas personas/<market>.yaml.",
            file=sys.stderr,
        )
        return 2

    personas = load_personas(Path(personas_path))
    init_db()

    schedule = make_schedule(args.schedule, event.event_date)
    n_rounds = args.rounds or schedule.n_rounds

    print(f"# ssFlow run: {event.ticker} {event.event_type} {event.event_date}", file=sys.stderr)
    print(f"#   {len(personas)} personas ({sum(1 for p in personas if p.sandbox is not None)} traders + "
          f"{sum(1 for p in personas if p.sandbox is None)} info entities), "
          f"{n_rounds} rounds, model={settings.default_model}",
          file=sys.stderr)
    print(f"#   context completeness: {event.context_completeness:.0%}", file=sys.stderr)

    # Build entity graph for the Entity State Sandbox.
    # Uses template defaults (no LLM call, zero cost).
    entity_graph = build_from_template(
        topic=event.event_text or f"{event.ticker} {event.event_type}",
        market=event.market or "ashare",
        current_price=float(event.current_price or 0),
    )
    print(f"#   entity graph: {len(entity_graph.entities)} entities, "
          f"{len(entity_graph.flows)} flows, "
          f"{len(entity_graph.thresholds)} thresholds "
          f"({sum(1 for t in entity_graph.thresholds if t.effect.effect_type == 'force_action')} force_action)",
          file=sys.stderr)

    # Distillation: build InstrumentUniverse with 3-6 related instruments +
    # real 30-day K-line history, so agents see sector context and trends
    # instead of just a single ticker's current price.
    instrument_universe = None
    if not args.no_universe:
        print(f"#   distilling instrument universe (LLM + Sina fetches)...", file=sys.stderr, flush=True)
        try:
            instrument_universe = await distill(
                topic=event.event_text[:400] if event.event_text else f"{event.ticker} {event.event_type}",
                market=event.market or "ashare",
                event_ticker=event.ticker,
                event_price=float(event.current_price or 0) or None,
            )
            tickers_str = ", ".join(
                f"{i.ticker}[{i.relationship}]"
                for i in instrument_universe.instruments
            )
            kline_counts = [
                len(i.kline_30d) for i in instrument_universe.instruments
            ]
            print(
                f"#   universe: {len(instrument_universe.instruments)} "
                f"instruments — {tickers_str}",
                file=sys.stderr,
            )
            print(
                f"#   K-line bars per instrument: "
                f"{sum(kline_counts)} total (min={min(kline_counts) if kline_counts else 0}, "
                f"max={max(kline_counts) if kline_counts else 0})",
                file=sys.stderr,
            )
        except Exception as exc:
            print(
                f"#   distillation failed ({exc}) — falling back to "
                f"single-instrument mode",
                file=sys.stderr,
            )
            instrument_universe = None

    print(f"#   running OASIS simulation...", file=sys.stderr, flush=True)

    # OASIS engine is async; await it from amain.
    result = await run_simulation(
        event, personas, n_rounds=n_rounds, round_schedule=schedule,
        entity_graph=entity_graph,
        instrument_universe=instrument_universe,
    )
    display, ok = render_simulation_safe_or_quarantine(result)
    report_path = save_report(display, result.simulation_id)

    # Collect publication log for the scorecard
    publication_log = [
        {
            "publication_id": pub.publication_id,
            "author_persona_id": pub.author_persona_id,
            "author_archetype": pub.author_archetype,
            "content_type": pub.content_type,
            "text": pub.text,
            "round_idx": pub.round_idx,
            "authority_weight": pub.authority_weight,
            "references": pub.references,
            "oasis_post_id": pub.oasis_post_id,
            "likes": pub.likes,
            "reposts": pub.reposts,
        }
        for pub in result.all_publications
    ]

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
        strategic_signals=None,  # Phase I moves strategic signals into publications
        lambda_used=result.lambda_used,
        adv_used=result.adv_value_used,
        full_report_path=str(report_path),
        cost_usd=result.cost_usd,
        elapsed_seconds=result.elapsed_seconds,
        simulation_id=result.simulation_id,
        round_fingerprints=None,
        llm_seed=result.llm_seed,
        publication_log=publication_log or None,
        oasis_db_path=result.oasis_db_path,
    )

    print(f"# done in {result.elapsed_seconds:.1f}s, "
          f"cost ${result.cost_usd:.4f} (session total ${cost_tracker.total_cost_usd:.4f})",
          file=sys.stderr)
    print(f"# price: {result.initial_price:.2f} → {result.final_price:.2f} "
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
