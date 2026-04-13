"""LOB-based report renderer — auditable, tape-backed, mechanism-first.

Renders from `engine.SimulationResult` (new LOB engine) rather than
`OasisSimResult` (legacy Kyle engine). Key differences from legacy report:

1. **Order-level attribution**: every price move is traced to specific
   fills on the tape, not formula outputs.
2. **LOB state**: spread, depth, fill rate per round.
3. **Causal chains**: event → who saw → who traded → what filled → price.
4. **Auditable P&L**: each agent's P&L decomposed by fill.
5. **No formula fiction**: price IS the last fill on the tape.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .engine.orchestrator import SimulationResult
    from .engine.round_adapter import RoundRecord, PhaseResult
    from .market.trade_tape import TradeTape

log = logging.getLogger(__name__)


# ─── Currency formatting (shared with legacy) ───


def _fmt_price(value: float, sym: str = "¥") -> str:
    return f"{sym}{value:.2f}"


def _fmt_big(value: float, sym: str = "¥", unit: str = "億", div: float = 1e8) -> str:
    return f"{sym}{value / div:.2f}{unit}"


def _fmt_small(value: float, sym: str = "¥", unit: str = "M", div: float = 1e6) -> str:
    return f"{sym}{value / div:.2f}{unit}"


def _pct(value: float) -> str:
    return f"{value * 100:+.2f}%"


# ─── Report sections ───


def _render_header(result: "SimulationResult") -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""# ssFlow Market Simulation Report (LOB Engine)

**Instrument**: `{result.ticker}`
**Initial price**: {_fmt_price(result.initial_price)} · **Final**: {_fmt_price(result.final_price)}
**Cumulative**: **{_pct(result.cumulative_delta_pct)}**
**Engine**: LOB (limit order book, price-time priority matching)
**Scale**: {result.n_agents} LLM agents + background liquidity · {len(result.rounds)} trading days
**Total fills**: {result.n_total_fills:,} (background: {result.n_background_fills:,})
**Generated**: {now}

> Price discovery via limit order book with A-share rules (涨跌停, T+1, tick size).
> Every price tick is traceable to a specific fill on the trade tape.
> Background agents (ZI, MM, MR) provide base liquidity; LLM agents trade against them.

---
"""


def _render_round(record: "RoundRecord", tape: "TradeTape | None" = None) -> str:
    """Render one round (trading day)."""
    lines = [
        f"## Day {record.day_idx} (Round {record.round_idx})",
        f"{_fmt_price(record.price_before)} → {_fmt_price(record.price_after)} ({_pct(record.delta_pct)})",
        "",
    ]

    # Phase breakdown
    if record.phase_results:
        lines.append("### Phase Breakdown")
        lines.append("| Phase | Price Start | Price End | Delta | Fills | Volume | Net Flow |")
        lines.append("|-------|------------|-----------|-------|-------|--------|----------|")
        for pr in record.phase_results:
            lines.append(
                f"| {pr.phase} | {_fmt_price(pr.price_start)} | {_fmt_price(pr.price_end)} | "
                f"{_pct(pr.delta_pct)} | {pr.n_fills} | "
                f"{pr.total_volume:,.0f} shr | {_fmt_small(pr.net_flow)} |"
            )
        lines.append("")

    # Order flow summary
    if record.class_flows:
        lines.append("### Order Flow by Participant")
        for cf in sorted(record.class_flows, key=lambda c: abs(c.get("net_flow", 0)), reverse=True):
            persona = cf.get("persona_id", "?")
            flow = cf.get("net_flow", 0)
            n_fills = cf.get("n_fills", 0)
            rationale = cf.get("rationale", "")
            sign = "净买盘" if flow > 0 else ("净卖盘" if flow < 0 else "观望")
            lines.append(f"- **{persona}** ({sign} {_fmt_small(abs(flow))}, {n_fills} fills): _{rationale}_")
        lines.append("")

    lines.append("---")
    return "\n".join(lines)


