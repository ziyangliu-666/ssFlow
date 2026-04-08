"""Markdown report renderer.

Three sections per the design doc P1:
    1. 群体反应叙事 (group reaction narrative)
    2. 盲点清单 (blind spot list)
    3. 模拟群体的 implied price move (descriptive)

The full report is run through the compliance output_filter before display.
If a violation is found, the report is replaced with a generic
"verification in progress" message and the raw report is logged for review.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from .aggregation import AggregatedReport
from .config import settings
from .output_filter import ComplianceViolation, assert_compliant, sanitize_text
from .sandbox import SandboxResult
from .simulation import SimulationResult


log = logging.getLogger(__name__)


# ─────────────────────── Renderers ───────────────────────


def _render_narrative(result: SimulationResult, report: AggregatedReport) -> str:
    """Compose the group-reaction narrative section."""
    by_archetype: dict[str, list[str]] = {}
    by_id = {p.id: p for p in result.personas}
    for r in result.final_reactions:
        p = by_id.get(r.persona_id)
        if not p:
            continue
        by_archetype.setdefault(p.archetype, []).append(sanitize_text(r.comment))

    lines = []
    for archetype, comments in by_archetype.items():
        if not comments:
            continue
        comment = comments[0]  # one rep per archetype for compactness
        lines.append(f"- **{archetype}**: {comment}")

    histogram = report.sentiment_histogram
    total = sum(histogram.values())
    if total:
        neg_share = (histogram["strongly_negative"] + histogram["negative"]) / total
        pos_share = (histogram["positive"] + histogram["strongly_positive"]) / total
        neutral_share = histogram["neutral"] / total
        summary = (
            f"模拟群体情绪分布: {neg_share:.0%} 偏负面, {neutral_share:.0%} 中性, "
            f"{pos_share:.0%} 偏正面 (n={total})"
        )
    else:
        summary = "模拟群体: 无样本"

    return summary + "\n\n" + "\n".join(lines)


def _render_blind_spots(report: AggregatedReport) -> str:
    if not report.blind_spots:
        return "(本次模拟未识别出明显的盲点)"
    return "\n".join(f"{i+1}. {sanitize_text(bs)}" for i, bs in enumerate(report.blind_spots))


def _render_implied_move(report: AggregatedReport) -> str:
    confidence_label = (
        "高" if report.implied_move_confidence > 0.65 else
        ("中" if report.implied_move_confidence > 0.4 else "低")
    )
    return (
        f"模拟群体的 implied price move (描述性, 非预测): "
        f"**{report.implied_move_low:+.1f}% ~ {report.implied_move_high:+.1f}%**\n\n"
        f"模型置信度: {confidence_label} ({report.implied_move_confidence:.2f}) | "
        f"群体分歧度 (dispersion): {report.dispersion:.2f}\n\n"
        f"_注: 此区间是 {len(report.persona_summary)} 个模拟 persona 群体行为聚合后的衍生信号, "
        f"不构成对真实股价的预测, 也不作为任何投资决策依据。_"
    )


def _render_action_intent(report: AggregatedReport) -> str:
    if not report.action_intent_breakdown:
        return ""
    rows = "\n".join(
        f"- {intent}: {share:.0%}"
        for intent, share in report.action_intent_breakdown.items()
    )
    return f"### 行动倾向分布 (action intent)\n\n{rows}"


def render_markdown(result: SimulationResult, report: AggregatedReport) -> str:
    """Compose the full markdown report. Call assert_compliant before display."""
    event = result.event
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    md = f"""# ssFish 模拟报告

**事件**: `{event.ticker}` · {event.event_type} · {event.event_date}
**Simulation ID**: `{result.simulation_id}`
**模拟规模**: {result.n_personas} personas × {result.n_rounds} rounds
**耗时**: {result.elapsed_seconds:.1f}s · **成本**: ${result.cost_usd:.4f}
**生成时间**: {now}

---

## 1. 群体反应叙事

{_render_narrative(result, report)}

{_render_action_intent(report)}

---

## 2. 盲点清单

{_render_blind_spots(report)}

---

## 3. 模拟群体的 implied price move

{_render_implied_move(report)}

---

## 模拟元信息

