"""Markdown report renderer for sandbox-mode simulations.

The full report is run through the compliance output_filter before display.
If a violation is found, the report is replaced with a generic
"verification in progress" placeholder and the raw report is logged.

This module is currency-agnostic. The Event provides `current_price` +
`adv_value` + `price_currency` (or defaults via the Event init); the
renderer formats them with the appropriate symbol and unit per market.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from .config import settings
from .output_filter import ComplianceViolation, assert_compliant, sanitize_text
from .sandbox import SandboxResult


log = logging.getLogger(__name__)


# ─────────────────────── Currency / unit formatting ───────────────────────


# Currency display preferences. The renderer picks the symbol and unit
# multiplier based on the Event's currency. Add new currencies here.
CURRENCY_FORMATS: dict[str, dict[str, object]] = {
    "CNY": {"symbol": "¥",  "big_unit": "億", "big_divisor": 1e8, "small_unit": "M",  "small_divisor": 1e6},
    "USD": {"symbol": "$",  "big_unit": "B",  "big_divisor": 1e9, "small_unit": "M",  "small_divisor": 1e6},
    "EUR": {"symbol": "€",  "big_unit": "B",  "big_divisor": 1e9, "small_unit": "M",  "small_divisor": 1e6},
    "JPY": {"symbol": "¥",  "big_unit": "B",  "big_divisor": 1e9, "small_unit": "M",  "small_divisor": 1e6},
    "HKD": {"symbol": "HK$","big_unit": "B",  "big_divisor": 1e9, "small_unit": "M",  "small_divisor": 1e6},
    "BTC": {"symbol": "₿",  "big_unit": "K",  "big_divisor": 1e3, "small_unit": "",   "small_divisor": 1.0},
}


def _fmt_price(value: float, currency: str = "CNY") -> str:
    fmt = CURRENCY_FORMATS.get(currency, CURRENCY_FORMATS["USD"])
    return f"{fmt['symbol']}{value:.2f}"


def _fmt_big(value: float, currency: str = "CNY") -> str:
    """Format a large CNY/USD/etc. value with the appropriate unit."""
    fmt = CURRENCY_FORMATS.get(currency, CURRENCY_FORMATS["USD"])
    return f"{fmt['symbol']}{value / fmt['big_divisor']:.2f}{fmt['big_unit']}"


def _fmt_small(value: float, currency: str = "CNY") -> str:
    fmt = CURRENCY_FORMATS.get(currency, CURRENCY_FORMATS["USD"])
    return f"{fmt['symbol']}{value / fmt['small_divisor']:.2f}{fmt['small_unit']}"


def _event_currency(result: SandboxResult) -> str:
    """Best-effort currency detection from the Event."""
    return getattr(result.event, "price_currency", None) or "CNY"


# ─────────────────────── Sandbox renderers ───────────────────────


def _render_price_trajectory_ascii(result: SandboxResult, width: int = 50) -> str:
    """Compact ASCII chart of the price trajectory across rounds."""
    traj = result.price_trajectory
    if not traj:
        return "(no trajectory)"
    p_min = min(traj)
    p_max = max(traj)
    spread = p_max - p_min if p_max > p_min else 1.0
    currency = _event_currency(result)
    fmt = CURRENCY_FORMATS.get(currency, CURRENCY_FORMATS["USD"])
    sym = fmt["symbol"]

    lines = []
    for i, p in enumerate(traj):
        pct = (p / traj[0] - 1.0) * 100 if traj[0] else 0.0
        bar_len = int((p - p_min) / spread * width)
        bar = "█" * bar_len + "░" * (width - bar_len)
        lines.append(f"  R{i}  {sym}{p:8.2f}  {bar}  {pct:+6.2f}%")
    return "\n".join(lines)


def _render_sandbox_round_table(result: SandboxResult) -> str:
    """Markdown table summarizing each round's net flow + price update."""
    currency = _event_currency(result)
    fmt = CURRENCY_FORMATS.get(currency, CURRENCY_FORMATS["USD"])
    sym = fmt["symbol"]
    big_unit = fmt["big_unit"]
    big_div = fmt["big_divisor"]

    lines = [
        f"| Round | Price before | Price after | ΔP | Net flow ({sym}{big_unit}) |",
        "|---|---|---|---|---|",
    ]
    for r in result.rounds:
        lines.append(
            f"| R{r.round_idx} | {sym}{r.price_before:.2f} | {sym}{r.price_after:.2f} | "
            f"{r.delta_pct * 100:+.2f}% | {r.net_flow_cny / big_div:+.2f} |"
        )
    return "\n".join(lines)


