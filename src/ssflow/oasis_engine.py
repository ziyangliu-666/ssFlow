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
    EVENT_CLASS_FLOW_COMPUTED,
    EVENT_ERROR,
    EVENT_EXTERNAL_EVENT_INJECTED,
    EVENT_PERSONA_THOUGHT,
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
from .oasis_persona_adapter import MARKET_AGENT_ID_NAME, build_agent_graph
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
    class_flows: dict[str, ClassFlowResult]
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
) -> str:
    """The synthetic market broadcaster's price-update post."""
    sym = _currency_symbol(event.price_currency)
    delta_pct = (price_after / price_before - 1.0) * 100 if price_before else 0.0
    return (
        f"[Market Event] R{round_idx} price update: "
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
            (round_start, persona_thought, trade_submitted, price_updated, …)
            as the simulation runs. See `ssflow.event_bus` for the protocol
            and the available event types. Sink errors are swallowed.

    Returns:
        OasisSimResult with the full price trajectory + publication log.

    Raises:
        ValueError: if event is not sandbox-ready or personas list is empty
        BudgetExceeded: if cost guard trips mid-run
    """
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

    # ── Build the agent graph (with the trading tool wired into traders) ──
    channel = Channel()
    lm = build_default_lm()
    order_collector = OrderCollector()
    agent_graph, persona_id_to_oasis_id = build_agent_graph(
        personas, channel, model=lm, order_collector=order_collector,
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
    for persona in personas:
        if persona.sandbox is not None:
            agent_pops[persona.id] = spawn_agents(
                persona, current_price=initial_price, rng=spawn_rng,
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

    # ── Round loop ──
    rounds: list[RoundRecord] = []
    current_price = initial_price
    persona_by_id = {p.id: p for p in personas}

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

            # 3. Drain the OrderCollector: all trading intents submitted via
            #    the submit_order_distribution tool during the social step.
            pending_orders = order_collector.drain()
            log.info(
                "  R%d: %d order intents collected from %d traders",
                round_idx, len(pending_orders),
                len({o.persona_id for o in pending_orders}),
            )

            # 4. Apply each captured distribution to the matching agent pop.
            #    If a trader didn't submit an order this round (LLM chose not
            #    to call the tool), it's treated as a full-hold decision.
            class_flows: dict[str, ClassFlowResult] = {}
            submitted_ids: set[str] = set()

            for order in pending_orders:
                if order.persona_id not in agent_pops:
                    log.warning(
                        "unknown persona in order: %s (skipping)",
                        order.persona_id,
                    )
                    continue
                persona = persona_by_id[order.persona_id]
                safe_emit(
                    event_sink,
                    EVENT_TRADE_SUBMITTED,
                    simulation_id=simulation_id,
                    round_idx=round_idx,
                    persona_id=order.persona_id,
                    archetype=persona.archetype,
                    distribution=dict(order.distribution),
                    rationale=order.rationale,
                )
                flow = apply_distribution_to_agent_pop(
                    persona=persona,
                    agents=agent_pops[order.persona_id],
                    distribution=order.distribution,
                    current_price=current_price,
                    rng=sample_rng,
                    rationale=order.rationale,
                    raw_distribution=order.distribution,
                )
                class_flows[order.persona_id] = flow
                submitted_ids.add(order.persona_id)
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
                )
                class_flows[persona.id] = hold_flow
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

            # 4. Aggregate net flow + Kyle
            net_flow_total = sum(cf.net_flow for cf in class_flows.values())
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
            )

            # 5. Post the price update from the market broadcaster
            price_post = _build_price_update_post(
                event, round_idx, current_price, price_after, net_flow_total
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

            log.info(
                "  R%d: %.2f → %.2f (%+.2f%%), net_flow=%+.2e, "
                "%d new publications, %d external events",
                round_idx, current_price, price_after, delta_pct * 100,
                net_flow_total, len(publications_this_round),
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
    )


__all__ = [
    "OasisSimResult",
    "RoundRecord",
    "run_simulation",
]
