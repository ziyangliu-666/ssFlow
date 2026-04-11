"""ssFlow OASIS engine — the main `run_simulation` entry point.

Wires together:

  - `oasis_persona_adapter.build_agent_graph` (Persona YAML → OASIS AgentGraph
     with the `submit_order_distribution` tool injected into each trader)
  - `oasis_lm.SsFishCamelModel` (CAMEL LM backend with cost tracking + sanitization)
  - OASIS `OasisEnv` + `Channel` (the social simulation runtime)
  - `oasis_trading_tool.OrderCollector` (per-sim thread-safe order collector)
  - `trading_layer.apply_distribution_to_agent_pop` (pure-math agent-pop update)
  - `market_dynamics.compute_price_impact` (Kyle square-root price update)
  - `external_events.ExternalEventSchedule` (mid-sim event injection)

Round loop (per round):
  1. Inject any external events scheduled for this round via ManualAction
  2. Run OASIS env.step() with LLMAction for all real personas.
     Trader personas see both the 21 native OASIS social actions AND our
     custom `submit_order_distribution` tool — CAMEL's ChatAgent picks
     tool calls in a SINGLE unified decision (social + trading share memory).
     max_iteration=2 for traders gives them a follow-up turn to reach the
     trading tool reliably.
  3. Drain the OrderCollector: all trading intents submitted during step 2.
  4. For each trader, apply its distribution via
     `apply_distribution_to_agent_pop` (samples agents, applies, returns flow).
     Traders that didn't call the tool fall through to a synthetic hold.
  5. Sum net flows → Kyle → new price.
  6. Post the new price as ManualAction(CREATE_POST) by the __market__ agent.
  7. Record a RoundRecord and continue.

The simulation is async throughout because OASIS is async-native.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import oasis
from oasis.environment.env_action import LLMAction, ManualAction
from oasis.social_platform.channel import Channel
from oasis.social_platform.typing import ActionType, DefaultPlatformType

from .config import settings
from .event import Event
from .event_severity import resolve_event_severity
from .event_bus import (
    EVENT_AGENT_ACTION,
    EVENT_CLASS_FLOW_COMPUTED,
    EVENT_ERROR,
    EVENT_EXTERNAL_EVENT_INJECTED,
    EVENT_FORCE_ACTION_OVERRIDE,
    EVENT_PERSONA_STATE_UPDATED,
    EVENT_PERSONA_THOUGHT,
    EVENT_POLICY_CREATED,
    EVENT_POLICY_FIRED,
    EVENT_PRICE_UPDATED,
    EVENT_ROUND_COMPLETE,
    EVENT_ROUND_START,
    EVENT_SIMULATION_COMPLETE,
    EVENT_SIMULATION_START,
    EVENT_TRADE_SUBMITTED,
    EventSink,
    safe_emit,
)
from .information import Publication
from .information.external_events import (
    DynamicEventStream,
    ExternalEventSchedule,
    SimSnapshot,
)
from .llm_client import BudgetExceeded, cost_tracker
from .market_dynamics import (
    FLOW_KNEE,
    AdaptiveADV,
    compute_price_impact,
    lambda_for_market,
)
from .publication_effects import (
    AggregateEffect,
    apply_effects_to_participation,
    apply_effects_to_risk_budget,
)
from .oasis_feed_reader import (
    PublicationMetadata,
    PublicationRegistry,
)
from .oasis_lm import build_default_lm
from .oasis_persona_adapter import (
    MARKET_AGENT_ID_NAME,
    build_agent_graph,
    clear_round_context,
    set_round_context,
    update_conviction_context,
)
from .oasis_trading_tool import OrderCollector, PendingOrder
from .persona import Persona
from .self_model import (
    build_evaluators_for_personas,
)
from .self_model.schema import RoundCtx as SelfModelRoundCtx, TradeResult as SelfModelTradeResult
from .trading_layer import (
    Agent,
    ClassFlowResult,
    apply_distribution_to_agent_pop,
    spawn_agents,
)


log = logging.getLogger(__name__)


# ─────────────────────── Result dataclasses ───────────────────────


@dataclass
class RoundRecord:
    """Snapshot of one simulation round."""

    round_idx: int
    price_before: float
    price_after: float
    delta_pct: float
    net_flow: float
    class_flows: list[ClassFlowResult]
    publications_this_round: list[Publication]
    external_events_injected: int = 0
    limit_board_state: str = "normal"      # BoardState value
    limit_board_unfilled: float = 0.0      # Unfilled queue volume
    limit_board_seal: float = 0.0          # Seal strength (unfilled/ADV)
    avg_fill_rate: float = 1.0             # Average fill rate across all personas
    total_unfilled_volume: float = 0.0     # Total unfilled volume this round
    total_t1_blocked: int = 0              # Total T+1 blocked sell attempts


@dataclass
class OasisSimResult:
    """Top-level result of a Phase I OASIS-based simulation."""

    simulation_id: str
    event: Event
    personas: list[Persona]
    initial_price: float
    final_price: float
    rounds: list[RoundRecord]
    elapsed_seconds: float
    cost_usd_at_start: float
    cost_usd_at_end: float
    llm_seed: int | None
    lambda_used: float
    adv_value_used: float
    oasis_db_path: str
    final_agents_by_class: dict[str, list[Agent]] = field(default_factory=dict)
    # Multi-instrument: per-ticker price trajectories. Empty when single-instrument.
    price_trajectories: dict[str, list[float]] = field(default_factory=dict)

    @property
    def n_personas(self) -> int:
        return len(self.personas)

    @property
    def n_traders(self) -> int:
        return sum(1 for p in self.personas if p.sandbox is not None)

    @property
    def n_rounds(self) -> int:
        return len(self.rounds)

    @property
    def cost_usd(self) -> float:
        return max(0.0, self.cost_usd_at_end - self.cost_usd_at_start)

    @property
    def price_trajectory(self) -> list[float]:
        if not self.rounds:
            return [self.initial_price]
        return [self.initial_price] + [r.price_after for r in self.rounds]

    @property
    def cumulative_delta_pct(self) -> float:
        if not self.initial_price:
            return 0.0
        return self.final_price / self.initial_price - 1.0

    @property
    def all_publications(self) -> list[Publication]:
        out: list[Publication] = []
        for r in self.rounds:
            out.extend(r.publications_this_round)
        return out

    def compute_class_pnl(self) -> dict[str, float]:
        return {
            class_id: sum(a.pnl(self.final_price) for a in agents)
            for class_id, agents in self.final_agents_by_class.items()
        }


# ─────────────────────── Helpers ───────────────────────


def _participation_rate(
    persona: Persona,
    round_idx: int,
    cumulative_delta_pct: float,
    active_types: set[str] | None,
) -> float:
    """Fraction of agents that actually trade this round.

    Three factors combine:
      1. Activity schedule — non-active types get reduced (background noise)
      2. Urgency decay — later rounds have naturally declining participation
      3. Momentum exhaustion — large cumulative moves thin marginal flow
    """
    base = 1.0
    ptype = persona.agent_type or "retail"
    if active_types is not None and ptype not in active_types:
        base = 0.15
    urgency = 1.0 / (1.0 + 0.3 * round_idx)
    exhaustion = 1.0 / (1.0 + 2.0 * abs(cumulative_delta_pct))
    return max(0.05, base * urgency * exhaustion)


def _currency_symbol(currency: str) -> str:
    return {
        "CNY": "¥", "USD": "$", "EUR": "€", "JPY": "¥", "HKD": "HK$", "BTC": "₿"
    }.get(currency, "$")


def _build_initial_event_post(
    event: Event,
    *,
    initial_price: float,
    currency: str,
) -> str:
    """Render the seed event as the first post the market broadcaster makes
    at round 0. Lands in everyone's feed before round 1's social step.
    """
    sym = _currency_symbol(currency)
    parts = [
        f"[Market Event] {event.instrument or event.ticker} · "
        f"{event.event_type} · {event.event_date}",
        event.event_text.strip(),
    ]
    if event.prior_consensus.strip():
        parts.append(f"Prior consensus: {event.prior_consensus.strip()[:200]}")
    if event.recent_price_action.strip():
        parts.append(f"Recent price action: {event.recent_price_action.strip()[:200]}")
    parts.append(f"Current price: {sym}{initial_price:.2f}")
    return "\n".join(parts)


def _build_price_update_post(
    event: Event,
    round_idx: int,
    price_before: float,
    price_after: float,
    net_flow: float,
    round_label: str = "",
    initial_price: float = 0.0,
    *,
    currency: str = "CNY",
) -> str:
    """The synthetic market broadcaster's price-update post."""
    sym = _currency_symbol(currency)
    delta_pct = (price_after / price_before - 1.0) * 100 if price_before else 0.0
    cumulative = (
        (price_after / initial_price - 1.0) * 100
        if initial_price > 0 else 0.0
    )
    label = f" ({round_label})" if round_label else ""
    return (
        f"[Market Event] R{round_idx}{label} price update: "
        f"{sym}{price_before:.2f} → {sym}{price_after:.2f} "
        f"({delta_pct:+.2f}%, cumulative {cumulative:+.1f}%). "
        f"Net order flow: {sym}{net_flow:+,.0f}"
    )


def _build_publication_effect_context(agg: "AggregateEffect") -> str:
    """Render aggregate publication effects as a prompt context block.

    Produces bilingual (ZH/EN) text describing the current market-wide
    effects from recent publications so agents can factor them into
    their trading decisions.
    """
    parts = ["\n# 市场效应 / Market Effects:"]

    # Sentiment direction
    if agg.sentiment_shift > 0.10:
        parts.append(
            f"利好效应：市场情绪偏多 (sentiment shift: {agg.sentiment_shift:+.2f}). "
            "Positive publications are boosting confidence."
        )
    elif agg.sentiment_shift < -0.10:
        parts.append(
            f"利空效应：市场情绪偏空 (sentiment shift: {agg.sentiment_shift:+.2f}). "
            "Negative publications are weighing on sentiment."
        )

    # Participation
    if agg.participation_modifier > 1.05:
        parts.append(
            f"交投活跃度提升 ({agg.participation_modifier:.0%} of normal). "
            "More market participants are active."
        )
    elif agg.participation_modifier < 0.95:
        parts.append(
            f"交投活跃度降低 ({agg.participation_modifier:.0%} of normal). "
            "Fewer market participants are willing to trade."
        )

    # Urgency
    if agg.urgency_modifier > 1.10:
        parts.append(
            f"交易紧迫性上升 (urgency: {agg.urgency_modifier:.2f}x). "
            "Participants are trading more aggressively."
        )
    elif agg.urgency_modifier < 0.90:
        parts.append(
            f"交易紧迫性下降 (urgency: {agg.urgency_modifier:.2f}x). "
            "Participants are trading more cautiously."
        )

    # Risk budget
    if agg.risk_budget_shift > 0.05:
        parts.append(
            f"风险偏好提升 (risk budget shift: {agg.risk_budget_shift:+.2f}). "
            "Investors are more willing to take on risk."
        )
    elif agg.risk_budget_shift < -0.05:
        parts.append(
            f"风险偏好收缩 (risk budget shift: {agg.risk_budget_shift:+.2f}). "
            "Investors are reducing risk exposure."
        )

    # Source-specific color
    sources = agg.affected_personas
    if sources:
        parts.append(
            f"受影响的参与者类型: {', '.join(sorted(sources))}"
        )

    if len(parts) == 1:
        return ""  # No meaningful effects to report
    return "\n".join(parts)


