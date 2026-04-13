#!/usr/bin/env python3
"""Demo: run the LOB engine on a BYD Q1 earnings event and generate a report.

This simulates heterogeneous agents (retail, institutional, short fund, quant,
ETF AP) reacting to a BYD earnings beat, each thinking differently based on
their role-specific cognition pipeline.

No LLM calls — all decisions are programmatic to demonstrate the mechanism.
"""

import sys
sys.path.insert(0, "src")

from ssflow.engine.orchestrator import run_simulation_lob, SimulationResult
from ssflow.engine.round_adapter import RoundRecord
from ssflow.market.ashare_rules import BoardType
from ssflow.cognition.intent_resolver import StructuredIntent, Urgency
from ssflow.cognition.mechanical_agents import quant_decision, etf_ap_decision, QuantSignal
from ssflow.cognition.agent_memory import AgentMemory
from ssflow.report_lob import render_lob_report


# ── Agent configurations ──

AGENTS = [
    # Retail: chasers with enough capital to move the book
    {"agent_id": "retail_001", "persona_id": "散户(短线追涨)", "cash": 500_000, "holdings": {"_default": 1000}, "role": "retail"},
    {"agent_id": "retail_002", "persona_id": "散户(短线追涨)", "cash": 300_000, "holdings": {"_default": 800}, "role": "retail"},
    {"agent_id": "retail_003", "persona_id": "散户(恐慌卖盘)", "cash": 50_000, "holdings": {"_default": 2000}, "role": "retail_panic"},

    # Institutional: large capital, cautious TWAP
    {"agent_id": "inst_001", "persona_id": "公募基金(主动管理)", "cash": 5_000_000, "holdings": {"_default": 10000}, "role": "institutional"},
    {"agent_id": "inst_002", "persona_id": "险资(长期配置)", "cash": 10_000_000, "holdings": {"_default": 20000}, "role": "institutional_conservative"},

    # Short fund: looking for overreaction
    {"agent_id": "short_001", "persona_id": "事件驱动做空基金", "cash": 2_000_000, "holdings": {}, "role": "short_fund"},

    # Quant: signal-driven (no LLM)
    {"agent_id": "quant_001", "persona_id": "量化(动量策略)", "cash": 1_000_000, "holdings": {"_default": 2000}, "role": "quant"},

    # Strategic holder: massive position, barely moves
    {"agent_id": "strategic_001", "persona_id": "产业资本(大股东)", "cash": 1_000_000, "holdings": {"_default": 50000}, "role": "strategic"},
]


def heterogeneous_decisions(
    round_idx: int,
    day_idx: int,
    memories: list[AgentMemory],
    snapshot: dict,
) -> list[StructuredIntent]:
    """Simulate heterogeneous agent decisions without LLM calls.

    Each agent type reacts differently to the same earnings event:
    - Day 0 (event day): retail chases, institutional cautious, short waits
    - Day 1: momentum continues, short starts probing
    - Day 2: profit-taking begins, institutional starts TWAP
    - Day 3-4: mean-reversion, volumes thin out
    """
    intents = []
    last_price = snapshot.get("last_price", 218.50)
    price_history = []  # simplified

    for mem in memories:
        cfg = next((a for a in AGENTS if a["agent_id"] == mem.agent_id), None)
        if cfg is None:
            intents.append(StructuredIntent(
                persona_id=mem.persona_id, agent_id=mem.agent_id,
                side="hold", size_fraction=0.0, urgency=Urgency.NONE,
            ))
            continue

        role = cfg["role"]
        intent = _decide_by_role(role, day_idx, mem, last_price, snapshot)
        intents.append(intent)

    return intents


