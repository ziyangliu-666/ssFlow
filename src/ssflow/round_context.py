"""Per-round context stash for OASIS agents — dependency-free helpers.

These helpers used to live in ``oasis_persona_adapter`` but were pulled
out so tests (and other lightweight callers) can import the stash logic
without also dragging in ``camel`` / ``oasis`` at module load time. The
adapter re-exports them so existing imports keep working.

The stash mechanism exists because OASIS/CAMEL bakes ``user_info.profile``
into the system message exactly once at ``SocialAgent.__init__`` and
ignores every later mutation. Writing per-round context onto the agent
instance and prepending it in the patched ``perform_action_by_llm`` is
the round-scoped alternative.
"""

from __future__ import annotations

from typing import Any


__all__ = [
    "set_round_context",
    "clear_round_context",
]


def set_round_context(
    agent: Any,
    *,
    time_ctx: str = "",
    conviction_ctx: str = "",
    pub_effects_ctx: str = "",
    terminal_risk_ctx: str = "",
) -> None:
    """Stash per-round context on an agent for the patched action loop to
    pick up. The patched ``perform_action_by_llm`` prepends these strings
    to the user instruction on every step.

    Only non-empty values overwrite existing entries — empty strings are
    no-ops. Use :func:`clear_round_context` between rounds if you need a
    hard reset, otherwise conviction_ctx and pub_effects_ctx can survive
    from one round into the next unintentionally.
    """
    ctx = getattr(agent, "_ssflow_round_context", None) or {}
    if time_ctx:
        ctx["time_ctx"] = time_ctx
    if conviction_ctx:
        ctx["conviction_ctx"] = conviction_ctx
    if pub_effects_ctx:
        ctx["pub_effects_ctx"] = pub_effects_ctx
    if terminal_risk_ctx:
        ctx["terminal_risk_ctx"] = terminal_risk_ctx
    agent._ssflow_round_context = ctx


def clear_round_context(agent: Any) -> None:
    """Reset per-round context on an agent — call between rounds when you
    want to drop stale narratives before the next round populates new
    ones. Safe to call on an agent that was never set."""
    if hasattr(agent, "_ssflow_round_context"):
        agent._ssflow_round_context = {}