def _max_post_id(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT COALESCE(MAX(post_id), 0) FROM post").fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def _query_round_publications(
    db_path: str,
    registry: PublicationRegistry,
    persona_id_to_oasis_id: dict[str, int],
    since_post_id: int,
) -> list[Publication]:
    """After an env.step(), pull all new posts created since `since_post_id`."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT p.post_id, p.user_id, p.original_post_id, p.content,
                   p.num_likes, p.num_shares, u.agent_id
            FROM post p JOIN user u ON p.user_id = u.user_id
            WHERE p.post_id > ?
            ORDER BY p.post_id ASC
            """,
            (since_post_id,),
        ).fetchall()
    finally:
        conn.close()

    oasis_to_persona = {v: k for k, v in persona_id_to_oasis_id.items()}
    out: list[Publication] = []
    for row in rows:
        post_id, _user_id, original_post_id, content, num_likes, num_shares, agent_id = row
        persona_id = oasis_to_persona.get(int(agent_id), f"agent_{agent_id}")
        meta = registry.get(int(post_id))
        if meta is not None:
            content_type = meta.content_type
            archetype = meta.author_archetype
            authority = meta.authority_weight
            round_idx = meta.round_idx
            references = list(meta.references)
        else:
            content_type = "social_post"
            archetype = persona_id
            authority = 0.3
            round_idx = 0
            references = []
        if original_post_id is not None:
            references.append(f"post:{int(original_post_id)}")
        out.append(
            Publication(
                publication_id=f"post:{int(post_id)}",
                author_persona_id=persona_id,
                author_archetype=archetype,
                content_type=content_type,
                text=str(content or ""),
                round_idx=round_idx,
                authority_weight=authority,
                references=references,
                oasis_post_id=int(post_id),
                likes=int(num_likes or 0),
                reposts=int(num_shares or 0),
            )
        )
    return out


# ─────────────────────── OASIS bug workarounds ───────────────────────

_oasis_patched = False


def _patch_oasis_recsys() -> None:
    """Monkey-patch OASIS recsys to fix IndexError / ValueError crashes.

    OASIS's rec_sys_personalized_with_trace has two unguarded crash sites:
      1. swap_random_posts (line 646): random.sample crashes on empty list
      2. post_ids.index(_post_id) (line 754): ValueError when post filtered out

    These manifest as "social.twitter ERROR list index out of range" when
    30 agents run concurrently via asyncio.gather.
    """
    global _oasis_patched
    if _oasis_patched:
        return
    _oasis_patched = True

    try:
        import oasis.social_platform.recsys as recsys_mod
        import random as _random

        _orig_swap = recsys_mod.swap_random_posts

        def _safe_swap(rec_post_ids, post_ids, swap_percent=0.1):
            if not post_ids:
                return rec_post_ids
            num_to_swap = int(len(rec_post_ids) * swap_percent)
            num_to_swap = min(num_to_swap, len(post_ids), len(rec_post_ids))
            if num_to_swap <= 0:
                return rec_post_ids
            return _orig_swap(rec_post_ids, post_ids, swap_percent)

        recsys_mod.swap_random_posts = _safe_swap
        log.info("Patched OASIS swap_random_posts for empty-list safety")
    except Exception as exc:
        log.warning("Failed to patch OASIS recsys: %s", exc)


# ─────────────────────── Main run_simulation ───────────────────────


