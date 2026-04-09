"""Submit-order tool for the OASIS unified-decision path (Phase II).

This module is the core of the "one brain, one decision" architecture. It lets
each trader persona's OASIS `SocialAgent` see an extra tool alongside the 21
built-in social actions (create_post, repost, like, follow, ...), called
`submit_order_distribution`. When the LLM picks that tool, CAMEL's `ChatAgent`
automatically invokes it — and our closure-bound function captures the order
intent into a per-sim `OrderCollector`.

After OASIS's `env.step()` finishes the social round, `oasis_engine` drains
the collector, maps each captured distribution to the matching agent
population, and runs `trading_layer.apply_distribution_to_agent_pop` to produce
the per-class flow. Net flows sum → Kyle → price updates → posted back into
OASIS as a `__market__` post.

Why this matters architecturally:

  - **One LLM call per trader per round**, not two. The trader's social actions
    (post / repost / follow) and trading actions come from a SINGLE CAMEL
    `ChatAgent.astep()` call, using the same memory, the same feed context.
  - **No fork of OASIS.** The `SocialAgent.__init__` already accepts a `tools`
    parameter and explicitly handles non-social tool calls in
    `perform_action_by_llm` (line 145: `if action_name not in ALL_SOCIAL_ACTIONS`).
    Custom tools are an officially-supported extension mechanism.
  - **Cross-round memory continuity**: the tool call + its result go into the
    trader's CAMEL agent memory, so next round the LLM can see "I sold 30% last
    round after reading KOL X's bearish post" — giving coherent behavior over
    time.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

from camel.toolkits import FunctionTool

from .persona import Persona


log = logging.getLogger(__name__)


@dataclass
class PendingOrder:
    """One order-intent captured from a trader's LLM tool call."""

    persona_id: str
    distribution: dict[str, float]
    rationale: str
    # Round index when this order was collected. Set by the engine via
    # `OrderCollector.set_round(round_idx)` before each OASIS env.step().
    round_idx: int = 0
    raw_args: dict[str, Any] = field(default_factory=dict)
    # Multi-instrument: which ticker this order targets. None = primary/default.
    instrument: str | None = None