def _decide_by_role(
    role: str,
    day_idx: int,
    mem: AgentMemory,
    last_price: float,
    snapshot: dict,
) -> StructuredIntent:
    """Role-specific decision logic."""

    # ── Retail (momentum chaser) ──
    if role == "retail":
        if day_idx == 0:
            # Event day: chase the earnings beat
            return StructuredIntent(
                persona_id=mem.persona_id, agent_id=mem.agent_id,
                side="buy", size_fraction=0.4, urgency=Urgency.IMMEDIATE,
                confidence=0.8, rationale="财报超预期，追涨！营收+18%，新能源龙头确认",
            )
        elif day_idx == 1:
            # Day 1: still bullish but smaller size
            return StructuredIntent(
                persona_id=mem.persona_id, agent_id=mem.agent_id,
                side="buy", size_fraction=0.2, urgency=Urgency.PATIENT,
                confidence=0.6, rationale="昨天涨了不少但趋势还在，继续小仓加",
            )
        elif day_idx >= 2 and mem.n_trades > 0:
            # Day 2+: profit taking
            return StructuredIntent(
                persona_id=mem.persona_id, agent_id=mem.agent_id,
                side="sell", size_fraction=0.3, urgency=Urgency.PATIENT,
                confidence=0.5, rationale="涨了两天，锁定部分利润，落袋为安",
            )
        return _hold(mem)

    # ── Retail (panic type) ──
    if role == "retail_panic":
        if day_idx == 0:
            # Panic: sell on event day despite beat (scared of "利好出尽")
            return StructuredIntent(
                persona_id=mem.persona_id, agent_id=mem.agent_id,
                side="sell", size_fraction=0.5, urgency=Urgency.IMMEDIATE,
                confidence=0.7, rationale="利好出尽是利空，先跑再说，反正赚了不少",
            )
        return _hold(mem)

    # ── Institutional ──
    if role == "institutional":
        if day_idx == 0:
            # Event day: observe, don't chase
            return _hold(mem, "财报需要细看，今天不急，等研报出来再定")
        elif day_idx == 1:
            # Day 1: buy after reading research — use IMMEDIATE to ensure fill
            return StructuredIntent(
                persona_id=mem.persona_id, agent_id=mem.agent_id,
                side="buy", size_fraction=0.08, urgency=Urgency.IMMEDIATE,
                confidence=0.6,
                rationale="收入beat 3%但GM miss 2.3pp，整体中性偏多，加仓5000股",
            )
        elif day_idx == 3:
            # Day 3: rebalance — sell a bit
            return StructuredIntent(
                persona_id=mem.persona_id, agent_id=mem.agent_id,
                side="sell", size_fraction=0.05, urgency=Urgency.IMMEDIATE,
                confidence=0.4,
                rationale="组合再平衡，BYD权重超标1.2%，减至中性",
            )
        return _hold(mem)

    # ── Insurance (conservative institutional) ──
    if role == "institutional_conservative":
        if day_idx <= 2:
            return _hold(mem, "季报不改变长期配置逻辑，按兵不动")
        elif day_idx == 3:
            # Day 3: small buy on pullback
            return StructuredIntent(
                persona_id=mem.persona_id, agent_id=mem.agent_id,
                side="buy", size_fraction=0.02, urgency=Urgency.IMMEDIATE,
                confidence=0.4, rationale="回调后估值合理，小幅加仓（年度配置计划内）",
            )
        return _hold(mem)

    # ── Short fund ──
    if role == "short_fund":
        if day_idx <= 1:
            return _hold(mem, "观察情绪发酵，等待过度反应信号")
        elif day_idx == 2:
            # Day 2: after the rally, short the overreaction
            return StructuredIntent(
                persona_id=mem.persona_id, agent_id=mem.agent_id,
                side="short", size_fraction=0.15, urgency=Urgency.PATIENT,
                confidence=0.55,
                rationale="GM miss -2.3pp未被定价，股价+5%已过度反映收入beat，"
                          "PE 35x vs 历史中位数28x，做空20%仓位",
            )
        elif day_idx == 4:
            # Day 4: partial cover
            return StructuredIntent(
                persona_id=mem.persona_id, agent_id=mem.agent_id,
                side="buy", size_fraction=0.5, urgency=Urgency.PATIENT,
                confidence=0.5, rationale="部分回补空头，锁定利润",
            )
        return _hold(mem)

    # ── Quant (mechanical) ──
    if role == "quant":
        # Build signals from available data
        signals = []
        if day_idx == 0:
            signals.append(QuantSignal("earnings_surprise", 0.4, 0.7, 3))
            signals.append(QuantSignal("momentum_1d", 0.3, 0.6, 2))
        elif day_idx == 1:
            signals.append(QuantSignal("momentum_2d", 0.2, 0.5, 3))
            signals.append(QuantSignal("mean_reversion", -0.1, 0.4, 5))
        elif day_idx >= 2:
            signals.append(QuantSignal("momentum_decay", -0.15, 0.6, 2))
            signals.append(QuantSignal("volatility_compression", -0.1, 0.5, 3))

        return quant_decision(
            signals=signals,
            current_price=last_price,
            vwap=snapshot.get("vwap"),
            price_history=[218.5] * (day_idx + 1),  # simplified
            holdings=mem.holdings.get("_default", 0),
            cash=mem.current_cash,
            persona_id=mem.persona_id,
            agent_id=mem.agent_id,
        )

    # ── Strategic (大股东) ──
    if role == "strategic":
        # Almost never trades
        return _hold(mem, "季报不影响产业战略判断，窗口期内不交易")

    return _hold(mem)