- 事件文本 hash: `{event.text_hash}`
- 上下文完整度: {event.context_completeness:.0%} (prior_consensus + recent_price + sector_context)
- 情绪均值 / 标准差: {report.sentiment_mean:+.3f} / {report.sentiment_std:.3f}
- 情绪直方图: {report.sentiment_histogram}
"""
    return md.strip()


def render_safe_or_quarantine(
    result: SimulationResult, report: AggregatedReport
) -> tuple[str, bool]:
    """Render the report and run it through the compliance filter.

    Returns:
        (display_text, is_compliant): display_text is either the real report
        (if it passed the filter) or a placeholder message (if it failed).
        is_compliant tells the caller which one happened.
    """
    full_md = render_markdown(result, report)
    try:
        assert_compliant(full_md)
        return full_md, True
    except ComplianceViolation as exc:
        # Persist the offending report to a quarantine folder
        quarantine_dir = Path(settings.project_root) / "reports" / "quarantine"
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        path = quarantine_dir / f"{result.simulation_id}.md"
        path.write_text(full_md, encoding="utf-8")
        log.warning(
            "Report %s quarantined due to compliance filter. Violations: %s. Saved to %s",
            result.simulation_id,
            exc.violations,
            path,
        )
        placeholder = (
            f"# ssFish 模拟报告 - 验证中\n\n"
            f"Simulation ID: `{result.simulation_id}`\n\n"
            f"本次模拟的输出正在进行合规验证, 暂时无法显示。\n"
            f"如果您是工具的开发者, 请检查 `reports/quarantine/{result.simulation_id}.md`。\n"
        )
        return placeholder, False


def save_report(text: str, simulation_id: str) -> Path:
    """Persist a report to the reports/ dir, return its path."""
    reports_dir = settings.reports_dir
    path = reports_dir / f"{simulation_id}.md"
    path.write_text(text, encoding="utf-8")
    return path


# ─────────────────────── Sandbox-mode renderers ───────────────────────


def _render_price_trajectory_ascii(result: SandboxResult, width: int = 50) -> str:
    """A small ASCII chart of the price trajectory across rounds.

    Looks like:
        R0  ¥218.50  ████████████████████████░░░░░  +0.00%
        R1  ¥213.27  ████████████████████░░░░░░░░░  -2.39%
        ...
    """
    traj = result.price_trajectory
    if not traj:
        return "(no trajectory)"
    p_min = min(traj)
    p_max = max(traj)
    spread = p_max - p_min if p_max > p_min else 1.0

    lines = []
    for i, p in enumerate(traj):
        pct = (p / traj[0] - 1.0) * 100 if traj[0] else 0.0
        bar_len = int((p - p_min) / spread * width)
        bar = "█" * bar_len + "░" * (width - bar_len)
        lines.append(f"  R{i}  ¥{p:7.2f}  {bar}  {pct:+6.2f}%")
    return "\n".join(lines)


def _render_sandbox_round_table(result: SandboxResult) -> str:
    """Markdown table summarizing each round's net flow + price update."""
    lines = ["| Round | Price before | Price after | ΔP | Net flow (¥億) |",
             "|---|---|---|---|---|"]
    for r in result.rounds:
        lines.append(
            f"| R{r.round_idx} | ¥{r.price_before:.2f} | ¥{r.price_after:.2f} | "
            f"{r.delta_pct * 100:+.2f}% | {r.net_flow_cny / 1e8:+.2f} |"
        )
    return "\n".join(lines)


def _render_class_pnl_table(result: SandboxResult) -> str:
    """Markdown table of per-class P&L at the final price, sorted descending."""
    by_id = {p.id: p for p in result.personas}
    pnl = result.compute_class_pnl()
    if not pnl:
        return "(no agents)"
    rows_data = sorted(pnl.items(), key=lambda kv: kv[1], reverse=True)

    lines = ["| Persona class | Archetype | Final P&L (¥M) | Per-agent (¥k) | Agents |",
             "|---|---|---|---|---|"]
    for cid, total_pnl in rows_data:
        p = by_id.get(cid)
        if p is None:
            continue
        n_agents = (
            p.sandbox.instance_count if p.sandbox else 0
        )
        per_agent = (total_pnl / n_agents / 1e3) if n_agents else 0.0
        lines.append(
            f"| `{cid}` | {p.archetype} | "
            f"{total_pnl / 1e6:+.2f} | {per_agent:+.1f} | {n_agents} |"
        )
    return "\n".join(lines)


def _render_strategic_signals(result: SandboxResult) -> str:
    """Render the strategic-layer signals from strategic personas (if any).

    These are the qualitative outputs of personas with
    `contributes_to_strategic_signal=True`. The report renders them in a
    separate section because they don't participate in the price trajectory
    aggregation — they're a parallel signal track per spec §11 Q3.
    """
    by_id = {p.id: p for p in result.personas}
    # Pull strategic signals from the LAST round (most settled state)
    if not result.rounds:
        return "(no rounds)"
    last_round = result.rounds[-1]
    signals = []
    for cid, cf in last_round.class_flows.items():
        p = by_id.get(cid)
        if not p or not p.contributes_to_strategic_signal or not cf.strategic_signal:
            continue
        sig = cf.strategic_signal
        signals.append((p.archetype, cid, sig, cf.rationale))

    if not signals:
        return "_本次模拟未触发战略层信号_"

    lines = []
    for archetype, cid, sig, rationale in signals:
        direction = sig.get("direction", "?")
        magnitude = sig.get("magnitude", "?")
        horizon = sig.get("time_horizon_days", "?")
        lines.append(
            f"- **{archetype}** (`{cid}`): direction={direction}, "
            f"magnitude={magnitude}, time_horizon={horizon}天"
        )
        if rationale:
            lines.append(f"  - _{sanitize_text(rationale)[:200]}_")
    return "\n".join(lines)


