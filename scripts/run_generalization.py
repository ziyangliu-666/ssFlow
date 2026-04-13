#!/usr/bin/env python
"""Batch runner for generalizability testing.

Runs all 20 diverse inputs through ssFlow, collects reports + metadata.
Outputs a summary TSV and individual report files.

Usage:
    PYTHONPATH=src .venv/bin/python scripts/run_generalization.py [--start N] [--only ID]
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stderr,
)

from generalization_inputs import GENERALIZATION_INPUTS

from ssflow.config import settings
from ssflow.distillation import distill
from ssflow.event_extractor import extract_event
from ssflow.instrument import Instrument, InstrumentUniverse
from ssflow.llm_client import cost_tracker
from ssflow.oasis_engine import run_simulation
from ssflow.persona import load_personas, persona_set_hash
from ssflow.report import render_simulation_safe_or_quarantine, save_report
from ssflow.round_schedule import make_schedule
from ssflow.sandbox_generator import build_from_template
from ssflow.scorecard import init_db, insert_sandbox_simulation

OUTDIR = Path("review-loop/generalization")


async def run_one_input(case: dict, idx: int) -> dict:
    """Run a single test case, return metadata dict."""
    case_id = case["id"]
    input_text = case["input"]
    schedule_name = case["schedule"]

    print(f"\n{'='*60}", file=sys.stderr)
    print(f"[{idx+1}/20] {case_id}: {input_text[:80]}...", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr, flush=True)

    result_meta = {
        "idx": idx + 1,
        "id": case_id,
        "category": case["category"],
        "direction": case["direction"],
        "event_type": case["event_type"],
        "input": input_text[:120],
        "status": "pending",
        "error": "",
        "simulation_id": "",
        "initial_price": 0,
        "final_price": 0,
        "delta_pct": 0,
        "n_personas": 0,
        "cost_usd": 0,
        "elapsed_s": 0,
        "report_path": "",
        "compliance": "",
    }

    try:
        # Stage 0: extract event
        proposal = await extract_event(input_text)
        event = proposal.to_event()
        personas_path = proposal.suggested_personas_path

        if not personas_path:
            personas_path = "personas/ashare.yaml"

        personas = load_personas(Path(personas_path))
        schedule = make_schedule(schedule_name, event.event_date)
        n_rounds = schedule.n_rounds

        # Distillation
        instrument_universe: InstrumentUniverse | None = None
        try:
            instrument_universe = await distill(
                topic=event.event_text[:400] if event.event_text else f"{event.ticker} {event.event_type}",
                market=event.market or "ashare",
                event_ticker=event.ticker,
                event_price=None,
            )
        except Exception as exc:
            print(f"  distillation failed: {exc}, using fallback", file=sys.stderr)

        if instrument_universe is None:
            # Minimal fallback
            instrument_universe = InstrumentUniverse(
                instruments=[
                    Instrument(
                        ticker=event.ticker or "TEST",
                        name=event.instrument or event.ticker or "TEST",
                        market=event.market or "ashare",
                        relationship="event_subject",
                        current_price=100.0,
                        adv_value=5_000_000_000.0,
                    )
                ],
                topic=(event.event_text or "")[:200],
            )

        _subject = instrument_universe.get(instrument_universe.event_subject_ticker)
        _subject_price = _subject.current_price if _subject else 100.0
        entity_graph = build_from_template(
            topic=event.event_text or f"{event.ticker} {event.event_type}",
            market=event.market or "ashare",
            current_price=float(_subject_price),
        )

        # Run simulation
        result = await run_simulation(
            event, personas, n_rounds=n_rounds, round_schedule=schedule,
            entity_graph=entity_graph,
            instrument_universe=instrument_universe,
        )

        display, ok = render_simulation_safe_or_quarantine(result)
        report_path = save_report(display, result.simulation_id)

        # Also save to generalization dir
        gen_report = OUTDIR / f"{case_id}.md"
        gen_report.write_text(display, encoding="utf-8")

        result_meta.update({
            "status": "ok" if ok else "quarantined",
            "simulation_id": result.simulation_id,
            "initial_price": round(result.initial_price, 2),
            "final_price": round(result.final_price, 2),
            "delta_pct": round(result.cumulative_delta_pct * 100, 2),
            "n_personas": result.n_personas,
            "cost_usd": round(result.cost_usd, 4),
            "elapsed_s": round(result.elapsed_seconds, 1),
            "report_path": str(gen_report),
            "compliance": "PASS" if ok else "QUARANTINED",
        })

        print(f"  ✓ {case_id}: {result.initial_price:.2f}→{result.final_price:.2f} "
              f"({result.cumulative_delta_pct*100:+.2f}%) ${result.cost_usd:.4f} "
              f"{result.elapsed_seconds:.1f}s", file=sys.stderr)

    except Exception as exc:
        result_meta["status"] = "error"
        result_meta["error"] = f"{type(exc).__name__}: {exc}"
        print(f"  ✗ {case_id}: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)

    return result_meta


async def amain():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, default=0, help="Start from index N (0-based)")
    parser.add_argument("--only", type=str, default=None, help="Run only this case ID")
    args = parser.parse_args()

    OUTDIR.mkdir(parents=True, exist_ok=True)
    init_db()

    cases = GENERALIZATION_INPUTS
    if args.only:
        cases = [c for c in cases if c["id"] == args.only]
        if not cases:
            print(f"ERROR: no case with id={args.only}", file=sys.stderr)
            return 1

    results = []
    tsv_path = OUTDIR / "summary.tsv"

    # Load existing results if resuming
    existing_ids = set()
    if tsv_path.exists() and args.start == 0 and not args.only:
        with open(tsv_path, "r") as f:
            reader = csv.DictReader(f, delimiter="\t")
            for row in reader:
                if row.get("status") == "ok":
                    existing_ids.add(row["id"])
                    results.append(row)

    for idx, case in enumerate(cases):
        if idx < args.start:
            continue
        if case["id"] in existing_ids:
            print(f"[{idx+1}/20] {case['id']}: already done, skipping", file=sys.stderr)
            continue

        meta = await run_one_input(case, idx)
        results.append(meta)

        # Write TSV incrementally
        if results:
            fields = list(results[0].keys())
            with open(tsv_path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fields, delimiter="\t")
                writer.writeheader()
                writer.writerows(results)

    # Print summary
    ok_count = sum(1 for r in results if r.get("status") == "ok")
    err_count = sum(1 for r in results if r.get("status") == "error")
    q_count = sum(1 for r in results if r.get("status") == "quarantined")
    total_cost = sum(float(r.get("cost_usd", 0)) for r in results)

    print(f"\n{'='*60}", file=sys.stderr)
    print(f"GENERALIZATION SUMMARY: {ok_count} ok, {q_count} quarantined, {err_count} errors", file=sys.stderr)
    print(f"Total cost: ${total_cost:.4f}", file=sys.stderr)
    print(f"Results: {tsv_path}", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)

    return 0


def main():
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