async def run_simulation(
    event: Event,
    personas: list[Persona],
    n_rounds: int | None = None,
    *,
    simulation_id: str | None = None,
    lambda_market: float | None = None,
    seed: int | None = None,
    external_events: ExternalEventSchedule | DynamicEventStream | None = None,
    db_path: str | None = None,
    event_sink: EventSink | None = None,
    entity_graph: "EntityGraph | None" = None,
    instrument_universe: "InstrumentUniverse | None" = None,
    round_schedule: "RoundSchedule | None" = None,
    sim_graph: "SimGraph | None" = None,
    self_model_enabled: bool = True,
) -> OasisSimResult:
    """Run a Phase I OASIS-based market simulation.

    The trading layer sits on top of OASIS: each round, after OASIS advances
    the social state, our trading layer reads each trader's feed and runs a
    structured order-decision LLM call. Net flows feed Kyle, prices feed back
    into OASIS as `__market__` posts.

    Args:
        event: event metadata (ticker, type, date, text, context strings)
        personas: schema v3 list (mix of trader + non-trader entities)
        instrument_universe: REQUIRED. The full set of tradeable instruments.
            Price, ADV, and currency are read from the `event_subject`
            instrument inside this universe; the engine never reads them
            from the Event itself. For single-instrument runs, build a
            one-element universe.
        n_rounds: defaults to settings.n_rounds
        simulation_id: stable id for scorecard tracking
        lambda_market: market impact coefficient; defaults to lookup by event.market
        seed: deterministic seed for spawn + sampling RNGs
        external_events: optional schedule of mid-sim policy/news events
        db_path: optional path for the OASIS SQLite db. Defaults to a temp file
            under reports/oasis_dbs/.
        event_sink: optional `EventSink` that receives progress events
        entity_graph: optional EntityGraph for the Entity State Sandbox.
            When provided, each round executes resource flows, evaluates
            thresholds, and injects entity state ("处境") into agent prompts.
        round_schedule: optional RoundSchedule for time-aware rounds.
            When provided, each round's time context is injected into
            agent prompts.
            (round_start, persona_thought, trade_submitted, price_updated, …)
            as the simulation runs. See `ssflow.event_bus` for the protocol
            and the available event types. Sink errors are swallowed.

    Returns:
        OasisSimResult with the full price trajectory + publication log.

    Raises:
        ValueError: if instrument_universe is missing/empty or personas list is empty
        BudgetExceeded: if cost guard trips mid-run
    """
    _patch_oasis_recsys()

    if not personas:
        raise ValueError("run_simulation requires at least one persona")
    if instrument_universe is None or not instrument_universe.instruments:
        raise ValueError(
            "run_simulation requires a non-empty instrument_universe — "
            "build one from your event's primary instrument before calling."
        )

    # Resolve the event-subject scalars once. Every downstream reference to
    # "the price" / "the ADV" / "the currency" reads from this single point.
    _subject_ticker = instrument_universe.event_subject_ticker
    _subject = instrument_universe.get(_subject_ticker)
    if _subject is None or _subject.current_price <= 0:
        raise ValueError(
            f"instrument_universe event_subject {_subject_ticker!r} has no "
            f"valid price — cannot run simulation."
        )
    initial_price = float(_subject.current_price)
    initial_adv = float(_subject.adv_value)
    event_currency = _subject.price_currency

    n_rounds = n_rounds or settings.n_rounds
    simulation_id = simulation_id or f"oasis_{uuid.uuid4().hex[:12]}"
    seed = seed if seed is not None else settings.seed
    external_events = external_events or ExternalEventSchedule()
    # Determine lambda: explicit override > calibration library > literature default
    _calibrated_knee: float = FLOW_KNEE  # default; overridden by calibration
    if lambda_market is not None:
        lambda_used = lambda_market
        _lambda_source = "explicit_override"
    else:
        from .calibration_library import select_impact_params
        _cal_params = select_impact_params(
            event_type=event.event_type or "",
            board=getattr(event, "board", "normal") or "normal",
            float_cap_cny=getattr(event, "float_market_cap", None),
            adv_value=initial_adv if initial_adv > 0 else None,
        )
        lambda_used = _cal_params["lambda"]
        _lambda_source = _cal_params["source"]
        _calibrated_knee = _cal_params["knee"]
        log.info(
            "Lambda selected: %.4f (source=%s, knee=%.4f) for event_type=%s",
            lambda_used, _lambda_source, _calibrated_knee,
            event.event_type,
        )

    # ── Build SimGraph (unified world model) ──
    # If not provided, build from legacy inputs via the adapter.
    if sim_graph is None:
        from .sim_graph_builder import build_sim_graph
        sim_graph = build_sim_graph(personas, entity_graph, event)

    # Set up the OASIS db path
    if db_path is None:
        db_dir = Path(settings.project_root) / "reports" / "oasis_dbs"
        db_dir.mkdir(parents=True, exist_ok=True)
        db_path_obj = db_dir / f"{simulation_id}.db"
    else:
        db_path_obj = Path(db_path)
    if db_path_obj.exists():
        db_path_obj.unlink()

    log.info(
        "OASIS sim %s starting: ticker=%s personas=%d (%d traders) rounds=%d "
        "initial_price=%.2f adv=%.0f lambda=%.3f seed=%s db=%s",
        simulation_id, event.ticker, len(personas),
        sum(1 for p in personas if p.sandbox is not None),
        n_rounds, initial_price, initial_adv,
        lambda_used, seed, db_path_obj,
    )

    cost_at_start = cost_tracker.total_cost_usd
    t0 = time.time()

    # Per-ticker opening prices — critical for the multi-instrument
    # frontend to compute honest % moves against the actual open
    # instead of against the post-round-0 price (which is what
    # price_updated.prices dicts start with). Every ticker in the
    # universe is emitted here.
    opening_prices = {
        inst.ticker: float(inst.current_price)
        for inst in instrument_universe.instruments
        if inst.current_price > 0
    }
    opening_tickers = [
        {"ticker": inst.ticker, "name": inst.name}
        for inst in instrument_universe.instruments
    ]
    safe_emit(
        event_sink,
        EVENT_SIMULATION_START,
        simulation_id=simulation_id,
        ticker=event.ticker,
        instrument=event.instrument,
        market=event.market,
        market_event_type=event.event_type,
        event_date=event.event_date,
        initial_price=initial_price,
        price_currency=event_currency,
        adv_value=initial_adv,
        # Multi-instrument authoritative opening state. Frontend prefers
        # this dict over `initial_price` so all tickers get correct base.
        opening_prices=opening_prices,
        tickers=opening_tickers,
        event_subject_ticker=_subject_ticker,
        lambda_used=lambda_used,
        n_personas=len(personas),
        n_traders=sum(1 for p in personas if p.sandbox is not None),
        n_rounds=n_rounds,
        seed=seed,
    )

    # ── Build the agent graph (with trading + domain tools wired) ──
    channel = Channel()
    lm = build_default_lm()
    order_collector = OrderCollector()
    from .action_collector import ActionCollector
    action_collector = ActionCollector()
    # Freeform trading is always on when SimGraph is present
    use_freeform = (entity_graph is not None) or (instrument_universe is not None) or (sim_graph is not None)
    agent_graph, persona_id_to_oasis_id = build_agent_graph(
        personas, channel, model=lm, order_collector=order_collector,
        use_freeform_trading=use_freeform,
        instrument_universe=instrument_universe,
        event=event,
        action_collector=action_collector,
    )

    # ── Build the OASIS env ──
    env = oasis.make(
        agent_graph=agent_graph,
        platform=DefaultPlatformType.TWITTER,
        database_path=str(db_path_obj),
    )
    await env.reset()

    # ── Initialize trading state for trader personas ──
    spawn_rng = random.Random(seed if seed is not None else 0)
    sample_rng = random.Random((seed if seed is not None else 0) + 1000)
    agent_pops: dict[str, list[Agent]] = {}
    multi_prices_for_spawn = instrument_universe.prices()
    # Build {ticker: {persona_id: pct}} from instrument holder data
    holdings_by_persona_map: dict[str, dict[str, float]] = {}
    for inst in instrument_universe.instruments:
        if inst.holdings_by_persona:
            holdings_by_persona_map[inst.ticker] = inst.holdings_by_persona
    # All bookkeeping is keyed by real instrument tickers from the universe.
    _primary_ticker = "_default"
    for persona in personas:
        if persona.sandbox is not None:
            agent_pops[persona.id] = spawn_agents(
                persona, current_price=initial_price, rng=spawn_rng,
                primary_ticker=_primary_ticker,
                multi_prices=multi_prices_for_spawn,
                holdings_by_persona=holdings_by_persona_map or None,
            )

    # ── Self-model evaluators ──
    # One per trader persona. Holds the per-persona structured state,
    # utility weights, and render sections. Personas without an explicit
    # `self_model:` block in their YAML (= all 39 hand-authored ashare
    # personas) get the DEFAULT_SELF_MODEL bundle applied automatically.
    # Evaluator updates happen after sync_population_state each round
    # (so nav / pnl figures reflect the authoritative post-trade state),
    # and rendered output is appended to conviction_ctx at the START of
    # the NEXT round so the agent sees its end-of-previous-round self.
    #
    # When ``self_model_enabled=False`` (A/B ablation) we skip the
    # factory entirely and leave ``self_model_evaluators`` empty —
    # every use site checks ``if sm_ev is None: continue`` so an empty
    # dict fully disables rendering, update, and event emission.
    if self_model_enabled:
        self_model_evaluators = build_evaluators_for_personas(personas, event)
    else:
        self_model_evaluators = {}
        log.info("self_model_enabled=False — skipping evaluator setup")
    # Track the last round's price delta so atoms like `regret` can
    # reason about missed moves.
    _sm_prev_round_delta_pct: float = 0.0

    publication_registry = PublicationRegistry()

    # ── Round 0 prelude: post the seed event from the market broadcaster ──
    market_agent = agent_graph.get_agent(persona_id_to_oasis_id[MARKET_AGENT_ID_NAME])
    initial_post_text = _build_initial_event_post(
        event, initial_price=initial_price, currency=event_currency,
    )
    last_post_id = _max_post_id(str(db_path_obj))
    await env.step({
        market_agent: ManualAction(
            action_type=ActionType.CREATE_POST,
            action_args={"content": initial_post_text},
        ),
    })
    new_seed_post_id = _max_post_id(str(db_path_obj))
    if new_seed_post_id > last_post_id:
        publication_registry.register(
            new_seed_post_id,
            PublicationMetadata(
                content_type="company_announcement",
                author_persona_id=MARKET_AGENT_ID_NAME,
                author_archetype="Market Event Wire",
                authority_weight=1.0,
                round_idx=-1,
            ),
        )

    # Note: a "R0 catalyst amplifier post" idea was tried in iter5 and
    # reverted. Adding a second market-wire post at R0 with "国家队
    # 净流入 ¥58亿" framing helped CATL marginally (-6.57 → -0.14) but
    # caused 东财 to flip negative (+12.6 → -10.78) in the same run —
    # probably because the extra post shifted recency anchors in ways
    # the LLM sampled differently per event. Across iter3 / iter3_repeat
    # / iter5, the 5-event direction accuracy oscillates between 2/5 and
    # 4/5 purely on LLM sampling; the engine-level config can't
    # eliminate that variance without per-event overfitting. Reverting
    # to iter3's stable config (scale 0.3, no catalyst amplifier) as
    # the final tuned state.

    # ── Cross-market context: extract and inject as round 0 events ──
    from .information.cross_market import (
        cross_market_from_explicit,
        cross_market_to_external_events,
        extract_cross_market_from_event,
        should_persona_see_cross_market,
    )
    if event.cross_market_context:
        _cross_market = cross_market_from_explicit(event.cross_market_context)
    else:
        _cross_market = extract_cross_market_from_event(
            event.event_text, event.sector_context,
        )
    if _cross_market.data_points:
        cross_events = cross_market_to_external_events(_cross_market)
        for cev in cross_events:
            external_events.add(cev)
        log.info(
            "Cross-market: %d data points injected (%s)",
            len(_cross_market.data_points),
            ", ".join(dp.ticker for dp in _cross_market.data_points),
        )
        # Inject cross-market summary into institutional/strategic/analyst profiles
        summary = _cross_market.summary_text_zh
        if summary:
            for persona in personas:
                ptype = persona.agent_type or "retail"
                if not should_persona_see_cross_market(ptype):
                    continue
                if persona.id not in persona_id_to_oasis_id:
                    continue
                oasis_id = persona_id_to_oasis_id[persona.id]
                oasis_agent = agent_graph.get_agent(oasis_id)
                profile = oasis_agent.user_info.profile
                other = profile.get("other_info", {})
                up = other.get("user_profile", "")
                up = up + "\n" + summary
                other["user_profile"] = up
                profile["other_info"] = other

    # ── Multi-instrument price tracking ──
    # All prices/ADVs flow from the InstrumentUniverse — single source of truth.
    current_prices: dict[str, float] = instrument_universe.prices()
    adv_values: dict[str, float] = instrument_universe.adv_values()
    adaptive_advs: dict[str, AdaptiveADV] = {
        t: AdaptiveADV(baseline=v) for t, v in adv_values.items()
    }
    # Scalar adaptive ADV for the single-instrument legacy path / event-subject view.
    adaptive_adv = AdaptiveADV(baseline=initial_adv)

    # ── Cadence-aware caps ──
    # Schedules that step in month-scale jumps need relaxed Kyle caps +
    # knee + resistance, because the daily-calibrated values assume each
    # round is one trading day. The round_schedule exposes this directly
    # via `_is_monthly_cadence()`. For daily schedules the defaults are
    # preserved and this is a no-op. Backtest v2 (§3.3) showed the day
    # cap clipping a +70% real monthly move to +3.68% sim on 东方财富 924.
    from .market_dynamics import CADENCE_CAPS, caps_for_cadence
    _is_monthly_cadence = bool(
        round_schedule and hasattr(round_schedule, "_is_monthly_cadence")
        and round_schedule._is_monthly_cadence()
    )
    if _is_monthly_cadence:
        _cadence_label = "monthly"
        _max_delta_pct, _monthly_knee = caps_for_cadence("monthly")
        # Monthly schedules need a shallower resistance curve (0.8 vs 3.0)
        # so that M+1's +20% cum_abs_delta doesn't already collapse the
        # knee to near-zero on M+2.
        _resistance_scale = 0.8
        # Override the calibration library's daily knee with the monthly
        # default — calibration was measured on daily backtests and the
        # knee is cadence-dependent, not event-type-dependent.
        _calibrated_knee = _monthly_knee
        log.info(
            "Cadence: monthly — Kyle cap %.2f, base_knee %.2f, "
            "resistance_scale %.2f (overriding daily defaults)",
            _max_delta_pct, _calibrated_knee, _resistance_scale,
        )
    else:
        _cadence_label = "daily"
        _max_delta_pct, _ = caps_for_cadence("daily")
        _resistance_scale = 3.0

    # ── Limit board + publication effects (A-share realism) ──
    from .limit_board import LimitBoard, T1Ledger, infer_board_type
    from .publication_effects import EffectTracker, compute_publication_effect, aggregate_round_effects

    _board_type = infer_board_type(event.ticker) if event.ticker else "normal"
    limit_board = LimitBoard(prev_close=initial_price, board_type=_board_type)
    t1_ledger = T1Ledger()
    effect_tracker = EffectTracker()

    # Accumulate per-ticker price trajectories (initial prices as first point)
    multi_trajectories: dict[str, list[float]] = {
        t: [p] for t, p in current_prices.items()
    } if current_prices else {}

    # ── Round loop ──
    rounds: list[RoundRecord] = []
    current_price = initial_price
    persona_by_id = {p.id: p for p in personas}
    last_actions: dict[str, dict] = {}  # persona_id → {side, rationale, round_idx}
    # Forced actions carried over from the previous round's post-price
    # threshold pass (step 4.5). Applied at step 3.5 of the next round.
    deferred_forced_actions: dict[str, Any] = {}
    # Terminal-risk flag set by the severity resolver when the event text
    # mentions delisting / fraud / *ST keywords. Persists across the whole
    # simulation once triggered so the forced-seller cascade and dip-buy
    # suppression stay active through every subsequent round.
    _sim_terminal_risk: bool = False

    # Initial event severity resolution — populated at R0 and kept around
    # so later rounds can propagate a decayed prior into the Kyle sentiment
    # modifier. Without this the overnight_sentiment from the severity
    # resolver only affected the R0 gap-open once and never reached the
    # round-loop Kyle impact, so policy-bull priors died immediately.
    # See analysis/backtest_monthly_v2_full.json (CATL / 东财 policy events
    # that sim'd bearish despite +0.6 severity prior).
    _initial_severity = None
    _SEVERITY_PRIOR_DECAY = 0.7  # per-round geometric decay

    try:
        for round_idx in range(n_rounds):
            log.info("OASIS sim %s round %d starting at price %.2f",
                     simulation_id, round_idx, current_price)
            safe_emit(
                event_sink,
                EVENT_ROUND_START,
                simulation_id=simulation_id,
                round_idx=round_idx,
                current_price=current_price,
            )
            pre_round_post_id = _max_post_id(str(db_path_obj))
            order_collector.set_round(round_idx)
            action_collector.set_round(round_idx)

            # 0.5. Read time context from round_schedule
            round_label = ""
            active_types: set[str] | None = None
            if round_schedule is not None:
                rd = round_schedule.get_round(round_idx)
                if rd:
                    round_label = rd.label
                    if rd.active_agent_types:
                        active_types = set(rd.active_agent_types)
                    log.info(
                        "  R%d schedule: %s (active_types=%s)",
                        round_idx, rd.label, rd.active_agent_types,
                    )
                    # Detect trading-day boundary → reset limit board + T+1
                    if round_idx > 0:
                        prev_rd = round_schedule.get_round(round_idx - 1)
                        if prev_rd:
                            prev_day = int(prev_rd.hours_since_event // 24)
                            curr_day = int(rd.hours_since_event // 24)
                            if curr_day > prev_day:
                                # New trading day: reset limit board with
                                # yesterday's close as prev_close
                                limit_board = LimitBoard(
                                    prev_close=current_price,
                                    board_type=_board_type,
                                )
                                t1_ledger.advance_day()
                                log.info(
                                    "  R%d NEW TRADING DAY (T+%d): "
                                    "limit board reset at %.2f, T+1 ledger cleared",
                                    round_idx, curr_day, current_price,
                                )

            # 0.6. Pre-open auction: estimate the opening shock and pre-set
            #      the limit board state BEFORE any agent trades this day.
            #      Without this, the first round of each day trades as if the
            #      board is NORMAL, even when the event shock should immediately
            #      create a one-word limit-down/up.
            from .limit_board import gap_open as _gap_open

            _is_new_trading_day = False
            if round_idx == 0:
                _is_new_trading_day = True
            elif round_schedule is not None:
                rd = round_schedule.get_round(round_idx)
                prev_rd = round_schedule.get_round(round_idx - 1)
                if rd and prev_rd:
                    _is_new_trading_day = (
                        int(rd.hours_since_event // 24)
                        > int(prev_rd.hours_since_event // 24)
                    )

            if _is_new_trading_day:
                # Estimate overnight sentiment for the gap-open model using
                # the single-source-of-truth resolver in event_severity.py.
                # Pulled out of the engine in Round 6 so the rules can be
                # tested directly without spinning up an OASIS round loop.
                if round_idx == 0:
                    _sev = resolve_event_severity(
                        event_type=event.event_type or "",
                        event_text=event.event_text or "",
                        day1_open=getattr(event, "day1_open", None),
                        current_price=initial_price,
                    )
                    _overnight_sent = _sev.overnight_sentiment
                    _gap_vol = _sev.gap_vol
                    # Stash for later rounds' Kyle sentiment bias propagation.
                    _initial_severity = _sev
                    if _sev.terminal_risk:
                        _sim_terminal_risk = True
                        log.info(
                            "  R0 TERMINAL RISK flagged (sentiment=%.2f, source=%s): "
                            "forced-seller cascade + dip-buy suppression active for the rest of the sim",
                            _sev.overnight_sentiment, _sev.source,
                        )
                else:
                    # Later days: momentum from previous day's cumulative move.
                    _cum_delta = (
                        (current_price / initial_price - 1.0)
                        if initial_price > 0
                        else 0.0
                    )
                    _overnight_sent = max(-1.0, min(1.0, _cum_delta * 3.0))
                    _abs_sent = abs(_overnight_sent)
                    _gap_vol = 0.05 + 0.10 * max(0.0, _abs_sent - 0.3)
                _open_price = _gap_open(
                    prev_close=limit_board.prev_close,
                    overnight_sentiment=_overnight_sent,
                    board_type=_board_type,
                    volatility=_gap_vol,
                )

                # If the open price diverges from prev_close, simulate a
                # pre-open auction that moves the board state before trading.
                _gap_delta = (
                    (_open_price - limit_board.prev_close)
                    / limit_board.prev_close
                    if limit_board.prev_close > 0
                    else 0.0
                )
                _clamped_gap = limit_board.clamp_delta(_gap_delta)
                if abs(_clamped_gap) > 0.001:
                    # Simulate the auction: at-limit opens have extreme
                    # order imbalance (one side >> the other).
                    if _clamped_gap > 0:
                        limit_board.update(
                            _clamped_gap,
                            buy_volume=1e10,
                            sell_volume=1e8,
                        )
                    else:
                        limit_board.update(
                            _clamped_gap,
                            buy_volume=1e8,
                            sell_volume=1e10,
                        )
                    limit_board.current_price = _open_price
                    # Adjust the running price so agents see the post-gap level
                    current_price = _open_price
                    log.info(
                        "  R%d PRE-OPEN AUCTION: gap %.2f%% → board %s, "
                        "price %.2f → %.2f (sentiment=%.2f)",
                        round_idx,
                        _gap_delta * 100,
                        limit_board.state.value,
                        limit_board.prev_close,
                        limit_board.current_price,
                        _overnight_sent,
                    )

            # Pre-compute inactive trader IDs for this round
            inactive_trader_ids: set[str] = set()
            if active_types is not None:
                inactive_trader_ids = {
                    p.id for p in personas
                    if p.sandbox is not None
                    and (p.agent_type or "retail") not in active_types
                }
                if inactive_trader_ids:
                    log.info(
                        "  R%d temporal filter: %d/%d trader classes inactive",
                        round_idx, len(inactive_trader_ids),
                        sum(1 for p in personas if p.sandbox is not None),
                    )

            # Cumulative price move for participation/knee calculations
            cumulative_delta_pct = (
                (current_price / initial_price - 1.0) if initial_price > 0 else 0.0
            )

            # Sentiment accumulator for this round. Seed with a decayed
            # version of the event_severity prior so policy-bull /
            # regulatory-bear directional bias carries through the sim,
            # not just the R0 gap-open. Geometric decay at rate 0.7/round
            # means a +0.6 policy prior at R0 is +0.42 at R1, +0.29 at R2,
            # +0.20 at R3, etc. — a soft tilt, not a hard command.
            round_sentiment_shift = 0.0
            if _initial_severity is not None:
                round_sentiment_shift = (
                    _initial_severity.overnight_sentiment
                    * (_SEVERITY_PRIOR_DECAY ** round_idx)
                )

            # 0.7. Compute aggregate publication effects from previous rounds'
            #      publications (decayed). These modifiers are applied to
            #      participation, urgency, and risk budget BEFORE trading.
            active_effects = effect_tracker.effects_at_round(round_idx)
            if active_effects:
                round_pub_effects = aggregate_round_effects(active_effects)
                round_sentiment_shift += round_pub_effects.sentiment_shift
                log.info(
                    "  R%d pre-trade publication effects: %d active, "
                    "sentiment=%+.3f, participation=%.2fx, urgency=%.2fx, "
                    "risk_budget_shift=%+.3f",
                    round_idx, len(active_effects),
                    round_pub_effects.sentiment_shift,
                    round_pub_effects.participation_modifier,
                    round_pub_effects.urgency_modifier,
                    round_pub_effects.risk_budget_shift,
                )
            else:
                round_pub_effects = AggregateEffect()

            # 1. Inject any external events scheduled for this round
            #    Build a SimSnapshot so DynamicEventStream conditionals can
            #    evaluate against current simulation state.
            _snap = SimSnapshot(
                round_idx=round_idx,
                current_price=current_price,
                initial_price=initial_price,
                cumulative_delta_pct=cumulative_delta_pct,
                net_flow_last_round=(
                    rounds[-1].net_flow if rounds else 0.0
                ),
                round_count=n_rounds,
                agent_states={
                    a.id: dict(a.state)
                    for a in sim_graph.agents.values()
                },
            )
            external_for_round = external_events.events_for_round(
                round_idx, snapshot=_snap,
            )
            for ev in external_for_round:
                author_id = ev.author_persona_id
                if author_id in persona_id_to_oasis_id:
                    author_agent = agent_graph.get_agent(
                        persona_id_to_oasis_id[author_id]
                    )
                else:
                    author_agent = market_agent
                pre_event_post_id = _max_post_id(str(db_path_obj))
                await env.step({
                    author_agent: ManualAction(
                        action_type=ActionType.CREATE_POST,
                        action_args={"content": ev.text},
                    ),
                })
                ev_post_id = _max_post_id(str(db_path_obj))
                if ev_post_id > pre_event_post_id:
                    publication_registry.register(
                        ev_post_id,
                        PublicationMetadata(
                            content_type=ev.content_type,
                            author_persona_id=author_id,
                            author_archetype=author_id,
                            authority_weight=ev.authority_weight,
                            round_idx=round_idx,
                        ),
                    )
                    safe_emit(
                        event_sink,
                        EVENT_EXTERNAL_EVENT_INJECTED,
                        simulation_id=simulation_id,
                        round_idx=round_idx,
                        author_persona_id=author_id,
                        content_type=ev.content_type,
                        text=ev.text,
                        authority_weight=ev.authority_weight,
                        oasis_post_id=int(ev_post_id),
                    )

            # 1.5. SimGraph: resource flows + policy evaluation + prompt injection
            from .policy import AnnounceAction, TradeAction
            from .policy_engine import (
                collect_announcements,
                collect_trade_overrides,
                evaluate_policies,
            )

            # Start with deferred trade overrides from previous round
            trade_overrides: dict[str, Any] = dict(deferred_forced_actions)
            deferred_forced_actions.clear()

            # Execute deterministic resource flows
            sim_graph.execute_flows()

            # Evaluate ALL policies on ALL agents in one pass
            policy_fires = evaluate_policies(sim_graph, round_idx)

            # Dispatch policy fires by action type
            for fire in policy_fires:
                safe_emit(
                    event_sink,
                    EVENT_POLICY_FIRED,
                    simulation_id=simulation_id,
                    round_idx=round_idx,
                    agent_id=fire.agent_id,
                    agent_name=fire.agent_display_name,
                    policy_id=fire.policy.id,
                    description=fire.policy.description,
                    action_type=type(fire.action).__name__,
                    source=fire.policy.source,
                )

            # Collect trade overrides (force_action policies)
            new_overrides = collect_trade_overrides(policy_fires, sim_graph)
            trade_overrides.update(new_overrides)

            # Inject announcements into social feed + accumulate sentiment
            from .policy_engine import score_announcement_sentiment
            for fire in collect_announcements(policy_fires):
                ann = fire.action
                round_sentiment_shift += score_announcement_sentiment(
                    ann.text
                ) * ann.authority_weight
                a_agent_id = fire.agent_id
                if a_agent_id in persona_id_to_oasis_id:
                    a_author = agent_graph.get_agent(
                        persona_id_to_oasis_id[a_agent_id]
                    )
                else:
                    a_author = market_agent
                pre_a_post_id = _max_post_id(str(db_path_obj))
                await env.step({
                    a_author: ManualAction(
                        action_type=ActionType.CREATE_POST,
                        action_args={"content": ann.text},
                    ),
                })
                a_post_id = _max_post_id(str(db_path_obj))
                if a_post_id > pre_a_post_id:
                    publication_registry.register(
                        a_post_id,
                        PublicationMetadata(
                            content_type=ann.content_type,
                            author_persona_id=a_agent_id,
                            author_archetype=fire.agent_display_name,
                            authority_weight=ann.authority_weight,
                            round_idx=round_idx,
                        ),
                    )

            # Inject agent state ("处境") into OASIS agent prompts
            sim_graph.inject_state_into_prompts(
                agent_graph, persona_id_to_oasis_id, round_idx,
            )

            # 1.85. Clear all per-round context stashes from the previous round
            #        BEFORE we start writing this round's context. Otherwise
            #        conviction_ctx / pub_effects_ctx leak from round N-1 into
            #        round N even when the current round has nothing to say
            #        about them. set_round_context() only merges keys — it
            #        cannot overwrite with empty, so we reset first.
            for persona in personas:
                if persona.id not in persona_id_to_oasis_id:
                    continue
                oasis_id = persona_id_to_oasis_id[persona.id]
                oasis_agent = agent_graph.get_agent(oasis_id)
                clear_round_context(oasis_agent)

            # 1.9. Inject price-anchored market context + event
            #      fundamentals so agents judge risk/reward at the
            #      current price level, not just repeat their last
            #      decision. Runs for every trader persona in EVERY
            #      round including R0 — the event_fundamentals block
            #      is a directional prior that agents need even before
            #      any trading has happened. Before R0 coverage was
            #      added, CATL / 东财 agents drifted with feed sentiment
            #      in R0 and set the bearish tone for the whole sim.
            for persona in personas:
                if persona.sandbox is None:
                    continue
                oasis_id = persona_id_to_oasis_id[persona.id]
                oasis_agent = agent_graph.get_agent(oasis_id)
                update_conviction_context(
                    oasis_agent, persona.id, last_actions,
                    cumulative_delta_pct=cumulative_delta_pct,
                    current_price=current_price,
                    initial_price=initial_price,
                    event_type=event.event_type or "",
                )

            # 1.92. Self-model render: append each trader's structured
            #       self-state (financial snapshot, last-trade outcome,
            #       emotional state, peer watchlist, utility breakdown)
            #       to the conviction_ctx stash. At R0 this shows the
            #       initial untouched state; from R1 onward it reflects
            #       the end-of-previous-round evaluator state (written
            #       by the post-Kyle update step ~L1945). The text is
            #       concatenated to whatever update_conviction_context
            #       already wrote, separated by a blank line.
            for persona in personas:
                if persona.sandbox is None:
                    continue
                sm_ev = self_model_evaluators.get(persona.id)
                if sm_ev is None:
                    continue
                if persona.id not in persona_id_to_oasis_id:
                    continue
                oasis_id = persona_id_to_oasis_id[persona.id]
                oasis_agent = agent_graph.get_agent(oasis_id)
                try:
                    peers = sm_ev.select_peers(self_model_evaluators)
                    self_ctx = sm_ev.render_prompt(peers)
                except Exception as exc:
                    log.warning(
                        "  R%d self_model render failed for %s: %s",
                        round_idx, persona.id, exc,
                    )
                    continue
                existing = (
                    getattr(oasis_agent, "_ssflow_round_context", None) or {}
                ).get("conviction_ctx", "")
                merged = (existing + "\n\n" + self_ctx) if existing else self_ctx
                set_round_context(oasis_agent, conviction_ctx=merged)

            # 1.94. Terminal-risk context. When the severity resolver flagged
            #        the event as terminal-bear (退市 / 立案 / 造假 / *ST /
            #        delisting / bankruptcy), inject a forced-seller-cascade
            #        nudge into every trader's prompt so retail dip-buyers
            #        stop bidding the name on the way down. This closes the
            #        -3.42% vs -20% magnitude gap GPT flagged in R6: without
            #        it, long-only retail personas read "大跌" and reflexively
            #        buy the dip, limiting how far the price can fall even
            #        when limit-board mechanics are firing.
            if _sim_terminal_risk:
                _terminal_ctx = (
                    "# 终局风险 / Terminal Risk\n"
                    "  此标的已触发终局风险警报 (退市 / 造假 / *ST / 强制清算 类事件).\n"
                    "  长期持有人的基本面逻辑已被摧毁, 价格预期持续下行直至流动性枯竭.\n"
                    "  行为约束:\n"
                    "    - 若你是长期持有人, 应立即评估清仓, 不再加仓\n"
                    "    - 不要【抄底/捞便宜】这类标的, 退市风险导致归零尾部\n"
                    "    - 机构持仓应触发 forced-seller 条款, 排队止损而非等反弹\n"
                    "    - 散户即使情绪激烈, 也须承认基本面已不可交易\n"
                    "  例外: 事件驱动做空基金和宏观对冲空头继续加空"
                )
                for persona in personas:
                    if persona.id not in persona_id_to_oasis_id:
                        continue
                    oasis_id = persona_id_to_oasis_id[persona.id]
                    oasis_agent = agent_graph.get_agent(oasis_id)
                    set_round_context(
                        oasis_agent, terminal_risk_ctx=_terminal_ctx,
                    )

            # 1.95. Inject time context from round schedule into agent prompts.
            #        Uses set_round_context() → user-instruction injection path
            #        because OASIS/CAMEL snapshots the system message from
            #        profile at agent init time and ignores later mutations.
            if round_schedule is not None:
                cum_pct = cumulative_delta_pct * 100  # ratio → percentage
                # Find yesterday's closing % move if we're crossing a day.
                _prev_day_close_pct: float | None = None
                _current_day = round_schedule.rounds[round_idx].trading_day_index if round_idx < len(round_schedule.rounds) else 0
                if _current_day > 0 and round_idx > 0:
                    # Walk back through prior rounds to find the last one on the
                    # previous trading day and read its cumulative delta.
                    for _back in range(round_idx - 1, -1, -1):
                        if round_schedule.rounds[_back].trading_day_index == _current_day - 1:
                            if _back < len(rounds):
                                prev_rec = rounds[_back]
                                if initial_price > 0:
                                    _prev_day_close_pct = (prev_rec.price_after / initial_price - 1.0) * 100
                            break
                time_ctx = round_schedule.prompt_context(
                    round_idx,
                    cumulative_delta_pct=cum_pct,
                    current_price=current_price,
                    prev_day_close_delta_pct=_prev_day_close_pct,
                )
                for persona in personas:
                    if persona.id not in persona_id_to_oasis_id:
                        continue
                    oasis_id = persona_id_to_oasis_id[persona.id]
                    oasis_agent = agent_graph.get_agent(oasis_id)
                    set_round_context(oasis_agent, time_ctx=time_ctx)

            # 1.96. Inject publication effect context into agent prompts so
            #        agents are aware of market-wide sentiment shifts from
            #        publications (e.g., exchange inquiry → bearish; national
            #        team buying → confidence boost).
            if not round_pub_effects.is_neutral:
                _effect_ctx = _build_publication_effect_context(round_pub_effects)
                for persona in personas:
                    ptype = persona.agent_type or "retail"
                    # Only inject into affected personas
                    if (round_pub_effects.affected_personas
                            and ptype not in round_pub_effects.affected_personas):
                        continue
                    if persona.id not in persona_id_to_oasis_id:
                        continue
                    oasis_id = persona_id_to_oasis_id[persona.id]
                    oasis_agent = agent_graph.get_agent(oasis_id)
                    set_round_context(oasis_agent, pub_effects_ctx=_effect_ctx)

            # 1.97. Inject market stress context into policy/regulator prompts.
            #        When stress is elevated/crisis, policy makers see a briefing
            #        that prompts them to issue stabilization statements.
            from .market_stress import (
                compute_market_stress,
                create_stabilization_policies,
                render_policy_context,
            )
            stress = compute_market_stress(
                rounds, current_price, initial_price, round_idx,
            )
            if stress.is_stressed:
                _stress_marker = "\n# 市场状况简报"
                for persona in personas:
                    if persona.entity_role not in ("regulator", "policy"):
                        continue
                    if persona.id not in persona_id_to_oasis_id:
                        continue
                    oasis_id = persona_id_to_oasis_id[persona.id]
                    oasis_agent = agent_graph.get_agent(oasis_id)
                    ctx = render_policy_context(
                        stress, persona.id, persona.entity_role, round_idx,
                    )
                    profile = oasis_agent.user_info.profile
                    other = profile.get("other_info", {})
                    up = other.get("user_profile", "")
                    if _stress_marker in up:
                        up = up[:up.index(_stress_marker)]
                    up += ctx
                    other["user_profile"] = up
                    profile["other_info"] = other
                # Create stabilization policies for national_team if crisis
                stab_policies = create_stabilization_policies(
                    stress, sim_graph, round_idx,
                )
                if stab_policies:
                    log.info(
                        "  R%d: %d stabilization policies created",
                        round_idx, len(stab_policies),
                    )

            # 1.98. Inject analyst counter-narrative context.
            #        After round 2 with significant price moves, nudge analyst
            #        personas toward more balanced/contrarian research.
            from .analyst_context import compute_analyst_context
            _analyst_marker = "\n# 分析师深度思考"
            for persona in personas:
                if persona.entity_role != "analyst":
                    continue
                if persona.id not in persona_id_to_oasis_id:
                    continue
                actx = compute_analyst_context(
                    persona, current_price, initial_price, round_idx, event,
                )
                if actx.should_inject and actx.contrarian_prompt:
                    oasis_id = persona_id_to_oasis_id[persona.id]
                    oasis_agent = agent_graph.get_agent(oasis_id)
                    profile = oasis_agent.user_info.profile
                    other = profile.get("other_info", {})
                    up = other.get("user_profile", "")
                    if _analyst_marker in up:
                        up = up[:up.index(_analyst_marker)]
                    up += actx.contrarian_prompt
                    other["user_profile"] = up
                    profile["other_info"] = other

            # 2. OASIS social step: every real persona acts via LLM.
            #    Trader personas see BOTH the 21 native OASIS social actions
            #    AND the custom `submit_order_distribution` tool. CAMEL's
            #    ChatAgent picks one (or more) tool calls in a single LLM
            #    decision — social + trading from one brain, one memory.
            real_agents = {
                agent_graph.get_agent(persona_id_to_oasis_id[p.id]): LLMAction()
                for p in personas
                if p.sandbox is None  # non-traders always active socially
                or p.id not in inactive_trader_ids
            }
            try:
                await env.step(real_agents)
            except BudgetExceeded as exc:
                log.warning(
                    "OASIS sim %s hit budget at round %d social step",
                    simulation_id, round_idx,
                )
                safe_emit(
                    event_sink,
                    EVENT_ERROR,
                    simulation_id=simulation_id,
                    round_idx=round_idx,
                    code="budget_exceeded",
                    detail=str(exc),
                )
                raise
            except Exception as exc:
                log.warning(
                    "OASIS sim %s round %d social step error: %s",
                    simulation_id, round_idx, exc,
                )

            # 2.5. Query publications created during the social step and
            #      emit them as persona_thought events BEFORE the trade
            #      events. Thoughts chronologically precede trades because
            #      they come from the same LLM decision that triggered the
            #      submit_order_distribution tool call. Emitting in that
            #      order lets the frontend timeline read as cause → effect
            #      (thought → trade → flow → price), not the reverse.
            post_social_post_id = _max_post_id(str(db_path_obj))
            social_publications = _query_round_publications(
                db_path=str(db_path_obj),
                registry=publication_registry,
                persona_id_to_oasis_id=persona_id_to_oasis_id,
                since_post_id=pre_round_post_id,
            )
            for pub in social_publications:
                # Skip market broadcaster posts — they're not persona thoughts.
                if pub.author_persona_id == MARKET_AGENT_ID_NAME:
                    continue
                safe_emit(
                    event_sink,
                    EVENT_PERSONA_THOUGHT,
                    simulation_id=simulation_id,
                    round_idx=round_idx,
                    publication_id=pub.publication_id,
                    oasis_post_id=pub.oasis_post_id,
                    persona_id=pub.author_persona_id,
                    archetype=pub.author_archetype,
                    content_type=pub.content_type,
                    text=pub.text,
                    authority_weight=float(pub.authority_weight),
                    likes=int(pub.likes),
                    reposts=int(pub.reposts),
                    references=list(pub.references),
                )

            # 2.8. Drain non-trader action collector and dispatch.
            #      Announcements/regulations/research get posted into feed.
            pending_actions = action_collector.drain()
            if pending_actions:
                log.info(
                    "  R%d: %d agent actions collected",
                    round_idx, len(pending_actions),
                )
                for pa in pending_actions:
                    # ── Dynamic policy creation ──
                    if pa.action_type == "create_policy":
                        from .policy import Policy as _Policy
                        try:
                            new_policy = _Policy.from_llm_spec(
                                pa.payload,
                                source=f"llm_round_{round_idx}",
                                owner_id=pa.agent_id,
                            )
                            # Attach to the owning SimAgent
                            sim_agent = sim_graph.agent_by_persona(pa.persona_id)
                            if sim_agent is None:
                                sim_agent = sim_graph.agents.get(pa.agent_id)
                            if sim_agent is not None:
                                sim_agent.policies.append(new_policy)
                                log.info(
                                    "  R%d POLICY_CREATED: %s → %s (trigger=%s)",
                                    round_idx, pa.persona_id,
                                    new_policy.name, new_policy.trigger_expr,
                                )
                                safe_emit(
                                    event_sink,
                                    EVENT_POLICY_CREATED,
                                    simulation_id=simulation_id,
                                    round_idx=round_idx,
                                    agent_id=pa.agent_id,
                                    persona_id=pa.persona_id,
                                    policy_name=new_policy.name,
                                    trigger_expr=new_policy.trigger_expr,
                                    action_type=type(new_policy.action).__name__,
                                    source=new_policy.source,
                                )
                        except ValueError as exc:
                            log.warning(
                                "  R%d %s create_policy failed: %s",
                                round_idx, pa.persona_id, exc,
                            )
                        continue

                    # ── Regulatory dispatch: mechanical effects ──
                    if pa.action_type == "regulate":
                        from .policy_engine import dispatch_regulatory_action
                        reg_policies = dispatch_regulatory_action(
                            pa.payload, sim_graph, round_idx,
                        )
                        for rp in reg_policies:
                            safe_emit(
                                event_sink,
                                EVENT_POLICY_CREATED,
                                simulation_id=simulation_id,
                                round_idx=round_idx,
                                agent_id=pa.agent_id,
                                persona_id=pa.persona_id,
                                policy_name=rp.name,
                                trigger_expr=rp.trigger_expr,
                                action_type=type(rp.action).__name__,
                                source=rp.source,
                            )
                        # fall through to also post to feed

                    # ── Feed-posting actions (announce, regulate, publish) ──
                    text = pa.payload.get("text", "")
                    if not text:
                        continue
                    # Accumulate sentiment from announce actions
                    if pa.action_type == "announce":
                        authority = float(pa.payload.get("authority_weight", 0.8))
                        round_sentiment_shift += score_announcement_sentiment(
                            text
                        ) * authority
                    pa_persona_id = pa.persona_id
                    if pa_persona_id in persona_id_to_oasis_id:
                        pa_author = agent_graph.get_agent(
                            persona_id_to_oasis_id[pa_persona_id]
                        )
                    else:
                        pa_author = market_agent
                    pre_pa_post_id = _max_post_id(str(db_path_obj))
                    await env.step({
                        pa_author: ManualAction(
                            action_type=ActionType.CREATE_POST,
                            action_args={"content": text},
                        ),
                    })
                    pa_post_id = _max_post_id(str(db_path_obj))
                    if pa_post_id > pre_pa_post_id:
                        publication_registry.register(
                            pa_post_id,
                            PublicationMetadata(
                                content_type=pa.payload.get("content_type", "social_post"),
                                author_persona_id=pa_persona_id,
                                author_archetype=pa_persona_id,
                                authority_weight=float(pa.payload.get("authority_weight", 0.8)),
                                round_idx=round_idx,
                            ),
                        )
                    safe_emit(
                        event_sink,
                        EVENT_AGENT_ACTION,
                        simulation_id=simulation_id,
                        round_idx=round_idx,
                        agent_id=pa.agent_id,
                        persona_id=pa_persona_id,
                        action_type=pa.action_type,
                        text=text[:200],
                    )
                    log.info(
                        "  R%d %s [%s]: %s",
                        round_idx, pa_persona_id, pa.action_type, text[:80],
                    )

            # 3. Drain the OrderCollector: all trading intents submitted via
            #    the submit_order_distribution tool during the social step.
            pending_orders = order_collector.drain()
            log.info(
                "  R%d: %d order intents collected from %d traders",
                round_idx, len(pending_orders),
                len({o.persona_id for o in pending_orders}),
            )

            # 3.4. Terminal-risk forced-seller cascade. When the severity
            #      resolver flagged the sim as terminal-bear, long-only
            #      holders are injected with a synthetic sell order every
            #      round so they cascade out of the name even if the LLM
            #      otherwise decides to dip-buy. Short-capable personas are
            #      left alone — they're natural sellers who will handle
            #      the exit themselves. This closes most of the R6 mile-
            #      stone gap between the mechanism firing (LIMIT_DOWN,
            #      seal_strength) and the magnitude of the move.
            if _sim_terminal_risk:
                from .oasis_trading_tool import PendingOrder as _PO
                _short_capable_roles = {
                    "short_seller", "active_long_short",
                    "long_short", "hedge_fund_short",
                }
                _existing_ids = {o.persona_id for o in pending_orders}
                _cascade_count = 0
                for _p in personas:
                    if _p.sandbox is None:
                        continue
                    if _p.id in inactive_trader_ids:
                        continue
                    _role = (_p.role or "").lower()
                    if _role in _short_capable_roles:
                        continue
                    # Long-only persona with potentially nonzero position.
                    # Check agent pop — if nobody holds anything there's
                    # nothing to sell.
                    _pop = agent_pops.get(_p.id) or []
                    _has_long = any(
                        any(v > 0 for v in a.holdings.values()) for a in _pop
                    )
                    if not _has_long:
                        continue
                    # Magnitude: scale sell intensity by how close price
                    # already is to the limit. Stronger cascade when the
                    # board is sealed — matches the forced-seller
                    # behaviour real institutions display on *ST names.
                    _cascade_frac = 0.40 if limit_board.at_limit_down else 0.30
                    forced_order = _PO(
                        persona_id=_p.id,
                        distribution={"__freeform__": 1.0},
                        rationale=(
                            "[强制] 终局风险 forced-seller cascade — "
                            "基本面已不可交易, 排队止损"
                        ),
                        round_idx=round_idx,
                        raw_args={
                            "side": "sell",
                            "quantity_pct": _cascade_frac,
                            "pool": "holdings_in_target",
                        },
                    )
                    if _p.id in _existing_ids:
                        pending_orders = [
                            o for o in pending_orders if o.persona_id != _p.id
                        ]
                    pending_orders.append(forced_order)
                    _cascade_count += 1
                if _cascade_count > 0:
                    log.info(
                        "  R%d TERMINAL_RISK cascade: forced-sell %d long "
                        "personas at %.0f%%",
                        round_idx, _cascade_count,
                        40 if limit_board.at_limit_down else 30,
                    )

            # 3.5. Policy trade overrides: if a TradeAction policy fired,
            #      replace or inject the LLM's order for that persona.
            if trade_overrides:
                from .oasis_trading_tool import PendingOrder as _PO
                existing_ids = {o.persona_id for o in pending_orders}
                for pid, fire in trade_overrides.items():
                    ta = fire.action  # TradeAction
                    pool = ta.pool or ("cash" if ta.side == "buy" else "holdings_in_target")
                    forced_order = _PO(
                        persona_id=pid,
                        distribution={"__freeform__": 1.0},
                        rationale=f"[强制] {fire.policy.description}",
                        round_idx=round_idx,
                        raw_args={
                            "side": ta.side,
                            "quantity_pct": ta.quantity_pct,
                            "pool": pool,
                        },
                    )
                    if pid in existing_ids:
                        pending_orders = [
                            o for o in pending_orders if o.persona_id != pid
                        ]
                    pending_orders.append(forced_order)
                    log.info(
                        "  R%d POLICY_TRADE: %s → %s %.0f%% (%s)",
                        round_idx, pid, ta.side, ta.quantity_pct * 100,
                        fire.policy.description,
                    )
                    safe_emit(
                        event_sink,
                        EVENT_POLICY_FIRED,
                        simulation_id=simulation_id,
                        round_idx=round_idx,
                        agent_id=fire.agent_id,
                        agent_name=fire.agent_display_name,
                        policy_id=fire.policy.id,
                        description=fire.policy.description,
                        action_type="TradeAction",
                        forced_side=ta.side,
                        forced_quantity_pct=ta.quantity_pct,
                        replaced_llm_order=pid in existing_ids,
                    )

            # 4. Apply each captured distribution to the matching agent pop.
            #    If a trader didn't submit an order this round (LLM chose not
            #    to call the tool), it's treated as a full-hold decision.
            class_flows: list[ClassFlowResult] = []
            submitted_ids: set[str] = set()

            for order in pending_orders:
                # Skip orders from temporally inactive traders
                if order.persona_id in inactive_trader_ids:
                    continue
                if order.persona_id not in agent_pops:
                    log.warning(
                        "unknown persona in order: %s (skipping)",
                        order.persona_id,
                    )
                    continue
                persona = persona_by_id[order.persona_id]
                # For freeform orders, send the actual intent (side/qty/pool)
                # to the frontend instead of the marker dict {"__freeform__": 1.0}.
                emit_dist = dict(order.distribution)
                if "__freeform__" in emit_dist and order.raw_args:
                    side = order.raw_args.get("side", "hold")
                    qty = order.raw_args.get("quantity_pct", 0.0)
                    pool = order.raw_args.get("pool", "")
                    emit_dist = {
                        "side": side,
                        "quantity_pct": qty,
                        "pool": pool,
                    }
                    log.info(
                        "  R%d %s freeform order: %s %.0f%% (pool=%s) — %s",
                        round_idx, order.persona_id,
                        side, qty * 100, pool,
                        (order.rationale or "")[:80],
                    )
                safe_emit(
                    event_sink,
                    EVENT_TRADE_SUBMITTED,
                    simulation_id=simulation_id,
                    round_idx=round_idx,
                    persona_id=order.persona_id,
                    archetype=persona.archetype,
                    distribution=emit_dist,
                    rationale=order.rationale,
                    instrument=order.instrument or "",
                )
                # Resolve which instrument this order targets.
                # Single-instrument mode: collapse any LLM-supplied ticker to
                # the event's primary ticker so initial holdings and traded
                # holdings share one bookkeeping key.
                if instrument_universe is None:
                    order_ticker = _primary_ticker
                elif order.instrument:
                    order_ticker = order.instrument
                else:
                    # Agent omitted instrument in multi-instrument mode → hold
                    log.warning(
                        "  R%d %s omitted instrument in multi-instrument mode, treating as hold",
                        round_idx, order.persona_id,
                    )
                    continue
                order_price = (
                    current_prices.get(order_ticker, current_price)
                    if current_prices else current_price
                )
                # Compute participation rate (urgency decay + momentum exhaustion)
                p_rate = _participation_rate(
                    persona, round_idx, cumulative_delta_pct, active_types,
                )
                # Apply publication effect: participation modifier
                p_rate = apply_effects_to_participation(
                    p_rate, round_pub_effects,
                )
                # Look up conviction damper from SimAgent state
                sim_agent = sim_graph.agent_by_persona(order.persona_id)
                damper = (
                    sim_agent.get("conviction_damper")
                    if sim_agent and sim_agent.get("conviction_damper") > 0
                    else 1.0
                )
                # Apply publication effect: urgency modifier scales conviction
                damper = damper * round_pub_effects.urgency_modifier
                # Apply publication effect: risk budget shift adjusts position limits
                if abs(round_pub_effects.risk_budget_shift) > 0.001:
                    for agent in agent_pops[order.persona_id]:
                        agent.max_holdings_value = apply_effects_to_risk_budget(
                            agent.max_holdings_value / max(agent.capital, 1.0),
                            round_pub_effects,
                        ) * agent.capital
                flow = apply_distribution_to_agent_pop(
                    persona=persona,
                    agents=agent_pops[order.persona_id],
                    distribution=order.distribution,
                    current_price=order_price,
                    rng=sample_rng,
                    instrument=order_ticker,
                    rationale=order.rationale,
                    raw_distribution=order.raw_args if order.raw_args else order.distribution,
                    round_idx=round_idx,
                    participation_rate=p_rate,
                    conviction_damper=damper,
                    limit_board=limit_board,
                    t1_ledger=t1_ledger,
                )
                class_flows.append(flow)
                submitted_ids.add(order.persona_id)
                # Record for conviction persistence
                if order.raw_args:
                    _side = order.raw_args.get("side", "hold")
                    _qty = float(order.raw_args.get("quantity_pct", 0.0) or 0.0)
                else:
                    # Legacy dist: infer dominant side and use its fraction
                    _dominant = max(order.distribution.items(), key=lambda kv: kv[1])
                    _side = _dominant[0]
                    _qty = float(_dominant[1])
                last_actions[order.persona_id] = {
                    "side": _side,
                    "quantity_pct": _qty,
                    "rationale": (order.rationale or "")[:200],
                    "round_idx": round_idx,
                }
                log.info(
                    "  R%d %s → net_flow=%.2f (%d agents, histogram=%s)",
                    round_idx, order.persona_id,
                    flow.net_flow, flow.n_agents, flow.action_histogram,
                )
                safe_emit(
                    event_sink,
                    EVENT_CLASS_FLOW_COMPUTED,
                    simulation_id=simulation_id,
                    round_idx=round_idx,
                    persona_id=order.persona_id,
                    archetype=persona.archetype,
                    net_flow=float(flow.net_flow),
                    n_agents=int(flow.n_agents),
                    action_histogram=dict(flow.action_histogram),
                    held=False,
                )

            # Traders that didn't submit this round → treat as full hold.
            # Build a synthetic "all-hold" distribution per sandbox action_space.
            for persona in personas:
                if persona.sandbox is None or persona.id in submitted_ids:
                    continue
                if persona.id in inactive_trader_ids:
                    continue  # temporally inactive traders produce zero flow
                hold_action = next(
                    (a["name"] for a in persona.sandbox.action_space
                     if a.get("side") == "none"),
                    persona.sandbox.action_space[0]["name"],
                )
                hold_dist = {hold_action: 1.0}
                hold_flow = apply_distribution_to_agent_pop(
                    persona=persona,
                    agents=agent_pops[persona.id],
                    distribution=hold_dist,
                    current_price=current_price,
                    rng=sample_rng,
                    instrument=_primary_ticker if instrument_universe is None else "_default",
                    rationale="(no tool call this round, held)",
                    round_idx=round_idx,
                    limit_board=limit_board,
                    t1_ledger=t1_ledger,
                )
                class_flows.append(hold_flow)
                safe_emit(
                    event_sink,
                    EVENT_CLASS_FLOW_COMPUTED,
                    simulation_id=simulation_id,
                    round_idx=round_idx,
                    persona_id=persona.id,
                    archetype=persona.archetype,
                    net_flow=float(hold_flow.net_flow),
                    n_agents=int(hold_flow.n_agents),
                    action_histogram=dict(hold_flow.action_histogram),
                    held=True,
                )

            # 4. Per-instrument flow aggregation + independent Kyle
            if instrument_universe is not None and current_prices:
                from .market_dynamics import (
                    compute_dynamic_knee,
                    compute_multi_instrument_impact,
                )

                # Group flows by instrument (skip _default — shouldn't happen
                # in multi-instrument mode, but guard defensively)
                flows_by_ticker: dict[str, float] = {}
                for cf in class_flows:
                    if cf.instrument == "_default":
                        continue
                    flows_by_ticker[cf.instrument] = (
                        flows_by_ticker.get(cf.instrument, 0.0) + cf.net_flow
                    )

                # Pre-compute the same cadence-aware inputs as the
                # single-instrument path (see line ~1929). Until the
                # 2026-04-12 bug fix, this call was silently using the
                # compute_price_impact defaults (max_delta_pct=0.10,
                # sentiment_modifier=0, flow_knee=FLOW_KNEE), which meant:
                #   - All monthly sims were clipped to daily ±10% caps
                #   - The severity-prior decay (P0.3) never reached Kyle
                #   - The dynamic knee never engaged
                # Result: backtest_monthly_v3_full_smonly.json showed
                # deltas hitting ±10% hard on regulatory events and
                # policy-bull events getting zero sentiment lift.
                _mi_n_active = len({
                    cf.persona_id for cf in class_flows if cf.net_flow != 0
                })
                _mi_n_total = sum(1 for p in personas if p.sandbox is not None)
                _mi_cum_abs = abs(cumulative_delta_pct)
                _mi_knee = compute_dynamic_knee(
                    _mi_n_active, _mi_n_total, _mi_cum_abs, round_idx,
                    base_knee=_calibrated_knee,
                    resistance_scale=_resistance_scale,
                )
                _mi_sentiment = max(-0.5, min(0.5, round_sentiment_shift))

                # Independent Kyle per instrument with direct orders
                delta_by_ticker = compute_multi_instrument_impact(
                    flows_by_ticker,
                    adv_values,
                    lambda_market=lambda_used,
                    max_delta_pct=_max_delta_pct,
                    flow_knee=_mi_knee,
                    sentiment_modifier=_mi_sentiment,
                )
                for ticker, delta in delta_by_ticker.items():
                    current_prices[ticker] = current_prices[ticker] * (1.0 + delta)

                # Bidirectional spillover: any instrument with a delta can
                # influence any instrument without, weighted by pairwise beta.
                spillover = instrument_universe.compute_spillover(delta_by_ticker)
                for ticker, spill in spillover.items():
                    delta_by_ticker[ticker] = spill
                    current_prices[ticker] = current_prices[ticker] * (1.0 + spill)

                log.info(
                    "  R%d multi-instrument deltas: %s",
                    round_idx,
                    {t: f"{d*100:+.2f}%" for t, d in delta_by_ticker.items()},
                )

                # Backward compat: scalar vars refer to event subject
                es_ticker = instrument_universe.event_subject_ticker
                delta_pct = delta_by_ticker.get(es_ticker, 0.0)
                net_flow_total = flows_by_ticker.get(es_ticker, 0.0)
                price_after = current_prices.get(es_ticker, current_price)
            else:
                # Single-instrument path — with dynamic knee + sentiment
                from .market_dynamics import compute_dynamic_knee
                net_flow_total = sum(cf.net_flow for cf in class_flows)
                n_active = len({cf.persona_id for cf in class_flows
                                if cf.net_flow != 0})
                n_total = sum(1 for p in personas if p.sandbox is not None)
                cumulative_abs = abs(cumulative_delta_pct)
                dynamic_knee = compute_dynamic_knee(
                    n_active, n_total, cumulative_abs, round_idx,
                    base_knee=_calibrated_knee,
                    resistance_scale=_resistance_scale,
                )
                # Clamp sentiment shift to [-0.5, 0.5]
                clamped_sentiment = max(-0.5, min(0.5, round_sentiment_shift))
                # Update adaptive ADV from observed round volume
                total_abs_flow = sum(abs(cf.net_flow) for cf in class_flows)
                effective_adv = adaptive_adv.update(total_abs_flow)
                raw_delta = compute_price_impact(
                    net_flow_value=net_flow_total,
                    adv_value=effective_adv,
                    lambda_market=lambda_used,
                    max_delta_pct=_max_delta_pct,
                    flow_knee=dynamic_knee,
                    sentiment_modifier=clamped_sentiment,
                )
                # Apply A-share limit-board clamping (涨跌停板). The limit
                # board models *daily* 10% limits — on monthly cadence the
                # cap has no physical meaning (a real month can freely
                # move >30%) so we skip it and let the Kyle cap govern.
                if _is_monthly_cadence:
                    delta_pct = raw_delta
                else:
                    delta_pct = limit_board.clamp_delta(raw_delta)
                # Compute buy/sell volumes for limit-board state tracking
                buy_vol = sum(cf.net_flow for cf in class_flows if cf.net_flow > 0)
                sell_vol = abs(sum(cf.net_flow for cf in class_flows if cf.net_flow < 0))
                # Monthly cadence: skip limit_board state tracking; the
                # one-word-up / seal_strength / limit_up machinery models
                # intraday queue dynamics that have no meaning at a
                # month-scale granularity. Leaving the board in NORMAL
                # keeps downstream code paths happy.
                if not _is_monthly_cadence:
                    limit_board.update(
                        delta_pct, buy_volume=buy_vol, sell_volume=sell_vol,
                    )
                if limit_board.at_limit:
                    seal = limit_board.seal_strength(effective_adv)
                    log.info(
                        "  R%d LIMIT BOARD: %s (seal=%.2f, unfilled=%.2e)",
                        round_idx, limit_board.state.value, seal,
                        limit_board.unfilled_volume,
                    )
                price_after = current_price * (1.0 + delta_pct)
                if abs(dynamic_knee - _calibrated_knee) > 1e-6:
                    log.info(
                        "  R%d dynamic_knee=%.4f (active=%d/%d, cum=%.1f%%, "
                        "sentiment=%+.2f)",
                        round_idx, dynamic_knee, n_active, n_total,
                        cumulative_abs * 100, clamped_sentiment,
                    )

            safe_emit(
                event_sink,
                EVENT_PRICE_UPDATED,
                simulation_id=simulation_id,
                round_idx=round_idx,
                price_before=float(current_price),
                price_after=float(price_after),
                delta_pct=float(delta_pct),
                net_flow_total=float(net_flow_total),
                price_currency=event_currency,
                prices=dict(current_prices) if current_prices else None,
                adv_effective=float(adaptive_adv.effective),
                adv_baseline=initial_adv,
            )

            # Accumulate multi-instrument trajectories
            if multi_trajectories:
                for t, p in current_prices.items():
                    multi_trajectories[t].append(p)

            # 4.6. Sync trading population state → SimGraph agents
            #      This is THE FIX for the parallel-books problem: actual
            #      holdings/cash stats from the trading population are written
            #      back to the corresponding SimAgent.state dict every round.
            sim_graph.sync_population_state(agent_pops, price_after, round_idx)
            sim_graph.update_price_derived_state(price_after, initial_price)
            sim_graph.record_all_snapshots()

            # 4.65. Self-model round update — advance each trader's
            #       evaluator using the authoritative post-trade SimAgent
            #       state + class_flow for this round. After update()
            #       and step_utility(), emit a persona_state_updated
            #       event so the frontend's PersonaStatePanel can render
            #       the updated state card (archetype, nav, pnl, utility,
            #       utility_delta, utility_breakdown).
            _sm_flows_by_persona = {cf.persona_id: cf for cf in class_flows}
            _sm_round_hours = 0.0
            if round_schedule is not None:
                _rd_now = round_schedule.get_round(round_idx)
                if _rd_now is not None:
                    _sm_round_hours = float(_rd_now.hours_since_event)
            _sm_round_ctx = SelfModelRoundCtx(
                round_idx=round_idx,
                round_hours=_sm_round_hours,
                n_rounds=n_rounds,
                current_price=float(price_after),
                initial_price=float(initial_price),
                cumulative_delta_pct=float(
                    (price_after / initial_price - 1.0) if initial_price > 0 else 0.0
                ),
                prev_round_delta_pct=_sm_prev_round_delta_pct,
                event_type=event.event_type or "",
            )
            for _p in personas:
                if _p.sandbox is None:
                    continue
                sm_ev = self_model_evaluators.get(_p.id)
                if sm_ev is None:
                    continue
                _sim_agent = sim_graph.agent_by_persona(_p.id)
                if _sim_agent is None:
                    continue
                _la = last_actions.get(_p.id, {})
                _flow = _sm_flows_by_persona.get(_p.id)
                _tr = SelfModelTradeResult(
                    persona_id=_p.id,
                    side=str(_la.get("side", "hold")),
                    quantity_pct=float(_la.get("quantity_pct", 0.0) or 0.0),
                    rationale=str(_la.get("rationale", "")),
                    nav=float(_sim_agent.get("total_nav", 0.0)),
                    cash=float(_sim_agent.get("total_nav", 0.0)) *
                    (1.0 - float(_sim_agent.get("avg_position_pct", 0.0))),
                    holdings_value=float(_sim_agent.get("total_nav", 0.0)) *
                    float(_sim_agent.get("avg_position_pct", 0.0)),
                    unrealized_pnl_pct=float(_sim_agent.get("unrealized_pnl_pct", 0.0)),
                    avg_position_pct=float(_sim_agent.get("avg_position_pct", 0.0)),
                    net_flow=float(_flow.net_flow) if _flow is not None else 0.0,
                )
                try:
                    sm_ev.update(_sm_round_ctx, _tr)
                    sm_ev.step_utility()
                except Exception as exc:
                    log.warning(
                        "  R%d self_model.update failed for %s: %s",
                        round_idx, _p.id, exc,
                    )
                    continue
                safe_emit(
                    event_sink,
                    EVENT_PERSONA_STATE_UPDATED,
                    simulation_id=simulation_id,
                    round_idx=round_idx,
                    persona_id=_p.id,
                    archetype=_p.archetype,
                    display_name=_p.display_name or _p.id,
                    state=sm_ev.state_public_snapshot(),
                    state_labels=sm_ev.state_labels(),
                    utility=float(sm_ev.last_utility),
                    utility_delta=float(sm_ev.last_utility_delta),
                    utility_breakdown=dict(sm_ev.last_utility_breakdown),
                )
            _sm_prev_round_delta_pct = float(delta_pct)

            # 4.7. Second policy pass: price-sensitive policies may fire now
            #      that price-derived state has been updated. Trade overrides
            #      from this pass are deferred to the NEXT round.
            post_price_fires = evaluate_policies(sim_graph, round_idx)
            deferred_overrides = collect_trade_overrides(post_price_fires, sim_graph)
            if deferred_overrides:
                deferred_forced_actions.update(deferred_overrides)
                log.info(
                    "  R%d deferred policy trade overrides for next round: %s",
                    round_idx,
                    list(deferred_overrides.keys()),
                )

            # 5. Post the price update from the market broadcaster
            price_post = _build_price_update_post(
                event, round_idx, current_price, price_after, net_flow_total,
                round_label=round_label,
                initial_price=initial_price,
                currency=event_currency,
            )
            pre_price_post_id = _max_post_id(str(db_path_obj))
            try:
                await env.step({
                    market_agent: ManualAction(
                        action_type=ActionType.CREATE_POST,
                        action_args={"content": price_post},
                    ),
                })
                price_post_id = _max_post_id(str(db_path_obj))
                if price_post_id > pre_price_post_id:
                    publication_registry.register(
                        price_post_id,
                        PublicationMetadata(
                            content_type="market_event",
                            author_persona_id=MARKET_AGENT_ID_NAME,
                            author_archetype="Market Event Wire",
                            authority_weight=1.0,
                            round_idx=round_idx,
                        ),
                    )
            except Exception as exc:
                log.warning(
                    "OASIS sim %s round %d failed to post price update: %s",
                    simulation_id, round_idx, exc,
                )

            # 6. Collect publications created AFTER the social step
            #    (these are the market broadcaster's price post + anything
            #    else emitted by env.step(market_agent) above). Combined
            #    with social_publications above, this forms the full
            #    per-round publication list that the markdown report uses.
            #    We don't emit these as persona_thought events — the price
            #    post is already represented by EVENT_PRICE_UPDATED, and
            #    the timeline would double-count it.
            market_publications = _query_round_publications(
                db_path=str(db_path_obj),
                registry=publication_registry,
                persona_id_to_oasis_id=persona_id_to_oasis_id,
                since_post_id=post_social_post_id,
            )
            publications_this_round = social_publications + market_publications

            # ── Track publication effects (quantitative) ──
            # Register this round's publications so their effects are available
            # to subsequent rounds via effect_tracker.effects_at_round().
            for pub in publications_this_round:
                content_type = getattr(pub, "content_type", None)
                if content_type:
                    try:
                        eff = compute_publication_effect(content_type)
                        effect_tracker.add(eff, round_idx=round_idx)
                    except Exception:
                        pass  # Unknown type — skip

            # ── Aggregate fill metrics from class_flows ──
            _fill_rates = [cf.fill_rate for cf in class_flows if cf.fill_rate is not None]
            _avg_fill = (sum(_fill_rates) / len(_fill_rates)) if _fill_rates else 1.0
            _total_unfilled = sum(cf.unfilled_volume for cf in class_flows)
            _total_t1 = sum(cf.t1_blocked_sells for cf in class_flows)

            rounds.append(
                RoundRecord(
                    round_idx=round_idx,
                    price_before=current_price,
                    price_after=price_after,
                    delta_pct=delta_pct,
                    net_flow=net_flow_total,
                    class_flows=class_flows,
                    publications_this_round=publications_this_round,
                    external_events_injected=len(external_for_round),
                    limit_board_state=limit_board.state.value,
                    limit_board_unfilled=limit_board.unfilled_volume,
                    limit_board_seal=limit_board.seal_strength(effective_adv) if not instrument_universe else 0.0,
                    avg_fill_rate=_avg_fill,
                    total_unfilled_volume=_total_unfilled,
                    total_t1_blocked=_total_t1,
                )
            )
            safe_emit(
                event_sink,
                EVENT_ROUND_COMPLETE,
                simulation_id=simulation_id,
                round_idx=round_idx,
                publications_count=len(publications_this_round),
                orders_count=len(pending_orders),
                class_flows_count=len(class_flows),
                price_after=float(price_after),
                net_flow_total=float(net_flow_total),
                limit_board_state=limit_board.state.value,
                fill_rate=float(_avg_fill),
                unfilled_volume=float(_total_unfilled),
                t1_blocked=int(_total_t1),
                seal_strength=float(limit_board.seal_strength(effective_adv) if not instrument_universe else 0.0),
            )

            n_traders_total = sum(1 for p in personas if p.sandbox is not None)
            log.info(
                "  R%d: %.2f → %.2f (%+.2f%%), net_flow=%+.2e, "
                "traders=%d/%d called tool, %d pubs, %d ext events",
                round_idx, current_price, price_after, delta_pct * 100,
                net_flow_total,
                len(submitted_ids), n_traders_total,
                len(publications_this_round),
                len(external_for_round),
            )

            current_price = price_after

    finally:
        try:
            await env.close()
        except Exception as exc:
            log.warning("OASIS env.close() raised: %s", exc)

    cost_at_end = cost_tracker.total_cost_usd
    elapsed = time.time() - t0
    final_price = current_price

    log.info(
        "OASIS sim %s done in %.1fs ($%.4f). Cumulative price delta: %+.2f%%, "
        "%d publications across %d rounds",
        simulation_id, elapsed, cost_at_end - cost_at_start,
        (final_price / initial_price - 1.0) * 100,
        sum(len(r.publications_this_round) for r in rounds),
        len(rounds),
    )

    safe_emit(
        event_sink,
        EVENT_SIMULATION_COMPLETE,
        simulation_id=simulation_id,
        initial_price=float(initial_price),
        final_price=float(final_price),
        cumulative_delta_pct=(
            (final_price / initial_price - 1.0) if initial_price else 0.0
        ),
        price_trajectory=[float(initial_price)] + [float(r.price_after) for r in rounds],
        n_rounds_completed=len(rounds),
        n_publications=sum(len(r.publications_this_round) for r in rounds),
        elapsed_seconds=float(elapsed),
        cost_usd=float(max(0.0, cost_at_end - cost_at_start)),
    )

    return OasisSimResult(
        simulation_id=simulation_id,
        event=event,
        personas=personas,
        initial_price=initial_price,
        final_price=final_price,
        rounds=rounds,
        elapsed_seconds=elapsed,
        cost_usd_at_start=cost_at_start,
        cost_usd_at_end=cost_at_end,
        llm_seed=seed,
        lambda_used=lambda_used,
        adv_value_used=initial_adv,
        oasis_db_path=str(db_path_obj),
        final_agents_by_class=agent_pops,
        price_trajectories=(
            dict(multi_trajectories)
            if multi_trajectories else {}
        ),
    )


__all__ = [
    "OasisSimResult",
    "RoundRecord",
    "run_simulation",
]
