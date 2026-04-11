#!/usr/bin/env python
"""Dynamic backtest: long-horizon simulation with mid-sim event injections.

Creates a realistic multi-day market scenario where bulls and bears fight
through multiple information shocks. Tests whether the simulation produces
genuine oscillation (有涨有跌) rather than monotonic trends.
"""

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
from ssflow.information.external_events import ExternalEvent, ExternalEventSchedule
from ssflow.instrument import Instrument, InstrumentUniverse
from ssflow.oasis_engine import run_simulation
from ssflow.persona import load_personas
from ssflow.round_schedule import make_schedule
from ssflow.sandbox_generator import build_from_template

PERSONAS_PATH = Path("personas/ashare.yaml")


def _trivial_universe(
    *, ticker: str, name: str, current_price: float, adv_value: float
) -> InstrumentUniverse:
    """Build a one-element InstrumentUniverse for single-instrument backtests."""
    return InstrumentUniverse(
        instruments=[
            Instrument(
                ticker=ticker,
                name=name,
                market="ashare",
                relationship="event_subject",
                current_price=float(current_price),
                adv_value=float(adv_value),
            )
        ],
        topic=name,
    )


# ═══════════════════════════════════════════════════════════════
# Scenario 1: 药明康德 BIOSECURE 多日博弈
# Real timeline: Jan 2024 bill introduced → partial recovery →
# committee vote → another drop → modified bill → bounce →
# Senate pass → final crash
# Real outcome: -30% over 2 months, but NOT monotonic
# ═══════════════════════════════════════════════════════════════

WUXI_EVENT = Event(
    ticker="603259",
    event_text=(
        "美国参议院提出《生物安全法案》(BIOSECURE Act)，"
        "将药明康德(603259)列入'受关注的生物技术公司'清单。"
        "若法案通过，美国政府机构和受资助实体将被禁止与药明康德签订新合同。"
        "药明康德约30%营收来自美国客户。公司回应称'对指控深表遗憾'，"
        "强调不会威胁美国国家安全。"
    ),
    event_type="regulatory",
    event_date="2024-01-25",
    prior_consensus=(
        "药明康德是全球CRO/CDMO龙头，2023年业绩稳健增长。"
        "市场一致预期2024年营收400亿+，PE约25x。"
        "中美脱钩风险存在但此前未实质落地。"
    ),
)
WUXI_PRICE = 72.0
WUXI_ADV = 5_000_000_000

WUXI_EXTERNAL_EVENTS = ExternalEventSchedule()
# R2 (T+1): 公司紧急电话会议，安抚市场
WUXI_EXTERNAL_EVENTS.add(ExternalEvent(
    round_idx=2,
    content_type="company_announcement",
    author_persona_id="__market__",
    text=(
        "[公司公告] 药明康德召开紧急投资者电话会，管理层表示："
        "1) 目前无任何美国客户取消订单；"
        "2) 法案从提出到通过至少需6-12个月；"
        "3) 公司海外非美业务占比持续提升。"
        "CFO强调2024年业绩指引不变。"
    ),
    authority_weight=0.85,
))
# R4 (T+3): 多家券商发布看法分歧的研报
WUXI_EXTERNAL_EVENTS.add(ExternalEvent(
    round_idx=4,
    content_type="research_note",
    author_persona_id="__market__",
    text=(
        "[研报] 中金证券维持'跑赢行业'评级：法案通过概率仅30%，"
        "即使通过也有2年过渡期，影响可控。目标价85元。"
        "但摩根士丹利下调至'减持'：地缘政治风险重定价不可逆，"
        "美国客户可能预防性转移订单，目标价45元。"
    ),
    authority_weight=0.80,
))
# R6 (T+5): 法案在委员会推进的消息
WUXI_EXTERNAL_EVENTS.add(ExternalEvent(
    round_idx=6,
    content_type="news_brief",
    author_persona_id="__market__",
    text=(
        "[快讯] 美国众议院监督委员会宣布将于下周对BIOSECURE法案进行投票。"
        "两党议员联合声明支持加速立法进程。"
        "分析人士称法案通过概率从30%上调至55%。"
        "药明康德ADR盘前跌6%。"
    ),
    authority_weight=0.90,
))
# R8 (T+7): 北向资金大幅流出 + 同业竞对受益
WUXI_EXTERNAL_EVENTS.add(ExternalEvent(
    round_idx=8,
    content_type="news_brief",
    author_persona_id="__market__",
    text=(
        "[市场动态] 北向资金连续3日净卖出药明康德，累计减持超20亿元。"
        "凯莱英、康龙化成等国内CRO公司股价逆势上涨5-8%，"
        "市场预期美国客户订单将转移至非受限企业。"
        "药明康德港股跌至历史新低。"
    ),
    authority_weight=0.85,
))
# R10 (T+9): 管理层回购公告 (反转信号)
WUXI_EXTERNAL_EVENTS.add(ExternalEvent(
    round_idx=10,
    content_type="company_announcement",
    author_persona_id="__market__",
    text=(
        "[公司公告] 药明康德宣布10亿元回购计划，回购价格上限不超过当前价格的150%。"
        "同时公告显示Q1新签订单同比+12%，美国客户续约率维持在90%以上。"
        "多位高管宣布增持。市场解读为管理层信心信号。"
    ),
    authority_weight=0.80,
))


