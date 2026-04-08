"""Simulation orchestration — the engine core.

Per the eng-review's batched-output decision (A2): each round is ONE LLM call
that returns reactions for ALL personas via JSON mode, NOT N sequential per-
persona calls. This gives 5-6× better latency at the same cost.

Round structure:
    Round 0  - personas read the event in isolation, produce initial reactions
    Round 1  - each persona sees a sample of round-0 reactions, updates their own
    Round 2-N - more cross-influence rounds; group dynamics emerge

Output: SimulationResult with all rounds' reactions, costs, elapsed time.
"""

from __future__ import annotations

import json
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .config import settings
from .event import Event
from .llm_client import chat_json, cost_tracker
from .persona import Persona


# ─────────────────────── Data classes ───────────────────────


@dataclass
class Reaction:
    persona_id: str
    sentiment: float  # -1.0 (very negative) .. 1.0 (very positive)
    action_intent: str  # "panic_observe" | "observe" | "lean_in" | "lean_out" | "watch_kols"
    comment: str  # short, in persona voice
    confidence: float  # 0.0 .. 1.0 — persona's confidence in their own read

    @classmethod
    def from_dict(cls, d: dict[str, Any], fallback_id: str = "unknown") -> "Reaction":
        return cls(
            persona_id=str(d.get("persona_id") or fallback_id),
            sentiment=_clamp(float(d.get("sentiment", 0.0)), -1.0, 1.0),
            action_intent=str(d.get("action_intent", "observe")),
            comment=str(d.get("comment", "")).strip()[:280],
            confidence=_clamp(float(d.get("confidence", 0.5)), 0.0, 1.0),
        )


@dataclass
class SimulationResult:
    simulation_id: str
    event: Event
    personas: list[Persona]
    rounds: list[list[Reaction]]  # rounds[i][j] = persona j's reaction in round i
    elapsed_seconds: float
    cost_usd_at_start: float
    cost_usd_at_end: float

    @property
    def cost_usd(self) -> float:
        return self.cost_usd_at_end - self.cost_usd_at_start

    @property
    def n_rounds(self) -> int:
        return len(self.rounds)

    @property
    def n_personas(self) -> int:
        return len(self.personas)

    @property
    def final_reactions(self) -> list[Reaction]:
        return self.rounds[-1] if self.rounds else []


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# ─────────────────────── Prompt builders ───────────────────────


def _build_personas_block(personas: list[Persona]) -> str:
    """Render the persona roster as a compact block for the prompt."""
    lines = ["# Personas in this simulation"]
    for p in personas:
        biases_short = ", ".join(f"{k}={v}" for k, v in list(p.biases.items())[:3])
        lines.append(
            f"  - {p.id} ({p.archetype}, {p.display_name}); biases: {biases_short}"
        )
    return "\n".join(lines)


def _build_round_zero_prompt(event: Event, personas: list[Persona]) -> list[dict[str, str]]:
    persona_block = _build_personas_block(personas)
    persona_voices = "\n\n".join(
        f"### Persona `{p.id}` voice rules\n{p.voice_prompt}"
        for p in personas
    )

    system = (
        "You are a market simulation engine. You roleplay multiple Chinese A-share "
        "market participants (personas) reacting to an event.\n\n"
        "STRICT RULES:\n"
        "1. You MUST output ONLY a JSON object with key 'reactions' that is a list of "
        f"{len(personas)} reaction objects, ONE per persona, in the SAME ORDER as the persona "
        "roster below.\n"
        "2. Each reaction object has fields: persona_id (string), sentiment (number -1.0 to 1.0), "
        "action_intent (one of: 'panic_observe', 'observe', 'lean_in', 'lean_out', 'watch_kols', "
        "'wait_for_more_info'), comment (string, max 80 Chinese chars in the persona's own voice, "
        "first-person), confidence (number 0.0 to 1.0).\n"
        "3. NEVER output investment advice. NEVER use words like 建议/推荐/买入/卖出/目标价/评级. "
        "Each persona describes how THEY would react, not what the user should do.\n"
        "4. Personas MUST disagree where their character would. Do not produce homogeneous output. "
        "If 散户大妈 and 量化机器人 give the same sentiment, you have failed.\n"
        "5. Each comment is in CHINESE and STAYS IN CHARACTER. 量化机器人 uses data language, "
        "散户大妈 uses emotional simple language, etc.\n"
    )

    user = (
        f"{persona_block}\n\n"
        f"{persona_voices}\n\n"
        f"---\n\n"
        f"{event.to_simulation_input()}\n\n"
        f"---\n\n"
        f"This is ROUND 0 (initial read). Each persona reads the event in isolation, "
        f"WITHOUT seeing others' reactions. Output the JSON."
    )

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _build_round_n_prompt(
    event: Event,
    personas: list[Persona],
    round_idx: int,
    prior_round: list[Reaction],
    n_rounds_total: int,
) -> list[dict[str, str]]:
    persona_block = _build_personas_block(personas)
    persona_voices = "\n\n".join(
        f"### Persona `{p.id}` voice rules\n{p.voice_prompt}"
        for p in personas
    )

    prior_summary = "\n".join(
        f"  - {r.persona_id}: sentiment={r.sentiment:+.2f} intent={r.action_intent} "
        f"comment=\"{r.comment}\""
        for r in prior_round
    )

    system = (
        "You are a market simulation engine, continuing a running multi-round simulation.\n\n"
        "STRICT RULES (same as round 0):\n"
        f"1. Output ONLY a JSON object with key 'reactions' = list of {len(personas)} objects "
        "in the SAME ORDER as the persona roster.\n"
        "2. Each object: persona_id, sentiment (-1..1), action_intent, comment (CN, <80 chars, "
        "first-person, in persona voice), confidence (0..1).\n"
        "3. NEVER output investment advice (no 建议/推荐/买入/卖出/目标价/评级).\n"
        "4. In this round, each persona has SEEN the previous round's reactions (shown below). "
        "They may update their stance based on social influence, but stubborn personas (e.g., "
        "雪球大V long-term, policy_observer) should resist herd movement; impulsive personas "
        "(e.g., 韭菜, 散户大妈) move more easily.\n"
        "5. Personas MUST diverge where character demands it.\n"
    )

    user = (
        f"{persona_block}\n\n"
        f"{persona_voices}\n\n"
        f"---\n\n"
        f"# The event being analyzed\n"
        f"{event.to_simulation_input()}\n\n"
        f"---\n\n"
        f"# Round {round_idx}/{n_rounds_total - 1} — previous round's reactions\n"
        f"{prior_summary}\n\n"
        f"---\n\n"
        f"Each persona now updates their stance after seeing the others. Output the JSON."
    )

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


