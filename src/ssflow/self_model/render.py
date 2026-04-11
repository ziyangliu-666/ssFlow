"""Prompt render section library for the self-model subsystem.

Each section is a ``(state, prev_state, peers) -> str`` template that
produces a Chinese multi-line block for the agent's user-instruction
prepend. Sections are keyed by name and selected per-persona via
``SelfModelSpec.render_sections``.

Design rules:
- Every section must gracefully handle missing keys via ``.get``
- Keep each section ≤ 6 lines of output to respect the token budget
  (9 sections × 6 lines × 25 personas × 6 rounds would be brutal)
- Use `_fmt_money` / `_fmt_pct` helpers so number formatting is
  consistent across sections
- Chinese-first; never direct-translate EN metaphors (per user
  global preference)
"""

from __future__ import annotations

from typing import Any, Callable


# ─────────────────────── Formatting helpers ───────────────────────


def _fmt_money(v: float) -> str:
    if v is None or v == 0:
        return "—"
    abs_v = abs(v)
    sign = "-" if v < 0 else ""
    if abs_v >= 1e12:
        return f"{sign}¥{abs_v/1e12:.2f}万亿"
    if abs_v >= 1e8:
        return f"{sign}¥{abs_v/1e8:.2f}亿"
    if abs_v >= 1e4:
        return f"{sign}¥{abs_v/1e4:.1f}万"
    return f"{sign}¥{abs_v:,.0f}"


def _fmt_pct(v: float, sign: bool = True) -> str:
    if v is None:
        return "—"
    if sign:
        return f"{v:+.1f}%"
    return f"{v:.1f}%"


def _side_zh(numeric: float) -> str:
    """Decode numeric side back to Chinese."""
    if numeric > 0.5:
        return "买入"
    if numeric < -0.5:
        return "卖出"
    return "观望"


def _bar(val: float, max_v: int = 5) -> str:
    """Unicode ▓░ progress bar for [0,1] values."""
    try:
        filled = max(0, min(max_v, int(val * max_v)))
    except (TypeError, ValueError):
        filled = 0
    return "▓" * filled + "░" * (max_v - filled)


# ─────────────────────── Section renderers ───────────────────────


def _render_financial_snapshot(
    state: dict[str, float],
    prev: dict[str, float],
    peers: list[dict[str, Any]],
) -> str:
    nav = state.get("nav", 0)
    init = state.get("initial_nav", nav)
    pnl = state.get("unrealized_pnl_pct", 0)
    dd = state.get("max_drawdown_pct", 0)
    cash = state.get("cash", 0)
    holdings = state.get("holdings_value", 0)
    return (
        f"# 你的财务状况 / Your Position\n"
        f"  账户净值: {_fmt_money(nav)} (初始 {_fmt_money(init)})\n"
        f"  未实现盈亏: {_fmt_pct(pnl)}\n"
        f"  本 sim 最大回撤: {_fmt_pct(dd, sign=False)}\n"
        f"  现金: {_fmt_money(cash)}  持仓: {_fmt_money(holdings)}"
    )


def _render_last_trade_outcome(
    state: dict[str, float],
    prev: dict[str, float],
    peers: list[dict[str, Any]],
) -> str:
    side_num = state.get("last_action_side", 0)
    size = state.get("last_action_size_pct", 0)
    price = state.get("last_action_price", 0)
    outcome = state.get("last_action_outcome_pct", 0)
    idle = int(state.get("rounds_since_last_trade", 0))

    if idle == 0 and size > 0:
        outcome_note = "✓ 正收益" if outcome > 0 else ("✗ 负收益" if outcome < 0 else "持平")
        return (
            f"# 你的上轮决策 / Last Round\n"
            f"  方向: {_side_zh(side_num)}  规模: {int(size*100)}%\n"
            f"  入场价: {price:.2f}\n"
            f"  决策后价格变化: {_fmt_pct(outcome)}  {outcome_note}"
        )
    if idle > 0:
        note = f"(已观望 {idle} 轮)" if size == 0 else f"(上次有动作 {idle} 轮前)"
        return (
            f"# 你的上轮决策 / Last Round\n"
            f"  方向: 观望  {note}\n"
            f"  距上轮入场: {_fmt_pct(outcome)}"
        )
    return (
        f"# 你的上轮决策 / Last Round\n"
        f"  尚未有任何交易 (首轮)"
    )


def _render_benchmark_gap(
    state: dict[str, float],
    prev: dict[str, float],
    peers: list[dict[str, Any]],
) -> str:
    gap = state.get("benchmark_gap_pct", 0)
    rank = state.get("peer_rank_percentile", 0.5)
    my_return = state.get("unrealized_pnl_pct", 0)
    benchmark_return = my_return - gap
    note = "✓ 跑赢基准" if gap > 0 else ("✗ 落后基准" if gap < 0 else "持平基准")
    return (
        f"# 相对基准 / vs Benchmark\n"
        f"  你: {_fmt_pct(my_return)}   基准: {_fmt_pct(benchmark_return)}\n"
        f"  差距: {_fmt_pct(gap)}pp  {note}\n"
        f"  同业排名: 前 {int((1-rank)*100)}%"
    )