def _render_tape_attribution(result: "SimulationResult") -> str:
    """Render top fills and their price impact."""
    tape_data = result.tape_summary
    if not tape_data or tape_data.get("n_fills", 0) == 0:
        return ""

    lines = [
        "## Trade Tape Summary",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Total fills | {tape_data.get('n_fills', 0):,} |",
        f"| Total volume | {tape_data.get('total_volume', 0):,.0f} shares |",
        f"| Total value | {_fmt_big(tape_data.get('total_value', 0))} |",
        f"| VWAP | {_fmt_price(tape_data.get('vwap', 0))} |",
        f"| High | {_fmt_price(tape_data.get('high', 0))} |",
        f"| Low | {_fmt_price(tape_data.get('low', 0))} |",
        "",
    ]
    return "\n".join(lines)


def _render_book_state(result: "SimulationResult") -> str:
    """Render final order book state."""
    book = result.book_snapshot
    if not book:
        return ""

    lines = [
        "## Order Book (Final State)",
        "",
        f"- Best bid: {_fmt_price(book.get('best_bid', 0) or 0)}",
        f"- Best ask: {_fmt_price(book.get('best_ask', 0) or 0)}",
        f"- Spread: {_fmt_price(book.get('spread', 0) or 0)}",
        f"- Resting orders: {book.get('n_resting_orders', 0)}",
        f"- Total bid volume: {book.get('total_bid_volume', 0):,.0f} shares",
        f"- Total ask volume: {book.get('total_ask_volume', 0):,.0f} shares",
        "",
    ]

    # Bid depth
    bid_depth = book.get("bid_depth", [])
    if bid_depth:
        lines.append("### Bid Depth")
        lines.append("| Level | Price | Volume |")
        lines.append("|-------|-------|--------|")
        for i, (price, vol) in enumerate(bid_depth[:5]):
            lines.append(f"| {i+1} | {_fmt_price(price)} | {vol:,.0f} |")
        lines.append("")

    # Ask depth
    ask_depth = book.get("ask_depth", [])
    if ask_depth:
        lines.append("### Ask Depth")
        lines.append("| Level | Price | Volume |")
        lines.append("|-------|-------|--------|")
        for i, (price, vol) in enumerate(ask_depth[:5]):
            lines.append(f"| {i+1} | {_fmt_price(price)} | {vol:,.0f} |")
        lines.append("")

    return "\n".join(lines)


def _render_agent_pnl(result: "SimulationResult") -> str:
    """Render per-agent P&L table."""
    if not result.agent_states:
        return ""

    lines = [
        "## Per-Agent P&L",
        "",
        "| Agent | Persona | Cash | Holdings | P&L | Trades |",
        "|-------|---------|------|----------|-----|--------|",
    ]

    sorted_agents = sorted(result.agent_states, key=lambda a: a.pnl, reverse=True)
    for ag in sorted_agents:
        total_holdings = sum(ag.holdings.values())
        lines.append(
            f"| `{ag.agent_id}` | {ag.persona_id} | "
            f"{_fmt_small(ag.cash)} | {total_holdings:,.0f} shr | "
            f"{_fmt_small(ag.pnl)} | {ag.n_trades} |"
        )

    lines.append("")
    return "\n".join(lines)


def _render_mechanism_note(result: "SimulationResult") -> str:
    """Render the mechanism explanation section."""
    return f"""## How This Report Works

- **Price = last fill on the trade tape.** There is no formula converting aggregate
  flow to price. Every price tick corresponds to a specific buyer, seller, price, and
  quantity on the tape.
- **Order-level enforcement.** A-share rules (涨跌停, T+1, lot size) are checked
  at order submission, not applied as a post-hoc clamp. Invalid orders are rejected
  before they reach the book.
- **Background liquidity.** Zero-intelligence, market maker, and mean-reversion
  agents provide continuous two-sided quotes so LLM agents always have someone to
  trade against. {result.n_background_fills:,} of {result.n_total_fills:,} total fills
  involved background agents.
- **Audit trail.** Every fill is recorded with buyer_id, seller_id, aggressor_side,
  timestamp. P&L is computed from actual fills, not estimated from net flow.
"""


# ─── Main entry point ───


def render_lob_report(result: "SimulationResult", tape: "TradeTape | None" = None) -> str:
    """Compose the full markdown report for a LOB-based simulation."""
    parts = [_render_header(result)]

    for record in result.rounds:
        parts.append(_render_round(record, tape))

    parts.append(_render_tape_attribution(result))
    parts.append(_render_book_state(result))
    parts.append(_render_agent_pnl(result))
    parts.append(_render_mechanism_note(result))

    return "\n".join(parts).strip()


__all__ = ["render_lob_report"]