def _render_round_voices(result: SandboxResult, max_per_round: int = 3) -> str:
    """Pull a few class rationales from each round so the qualitative voice
    isn't lost behind the price chart.
    """
    by_id = {p.id: p for p in result.personas}
    sections = []
    for r in result.rounds:
        round_lines = [f"### R{r.round_idx} (¥{r.price_before:.2f} → ¥{r.price_after:.2f}, {r.delta_pct * 100:+.2f}%)"]
        # Sort classes by absolute flow contribution (most active first)
        sorted_classes = sorted(
            r.class_flows.items(),
            key=lambda kv: abs(kv[1].net_flow_cny),
            reverse=True,
        )[:max_per_round]
        for cid, cf in sorted_classes:
            p = by_id.get(cid)
            if not p:
                continue
            sign = "净买盘" if cf.net_flow_cny > 0 else ("净卖盘" if cf.net_flow_cny < 0 else "观望")
            round_lines.append(
                f"- **{p.archetype}** ({sign} ¥{abs(cf.net_flow_cny) / 1e8:.2f}億): "
                f"_{sanitize_text(cf.rationale)[:200]}_"
            )
        sections.append("\n".join(round_lines))
    return "\n\n".join(sections)


def render_sandbox_markdown(result: SandboxResult) -> str:
    """Compose the full markdown report for a sandbox-mode simulation."""
    event = result.event
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    md = f"""# ssFish 沙箱推演报告

**事件**: `{event.ticker}` · {event.event_type} · {event.event_date}
**Simulation ID**: `{result.simulation_id}`
**模拟规模**: {result.n_personas} persona 类别 × {result.n_rounds} rounds (agent-based market model)
**初始价格**: ¥{result.initial_price:.2f} · **终值**: ¥{result.final_price:.2f}
**累计变动**: **{result.cumulative_delta_pct * 100:+.2f}%**
**耗时**: {result.elapsed_seconds:.1f}s · **成本**: ${result.cost_usd:.4f}
**生成时间**: {now}

> 本报告由 ssFish 沙箱执行模式生成. 价格轨迹是从模拟订单流中**emerge**的,
> 不是 sentiment 启发式映射. λ 系数 = {result.lambda_used:.2f} (literature value),
> ADV = ¥{result.adv_cny_used / 1e8:.0f}億. 输出可与真实 T+1 / T+5 价格直接对账.

---

## 1. 价格轨迹 (Price Trajectory)

```
{_render_price_trajectory_ascii(result)}
```

{_render_sandbox_round_table(result)}

---

## 2. 类别盈亏 (Class P&L)

按最终价格 ¥{result.final_price:.2f} 计算的每类 persona 累计盈亏:

{_render_class_pnl_table(result)}

---

## 3. 战略层信号 (Strategic Layer)

这一节是产业资本/政府队/大股东类 persona 的**行为意图信号**, 它们不参与
sentiment 加权 (因为它们的反应是月级而非日级), 但它们的行为意图对中期
价格有强约束.

{_render_strategic_signals(result)}

---

## 4. 各轮反应 (Class Voices)

每轮取流量贡献最大的 3 个 persona 类别, 显示它们的 LLM 推理:

{_render_round_voices(result)}

---

## 模拟元信息

- 事件文本 hash: `{event.text_hash}`
- 上下文完整度: {event.context_completeness:.0%}
- 模式: **sandbox** (agent-based market model with Kyle square-root impact)
- λ used: {result.lambda_used:.3f}
- ADV used: ¥{result.adv_cny_used / 1e8:.1f}億
- 价格上下界 (历轮): ¥{min(result.price_trajectory):.2f} – ¥{max(result.price_trajectory):.2f}
- LLM seed: {result.llm_seed}
- 总订单流 (∑): ¥{sum(r.net_flow_cny for r in result.rounds) / 1e8:+.2f}億

_注: 此输出是 14 个真实数据校准的 persona 类别推演出的结果. 价格不是预测, 是
模拟出的市场反应. 请把它当成一个"假设这个事件触发了真实分布的市场参与者反应"
的思想实验, 不构成任何投资决策依据._
"""
    return md.strip()


def render_sandbox_safe_or_quarantine(result: SandboxResult) -> tuple[str, bool]:
    """Render a sandbox report and run it through the compliance filter."""
    full_md = render_sandbox_markdown(result)
    try:
        assert_compliant(full_md)
        return full_md, True
    except ComplianceViolation as exc:
        quarantine_dir = Path(settings.project_root) / "reports" / "quarantine"
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        path = quarantine_dir / f"{result.simulation_id}.md"
        path.write_text(full_md, encoding="utf-8")
        log.warning(
            "Sandbox report %s quarantined. Violations: %s. Saved to %s",
            result.simulation_id, exc.violations, path,
        )
        placeholder = (
            f"# ssFish 沙箱报告 - 验证中\n\n"
            f"Simulation ID: `{result.simulation_id}`\n\n"
            f"本次沙箱模拟的输出正在进行合规验证, 暂时无法显示.\n"
            f"开发者请检查 `reports/quarantine/{result.simulation_id}.md`.\n"
        )
        return placeholder, False


__all__ = [
    "render_markdown",
    "render_safe_or_quarantine",
    "render_sandbox_markdown",
    "render_sandbox_safe_or_quarantine",
    "save_report",
]
