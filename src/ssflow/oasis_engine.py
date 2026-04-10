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
from .event_bus import (
    EVENT_AGENT_ACTION,
    EVENT_CLASS_FLOW_COMPUTED,
    EVENT_ERROR,
    EVENT_EXTERNAL_EVENT_INJECTED,
    EVENT_FORCE_ACTION_OVERRIDE,
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
from .information.external_events import ExternalEventSchedule
from .llm_client import BudgetExceeded, cost_tracker
from .market_dynamics import compute_price_impact, lambda_for_market
from .oasis_feed_reader import (
    PublicationMetadata,
    PublicationRegistry,
)
from .oasis_lm import build_default_lm
from .oasis_persona_adapter import (
    MARKET_AGENT_ID_NAME,
    build_agent_graph,
    update_conviction_context,
)
from .oasis_trading_tool import OrderCollector, PendingOrder
from .persona import Persona
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


def _currency_symbol(currency: str) -> str:
    return {
        "CNY": "¥", "USD": "$", "EUR": "€", "JPY": "¥", "HKD": "HK$", "BTC": "₿"
    }.get(currency, "$")


def _build_initial_event_post(event: Event) -> str:
    """Render the seed event as the first post the market broadcaster makes
    at round 0. Lands in everyone's feed before round 1's social step.
    """
    sym = _currency_symbol(event.price_currency)
    parts = [
        f"[Market Event] {event.instrument or event.ticker} · "
        f"{event.event_type} · {event.event_date}",
        event.event_text.strip(),
    ]
    if event.prior_consensus.strip():
        parts.append(f"Prior consensus: {event.prior_consensus.strip()[:200]}")
    if event.recent_price_action.strip():
        parts.append(f"Recent price action: {event.recent_price_action.strip()[:200]}")
    parts.append(f"Current price: {sym}{event.current_price:.2f}")
    return "\n".join(parts)


def _build_price_update_post(
    event: Event,
    round_idx: int,
    price_before: float,
    price_after: float,
    net_flow: float,
    round_label: str = "",
) -> str:
    """The synthetic market broadcaster's price-update post."""
    sym = _currency_symbol(event.price_currency)
    delta_pct = (price_after / price_before - 1.0) * 100 if price_before else 0.0
    label = f" ({round_label})" if round_label else ""
    return (
        f"[Market Event] R{round_idx}{label} price update: "
        f"{sym}{price_before:.2f} → {sym}{price_after:.2f} "
        f"({delta_pct:+.2f}%). Net order flow: {sym}{net_flow:+,.0f}"
    )


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
    external_events: ExternalEventSchedule | None = None,
    db_path: str | None = None,
    event_sink: EventSink | None = None,
    entity_graph: "EntityGraph | None" = None,
    instrument_universe: "InstrumentUniverse | None" = None,
    round_schedule: "RoundSchedule | None" = None,
    sim_graph: "SimGraph | None" = None,
) -> OasisSimResult:
    """Run a Phase I OASIS-based market simulation.

    The trading layer sits on top of OASIS: each round, after OASIS advances
    the social state, our trading layer reads each trader's feed and runs a
    structured order-decision LLM call. Net flows feed Kyle, prices feed back
    into OASIS as `__market__` posts.

    Args:
        event: must be sandbox-ready (current_price + adv_value set)
        personas: schema v3 list (mix of trader + non-trader entities)
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
        instrument_universe: optional InstrumentUniverse for multi-instrument
            simulations. When provided, agents can trade across multiple
            instruments with independent Kyle pricing per ticker.
        round_schedule: optional RoundSchedule for time-aware rounds.
            When provided, each round's time context is injected into
            agent prompts.
            (round_start, persona_thought, trade_submitted, price_updated, …)
            as the simulation runs. See `ssflow.event_bus` for the protocol
            and the available event types. Sink errors are swallowed.

    Returns:
        OasisSimResult with the full price trajectory + publication log.

    Raises:
        ValueError: if event is not sandbox-ready or personas list is empty
        BudgetExceeded: if cost guard trips mid-run
    """
    _patch_oasis_recsys()

    if not personas:
        raise ValueError("run_simulation requires at least one persona")
    if not event.is_sandbox_ready:
        raise ValueError(
            f"event {event.ticker} is not sandbox-ready: "
            f"current_price={event.current_price}, adv_value={event.adv_value}"
        )

    n_rounds = n_rounds or settings.n_rounds
    simulation_id = simulation_id or f"oasis_{uuid.uuid4().hex[:12]}"
    seed = seed if seed is not None else settings.seed
    external_events = external_events or ExternalEventSchedule()
    lambda_used = (
        lambda_market if lambda_market is not None
        else lambda_for_market(event.market)
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
        n_rounds, event.current_price, event.adv_value,
        lambda_used, seed, db_path_obj,
    )

    cost_at_start = cost_tracker.total_cost_usd
    t0 = time.time()

    safe_emit(
        event_sink,
        EVENT_SIMULATION_START,
        simulation_id=simulation_id,
        ticker=event.ticker,
        instrument=event.instrument,
        market=event.market,
        market_event_type=event.event_type,
        event_date=event.event_date,
        initial_price=float(event.current_price),
        price_currency=event.price_currency,
        adv_value=float(event.adv_value),
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
    initial_price = float(event.current_price)
    agent_pops: dict[str, list[Agent]] = {}
    multi_prices_for_spawn = (
        instrument_universe.prices() if instrument_universe is not None else None
    )
    # Build {ticker: {persona_id: pct}} from instrument holder data
    holdings_by_persona_map: dict[str, dict[str, float]] | None = None
    if instrument_universe is not None:
        holdings_by_persona_map = {}
        for inst in instrument_universe.instruments:
            if inst.holdings_by_persona:
                holdings_by_persona_map[inst.ticker] = inst.holdings_by_persona
    for persona in personas:
        if persona.sandbox is not None:
            agent_pops[persona.id] = spawn_agents(
                persona, current_price=initial_price, rng=spawn_rng,
                multi_prices=multi_prices_for_spawn,
                holdings_by_persona=holdings_by_persona_map or None,
            )

    publication_registry = PublicationRegistry()

    # ── Round 0 prelude: post the seed event from the market broadcaster ──
    market_agent = agent_graph.get_agent(persona_id_to_oasis_id[MARKET_AGENT_ID_NAME])
    initial_post_text = _build_initial_event_post(event)
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

    # ── Multi-instrument price tracking ──
    # When instrument_universe is present, we track prices per ticker.
    # The single current_price variable remains as the primary instrument's price
    # for backward compat with all the existing code paths.
    if instrument_universe is not None:
        current_prices: dict[str, float] = instrument_universe.prices()
        adv_values: dict[str, float] = instrument_universe.adv_values()
    else:
        current_prices = {}
        adv_values = {}

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
            if round_schedule is not None:
                rd = round_schedule.get_round(round_idx)
                if rd:
                    round_label = rd.label
                    log.info(
                        "  R%d schedule: %s (active_types=%s)",
                        round_idx, rd.label, rd.active_agent_types,
                    )

            # 1. Inject any external events scheduled for this round
            external_for_round = external_events.events_for_round(round_idx)
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

            # Inject announcements into social feed
            for fire in collect_announcements(policy_fires):
                ann = fire.action
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

            # 1.9. Inject conviction persistence: tell each trader what they
            #      decided last round so the LLM maintains directional consistency.
            for persona in personas:
                if persona.sandbox is not None and persona.id in last_actions:
                    oasis_id = persona_id_to_oasis_id[persona.id]
                    oasis_agent = agent_graph.get_agent(oasis_id)
                    update_conviction_context(oasis_agent, persona.id, last_actions)

            # 2. OASIS social step: every real persona acts via LLM.
            #    Trader personas see BOTH the 21 native OASIS social actions
            #    AND the custom `submit_order_distribution` tool. CAMEL's
            #    ChatAgent picks one (or more) tool calls in a single LLM
            #    decision — social + trading from one brain, one memory.
            real_agents = {
                agent_graph.get_agent(persona_id_to_oasis_id[p.id]): LLMAction()
                for p in personas
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

                    # ── Feed-posting actions (announce, regulate, publish) ──
                    text = pa.payload.get("text", "")
                    if not text:
                        continue
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
                # In multi-instrument mode, agent must specify — no default.
                if order.instrument:
                    order_ticker = order.instrument
                elif instrument_universe is not None:
                    # Agent omitted instrument in multi-instrument mode → hold
                    log.warning(
                        "  R%d %s omitted instrument in multi-instrument mode, treating as hold",
                        round_idx, order.persona_id,
                    )
                    continue
                else:
                    order_ticker = "_default"
                order_price = (
                    current_prices.get(order_ticker, current_price)
                    if current_prices else current_price
                )
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
                )
                class_flows.append(flow)
                submitted_ids.add(order.persona_id)
                # Record for conviction persistence
                if order.raw_args:
                    _side = order.raw_args.get("side", "hold")
                else:
                    # Legacy dist: infer dominant side
                    _dominant = max(order.distribution.items(), key=lambda kv: kv[1])
                    _side = _dominant[0]
                last_actions[order.persona_id] = {
                    "side": _side,
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
                    rationale="(no tool call this round, held)",
                    round_idx=round_idx,
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
                from .market_dynamics import compute_multi_instrument_impact

                # Group flows by instrument (skip _default — shouldn't happen
                # in multi-instrument mode, but guard defensively)
                flows_by_ticker: dict[str, float] = {}
                for cf in class_flows:
                    if cf.instrument == "_default":
                        continue
                    flows_by_ticker[cf.instrument] = (
                        flows_by_ticker.get(cf.instrument, 0.0) + cf.net_flow
                    )

                # Independent Kyle per instrument with direct orders
                delta_by_ticker = compute_multi_instrument_impact(
                    flows_by_ticker, adv_values, lambda_market=lambda_used,
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
                # Single-instrument path (unchanged)
                net_flow_total = sum(cf.net_flow for cf in class_flows)
                delta_pct = compute_price_impact(
                    net_flow_value=net_flow_total,
                    adv_value=event.adv_value,
                    lambda_market=lambda_used,
                )
                price_after = current_price * (1.0 + delta_pct)

            safe_emit(
                event_sink,
                EVENT_PRICE_UPDATED,
                simulation_id=simulation_id,
                round_idx=round_idx,
                price_before=float(current_price),
                price_after=float(price_after),
                delta_pct=float(delta_pct),
                net_flow_total=float(net_flow_total),
                price_currency=event.price_currency,
                prices=dict(current_prices) if current_prices else None,
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
        adv_value_used=event.adv_value,
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