# ═══════════════════════════════════════════════════════════════
# Scenario 2: 924央行政策 + 后续震荡
# Real timeline: 924 triple-cut → euphoric rally → profit taking →
# 国庆假期 → 10/8 开盘高开后巨量回调
# Real: CITIC Sec +70% in 1 week, then -20% pullback
# ═══════════════════════════════════════════════════════════════

CITIC_EVENT = Event(
    ticker="600030",
    event_text=(
        "2024年9月24日，中国人民银行、证监会、金融监管总局同时发布多项重磅政策："
        "1) 降准50bp释放长期资金约1万亿；"
        "2) 降息20bp(7天逆回购利率从1.7%降至1.5%)；"
        "3) 创设5000亿证券基金保险互换便利工具(SFISF)；"
        "4) 创设3000亿股票回购增持再贷款；"
        "5) 降低存量房贷利率约0.5pp。"
        "三部委联合发布会释放极强稳市信号，A股午后直线拉升。"
        "中信证券(600030)作为券商龙头直接受益。"
    ),
    event_type="policy",
    event_date="2024-09-24",
    prior_consensus=(
        "924之前A股持续阴跌，上证跌破2700。"
        "市场极度悲观，融资余额降至1.3万亿，北向资金持续流出。"
        "券商板块估值处于历史底部，PB仅1.1x。"
        "市场预期降准降息空间有限，未预期到'金融组合拳'。"
    ),
)
CITIC_PRICE = 20.50
CITIC_ADV = 10_000_000_000

CITIC_EXTERNAL_EVENTS = ExternalEventSchedule()
# R2 (T+1): 延续狂热 + 各路资金涌入
CITIC_EXTERNAL_EVENTS.add(ExternalEvent(
    round_idx=2,
    content_type="news_brief",
    author_persona_id="__market__",
    text=(
        "[市场狂热] 两市成交额突破1.8万亿，创2015年以来新高。"
        "开户数单日新增46万户。融资余额单日增加300亿。"
        "北向资金单日净买入超200亿。全市场超4800只个股上涨。"
        "券商板块集体涨停，中信证券封死涨停板。"
    ),
    authority_weight=0.90,
))
# R4 (T+3): 获利了结 + 理性声音出现
CITIC_EXTERNAL_EVENTS.add(ExternalEvent(
    round_idx=4,
    content_type="research_note",
    author_persona_id="__market__",
    text=(
        "[冷静提示] 社科院研究员发文：'本轮行情是政策驱动的估值修复，"
        "非基本面改善。当前券商PB已修复至1.8x，接近合理区间上沿。"
        "2015年牛市教训犹在，追涨需谨慎。'"
        "部分早期获利盘开始兑现，龙虎榜显示机构净卖出中信证券3.2亿。"
    ),
    authority_weight=0.75,
))
# R6 (T+5): 假期效应 + 港股继续涨
CITIC_EXTERNAL_EVENTS.add(ExternalEvent(
    round_idx=6,
    content_type="news_brief",
    author_persona_id="__market__",
    text=(
        "[假期行情] 国庆假期期间港股继续大涨，恒生指数累涨超10%。"
        "A50期指夜盘大涨6%。社交媒体刷屏'节后开盘即上车最后机会'。"
        "券商开户预约排到节后第三天。场外资金跑步入场情绪极高。"
        "但高盛发出警告：'We are tactically cautious on the rally.'"
    ),
    authority_weight=0.85,
))
# R8 (T+7): 10.8 开盘冲高回落
CITIC_EXTERNAL_EVENTS.add(ExternalEvent(
    round_idx=8,
    content_type="news_brief",
    author_persona_id="__market__",
    text=(
        "[巨量震荡] 10月8日A股高开后巨幅震荡，上证指数盘中涨超10%后回落。"
        "两市成交额创纪录达3.45万亿。中信证券高开15%后冲高回落，"
        "尾盘仅涨2%。大量追高资金被套。龙虎榜显示机构大幅净卖出。"
        "市场分歧加剧：有人喊'牛市起点'，有人喊'2015年翻版'。"
    ),
    authority_weight=0.90,
))
# R10 (T+9): 政策后续不及预期
CITIC_EXTERNAL_EVENTS.add(ExternalEvent(
    round_idx=10,
    content_type="policy_statement",
    author_persona_id="__market__",
    text=(
        "[政策跟进] 国新办发布会：财政部表示'将加大逆周期调节力度'，"
        "但未公布具体财政刺激规模。市场此前预期至少2万亿增量财政，"
        "但发布会措辞模糊，缺乏增量信息。"
        "A股午后跳水，上证跌破3200。乐观情绪明显降温。"
    ),
    authority_weight=0.90,
))


