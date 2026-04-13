"""LOB-based simulation orchestrator.

Replaces the monolithic oasis_engine.py with a modular pipeline that
wires together:
  - AShareClock: session scheduler (micro-steps, not rounds)
  - Exchange: LOB with A-share rules
  - BackgroundAgentPool: ZI, MM, MR for base liquidity
  - Cognition pipeline: intent → constraint → order per LLM agent
  - RoundAdapter: maps micro-steps to round records for reports

The orchestrator DOES NOT make LLM calls itself — it delegates to the
cognition pipeline. LLM calls are the caller's responsibility (or
can be mocked for testing).

Usage:
    result = run_simulation_lob(
        event=event,
        personas=personas,
        instrument_universe=instrument_universe,
        agent_decisions=decisions_callback,
    )
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from .clock import AShareClock, SessionEvent
from .round_adapter import RoundAdapter, RoundRecord
from ..market.ashare_rules import BoardType, SessionPhase, infer_board_type
from ..market.exchange import Exchange
from ..market.background_agents import BackgroundAgentPool
from ..market.trade_tape import Fill
from ..cognition.constraint_solver import ConstraintSolver, ValidatedIntent, RejectedIntent
from ..cognition.order_generator import OrderGenerator, OrderSpec
from ..cognition.intent_resolver import StructuredIntent, Urgency
from ..cognition.agent_memory import AgentMemory, FillRecord

log = logging.getLogger(__name__)


# ── Result types ──


@dataclass
class AgentState:
    """Snapshot of one agent's state for reporting."""

    agent_id: str
    persona_id: str
    cash: float
    holdings: dict[str, float]
    pnl: float
    n_trades: int


@dataclass
class SimulationResult:
    """Complete result of a LOB-based simulation.

    Compatible with the legacy OasisSimResult shape for report generation.
    """

    ticker: str
    initial_price: float
    final_price: float
    cumulative_delta_pct: float
    rounds: list[RoundRecord]
    agent_states: list[AgentState]
    tape_summary: dict
    book_snapshot: dict
    n_total_fills: int
    n_agents: int
    n_background_fills: int = 0


# ── Decision callback type ──

# The orchestrator calls this to get trading decisions from LLM agents.
# The caller provides the implementation (which may call the LLM, or
# return pre-computed decisions for testing).
#
# Signature: (round_idx, day_idx, agent_memories, market_snapshot) -> list[StructuredIntent]
DecisionCallback = Callable[
    [int, int, list[AgentMemory], dict],
    list[StructuredIntent],
]


def _noop_decisions(
    round_idx: int,
    day_idx: int,
    memories: list[AgentMemory],
    snapshot: dict,
) -> list[StructuredIntent]:
    """Default no-op decision callback: all agents hold."""
    return [
        StructuredIntent(
            persona_id=m.persona_id,
            agent_id=m.agent_id,
            side="hold",
            size_fraction=0.0,
            urgency=Urgency.NONE,
        )
        for m in memories
    ]


# ── Main orchestrator ──


