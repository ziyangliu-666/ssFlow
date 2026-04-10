#!/usr/bin/env python
"""End-to-end test: distillation → multi-instrument simulation.

Exercises the full flattened instrument universe pipeline:
  1. Distill a topic into InstrumentUniverse (flat, no primary)
  2. Run simulation with instrument_universe + entity_graph=None
  3. Print detailed per-round behavior for manual review
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stderr,
)

from ssflow.config import settings
from ssflow.distillation import distill
from ssflow.event import Event
from ssflow.event_bus import ListSink
from ssflow.oasis_engine import run_simulation
from ssflow.persona import load_personas


async def main():
    topic = sys.argv[1] if len(sys.argv) > 1 else "光伏行业欧盟反补贴关税落地"
    n_rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 3

    print(f"\n{'='*60}", file=sys.stderr)
    print(f"Topic: {topic}", file=sys.stderr)
    print(f"Rounds: {n_rounds}", file=sys.stderr)
    print(f"{'='*60}\n", file=sys.stderr)

    # Step 1: Distill
    print("# Step 1: Distilling instrument universe...", file=sys.stderr, flush=True)
    universe = await distill(topic=topic, market="ashare")

    print(f"\n# Instruments ({len(universe.instruments)}):", file=sys.stderr)
    for inst in universe.instruments:
        print(
            f"  {inst.ticker:>8} {inst.name:<12} [{inst.relationship:<14}] "
            f"price={inst.price_currency}{inst.current_price:.2f}  "
            f"adv={inst.adv_value/1e8:.1f}亿  "
            f"kline={len(inst.kline_30d)}d",
            file=sys.stderr,
        )

    # Step 2: Build event from the event_subject instrument
    es = universe.get(universe.event_subject_ticker) or universe.instruments[0]
    event = Event(
        ticker=es.ticker,
        event_text=topic,
        event_type="other",
        event_date="2026-04-10",
        current_price=es.current_price,
        adv_value=es.adv_value,
        market="ashare",
        instrument=es.name,
    )

    # Step 3: Load personas and run
    personas = load_personas(Path("personas/ashare.yaml"))
    sink = ListSink()

    print(f"\n# Step 2: Running {n_rounds}-round simulation...", file=sys.stderr, flush=True)
    result = await run_simulation(
        event,
        personas,
        n_rounds=n_rounds,
        instrument_universe=universe,
        event_sink=sink,
    )

    # Step 4: Analyze per-round behavior
    events = sink.events
    print(f"\n{'='*60}", file=sys.stderr)
    print(f"# SIMULATION REVIEW", file=sys.stderr)
    print(f"{'='*60}\n", file=sys.stderr)

    for round_idx in range(n_rounds):
        round_trades = [
            e for e in events
            if e.get("type") == "trade_submitted" and e.get("round_idx") == round_idx
        ]
        round_flows = [
            e for e in events
            if e.get("type") == "class_flow_computed" and e.get("round_idx") == round_idx
        ]
        round_price = [
            e for e in events
            if e.get("type") == "price_updated" and e.get("round_idx") == round_idx
        ]

        print(f"\n## Round {round_idx}", file=sys.stderr)
        print(f"   Trades submitted: {len(round_trades)}", file=sys.stderr)

        # Per-instrument trade breakdown
        instruments_traded: dict[str, list] = {}
        for t in round_trades:
            inst = t.get("instrument", "?")
            instruments_traded.setdefault(inst, []).append(t)

        if instruments_traded:
            print(f"   Instruments traded:", file=sys.stderr)
            for inst, trades in sorted(instruments_traded.items()):
                names = [t.get("archetype", t.get("persona_id", "?")) for t in trades]
                print(f"     {inst}: {len(trades)} traders — {', '.join(names)}", file=sys.stderr)
        else:
            print(f"   ⚠ No trades this round!", file=sys.stderr)

        # Traders who held (no tool call)
        held = [e for e in round_flows if e.get("held")]
        if held:
            held_names = [e.get("archetype", e.get("persona_id", "?")) for e in held]
            print(f"   Held (no tool call): {len(held)} — {', '.join(held_names[:5])}{'...' if len(held_names)>5 else ''}", file=sys.stderr)

        # Price update
        if round_price:
            p = round_price[0]
            prices = p.get("prices", {})
            print(f"   Prices after round:", file=sys.stderr)
            for ticker, price in prices.items():
                inst_obj = universe.get(ticker)
                name = inst_obj.name if inst_obj else ticker
                initial = universe.prices().get(ticker, 0)
                if initial > 0:
                    cumul = (price / initial - 1) * 100
                    print(f"     {ticker} ({name}): {price:.2f} (cumul {cumul:+.1f}%)", file=sys.stderr)
                else:
                    print(f"     {ticker} ({name}): {price:.2f}", file=sys.stderr)

    # Summary
    print(f"\n{'='*60}", file=sys.stderr)
    print(f"# SUMMARY", file=sys.stderr)
    print(f"  Duration: {result.elapsed_seconds:.1f}s  Cost: ${result.cost_usd:.4f}", file=sys.stderr)
    print(f"  Event subject: {es.name} ({es.ticker})", file=sys.stderr)
    print(f"  Price trajectories:", file=sys.stderr)
    for ticker, traj in result.price_trajectories.items():
        inst_obj = universe.get(ticker)
        name = inst_obj.name if inst_obj else ticker
        if len(traj) >= 2:
            delta = (traj[-1] / traj[0] - 1) * 100
            print(f"    {ticker} ({name}): {traj[0]:.2f} → {traj[-1]:.2f} ({delta:+.1f}%)", file=sys.stderr)

    # Check: did agents trade multiple instruments?
    all_trades = [e for e in events if e.get("type") == "trade_submitted"]
    traded_instruments = set(e.get("instrument", "") for e in all_trades)
    traded_instruments.discard("")
    print(f"\n  Unique instruments traded: {traded_instruments or '{none}'}", file=sys.stderr)
    if len(traded_instruments) <= 1:
        print(f"  ⚠ PROBLEM: Agents only traded {traded_instruments or 'nothing'}!", file=sys.stderr)
    else:
        print(f"  ✓ Multi-instrument trading confirmed", file=sys.stderr)

    print(f"{'='*60}\n", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