# ═══════════════════════════════════════════════════════════════
# Scenario 3: 宁德时代 产能过剩 + 海外突破 拉锯战
# Mixed: domestic overcapacity concern vs overseas expansion
# Real: stock oscillated ±15% over 2 months in late 2024
# ═══════════════════════════════════════════════════════════════

CATL_EVENT = Event(
    ticker="300750",
    event_text=(
        "宁德时代(300750)发布2024年三季报：Q3营收922.8亿元,同比-12.5%；"
        "归母净利润131.4亿,同比+26.0%,利润超预期但营收不及预期。"
        "动力电池出货量全球份额37.5%,但国内价格战导致均价下滑20%。"
        "以价换量策略引发市场争议：利润率提升但市场担忧可持续性。"
    ),
    event_type="earnings",
    event_date="2024-10-18",
    prior_consensus=(
        "市场预期Q3营收980亿(miss)，净利润120亿(beat)。"
        "锂电池价格持续下跌，碳酸锂从60万跌至8万。"
        "市场主要分歧：是否已经进入成熟期低增长阶段。PE约20x。"
    ),
)
CATL_PRICE = 235.0
CATL_ADV = 8_000_000_000

CATL_EXTERNAL_EVENTS = ExternalEventSchedule()
# R2: 利润超预期 → 多头解读
CATL_EXTERNAL_EVENTS.add(ExternalEvent(
    round_idx=2,
    content_type="research_note",
    author_persona_id="__market__",
    text=(
        "[研报] 天风证券：宁德时代Q3利润超预期表明成本控制能力极强，"
        "毛利率环比提升2pp至26.9%，创近两年新高。"
        "预计Q4海外出货占比提升至35%，海外毛利率高出国内8pp。"
        "维持'买入'评级，目标价300元。"
    ),
    authority_weight=0.80,
))
# R4: 产能过剩担忧加剧
CATL_EXTERNAL_EVENTS.add(ExternalEvent(
    round_idx=4,
    content_type="news_brief",
    author_persona_id="__market__",
    text=(
        "[行业数据] 中国汽车动力电池产业创新联盟数据：10月电池产量108GWh，"
        "但装车量仅58GWh，产能利用率仅53.7%，库存创历史新高。"
        "二三线电池厂开始亏损出清，但宁德时代仍在扩产。"
        "市场担忧行业'内卷到底'将压制所有玩家利润率。"
    ),
    authority_weight=0.85,
))
# R6: 海外大单 → 反转信号
CATL_EXTERNAL_EVENTS.add(ExternalEvent(
    round_idx=6,
    content_type="company_announcement",
    author_persona_id="__market__",
    text=(
        "[重大合同] 宁德时代与福特汽车签署为期5年的电池供应协议，"
        "总金额超过200亿美元。同时宣布匈牙利工厂提前投产，"
        "年产能100GWh，已获得BMW、Stellantis等欧洲车企订单。"
        "海外产能布局领先竞争对手2-3年。"
    ),
    authority_weight=0.90,
))
# R8: 欧盟关税威胁
CATL_EXTERNAL_EVENTS.add(ExternalEvent(
    round_idx=8,
    content_type="news_brief",
    author_persona_id="__market__",
    text=(
        "[地缘风险] 欧盟委员会考虑对中国电池加征25%反补贴关税，"
        "若实施将显著影响宁德时代海外毛利率。"
        "美国IRA法案FEOC条款也可能限制宁德时代技术授权模式。"
        "海外扩张逻辑受到挑战，前期涨幅面临回吐压力。"
    ),
    authority_weight=0.85,
))
# R10: 月度销量数据超预期
CATL_EXTERNAL_EVENTS.add(ExternalEvent(
    round_idx=10,
    content_type="news_brief",
    author_persona_id="__market__",
    text=(
        "[行业数据] 11月新能源汽车销量126万辆,同比+35%,超市场预期的110万。"
        "宁德时代装车量市占率回升至40.2%。碳酸锂价格企稳于7.5万/吨，"
        "成本端压力缓解。储能业务Q4订单量同比翻倍。"
    ),
    authority_weight=0.80,
))


