#!/usr/bin/env python
"""Batch backtest: run multiple historical events and compare with real outcomes."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
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
from ssflow.oasis_engine import run_simulation
from ssflow.persona import load_personas
from ssflow.round_schedule import make_schedule
from ssflow.sandbox_generator import build_from_template

PERSONAS_PATH = Path("personas/ashare.yaml")

# ── Historical events to backtest ──────────────────────────────
EVENTS = [
    {
        "name": "茅台 Q3 2024 业绩不及预期",
        "ticker": "600519",
        "event_type": "earnings",
        "event_date": "2024-10-25",
        "current_price": 1510.0,
        "adv": 4_000_000_000,
        "schedule": "earnings-5d",
        "event_text": (
            "贵州茅台(600519)发布2024年三季报: Q3单季营收387.6亿元,同比+15.6%,"
            "低于市场预期的410亿; 归母净利润190.5亿,同比+13.1%,低于预期的205亿."
            "经销商渠道出现价格倒挂,飞天茅台批价跌破2300元."
            "管理层称将'稳价控量',但市场担忧白酒行业需求拐点已至."
            "三季度末合同负债环比减少18亿,预收款下滑引发渠道去库存担忧."
        ),
        "prior_consensus": "市场一致预期Q3营收410亿,净利润205亿. 飞天茅台建议零售价1499元,批价此前稳定在2500元以上.",
        "real_outcome": "1周内跌8-12%, 1个月内跌约15%. 批价持续走低至2200元以下.",
        "expected_direction": "negative",
    },
    {
        "name": "隆基绿能 2024年光伏产能过剩暴雷",
        "ticker": "601012",
        "event_type": "earnings",
        "event_date": "2024-04-25",
        "current_price": 22.0,
        "adv": 3_000_000_000,
        "schedule": "earnings-5d",
        "event_text": (
            "隆基绿能(601012)发布2024年一季报: 营收176.7亿元,同比-37.6%;"
            "归母净利润-23.5亿元,去年同期盈利36.4亿,同比由盈转亏."
            "光伏组件价格跌至0.8元/W以下,全行业亏损."
            "公司宣布裁员30%并关闭部分产线. 硅片库存超过正常水平3倍."
            "光伏行业进入'至暗时刻',产能严重过剩,供需失衡短期难以改善."
        ),
        "prior_consensus": "市场预期光伏行业Q1会亏损,但幅度超预期. 此前分析师预期亏损10-15亿.",
        "real_outcome": "事件后1周跌15%, 2个月内累计跌约30%. 全年跌幅超40%.",
        "expected_direction": "negative",
    },
    {
        "name": "中远海控 2024红海危机运价飙升",
        "ticker": "601919",
        "event_type": "geopolitical",
        "event_date": "2024-01-15",
        "current_price": 14.0,
        "adv": 2_000_000_000,
        "schedule": "earnings-5d",
        "event_text": (
            "也门胡塞武装持续袭击红海商船,全球航运巨头暂停红海航线."
            "马士基、地中海航运、达飞等宣布绕行好望角,航行时间增加10-14天."
            "上海出口集装箱运价指数(SCFI)两周内暴涨超80%."
            "中远海控(601919)作为全球第四大集装箱航运公司直接受益,"
            "预计2024Q1运价将同比翻倍. 市场预期红海危机至少持续数月."
        ),
        "prior_consensus": "2023年航运处于周期底部,运价低迷. 市场预期2024年运价温和回升5-10%.",
        "real_outcome": "1个月内涨25-35%, 2个月内涨约40%. 运价持续高位至2024年中.",
        "expected_direction": "positive",
    },
    {
        "name": "比亚迪 2024H1 营收利润双超预期",
        "ticker": "002594",
        "event_type": "earnings",
        "event_date": "2024-08-28",
        "current_price": 230.0,
        "adv": 8_000_000_000,
        "schedule": "earnings-5d",
        "event_text": (
            "比亚迪(002594)发布2024年半年报: 营收3011.3亿元,同比+15.8%;"
            "归母净利润136.3亿元,同比+24.4%,均超市场预期."
            "Q2单季净利润76.1亿,创单季新高. 毛利率提升至20.0%,"
            "好于市场预期的18.5%. 新能源汽车销量161万辆,同比+28.5%."
            "海外销量同比翻倍,达到20.3万辆. 现金储备充裕达850亿元."
        ),
        "prior_consensus": "市场一致预期H1营收2850亿,净利润120亿. 新能源车价格战担忧下,预期毛利率18-19%.",
        "real_outcome": "1周内涨8-12%, 1个月内涨约20-25%. 股价创历史新高.",
        "expected_direction": "positive",
    },
]


async def run_one(ev_def: dict) -> dict:
    """Run a single backtest and return summary."""
    t0 = time.time()
    event = Event(
        ticker=ev_def["ticker"],
        event_text=ev_def["event_text"],
        event_type=ev_def["event_type"],
        event_date=ev_def["event_date"],
        prior_consensus=ev_def.get("prior_consensus", ""),
        recent_price_action="",
        sector_context="",
    )
    instrument_universe = InstrumentUniverse(
        instruments=[
            Instrument(
                ticker=ev_def["ticker"],
                name=ev_def["ticker"],
                market="ashare",
                relationship="event_subject",
                current_price=float(ev_def["current_price"]),
                adv_value=float(ev_def["adv"]),
            )
        ],
        topic=ev_def["event_text"][:200],
    )

    personas = load_personas(PERSONAS_PATH)
    schedule = make_schedule(ev_def["schedule"], ev_def["event_date"])
    n_rounds = schedule.n_rounds

    entity_graph = build_from_template(
        topic=ev_def["event_text"][:200],
        market="ashare",
        current_price=ev_def["current_price"],
    )

    result = await run_simulation(
        event, personas, n_rounds=n_rounds,
        round_schedule=schedule,
        entity_graph=entity_graph,
        instrument_universe=instrument_universe,
    )

    elapsed = time.time() - t0
    return {
        "name": ev_def["name"],
        "ticker": ev_def["ticker"],
        "expected_direction": ev_def["expected_direction"],
        "real_outcome": ev_def["real_outcome"],
        "initial_price": result.initial_price,
        "final_price": result.final_price,
        "cumulative_delta_pct": round(result.cumulative_delta_pct * 100, 2),
        "price_trajectory": result.price_trajectory,
        "n_personas": result.n_personas,
        "n_rounds": result.n_rounds,
        "cost_usd": round(result.cost_usd, 4),
        "elapsed_s": round(elapsed, 1),
    }


async def main():
    print("=" * 70, file=sys.stderr)
    print("ssFlow Batch Backtest — 4 Historical Events", file=sys.stderr)
    print("=" * 70, file=sys.stderr, flush=True)

    results = []
    for i, ev_def in enumerate(EVENTS):
        print(f"\n[{i+1}/{len(EVENTS)}] Running: {ev_def['name']}...",
              file=sys.stderr, flush=True)
        try:
            r = await run_one(ev_def)
            results.append(r)
            direction_emoji = "+" if r["cumulative_delta_pct"] >= 0 else ""
            print(f"  => {direction_emoji}{r['cumulative_delta_pct']:.2f}% "
                  f"(expected: {r['expected_direction']}) "
                  f"[{r['elapsed_s']:.0f}s, ${r['cost_usd']:.3f}]",
                  file=sys.stderr, flush=True)
        except Exception as e:
            print(f"  => FAILED: {e}", file=sys.stderr, flush=True)
            results.append({
                "name": ev_def["name"],
                "ticker": ev_def["ticker"],
                "expected_direction": ev_def["expected_direction"],
                "real_outcome": ev_def["real_outcome"],
                "error": str(e),
            })

    # Print summary table
    print("\n" + "=" * 70, file=sys.stderr)
    print("RESULTS SUMMARY", file=sys.stderr)
    print("=" * 70, file=sys.stderr)
    print(f"{'Event':<35} {'Sim':>8} {'Expected':>10} {'Real Outcome':<30} {'Match':>5}",
          file=sys.stderr)
    print("-" * 95, file=sys.stderr)
    for r in results:
        if "error" in r:
            print(f"{r['name']:<35} {'ERROR':>8} {r['expected_direction']:>10} {r['real_outcome']:<30} {'N/A':>5}",
                  file=sys.stderr)
            continue
        sim_pct = f"{r['cumulative_delta_pct']:+.1f}%"
        direction_match = (
            (r["cumulative_delta_pct"] < 0 and r["expected_direction"] == "negative") or
            (r["cumulative_delta_pct"] > 0 and r["expected_direction"] == "positive")
        )
        match_str = "YES" if direction_match else "NO"
        print(f"{r['name']:<35} {sim_pct:>8} {r['expected_direction']:>10} {r['real_outcome']:<30} {match_str:>5}",
              file=sys.stderr)
    print(file=sys.stderr)

    # Output full JSON to stdout
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
