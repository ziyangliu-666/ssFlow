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


__all__ = [
    "render_markdown",
    "render_safe_or_quarantine",
    "save_report",
]