# ═══════════════════════════════════════════════════════════════
# Run all scenarios
# ═══════════════════════════════════════════════════════════════

SCENARIOS = [
    {
        "name": "药明康德 BIOSECURE 多日博弈",
        "event": WUXI_EVENT,
        "current_price": WUXI_PRICE,
        "adv_value": WUXI_ADV,
        "external_events": WUXI_EXTERNAL_EVENTS,
        "schedule": "policy-10d",
        "real_outcome": "2个月跌30-49%,但中间有反弹。非单调下跌。",
        "expected_pattern": "整体下跌,但中间有反弹(公司回应/回购时)",
    },
    {
        "name": "924政策 中信证券 狂热→回调",
        "event": CITIC_EVENT,
        "current_price": CITIC_PRICE,
        "adv_value": CITIC_ADV,
        "external_events": CITIC_EXTERNAL_EVENTS,
        "schedule": "policy-10d",
        "real_outcome": "1周涨70%,然后回调20%。先暴涨后震荡回落。",
        "expected_pattern": "前半段暴涨,后半段获利回吐+政策不及预期回落",
    },
    {
        "name": "宁德时代 Q3混合信号 拉锯战",
        "event": CATL_EVENT,
        "current_price": CATL_PRICE,
        "adv_value": CATL_ADV,
        "external_events": CATL_EXTERNAL_EVENTS,
        "schedule": "policy-10d",
        "real_outcome": "2个月内±15%震荡,无明确方向,多空拉锯。",
        "expected_pattern": "有涨有跌,围绕初始价震荡",
    },
]


async def run_scenario(scenario: dict) -> dict:
    t0 = time.time()
    event = scenario["event"]
    personas = load_personas(PERSONAS_PATH)
    schedule = make_schedule(scenario["schedule"], event.event_date)
    n_rounds = schedule.n_rounds

    instrument_universe = _trivial_universe(
        ticker=event.ticker,
        name=event.instrument or event.ticker,
        current_price=scenario["current_price"],
        adv_value=scenario["adv_value"],
    )

    entity_graph = build_from_template(
        topic=(event.event_text or "")[:200],
        market="ashare",
        current_price=float(scenario["current_price"]),
    )

    result = await run_simulation(
        event, personas, n_rounds=n_rounds,
        round_schedule=schedule,
        entity_graph=entity_graph,
        instrument_universe=instrument_universe,
        external_events=scenario["external_events"],
    )

    elapsed = time.time() - t0

    # Analyze price trajectory for oscillation
    traj = result.price_trajectory
    deltas = []
    direction_changes = 0
    for i in range(1, len(traj)):
        d = (traj[i] / traj[i - 1] - 1.0) * 100
        deltas.append(d)
        if i >= 2:
            prev_d = (traj[i - 1] / traj[i - 2] - 1.0) * 100
            if (d > 0 and prev_d < 0) or (d < 0 and prev_d > 0):
                direction_changes += 1

    up_rounds = sum(1 for d in deltas if d > 0)
    down_rounds = sum(1 for d in deltas if d < 0)

    return {
        "name": scenario["name"],
        "real_outcome": scenario["real_outcome"],
        "expected_pattern": scenario["expected_pattern"],
        "initial_price": result.initial_price,
        "final_price": result.final_price,
        "cumulative_pct": round(result.cumulative_delta_pct * 100, 2),
        "price_trajectory": [round(p, 2) for p in traj],
        "round_deltas_pct": [round(d, 2) for d in deltas],
        "direction_changes": direction_changes,
        "up_rounds": up_rounds,
        "down_rounds": down_rounds,
        "n_rounds": result.n_rounds,
        "n_personas": result.n_personas,
        "cost_usd": round(result.cost_usd, 4),
        "elapsed_s": round(elapsed, 1),
    }


