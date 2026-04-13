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
    # TWAP execution plan config from LLM tool call. None = immediate execution.
    execution_plan: dict | None = None


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
        execution_plan: dict | None = None,
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
                    execution_plan=dict(execution_plan) if execution_plan else None,
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
    # Short-seller personas (融券做空基金, 宏观对冲空头, 主动多空) default
    # sells to the margin pool so the trading layer treats them as borrow-
    # and-short orders rather than closing nonexistent long inventory.
    # Without this the freeform path hardcodes pool=holdings_in_target and
    # a short fund that starts flat produces net_flow=0 — the engine gets
    # regime direction wrong whenever shorts should be the marginal price
    # setter.
    #
    # Whitelist by canonical role — NOT by leverage_max. Using leverage_max>0
    # as a proxy was overbroad: retail_short_term_chaser / retail_passive_
    # holder / mutual_fund_active_pm all run some margin for buying on
    # dip, but they are long-only funds and should default sells to their
    # holdings, not borrow-to-short. Only roles that explicitly advertise
    # shorting capability qualify for the margin default. An LLM can still
    # force pool=margin on any persona by passing the kwarg explicitly.
    persona_role = (getattr(persona, "role", "") or "").lower()
    _SHORT_CAPABLE_ROLES = {
        "short_seller",
        "active_long_short",
        "long_short",
        "hedge_fund_short",
    }
    is_short_persona = persona_role in _SHORT_CAPABLE_ROLES

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
                pool_label = a.get("pool") or ("cash" if side == "buy" else "holdings")
                examples.append(f"{side} {frac:.0%} of {pool_label}")
        hints = f"\nExample decisions for reference: {', '.join(examples)}\n"

    def submit_trading_decision(
        side: str = "hold",
        quantity_pct: float = 0.0,
        rationale: str = "",
        instrument: str = "",
        pool: str = "",
        execution_rounds: int = 0,
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
          - pool: "cash" (default for buy), "holdings_in_target" (default for
            sell long inventory), or "margin" (borrow to short — only valid
            on the sell side, requires margin access).
          - execution_rounds: split this trade over N rounds (TWAP). E.g.,
            execution_rounds=4 executes 25% per round over 4 rounds. Use for
            large positions to minimize market impact. Default 0 = execute all
            immediately in this round.

        Short-seller personas (融券做空基金, 宏观空头对冲): omit `pool`
        and the engine will automatically route your sell to the margin
        borrow path so you can open a naked short against a bearish event
        even when you start with zero inventory.

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

        # ── Rationale-side consistency guard ──────────────────────────
        # 1) Hold override: if rationale says "观望" but side is buy/sell,
        #    force to hold.
        # 2) Direction flip: if rationale clearly says "进入/买入" but
        #    side is "sell" (or vice versa), flip to match the rationale.
        #    This catches the LLM+sampling failure where text says
        #    "进入头寸" but distribution sampling produced sell.
        if side_clean != "hold" and rationale:
            _r = rationale.lower()
            _hold_phrases = ("观望", "保持现有", "维持现有", "暂不操作", "暂不交易", "不做操作")
            _buy_phrases = ("进入头寸", "增持", "加仓", "买入", "入场", "进入",
                            "布局", "配置", "enter", "buy")
            _sell_phrases = ("减持", "减仓", "卖出", "降低敞口", "离场", "做空",
                             "锁定收益", "锁定利润", "获利了结", "exit", "sell")
            _text_says_hold = any(p in _r for p in _hold_phrases)
            _text_says_buy = any(p in _r for p in _buy_phrases)
            _text_says_sell = any(p in _r for p in _sell_phrases)
            if _text_says_hold and not _text_says_buy and not _text_says_sell:
                log.info(
                    "Rationale-side override for %s: side=%s→hold "
                    "(rationale says hold: '%s')",
                    persona_id, side_clean, rationale[:80],
                )
                side_clean = "hold"
                qty = 0.0
            elif side_clean == "sell" and _text_says_buy and not _text_says_sell:
                log.info(
                    "Rationale-side flip for %s: sell→buy (rationale: '%s')",
                    persona_id, rationale[:80],
                )
                side_clean = "buy"
            elif side_clean == "buy" and _text_says_sell and not _text_says_buy:
                log.info(
                    "Rationale-side flip for %s: buy→sell (rationale: '%s')",
                    persona_id, rationale[:80],
                )
                side_clean = "sell"

        # ── Hard role constraint: sell-only personas cannot buy ──────
        # If the persona's action_space has no buy actions, the LLM must
        # not produce a buy side. This catches cases where the LLM
        # ignores the voice_prompt (e.g., lockup seller "增持25%").
        if side_clean == "buy" and persona.sandbox and persona.sandbox.action_space:
            _has_buy_action = any(
                a.get("side") == "buy" for a in persona.sandbox.action_space
            )
            if not _has_buy_action:
                log.info(
                    "Role constraint override for %s: buy→hold "
                    "(action_space has no buy actions)",
                    persona_id,
                )
                side_clean = "hold"
                qty = 0.0

        inst = str(instrument).strip() if instrument else None
        pool_raw = str(pool).strip().lower() if pool else ""

        # Resolve pool from: explicit kwarg → persona role default → side default
        if pool_raw in ("cash", "holdings_in_target", "holdings", "margin"):
            resolved_pool = (
                "holdings_in_target" if pool_raw == "holdings" else pool_raw
            )
        elif side_clean == "buy":
            resolved_pool = "cash"
        elif side_clean == "sell":
            resolved_pool = "margin" if is_short_persona else "holdings_in_target"
        else:
            resolved_pool = "none"

        # Parse execution_rounds into an execution plan config
        try:
            exec_rounds = max(0, int(execution_rounds))
        except (TypeError, ValueError):
            exec_rounds = 0
        exec_plan = None
        if exec_rounds > 1 and side_clean != "hold":
            exec_plan = {"n_rounds": exec_rounds}

        # Store as a special __freeform__ distribution
        collector.add(
            persona_id=persona_id,
            distribution={"__freeform__": 1.0},
            rationale=str(rationale or ""),
            raw_args={
                "side": side_clean,
                "quantity_pct": qty,
                "pool": resolved_pool,
            },
            instrument=inst,
            execution_plan=exec_plan,
        )

        inst_label = inst or "primary"
        if side_clean == "hold":
            return f"Decision submitted for {persona_id}: HOLD {inst_label}. Rationale: {str(rationale)[:120]}"
        pool_label = {
            "cash": "cash",
            "holdings_in_target": "holdings",
            "margin": "margin (borrow-to-short)",
            "none": "none",
        }.get(resolved_pool, resolved_pool)
        return (
            f"Decision submitted for {persona_id}: {side_clean.upper()} "
            f"{qty:.0%} of {pool_label} in {inst_label}. "
            f"Rationale: {str(rationale)[:120]}"
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
    # Default hold action for fallback when LLM omits action_distribution
    hold_action = next(
        (a["name"] for a in persona.sandbox.action_space if a.get("side") == "none"),
        action_names[0],
    )

    def submit_order_distribution(
        action_distribution: dict = None,
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
        # LLM sometimes omits action_distribution entirely — default to hold
        if action_distribution is None:
            log.warning(
                "persona %s: submit_order_distribution called without "
                "action_distribution, defaulting to hold",
                persona_id,
            )
            action_distribution = {hold_action: 1.0}

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