def run_simulation_lob(
    *,
    ticker: str,
    prev_close: float,
    board_type: BoardType | str = BoardType.NORMAL,
    n_days: int = 5,
    ticks_per_session: int = 40,
    agent_configs: list[dict] | None = None,
    agent_decisions: DecisionCallback | None = None,
    background_seed: int = 42,
    n_zi: int = 5,
    n_mm: int = 1,
    n_mr: int = 2,
    event_callback: Callable | None = None,
) -> SimulationResult:
    """Run a LOB-based simulation.

    Args:
        ticker: instrument ticker (e.g., "002594.SZ")
        prev_close: previous day's closing price
        board_type: A-share board classification
        n_days: number of trading days to simulate
        ticks_per_session: micro-steps per trading day
        agent_configs: list of dicts with agent initialization data
            Each dict: {agent_id, persona_id, cash, holdings, agent_type}
        agent_decisions: callback that returns StructuredIntents per round
        background_seed: random seed for background agents
        n_zi, n_mm, n_mr: background agent counts
        event_callback: optional callback for live event streaming

    Returns:
        SimulationResult with round records, agent states, tape summary.
    """
    if isinstance(board_type, str):
        board_type = BoardType(board_type)

    decisions_fn = agent_decisions or _noop_decisions

    # ── Initialize exchange ──
    exchange = Exchange(ticker=ticker, prev_close=prev_close, board_type=board_type)

    # ── Initialize background agents ──
    pool = BackgroundAgentPool.default(
        exchange, n_zi=n_zi, n_mm=n_mm, n_mr=n_mr, seed=background_seed
    )

    # ── Initialize LLM agent memories ──
    memories: list[AgentMemory] = []
    if agent_configs:
        for cfg in agent_configs:
            mem = AgentMemory(
                agent_id=cfg["agent_id"],
                persona_id=cfg["persona_id"],
                initial_cash=cfg.get("cash", 100_000),
                current_cash=cfg.get("cash", 100_000),
            )
            # Initialize holdings
            for t, shares in cfg.get("holdings", {}).items():
                mem.holdings[t] = shares
                mem.avg_cost[t] = prev_close
            memories.append(mem)

    # ── Initialize components ──
    clock = AShareClock(n_days=n_days, ticks_per_session=ticks_per_session)
    adapter = RoundAdapter()
    solver = ConstraintSolver()
    order_gen = OrderGenerator()

    # ── Warm up the order book ──
    # Run background agents for a warm-up period so the LOB has depth
    # before any LLM agents try to trade.
    exchange.set_session(SessionPhase.CONTINUOUS_AM)
    for warmup_tick in range(50):
        exchange.set_tick(warmup_tick)
        pool.step(exchange)
    tape_before_sim = len(exchange.tape)

    # ── Track state ──
    current_day = -1
    decisions_this_day: dict[str, bool] = {}  # agent_id -> already decided today

    # How often LLM agents re-check within a continuous phase (every N ticks)
    DECISION_INTERVAL = 5

    # ── Main loop ──

    for event in clock:
        exchange.set_tick(event.tick + 50)  # offset past warm-up ticks

        # Day transition
        if event.day_idx != current_day:
            # Finalize previous round
            if current_day >= 0:
                record = adapter.finalize_round(exchange.last_price)
                if record and event_callback:
                    event_callback("round_complete", {
                        "round_idx": record.round_idx,
                        "price": exchange.last_price,
                        "delta_pct": record.delta_pct,
                    })

            current_day = event.day_idx
            decisions_this_day.clear()
            adapter.on_day_start(event.day_idx, exchange.last_price)
            exchange.set_round(event.day_idx)

            if event.day_idx > 0:
                exchange.advance_day()

            if event_callback:
                event_callback("day_start", {
                    "day_idx": event.day_idx,
                    "price": exchange.last_price,
                })

        # Session phase transitions
        if event.is_phase_start:
            exchange.set_session(event.phase)
            adapter.on_phase_start(event.phase.value, exchange.last_price)

        # ── Phase-specific processing ──

        if event.phase == SessionPhase.CALL_AUCTION:
            pool.step(exchange)

        elif event.phase in (SessionPhase.CONTINUOUS_AM, SessionPhase.CONTINUOUS_PM):
            # Background agents provide continuous liquidity every tick
            pool.step(exchange)

            # LLM agent decisions: fire at phase start, then periodically
            should_decide = (
                event.is_phase_start
                or (event.tick % DECISION_INTERVAL == 0)
            )
            if should_decide and memories:
                snapshot = {
                    "last_price": exchange.last_price,
                    "best_bid": exchange.best_bid,
                    "best_ask": exchange.best_ask,
                    "spread": exchange.spread,
                    "day_idx": event.day_idx,
                    "phase": event.phase.value,
                    "tape_summary": exchange.tape_summary(),
                }

                # Only get new decisions once per day per agent
                # (subsequent ticks just retry unfilled orders)
                undecided = [m for m in memories if m.agent_id not in decisions_this_day]
                if undecided and event.is_phase_start:
                    intents = decisions_fn(
                        adapter.current_round_idx,
                        event.day_idx,
                        undecided,
                        snapshot,
                    )
                    for intent in intents:
                        decisions_this_day[intent.agent_id] = True
                        if not intent.is_active:
                            continue
                        _process_intent(
                            intent, memories, exchange, solver, order_gen,
                            adapter, ticker, event, event_callback,
                        )

        elif event.phase == SessionPhase.CLOSING_CALL:
            if event.is_phase_start:
                fills = exchange.run_call_auction()
                if fills and event_callback:
                    event_callback("call_auction", {
                        "n_fills": len(fills),
                        "clearing_price": fills[-1].price,
                    })

        # Phase end
        if event.is_phase_start and adapter._current_phase is not None:
            adapter.on_phase_end(exchange.last_price)

        # Day end
        if event.is_day_end:
            adapter.on_phase_end(exchange.last_price)
            record = adapter.finalize_round(exchange.last_price)
            if record and event_callback:
                event_callback("round_complete", {
                    "round_idx": record.round_idx,
                    "price": exchange.last_price,
                    "delta_pct": record.delta_pct,
                })

    # ── Finalize any incomplete round ──
    if adapter._current is not None:
        adapter.on_phase_end(exchange.last_price)
        adapter.finalize_round(exchange.last_price)

    # ── Build result ──
    total_fills = len(exchange.tape)
    agent_agent_ids = {m.agent_id for m in memories}
    n_agent_fills = sum(
        1 for f in exchange.tape.all_fills[tape_before_sim:]
        if f.buyer_agent_id in agent_agent_ids or f.seller_agent_id in agent_agent_ids
    )

    agent_states = [
        AgentState(
            agent_id=m.agent_id,
            persona_id=m.persona_id,
            cash=m.current_cash,
            holdings=dict(m.holdings),
            pnl=m.total_pnl({ticker: exchange.last_price, "_default": exchange.last_price}),
            n_trades=m.n_trades,
        )
        for m in memories
    ]

    return SimulationResult(
        ticker=ticker,
        initial_price=prev_close,
        final_price=exchange.last_price,
        cumulative_delta_pct=(exchange.last_price / prev_close - 1.0) if prev_close > 0 else 0.0,
        rounds=adapter.rounds,
        agent_states=agent_states,
        tape_summary=exchange.tape_summary(),
        book_snapshot=exchange.book_snapshot(),
        n_total_fills=total_fills - tape_before_sim,
        n_agents=len(memories),
        n_background_fills=total_fills - tape_before_sim - n_agent_fills,
    )