def print_trajectory_chart(name: str, traj: list[float], deltas: list[float]):
    """ASCII mini-chart of price trajectory."""
    print(f"\n{'─' * 60}", file=sys.stderr)
    print(f"  {name}", file=sys.stderr)
    print(f"{'─' * 60}", file=sys.stderr)

    base = traj[0]
    for i, price in enumerate(traj):
        pct = (price / base - 1) * 100
        bar_len = int(abs(pct) * 2)
        if pct >= 0:
            bar = " " * 20 + "│" + "█" * min(bar_len, 30)
            label = f"+{pct:.1f}%"
        else:
            pad = max(0, 20 - bar_len)
            bar = " " * pad + "█" * min(bar_len, 20) + "│"
            label = f"{pct:.1f}%"

        round_label = f"R{i:>2}"
        delta_str = f"({deltas[i - 1]:+.1f}%)" if i > 0 else "(init)"
        print(f"  {round_label} {bar} {label:>8} {delta_str:>8}  {price:.2f}",
              file=sys.stderr)

    print(file=sys.stderr)


async def main():
    print("=" * 70, file=sys.stderr)
    print("ssFlow Dynamic Backtest — Multi-Day 博弈 Scenarios", file=sys.stderr)
    print(f"Each scenario: policy-10d (12 rounds) + external event injections", file=sys.stderr)
    print("=" * 70, file=sys.stderr, flush=True)

    results = []
    for i, scenario in enumerate(SCENARIOS):
        print(f"\n[{i+1}/{len(SCENARIOS)}] {scenario['name']}...",
              file=sys.stderr, flush=True)
        print(f"  External events: {len(scenario['external_events'])} injections",
              file=sys.stderr, flush=True)
        try:
            r = await run_scenario(scenario)
            results.append(r)

            print(f"  Result: {r['cumulative_pct']:+.1f}% | "
                  f"up={r['up_rounds']} down={r['down_rounds']} "
                  f"reversals={r['direction_changes']} | "
                  f"[{r['elapsed_s']:.0f}s, ${r['cost_usd']:.3f}]",
                  file=sys.stderr, flush=True)

            print_trajectory_chart(
                r["name"], r["price_trajectory"], r["round_deltas_pct"]
            )

        except Exception as e:
            import traceback
            traceback.print_exc(file=sys.stderr)
            results.append({"name": scenario["name"], "error": str(e)})

    # Summary
    print("\n" + "=" * 70, file=sys.stderr)
    print("SUMMARY: 博弈质量评估", file=sys.stderr)
    print("=" * 70, file=sys.stderr)
    print(f"{'Scenario':<30} {'Cum':>7} {'Up':>3} {'Dn':>3} {'Rev':>4} {'Pattern':>12}",
          file=sys.stderr)
    print("-" * 70, file=sys.stderr)
    for r in results:
        if "error" in r:
            print(f"{r['name']:<30} ERROR", file=sys.stderr)
            continue
        pattern = "monotonic" if r["direction_changes"] == 0 else \
                  "some_osc" if r["direction_changes"] <= 2 else "dynamic"
        print(f"{r['name']:<30} {r['cumulative_pct']:>+6.1f}% "
              f"{r['up_rounds']:>3} {r['down_rounds']:>3} "
              f"{r['direction_changes']:>4} {pattern:>12}",
              file=sys.stderr)
    print(file=sys.stderr)

    total_reversals = sum(r.get("direction_changes", 0) for r in results if "error" not in r)
    print(f"Total direction reversals across all scenarios: {total_reversals}", file=sys.stderr)
    if total_reversals >= 6:
        print("✓ 博弈质量良好 — agents are fighting", file=sys.stderr)
    elif total_reversals >= 3:
        print("△ 博弈质量一般 — some oscillation but could be more dynamic", file=sys.stderr)
    else:
        print("✗ 博弈不足 — still too monotonic, agents herd together", file=sys.stderr)

    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
