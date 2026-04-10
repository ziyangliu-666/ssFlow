"""Domain-specific FunctionTools for non-trader agents.

Each tool is a CAMEL FunctionTool injected into the OASIS SocialAgent
for a specific entity_role. When the LLM calls the tool, the closure
captures the action into the ActionCollector.

Phase 3 tools:
  - make_announce_tool:  company_ir agents — announce earnings, guidance, buybacks
  - make_regulate_tool:  regulator/policy agents — issue inquiry, halt, restrict
  - make_publish_tool:   analyst agents — publish research note, change rating

These tools give non-trader agents the ability to take domain-specific
actions beyond the 21 OASIS social actions (create_post, like, repost, etc.).
The key difference: these tools produce TYPED actions that the engine
dispatches mechanically, while social posts are just text.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .action_collector import ActionCollector
from .persona import Persona

if TYPE_CHECKING:
    from camel.toolkits import FunctionTool


def make_announce_tool(
    persona: Persona,
    collector: ActionCollector,
) -> Any:
    """Build an announcement tool for company_ir agents.

    The LLM can announce: earnings updates, guidance changes, buyback
    programs, management changes, strategic pivots. The announcement
    gets injected into the OASIS social feed as a high-authority post.
    """
    persona_id = persona.id
    agent_id = persona.entity_id or persona_id

    def announce(
        headline: str,
        detail: str = "",
        announcement_type: str = "general",
    ) -> str:
        """Publish an official company announcement.

        As the company's IR representative, you can make official announcements
        that the entire market will see. These carry high authority.

        Args:
            headline: 30-80 character announcement headline (will be the post title)
            detail: 50-200 character supporting detail (optional)
            announcement_type: one of "earnings_update", "guidance_change",
                "buyback", "management_change", "strategic", "general"

        Use this when significant company developments warrant market communication.
        Do NOT announce every round — only when you have material information.
        """
        text = f"[公司公告] {headline}"
        if detail:
            text += f"\n{detail}"

        collector.add(
            agent_id=agent_id,
            persona_id=persona_id,
            action_type="announce",
            payload={
                "text": text,
                "content_type": "company_announcement",
                "announcement_type": announcement_type,
                "authority_weight": 0.90,
            },
        )
        return f"Announcement published: {headline[:80]}"

    from camel.toolkits import FunctionTool
    return FunctionTool(announce)


def make_regulate_tool(
    persona: Persona,
    collector: ActionCollector,
) -> Any:
    """Build a regulation tool for regulator/policy agents.

    The LLM can issue: inquiries, trading halts, short-selling restrictions,
    window guidance. These create high-authority announcements AND can
    impose mechanical constraints on trader agents.
    """
    persona_id = persona.id
    agent_id = persona.entity_id or persona_id

    def issue_regulatory_action(
        action_type: str,
        target: str = "",
        detail: str = "",
    ) -> str:
        """Issue a regulatory action as a government/regulatory body.

        Args:
            action_type: one of:
                "inquiry" — send a formal inquiry to a company (关注函)
                "window_guidance" — issue informal guidance to market (窗口指导)
                "restriction" — impose trading restriction (限制做空/限售)
                "statement" — official policy statement (政策声明)
            target: which company/ticker this targets (e.g., "比亚迪")
            detail: 50-200 character explanation of the action

        Use sparingly — regulatory actions are rare and high-impact.
        Only act when market conditions genuinely warrant intervention.
        """
        text_prefix = {
            "inquiry": "[监管] ",
            "window_guidance": "[窗口指导] ",
            "restriction": "[监管限制] ",
            "statement": "[政策] ",
        }.get(action_type, "[监管] ")

        text = f"{text_prefix}{detail or action_type}"
        if target:
            text = f"{text_prefix}{target}: {detail or action_type}"

        collector.add(
            agent_id=agent_id,
            persona_id=persona_id,
            action_type="regulate",
            payload={
                "text": text,
                "content_type": "policy_statement",
                "regulation_type": action_type,
                "target": target,
                "authority_weight": 0.95,
            },
        )
        return f"Regulatory action issued: {action_type} — {detail[:80]}"

    from camel.toolkits import FunctionTool
    return FunctionTool(issue_regulatory_action)


def make_publish_tool(
    persona: Persona,
    collector: ActionCollector,
) -> Any:
    """Build a research publication tool for analyst agents.

    The LLM can publish: research notes, rating changes, target price
    updates, sector reports.
    """
    persona_id = persona.id
    agent_id = persona.entity_id or persona_id

    def publish_research(
        title: str,
        rating: str = "",
        summary: str = "",
    ) -> str:
        """Publish a research note or rating change.

        Args:
            title: 30-80 character research note title
            rating: "buy" | "hold" | "sell" | "outperform" | "underperform" | ""
                Leave empty if not changing rating.
            summary: 50-200 character key findings summary

        Publish when you have a substantive view to share based on new
        information. Do NOT publish generic summaries every round.
        """
        text = f"[研报] {title}"
        if rating:
            text += f" (评级: {rating})"
        if summary:
            text += f"\n{summary}"

        collector.add(
            agent_id=agent_id,
            persona_id=persona_id,
            action_type="publish_research",
            payload={
                "text": text,
                "content_type": "research_note",
                "rating": rating,
                "authority_weight": 0.80,
            },
        )
        return f"Research published: {title[:80]}"

    from camel.toolkits import FunctionTool
    return FunctionTool(publish_research)


def make_create_policy_tool(
    persona: Persona,
    collector: ActionCollector,
) -> Any:
    """Build a tool that lets any agent create a new Policy on itself.

    Traders can set stop-losses, regulators can self-impose review
    schedules, companies can commit to buyback programs. The created
    policy is validated and attached to the agent's policy list by
    the engine.
    """
    persona_id = persona.id
    agent_id = persona.entity_id or persona_id

    def create_rule(
        name: str,
        trigger: str,
        action_type: str,
        side: str = "",
        quantity_pct: float = 0.0,
        text: str = "",
        cooldown: int = 2,
    ) -> str:
        """Create a new automatic rule for yourself.

        Once created, this rule evaluates every round automatically.
        When the trigger condition is met, the action fires.

        Args:
            name: short name for the rule, e.g. "止损20%", "自动减仓"
            trigger: condition expression, e.g. "avg_position_pct > 0.5"
                or "price_change_pct < -20.0". Must use state variables
                you can see in your 处境 block.
            action_type: "trade" (force buy/sell) or "announce" (post to feed)
            side: for trade actions: "buy" or "sell"
            quantity_pct: for trade actions: fraction 0.0-1.0
            text: for announce actions: announcement text
            cooldown: minimum rounds between activations (default 2)

        Example: to set a -20% stop-loss that sells everything:
            create_rule(name="止损", trigger="price_change_pct < -20.0",
                       action_type="trade", side="sell", quantity_pct=1.0)
        """
        collector.add(
            agent_id=agent_id,
            persona_id=persona_id,
            action_type="create_policy",
            payload={
                "name": str(name),
                "trigger": str(trigger),
                "action_type": str(action_type),
                "side": str(side),
                "quantity_pct": float(quantity_pct),
                "text": str(text),
                "cooldown": int(cooldown),
            },
        )
        return f"Rule created: '{name}' — when {trigger}, {action_type}"

    from camel.toolkits import FunctionTool
    return FunctionTool(create_rule)


# ─────────────────────── Tool Assignment ───────────────────────

# Map entity_role → tool factory
ROLE_TOOL_MAP: dict[str, Any] = {
    "company_ir": make_announce_tool,
    "regulator": make_regulate_tool,
    "policy": make_regulate_tool,      # policy makers use same regulate tool
    "analyst": make_publish_tool,
}


def tool_for_role(
    persona: Persona,
    collector: ActionCollector,
) -> Any:
    """Return the appropriate domain tool for a non-trader persona, or None."""
    factory = ROLE_TOOL_MAP.get(persona.entity_role)
    if factory is None:
        return None
    return factory(persona, collector)


__all__ = [
    "ROLE_TOOL_MAP",
    "make_announce_tool",
    "make_create_policy_tool",
    "make_publish_tool",
    "make_regulate_tool",
    "tool_for_role",
]