def _render_class_pnl_table(result: SandboxResult) -> str:
    """Markdown table of per-class P&L at the final price, sorted descending."""
    currency = _event_currency(result)
    fmt = CURRENCY_FORMATS.get(currency, CURRENCY_FORMATS["USD"])
    sym = fmt["symbol"]
    sm_div = fmt["small_divisor"]
    sm_unit = fmt["small_unit"]

    by_id = {p.id: p for p in result.personas}
    pnl = result.compute_class_pnl()
    if not pnl:
        return "(no agents)"
    rows_data = sorted(pnl.items(), key=lambda kv: kv[1], reverse=True)

    lines = [
        f"| Persona class | Archetype | Final P&L ({sym}{sm_unit}) | Per-agent ({sym}k) | Agents |",
        "|---|---|---|---|---|",
    ]
    for cid, total_pnl in rows_data:
        p = by_id.get(cid)
        if p is None:
            continue
        n_agents = p.sandbox.instance_count if p.sandbox else 0
        per_agent = (total_pnl / n_agents / 1e3) if n_agents else 0.0
        lines.append(
            f"| `{cid}` | {p.archetype} | "
            f"{total_pnl / sm_div:+.2f} | {per_agent:+.1f} | {n_agents} |"
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
        return "_No strategic-layer signals triggered in this run_"

    lines = []
    for archetype, cid, sig, rationale in signals:
        direction = sig.get("direction", "?")
        magnitude = sig.get("magnitude", "?")
        horizon = sig.get("time_horizon_days", "?")
        lines.append(
            f"- **{archetype}** (`{cid}`): direction={direction}, "
            f"magnitude={magnitude}, time_horizon={horizon}d"
        )
        if rationale:
            lines.append(f"  - _{sanitize_text(rationale)[:200]}_")
    return "\n".join(lines)


def _render_round_voices(result: SandboxResult, max_per_round: int = 3) -> str:
    """Pull a few class rationales from each round so the qualitative voice
    isn't lost behind the price chart.
    """
    currency = _event_currency(result)
    fmt = CURRENCY_FORMATS.get(currency, CURRENCY_FORMATS["USD"])
    sym = fmt["symbol"]
    big_div = fmt["big_divisor"]
    big_unit = fmt["big_unit"]

    by_id = {p.id: p for p in result.personas}
    sections = []
    for r in result.rounds:
        round_lines = [
            f"### R{r.round_idx} ({sym}{r.price_before:.2f} → {sym}{r.price_after:.2f}, {r.delta_pct * 100:+.2f}%)"
        ]
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
                f"- **{p.archetype}** ({sign} {sym}{abs(cf.net_flow_cny) / big_div:.2f}{big_unit}): "
                f"_{sanitize_text(cf.rationale)[:200]}_"
            )
        sections.append("\n".join(round_lines))
    return "\n\n".join(sections)


def render_sandbox_markdown(result: SandboxResult) -> str:
    """Compose the full markdown report for a sandbox-mode simulation.

    Currency-aware: pulls the currency from `result.event.price_currency`
    and formats prices/units accordingly. Defaults to CNY for backward compat.
    """
    event = result.event
    currency = _event_currency(result)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    instrument = getattr(event, "instrument", None) or event.ticker

    md = f"""# ssFish Market Simulation Report

**Event**: `{instrument}` · {event.event_type} · {event.event_date}
**Simulation ID**: `{result.simulation_id}`
**Scale**: {result.n_personas} persona classes × {result.n_rounds} rounds (agent-based market model)
**Initial price**: {_fmt_price(result.initial_price, currency)} · **Final**: {_fmt_price(result.final_price, currency)}
**Cumulative**: **{result.cumulative_delta_pct * 100:+.2f}%**
**Wall clock**: {result.elapsed_seconds:.1f}s · **Cost**: ${result.cost_usd:.4f}
**Generated**: {now}

> Report generated by ssFish sandbox mode. Price trajectory **emerges** from
> simulated order flow, not from a heuristic mapping. λ = {result.lambda_used:.2f}
> (literature value), ADV = {_fmt_big(result.adv_cny_used, currency)}. Output
> can be directly compared against real T+1 / T+5 prices.

---

## 1. Price Trajectory

```
{_render_price_trajectory_ascii(result)}
```

{_render_sandbox_round_table(result)}

---

## 2. Class P&L

Per-class cumulative P&L at final price {_fmt_price(result.final_price, currency)}:

{_render_class_pnl_table(result)}

---

## 3. Strategic Layer

This section shows behavioral intent signals from strategic personas
(controlling shareholders / state holders / large individual holders).
They do NOT participate in sentiment aggregation — their reactions are
month-scale, not day-scale — but their action intent constrains the
medium-term price.

{_render_strategic_signals(result)}

---

## 4. Class Voices (per round)

Top 3 classes by absolute net flow per round, with their LLM reasoning:

{_render_round_voices(result)}

---

## Simulation Metadata

- Event text hash: `{event.text_hash}`
- Context completeness: {event.context_completeness:.0%}
- Mode: **sandbox** (agent-based market model with Kyle square-root impact)
- λ used: {result.lambda_used:.3f}
- ADV used: {_fmt_big(result.adv_cny_used, currency)}
- Price range (across rounds): {_fmt_price(min(result.price_trajectory), currency)} – {_fmt_price(max(result.price_trajectory), currency)}
- LLM seed: {result.llm_seed}
- Total order flow (Σ): {_fmt_big(sum(r.net_flow_cny for r in result.rounds), currency)}

_Note: This output is the result of {result.n_personas} real-data-calibrated
persona classes simulating their reactions to the event. The price is NOT
a prediction; it is a simulated market reaction. Treat this as a thought
experiment of "what if real-distribution market participants reacted to
this event" — not as investment advice._
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
            f"# ssFish Simulation Report - Verification in progress\n\n"
            f"Simulation ID: `{result.simulation_id}`\n\n"
            f"The output is undergoing compliance verification.\n"
            f"Developers: see `reports/quarantine/{result.simulation_id}.md`.\n"
        )
        return placeholder, False


def save_report(text: str, simulation_id: str) -> Path:
    """Persist a report to the reports/ dir, return its path."""
    reports_dir = settings.reports_dir
    path = reports_dir / f"{simulation_id}.md"
    path.write_text(text, encoding="utf-8")
    return path


__all__ = [
    "CURRENCY_FORMATS",
    "render_sandbox_markdown",
    "render_sandbox_safe_or_quarantine",
    "save_report",
]