# ─────────────────────── Round runner ───────────────────────


def _coerce_reactions(
    parsed: dict[str, Any] | list[Any],
    personas: list[Persona],
) -> list[Reaction]:
    """Turn whatever the LLM returned into a list of N reactions, one per persona.

    Tolerates: {'reactions': [...]} or just [...]. Aligns by persona_id when possible,
    falls back to positional alignment, and fills missing with neutral defaults.
    """
    if isinstance(parsed, dict):
        items = parsed.get("reactions") or parsed.get("data") or []
    else:
        items = parsed

    if not isinstance(items, list):
        items = []

    by_id: dict[str, dict[str, Any]] = {}
    positional: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        pid = str(it.get("persona_id", "")).strip()
        if pid:
            by_id[pid] = it
        positional.append(it)

    reactions: list[Reaction] = []
    for i, p in enumerate(personas):
        if p.id in by_id:
            reactions.append(Reaction.from_dict(by_id[p.id], fallback_id=p.id))
        elif i < len(positional):
            d = dict(positional[i])
            d.setdefault("persona_id", p.id)
            reactions.append(Reaction.from_dict(d, fallback_id=p.id))
        else:
            # Missing persona — neutral fallback
            reactions.append(
                Reaction(
                    persona_id=p.id,
                    sentiment=0.0,
                    action_intent="observe",
                    comment="(no response, neutral fallback)",
                    confidence=0.0,
                )
            )
    return reactions


# ─────────────────────── Public entry point ───────────────────────


async def run_simulation(
    event: Event,
    personas: list[Persona],
    n_rounds: int | None = None,
    *,
    simulation_id: str | None = None,
) -> SimulationResult:
    """Run the full multi-round group simulation.

    Strategy:
        Round 0: each persona reads the event independently (1 batched LLM call)
        Rounds 1..N-1: each persona sees the previous round's reactions, updates
                       (1 batched LLM call per round)

    Total: N LLM calls, regardless of persona count.

    Args:
        event: validated Event
        personas: list of Persona, ordering matters for prompt
        n_rounds: total rounds including round 0. Defaults to settings.n_rounds.
        simulation_id: optional; defaults to a new uuid4.

    Returns:
        SimulationResult containing all rounds' reactions and metadata.
    """
    if not personas:
        raise ValueError("run_simulation requires at least 1 persona")

    n_rounds = n_rounds if n_rounds is not None else settings.n_rounds
    if n_rounds < 1:
        raise ValueError("n_rounds must be >= 1")

    sim_id = simulation_id or str(uuid.uuid4())
    rng = random.Random(settings.seed)
    cost_at_start = cost_tracker.total_cost_usd
    t0 = time.time()

    rounds: list[list[Reaction]] = []

    # ── Round 0 ─────────────────────────────────────────────
    r0_messages = _build_round_zero_prompt(event, personas)
    r0_parsed = await chat_json(
        messages=r0_messages,
        model=settings.default_model,
        temperature=settings.temperature,
        max_tokens=2500,
    )
    rounds.append(_coerce_reactions(r0_parsed, personas))

    # ── Rounds 1..N-1 ───────────────────────────────────────
    for round_idx in range(1, n_rounds):
        # For each round, show previous round in shuffled order so personas don't
        # always see the same order (mild noise injection for diversity).
        prior = list(rounds[-1])
        rng.shuffle(prior)

        rn_messages = _build_round_n_prompt(
            event=event,
            personas=personas,
            round_idx=round_idx,
            prior_round=prior,
            n_rounds_total=n_rounds,
        )
        rn_parsed = await chat_json(
            messages=rn_messages,
            model=settings.default_model,
            temperature=settings.temperature,
            max_tokens=2500,
        )
        rounds.append(_coerce_reactions(rn_parsed, personas))

    elapsed = time.time() - t0
    return SimulationResult(
        simulation_id=sim_id,
        event=event,
        personas=personas,
        rounds=rounds,
        elapsed_seconds=elapsed,
        cost_usd_at_start=cost_at_start,
        cost_usd_at_end=cost_tracker.total_cost_usd,
    )


__all__ = [
    "Reaction",
    "SimulationResult",
    "run_simulation",
]
