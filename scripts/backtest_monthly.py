#!/usr/bin/env python
"""Monthly historical backtest harness.

Runs the simulator on a fixed set of historical A-share events at
month-scale cadence (schedule `monthly-6m`) and compares the 6-round
simulated trajectory against the *real* 6-month forward price series
fetched from Sina K-line.

The point is diagnostic: we want to know how often the engine gets
direction right, how far off the magnitude is, whether it ever produces
non-monotonic paths (real markets oscillate), and which structural
failure modes from `analysis/backtest_diagnostic_v1.md` are still live.

Usage:
    uv run python scripts/backtest_monthly.py [--no-run]

`--no-run` skips the live LLM simulations and only prints the real
trajectories + event catalogue (useful for sanity-checking the data
pipeline before paying for LLM calls).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "WARNING").upper(), logging.WARNING),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stderr,
)

from ssflow.event import Event
from ssflow.instrument import Instrument, InstrumentUniverse
from ssflow.market_data import fetch_kline_historical
from ssflow.oasis_engine import run_simulation
from ssflow.persona import load_personas
from ssflow.round_schedule import make_schedule
from ssflow.sandbox_generator import build_from_template


PERSONAS_PATH = Path("personas/ashare.yaml")
SCHEDULE_NAME = "monthly-6m"
MONTHS_FORWARD = 6

# ── Event catalogue ────────────────────────────────────────────
# Tuned for diversity: 2 bear events, 2 policy-driven bull events,
# 1 sideways/volatility test. All within Sina's trailing-bar reach.

EVENTS: list[dict] = [
    {
        "name": "药明康德 BIOSECURE 法案",
        "ticker": "603259",
        "event_type": "regulatory",
        "event_date": "2024-01-25",
        "adv": 5_000_000_000,
        "event_text": (
            "美国参议院提出《生物安全法案》(BIOSECURE Act), "
            "将药明康德(603259)列入'受关注的生物技术公司'清单. "
            "若法案通过, 美国政府机构和受资助实体将被禁止与药明康德签订新合同. "
            "药明康德约30%营收来自美国客户. 公司回应称'对指控深表遗憾', "
            "强调不会威胁美国国家安全. 业内担忧美国客户将预防性转移订单, "
            "即使法案最终未通过也会引发业务重定价. 市场恐慌情绪浓厚, "
            "盘前 ADR 暴跌 20%+."
        ),
        "prior_consensus": (
            "药明康德是全球CRO/CDMO龙头, 2023年业绩稳健增长. "
            "市场一致预期2024年营收400亿+, PE约25x. "
            "中美脱钩风险存在但此前未实质落地. 机构普遍持有."
        ),
    },
    {
        "name": "五粮液 2024Q3 业绩不及预期",
        "ticker": "000858",
        "event_type": "earnings",
        "event_date": "2024-10-30",
        "adv": 3_500_000_000,
        "event_text": (
            "五粮液(000858)发布2024年三季报: Q3单季营收172.7亿元, 同比+1.4%; "
            "归母净利润59.0亿元, 同比+1.3%, 均低于市场预期. "
            "普五批价持续走弱至 930-950 元区间, 渠道库存高企, "
            "经销商回款压力明显. 管理层强调'控量稳价', 但市场对白酒需求拐点担忧加剧. "
            "三季度末合同负债环比下滑, 预收款走弱引发去库存担忧. "
            "行业龙头已给出偏谨慎的 Q4 指引."
        ),
        "prior_consensus": (
            "市场原本预期Q3营收180亿, 净利润65亿+. "
            "高端白酒被视为消费降级避风港, 五粮液 PE 约 18x. "
            "机构持仓比例高, 基本面预期稳健."
        ),
    },
    {
        "name": "贝泰妮 2024Q1 消费降速",
        "ticker": "300957",
        "event_type": "earnings",
        "event_date": "2024-04-25",
        "adv": 400_000_000,
        "event_text": (
            "贝泰妮(300957)发布2024年一季报: 营收10.97亿元, 同比+27.1%; "
            "归母净利润1.77亿元, 同比-28.0%, 利润大幅下滑. "
            "薇诺娜品牌增速放缓, 线上渠道费用率大幅抬升, 毛利率同比下降3.2pp. "
            "行业进入'内卷+消费降级'阶段, 国货美妆增速普遍回落. "
            "公司正在通过发展多品牌矩阵应对单品牌依赖, 但新品牌尚未放量. "
            "市场对成长性担忧加深, 估值面临下杀压力."
        ),
        "prior_consensus": (
            "贝泰妮 2023 年高增长放缓但仍被视为成长股, PE 约 30x. "
            "机构重仓标的, 市场预期 Q1 净利润同比+10-15%. "
            "实际发布的 -28% 净利下滑远超预期."
        ),
    },
    {
        "name": "宁德时代 924 政策组合拳",
        "ticker": "300750",
        "event_type": "policy",
        "event_date": "2024-09-24",
        "adv": 8_000_000_000,
        "event_text": (
            "2024年9月24日, 中国人民银行、证监会、金融监管总局同时发布多项重磅政策: "
            "1) 降准50bp释放长期资金约1万亿; "
            "2) 降息20bp (7天逆回购利率从1.7%降至1.5%); "
            "3) 创设5000亿证券基金保险互换便利工具(SFISF); "
            "4) 创设3000亿股票回购增持再贷款; "
            "5) 降低存量房贷利率约0.5pp. "
            "三部委联合发布会释放极强稳市信号, A股午后直线拉升. "
            "宁德时代(300750)作为新能源权重股, 既受益于流动性释放, "
            "也受益于对成长股风险偏好的修复."
        ),
        "prior_consensus": (
            "924之前A股持续阴跌, 上证跌破2700. "
            "市场极度悲观, 融资余额降至1.3万亿, 北向资金持续流出. "
            "宁德时代此前受锂电产能过剩担忧压制, 处于相对低位. "
            "市场未预期到'金融组合拳'的规模和速度."
        ),
    },
    {
        "name": "东方财富 924 政策组合拳",
        "ticker": "300059",
        "event_type": "policy",
        "event_date": "2024-09-24",
        "adv": 10_000_000_000,
        "event_text": (
            "2024年9月24日, 中国人民银行、证监会、金融监管总局同时发布多项重磅政策: "
            "1) 降准50bp释放长期资金约1万亿; "
            "2) 降息20bp; "
            "3) 创设5000亿证券基金保险互换便利工具(SFISF), 允许机构从央行借入国债抵押换股票; "
            "4) 创设3000亿股票回购增持再贷款; "
            "5) 降低存量房贷利率. "
            "直接给权益市场注入流动性, 并为券商板块打开业务空间. "
            "东方财富(300059)作为互联网券商龙头, 是本次行情的最大弹性品种之一. "
            "市场预期散户开户潮 + 成交量爆发 + 两融扩张都将直接体现在公司基本面上."
        ),
        "prior_consensus": (
            "924之前 A 股日均成交约 5000 亿, 东方财富市占率 5-6%. "
            "公司基金代销业务受熊市拖累, 2024H1 业绩同比下滑. "
            "PB 约 2x, 估值修复空间较大. 市场未预期到如此强度的政策组合拳."
        ),
    },
]


async def _run_one_sim(ev: dict) -> dict:
    """Run a single 6-month sim and return the result summary."""
    t0 = time.time()
    event = Event(
        ticker=ev["ticker"],
        event_text=ev["event_text"],
        event_type=ev["event_type"],
        event_date=ev["event_date"],
        prior_consensus=ev.get("prior_consensus", ""),
        recent_price_action="",
        sector_context="",
    )
    instrument_universe = InstrumentUniverse(
        instruments=[
            Instrument(
                ticker=ev["ticker"],
                name=ev["ticker"],
                market="ashare",
                relationship="event_subject",
                current_price=float(ev["anchor_price"]),
                adv_value=float(ev["adv"]),
            )
        ],
        topic=ev["event_text"][:200],
    )

    personas = load_personas(PERSONAS_PATH)
    schedule = make_schedule(SCHEDULE_NAME, ev["event_date"])

    entity_graph = build_from_template(
        topic=ev["event_text"][:200],
        market="ashare",
        current_price=ev["anchor_price"],
    )

    result = await run_simulation(
        event, personas, n_rounds=schedule.n_rounds,
        round_schedule=schedule,
        entity_graph=entity_graph,
        instrument_universe=instrument_universe,  # trivial single-instrument universe
    )

    # Collect per-class net flow + action histogram across all rounds
    total_actions: Counter[str] = Counter()
    total_buy_flow = 0.0
    total_sell_flow = 0.0
    for round_state in result.rounds:
        for flow in round_state.class_flows:
            for action_name, n in flow.action_histogram.items():
                total_actions[action_name] += n
            if flow.net_flow > 0:
                total_buy_flow += flow.net_flow
            else:
                total_sell_flow += abs(flow.net_flow)

    # Price trajectory + round-level delta analysis
    traj = result.price_trajectory
    deltas = [
        round((traj[i] / traj[i - 1] - 1) * 100, 3)
        for i in range(1, len(traj))
    ]
    up_rounds = sum(1 for d in deltas if d > 0)
    down_rounds = sum(1 for d in deltas if d < 0)
    reversals = sum(
        1 for i in range(1, len(deltas))
        if (deltas[i] > 0) != (deltas[i - 1] > 0)
    )

    elapsed = time.time() - t0
    return {
        "simulation_id": result.simulation_id,
        "initial_price": result.initial_price,
        "final_price": result.final_price,
        "cum_pct": round(result.cumulative_delta_pct * 100, 2),
        "price_trajectory": [round(p, 2) for p in traj],
        "round_deltas_pct": deltas,
        "up_rounds": up_rounds,
        "down_rounds": down_rounds,
        "reversals": reversals,
        "total_actions": dict(total_actions),
        "total_buy_flow": round(total_buy_flow, 0),
        "total_sell_flow": round(total_sell_flow, 0),
        "buy_sell_ratio": round(
            total_buy_flow / total_sell_flow if total_sell_flow > 0
            else float("inf"), 2,
        ),
        "cost_usd": round(result.cost_usd, 4),
        "elapsed_s": round(elapsed, 1),
    }


async def _fetch_real_trajectory(ev: dict) -> dict:
    """Fetch the real 6-month forward closes and return them as a
    month-bucketed comparison structure.

    Uses the last trading day of each calendar month within the window
    as the comparison anchor — parallels how the monthly-6m preset
    spaces its rounds.
    """
    data = await fetch_kline_historical(
        ticker=ev["ticker"],
        as_of_date=ev["event_date"],
        months_forward=MONTHS_FORWARD,
        months_back=1,
        market="ashare",
    )
    if not data["anchor_bar"]:
        return {"error": "no anchor bar available"}

    anchor = data["anchor_bar"]
    fwd = data["forward_bars"]
    if not fwd:
        return {"error": "no forward bars"}

    anchor_close = float(anchor["close"])
    anchor_date = str(anchor["date"])
    final_close = float(fwd[-1]["close"])
    high = max(float(b["high"]) for b in fwd)
    low = min(float(b["low"]) for b in fwd)

    # Month-buckets: last bar of each calendar month after anchor
    month_buckets: dict[str, dict] = {}
    for bar in fwd:
        bar_date = str(bar.get("date", ""))
        if len(bar_date) < 7:
            continue
        ym = bar_date[:7]  # YYYY-MM
        # Keep the latest bar seen for each month
        month_buckets[ym] = bar
    # Sort and take the first 6 months after the event
    bucketed = sorted(month_buckets.items())[:MONTHS_FORWARD]
    monthly_closes = [
        (ym, round(float(b["close"]), 2)) for ym, b in bucketed
    ]
    # Compute real cumulative return using the last month bucket we
    # actually have (may be fewer than 6 if data runs out)
    real_final = (
        float(bucketed[-1][1]["close"]) if bucketed else final_close
    )

    return {
        "anchor_price": anchor_close,
        "anchor_date": anchor_date,
        "real_cum_pct": round((real_final / anchor_close - 1) * 100, 2),
        "real_last_close": round(real_final, 2),
        "range_high": round(high, 2),
        "range_low": round(low, 2),
        "max_up_pct": round((high / anchor_close - 1) * 100, 2),
        "max_dn_pct": round((low / anchor_close - 1) * 100, 2),
        "monthly_closes": monthly_closes,
        "n_bars": len(fwd),
    }


def _print_comparison_chart(name: str, sim_traj: list[float], real_closes: list[tuple]):
    """ASCII side-by-side chart."""
    print(f"\n  {name}", file=sys.stderr)
    print(f"  {'─' * 60}", file=sys.stderr)
    base_sim = sim_traj[0] if sim_traj else 0
    base_real = real_closes[0][1] if real_closes else 0
    print(f"  {'Round':<8} {'Sim':>12} {'Real':>14} {'Sim Δ':>8} {'Real Δ':>8}",
          file=sys.stderr)
    n = min(len(sim_traj), len(real_closes) + 1)
    for i in range(n):
        sim = sim_traj[i]
        sim_pct = (sim / base_sim - 1) * 100 if base_sim > 0 else 0
        if i == 0:
            label = "M0"
            real = base_real if base_real else sim
            real_pct = 0
        else:
            label = f"M+{i}"
            if i - 1 < len(real_closes):
                _, real = real_closes[i - 1]
                real_pct = (real / base_real - 1) * 100 if base_real > 0 else 0
            else:
                real = None
                real_pct = None
        real_str = f"{real:>14.2f}" if real is not None else f"{'N/A':>14}"
        real_delta = f"{real_pct:+7.2f}%" if real_pct is not None else f"{'N/A':>8}"
        print(f"  {label:<8} {sim:>12.2f} {real_str} {sim_pct:>+7.2f}% {real_delta}",
              file=sys.stderr)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-run", action="store_true", help="skip LLM sims")
    parser.add_argument("--only", default=None,
                        help="comma-sep ticker list to restrict run")
    parser.add_argument("--out", default="analysis/backtest_monthly_v1.json",
                        help="path for JSON dump")
    args = parser.parse_args()

    only = set(args.only.split(",")) if args.only else None
    event_list = [
        e for e in EVENTS if only is None or e["ticker"] in only
    ]

    print("=" * 70, file=sys.stderr)
    print("ssFlow Monthly Backtest — 6-round × 6-month historical replay",
          file=sys.stderr)
    print("=" * 70, file=sys.stderr, flush=True)

    # ── Phase 1: fetch real trajectories ──
    print(f"\n[Phase 1] Fetching real 6-month forward trajectories...",
          file=sys.stderr, flush=True)
    for ev in event_list:
        real = await _fetch_real_trajectory(ev)
        ev["real"] = real
        if "error" in real:
            print(f"  {ev['name']}: {real['error']}", file=sys.stderr)
            continue
        ev["anchor_price"] = real["anchor_price"]
        print(
            f"  {ev['name']:<32} {ev['ticker']} anchor ¥{real['anchor_price']:.2f} "
            f"→ ¥{real['real_last_close']:.2f} ({real['real_cum_pct']:+.2f}%) "
            f"range [{real['max_dn_pct']:+.1f}%, {real['max_up_pct']:+.1f}%]",
            file=sys.stderr,
        )

    if args.no_run:
        print("\n[--no-run] skipping LLM sims", file=sys.stderr)
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(
            json.dumps(event_list, ensure_ascii=False, indent=2),
        )
        return

    # ── Phase 2: run LLM sims ──
    print(f"\n[Phase 2] Running {len(event_list)} LLM sims (monthly-6m × 6 rounds)...",
          file=sys.stderr, flush=True)
    for i, ev in enumerate(event_list):
        if "anchor_price" not in ev:
            print(f"  [{i+1}/{len(event_list)}] SKIP {ev['name']} (no anchor)",
                  file=sys.stderr)
            continue
        print(f"  [{i+1}/{len(event_list)}] {ev['name']}...",
              file=sys.stderr, flush=True)
        try:
            sim = await _run_one_sim(ev)
            ev["sim"] = sim
            print(
                f"      => sim {sim['cum_pct']:+.2f}% "
                f"(real {ev['real']['real_cum_pct']:+.2f}%) "
                f"up/dn/rev {sim['up_rounds']}/{sim['down_rounds']}/{sim['reversals']} "
                f"[{sim['elapsed_s']}s, ${sim['cost_usd']}]",
                file=sys.stderr, flush=True,
            )
        except Exception as exc:
            import traceback
            traceback.print_exc(file=sys.stderr)
            ev["sim"] = {"error": str(exc)}

    # ── Phase 3: summary ──
    print("\n" + "=" * 70, file=sys.stderr)
    print("SUMMARY: sim vs real", file=sys.stderr)
    print("=" * 70, file=sys.stderr)
    print(
        f"{'Event':<30} {'Sim%':>8} {'Real%':>8} {'Dir':>4} {'Up/Dn':>7} {'Rev':>4} {'Buy/Sell':>10}",
        file=sys.stderr,
    )
    print("-" * 85, file=sys.stderr)

    direction_correct = 0
    direction_total = 0
    for ev in event_list:
        if "sim" not in ev or "error" in ev.get("sim", {}):
            print(f"{ev['name']:<30} ERROR", file=sys.stderr)
            continue
        sim = ev["sim"]
        real = ev["real"]
        real_pct = real["real_cum_pct"]
        sim_pct = sim["cum_pct"]
        direction_match = (
            (real_pct >= 0 and sim_pct >= 0)
            or (real_pct < 0 and sim_pct < 0)
        )
        if direction_match:
            direction_correct += 1
        direction_total += 1
        print(
            f"{ev['name']:<30} {sim_pct:>+7.2f}% {real_pct:>+7.2f}% "
            f"{'YES' if direction_match else 'NO':>4} "
            f"{sim['up_rounds']}/{sim['down_rounds']:<5} "
            f"{sim['reversals']:>4} "
            f"{sim.get('buy_sell_ratio', 'inf'):>10}",
            file=sys.stderr,
        )
        _print_comparison_chart(
            ev["name"], sim["price_trajectory"], real["monthly_closes"]
        )

    print("", file=sys.stderr)
    if direction_total > 0:
        print(
            f"Direction accuracy: {direction_correct}/{direction_total} "
            f"({direction_correct * 100 // direction_total}%)",
            file=sys.stderr,
        )

    # ── Persist full result ──
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(
        json.dumps(event_list, ensure_ascii=False, indent=2)
    )
    print(f"\nFull results saved to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