def _hold(mem: AgentMemory, reason: str = "") -> StructuredIntent:
    return StructuredIntent(
        persona_id=mem.persona_id, agent_id=mem.agent_id,
        side="hold", size_fraction=0.0, urgency=Urgency.NONE,
        rationale=reason or "本轮不交易",
    )


def main():
    print("=" * 70)
    print("ssFlow LOB Engine Demo — BYD Q1 Earnings Event")
    print("=" * 70)
    print()

    # Run simulation
    print("Running 5-day simulation with 9 heterogeneous agents...")
    print("  - 3 retail (2 chasers + 1 panic seller)")
    print("  - 2 institutional (1 active + 1 conservative)")
    print("  - 1 short fund")
    print("  - 1 quant (signal-driven, no LLM)")
    print("  - 1 strategic holder (大股东)")
    print()

    result = run_simulation_lob(
        ticker="002594.SZ",
        prev_close=218.50,
        board_type=BoardType.NORMAL,
        n_days=5,
        ticks_per_session=60,
        agent_configs=AGENTS,
        agent_decisions=heterogeneous_decisions,
        background_seed=42,
        n_zi=8,
        n_mm=2,
        n_mr=3,
    )

    # Generate report
    report = render_lob_report(result)

    # Print summary
    print(f"Simulation complete!")
    print(f"  Initial:    ¥{result.initial_price:.2f}")
    print(f"  Final:      ¥{result.final_price:.2f}")
    print(f"  Cumulative: {result.cumulative_delta_pct*100:+.2f}%")
    print(f"  Fills:      {result.n_total_fills:,} total ({result.n_background_fills:,} background)")
    print(f"  Rounds:     {len(result.rounds)}")
    print()

    # Print per-agent summary
    print("Per-Agent Results:")
    print("-" * 80)
    print(f"{'Agent':<20} {'Persona':<22} {'Cash':>12} {'Holdings':>10} {'P&L':>12} {'Trades':>7}")
    print("-" * 80)
    for ag in sorted(result.agent_states, key=lambda a: a.pnl, reverse=True):
        total_h = sum(ag.holdings.values())
        print(f"{ag.agent_id:<20} {ag.persona_id:<22} ¥{ag.cash:>10,.0f} {total_h:>10,.0f} ¥{ag.pnl:>+10,.0f} {ag.n_trades:>7}")
    print("-" * 80)
    print()

    # Print per-round price path
    print("Price Path:")
    for r in result.rounds:
        n_phases = len(r.phase_results)
        total_fills = sum(p.n_fills for p in r.phase_results)
        print(f"  Day {r.day_idx}: ¥{r.price_before:.2f} → ¥{r.price_after:.2f} "
              f"({r.delta_pct*100:+.2f}%) [{total_fills} fills, {n_phases} phases]")
    print()

    # Save report
    report_path = "reports/demo_lob_byd_q1.md"
    import os
    os.makedirs("reports", exist_ok=True)
    with open(report_path, "w") as f:
        f.write(report)
    print(f"Full report saved to: {report_path}")
    print()

    # Print full report to stdout
    print("=" * 70)
    print("FULL REPORT")
    print("=" * 70)
    print()
    print(report)


if __name__ == "__main__":
    main()