def _render_mandate_headroom(
    state: dict[str, float],
    prev: dict[str, float],
    peers: list[dict[str, Any]],
) -> str:
    used = state.get("risk_budget_used_pct", 0)
    breaches = int(state.get("compliance_breaches_this_sim", 0))
    warn = ""
    if used > 0.85:
        warn = "  ⚠ 接近合规上限"
    if used > 1.0:
        warn = f"  ⚠ 超限! 已违规 {breaches} 次"
    return (
        f"# 合规头寸 / Mandate Status\n"
        f"  风险预算使用: {_fmt_pct(used*100, sign=False)}\n"
        f"  距离上限: {_fmt_pct(max(0, (1-used))*100, sign=False)}"
        f"{warn}"
    )


def _render_emotional_state(
    state: dict[str, float],
    prev: dict[str, float],
    peers: list[dict[str, Any]],
) -> str:
    conviction = state.get("conviction", 0.5)
    stress = state.get("stress", 0)
    losses = int(state.get("consecutive_loss_rounds", 0))
    lines = [
        f"# 心理状态 / State of Mind",
        f"  信心: {_bar(conviction)}  ({conviction:.2f})",
        f"  压力: {_bar(stress)}  ({stress:.2f})",
    ]
    if losses >= 2:
        lines.append(f"  ⚠ 连续亏损 {losses} 轮, 决策偏谨慎")
    return "\n".join(lines)


def _render_peer_watchlist(
    state: dict[str, float],
    prev: dict[str, float],
    peers: list[dict[str, Any]],
) -> str:
    if not peers:
        return (
            f"# 你关注的同行 / Peers You Watch\n"
            f"  (暂无可见同行)"
        )
    lines = ["# 你关注的同行 / Peers You Watch"]
    for p in peers[:3]:
        name = p.get("display_name") or p.get("persona_id", "?")
        pnl = p.get("unrealized_pnl_pct", 0)
        last_side = _side_zh(p.get("last_action_side", 0))
        lines.append(f"  {name}: {last_side}, NAV {_fmt_pct(pnl)}")
    return "\n".join(lines)


def _render_career_pressure(
    state: dict[str, float],
    prev: dict[str, float],
    peers: list[dict[str, Any]],
) -> str:
    losses = int(state.get("consecutive_loss_rounds", 0))
    redemption = state.get("client_net_redemption", 0)
    pressure = state.get("career_risk_pressure", 0)
    warn = ""
    if pressure > 0.5:
        warn = "  ⚠ 你的 PM 位置开始不稳"
    if pressure > 0.8:
        warn = "  ⚠ 你面临撤换风险"
    return (
        f"# 你的职业压力 / Career Risk\n"
        f"  连续亏损轮数: {losses}\n"
        f"  客户净申赎压力: {redemption:+.2f}\n"
        f"  综合压力: {_bar(pressure)}  ({pressure:.2f}){warn}"
    )


def _render_runway_status(
    state: dict[str, float],
    prev: dict[str, float],
    peers: list[dict[str, Any]],
) -> str:
    months = state.get("cash_runway_months", 12)
    warn = ""
    if months < 6:
        warn = "  ⚠ 需要考虑融资"
    if months < 3:
        warn = "  ⚠ 现金流危机"
    return (
        f"# 公司现金 / Cash Runway\n"
        f"  预计支撑: {months:.1f} 个月{warn}"
    )


def _render_board_pressure(
    state: dict[str, float],
    prev: dict[str, float],
    peers: list[dict[str, Any]],
) -> str:
    index = state.get("board_pressure_index", 0)
    customer_risk = state.get("customer_concentration_risk", 0)
    neg_media = int(state.get("media_negative_mention_count", 0))
    lines = [
        f"# 公司外部压力 / External Pressure",
        f"  董事会压力指数: {_bar(index)}  ({index:.2f})",
        f"  客户集中风险: {_fmt_pct(customer_risk*100, sign=False)}",
        f"  负面媒体提及: {neg_media}",
    ]
    return "\n".join(lines)


# ─────────────────────── Registry ───────────────────────


RenderFn = Callable[
    [dict[str, float], dict[str, float], list[dict[str, Any]]],
    str,
]

RENDER_SECTIONS: dict[str, RenderFn] = {
    "financial_snapshot": _render_financial_snapshot,
    "last_trade_outcome": _render_last_trade_outcome,
    "benchmark_gap": _render_benchmark_gap,
    "mandate_headroom": _render_mandate_headroom,
    "emotional_state": _render_emotional_state,
    "peer_watchlist": _render_peer_watchlist,
    "career_pressure": _render_career_pressure,
    "runway_status": _render_runway_status,
    "board_pressure": _render_board_pressure,
}


__all__ = ["RENDER_SECTIONS"]