class OrderCollector:
    """Thread-safe per-sim collector for trader order intents.

    One instance per `run_simulation` call. Passed to
    `oasis_persona_adapter.build_agent_graph` so each trader's submit_order
    tool closure captures a reference to the same instance. After each
    OASIS `env.step()` the engine calls `drain()` to get all the orders
    collected during that step and apply them via Kyle.

    Thread safety: OASIS runs agents concurrently via asyncio, but the
    tool-call callback fires synchronously inside each agent's CAMEL step.
    Multiple callbacks can land in parallel, so the add path takes a lock.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: list[PendingOrder] = []
        self._current_round: int = 0

    def set_round(self, round_idx: int) -> None:
        """Called by the engine before each `env.step()` so newly-added orders
        get tagged with the right round index."""
        with self._lock:
            self._current_round = round_idx

    def add(
        self,
        persona_id: str,
        distribution: dict[str, float],
        rationale: str = "",
        raw_args: dict[str, Any] | None = None,
        instrument: str | None = None,
    ) -> None:
        with self._lock:
            self._pending.append(
                PendingOrder(
                    persona_id=persona_id,
                    distribution=dict(distribution),
                    rationale=rationale,
                    round_idx=self._current_round,
                    raw_args=dict(raw_args or {}),
                    instrument=instrument,
                )
            )

    def drain(self) -> list[PendingOrder]:
        """Return and clear all pending orders. Called by the engine after
        each `env.step()` to process what was submitted during that step."""
        with self._lock:
            out = list(self._pending)
            self._pending.clear()
            return out

    def __len__(self) -> int:
        with self._lock:
            return len(self._pending)


def make_freeform_trading_tool(
    persona: Persona,
    collector: OrderCollector,
) -> FunctionTool:
    """Build a free-form `submit_trading_decision` tool for the Entity Sandbox.

    Unlike `make_submit_order_tool` (which constrains the LLM to a fixed
    action_space), this tool lets the LLM specify any side + quantity_pct.
    The entire persona class executes the same action.

    The tool result goes into agent memory, providing cross-round continuity.
    """
    persona_id = persona.id

    # Build example hints from action_space if it exists (guidance, not constraint)
    hints = ""
    if persona.sandbox is not None and persona.sandbox.action_space:
        examples = []
        for a in persona.sandbox.action_space:
            side = a.get("side", "none")
            if side == "none":
                examples.append("hold")
            else:
                frac = a.get("fraction", 0)
                pool_label = "cash" if a.get("pool") == "cash" else "holdings"
                examples.append(f"{side} {frac:.0%} of {pool_label}")
        hints = f"\nExample decisions for reference: {', '.join(examples)}\n"

    def submit_trading_decision(
        side: str,
        quantity_pct: float = 0.0,
        rationale: str = "",
        instrument: str = "",
    ) -> str:
        """Submit a trading decision for this persona class this round.

        You decide EXACTLY what to do — no dropdown menu, no fixed options.
        Specify:
          - side: "buy", "sell", or "hold"
          - quantity_pct: what fraction (0.0 to 1.0) of available cash (for buy)
            or holdings (for sell) to deploy. 0.37 means 37%.
          - rationale: 50-150 char explanation referencing your 处境 and feed
          - instrument: which ticker to trade (e.g. "300750"). If omitted,
            trades the primary instrument.

        Your decision applies to the ENTIRE class of participants you represent.
        You can call this tool multiple times per round to trade different instruments.
        """
        side_clean = str(side).strip().lower()
        try:
            qty = max(0.0, min(1.0, float(quantity_pct)))
        except (TypeError, ValueError):
            qty = 0.0

        if side_clean == "hold" or qty <= 0:
            side_clean = "hold"
            qty = 0.0

        inst = str(instrument).strip() if instrument else None

        # Store as a special __freeform__ distribution
        collector.add(
            persona_id=persona_id,
            distribution={"__freeform__": 1.0},
            rationale=str(rationale or ""),
            raw_args={
                "side": side_clean,
                "quantity_pct": qty,
                "pool": "cash" if side_clean == "buy" else "holdings_in_target",
            },
            instrument=inst,
        )

        inst_label = inst or "primary"
        if side_clean == "hold":
            return f"Decision submitted for {persona_id}: HOLD {inst_label}. Rationale: {str(rationale)[:120]}"
        return (
            f"Decision submitted for {persona_id}: {side_clean.upper()} "
            f"{qty:.0%} of {'cash' if side_clean == 'buy' else 'holdings'} "
            f"in {inst_label}. Rationale: {str(rationale)[:120]}"
        )

    if hints:
        submit_trading_decision.__doc__ += hints

    return FunctionTool(submit_trading_decision)


def make_submit_order_tool(
    persona: Persona,
    collector: OrderCollector,
) -> FunctionTool:
    """Build a `submit_order_distribution` tool bound to one trader persona.

    Each trader's `SocialAgent` gets its own instance of this tool with
    `persona.id` captured in a closure, so when the LLM calls
    `submit_order_distribution(...)`, the collector knows which persona
    submitted it.

    The returned `FunctionTool` is a CAMEL wrapper that exposes the inner
    function as an OpenAI function-calling tool. The OpenAI schema (name,
    description, argument types) is auto-generated from the function's
    signature + docstring, so the docstring IS the LLM's view of the tool.

    Args:
        persona: must have `sandbox` config (raises if not)
        collector: the per-sim OrderCollector instance

    Returns:
        A `FunctionTool` wrapping a closure that submits to the collector.
    """
    if persona.sandbox is None:
        raise ValueError(
            f"make_submit_order_tool: persona '{persona.id}' has no sandbox "
            f"config; only traders get a submit_order tool"
        )

    persona_id = persona.id
    action_names = [a["name"] for a in persona.sandbox.action_space]
    action_names_str = ", ".join(action_names)

    def submit_order_distribution(
        action_distribution: dict,
        rationale: str = "",
    ) -> str:
        """Submit a class-wide trading action distribution for this round.

        Call this tool when your persona class has decided how to trade based
        on the social feed you just observed. You're speaking for an entire
        class of market participants (e.g., "retail short-term chasers"), so
        your decision is a PROBABILITY DISTRIBUTION over the class's available
        actions — different members of the class do different things, and the
        distribution reflects the true spread of behaviors.

        Args:
            action_distribution: A dict mapping action names to probabilities
                (values should sum to ~1.0). Valid action names for this
                class: [see persona's action_space].
                Example: {"hold": 0.3, "panic_sell_50pct": 0.6, "fomo_buy_30pct": 0.1}
            rationale: 50-150 character explanation of WHY this class is
                taking this distribution, referencing specific observations
                from the social feed when possible.

        Returns:
            Confirmation string. This goes into your agent memory, so next
            round you'll remember what your class did and why.
        """
        # Accept either a dict or a string-encoded dict (some LLMs do this)
        if isinstance(action_distribution, str):
            import json as _json
            try:
                action_distribution = _json.loads(action_distribution)
            except Exception:
                action_distribution = {}

        if not isinstance(action_distribution, dict):
            action_distribution = {}

        collector.add(
            persona_id=persona_id,
            distribution=action_distribution,
            rationale=str(rationale or ""),
            raw_args={"action_distribution": action_distribution, "rationale": rationale},
        )

        dist_summary = ", ".join(
            f"{k}={v:.0%}" for k, v in action_distribution.items() if v > 0
        ) or "(empty)"
        return (
            f"Order submitted for {persona_id}. Distribution: {dist_summary}. "
            f"Rationale: {str(rationale)[:120]}"
        )

    # Inject the valid action names into the docstring so the LLM sees which
    # names are acceptable for THIS particular persona.
    submit_order_distribution.__doc__ = submit_order_distribution.__doc__.replace(
        "[see persona's action_space]",
        action_names_str,
    )

    return FunctionTool(submit_order_distribution)


__all__ = [
    "OrderCollector",
    "PendingOrder",
    "make_freeform_trading_tool",
    "make_submit_order_tool",
]