def _process_intent(
    intent: StructuredIntent,
    memories: list[AgentMemory],
    exchange: Exchange,
    solver: ConstraintSolver,
    order_gen: OrderGenerator,
    adapter,
    ticker: str,
    event,
    event_callback,
) -> None:
    """Process a single agent intent through constraint → order → exchange."""
    mem = next((m for m in memories if m.agent_id == intent.agent_id), None)
    if mem is None:
        return

    holdings = mem.holdings.get(ticker, mem.holdings.get("_default", 0.0))
    t1_locked = exchange.t1.locked_shares(intent.agent_id, ticker, event.day_idx)

    validated = solver.validate(
        intent=intent,
        cash=mem.current_cash,
        holdings=holdings,
        t1_locked=t1_locked,
        last_price=exchange.last_price,
        upper_limit=exchange.rules.upper_limit,
        lower_limit=exchange.rules.lower_limit,
        capital=mem.initial_cash,
        holdings_value=holdings * exchange.last_price,
    )

    if isinstance(validated, RejectedIntent):
        log.debug("Intent rejected for %s: %s", intent.persona_id, validated.reason)
        return

    specs = order_gen.generate(
        validated,
        last_price=exchange.last_price,
        best_bid=exchange.best_bid,
        best_ask=exchange.best_ask,
    )

    for spec in specs:
        result = exchange.submit_order(
            agent_id=spec.agent_id,
            persona_id=spec.persona_id,
            side=spec.side,
            order_type=spec.order_type,
            price=spec.price,
            quantity=spec.quantity,
            agent_holdings=holdings,
            agent_cash=mem.current_cash,
            is_short=spec.is_short,
        )

        for fill in result.fills:
            is_buyer = fill.buyer_agent_id == spec.agent_id
            fill_side = "buy" if is_buyer else "sell"
            mem.record_fill(FillRecord(
                round_idx=event.day_idx,
                tick=event.tick,
                side=fill_side,
                price=fill.price,
                quantity=fill.quantity,
                value=fill.value,
                fill_id=fill.fill_id,
            ))
            adapter.on_fill(value=fill.value, quantity=fill.quantity, is_buy=is_buyer)

            if event_callback:
                event_callback("fill", {
                    "fill_id": fill.fill_id,
                    "agent": spec.persona_id,
                    "side": fill_side,
                    "price": fill.price,
                    "quantity": fill.quantity,
                })


__all__ = ["DecisionCallback", "SimulationResult", "run_simulation_lob"]
