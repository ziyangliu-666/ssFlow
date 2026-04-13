"""Information filter: per-agent-type lens on the information feed.

The same market event produces different information contexts for different
agent types. This is the core mechanism for cognitive diversity:

- Retail: sees headlines + KOL posts, delayed research access
- Analyst: sees full filings + financial data, produces original analysis
- Quant: sees price/volume tape only, no social narrative
- Institutional: sees analyst reports + aggregate flow data
- ETF AP: sees NAV calculations, premium/discount data
- Short fund: sees risk signals, borrow cost, short interest

Each agent type gets a FilteredContext that shapes what the LLM prompt
will contain — ensuring the same event generates genuinely different
perspectives rather than everyone repeating the same talking points.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

log = logging.getLogger(__name__)


class AgentRole(str, Enum):
    """Agent role categories for information filtering."""
    RETAIL = "retail"
    KOL = "kol"
    ANALYST = "analyst"
    INSTITUTIONAL = "institutional"
    QUANT = "quant"
    STRATEGIC = "strategic"       # long-term holders (大股东, 产业资本)
    MEDIA = "media"
    POLICY = "policy"
    REGULATOR = "regulator"
    SHORT_FUND = "short_fund"
    ETF_AP = "etf_ap"
    MARKET_MAKER = "market_maker"


@dataclass
class FilteredContext:
    """The information context an agent sees after filtering.

    Each field represents a category of information. Empty string means
    the agent type doesn't have access to that category.
    """

    # What the agent sees
    event_summary: str = ""           # the core event (available to all)
    price_data: str = ""              # current price, recent path, VWAP
    social_feed: str = ""             # KOL posts, retail chatter
    analyst_reports: str = ""         # sell-side research notes
    financial_data: str = ""          # detailed filings, ratios, consensus
    order_flow_data: str = ""         # aggregate flow info (institutional only)
    macro_context: str = ""           # FX, rates, global markets
    risk_signals: str = ""            # short interest, borrow cost, margin data
    regulatory_context: str = ""      # policy statements, regulatory actions
    nav_data: str = ""                # ETF NAV, premium/discount

    # Metadata
    agent_role: str = ""
    information_delay_rounds: int = 0  # how many rounds behind real-time

    def to_prompt_section(self) -> str:
        """Render as a prompt section for the LLM."""
        sections = []
        if self.event_summary:
            sections.append(f"### Event\n{self.event_summary}")
        if self.price_data:
            sections.append(f"### Price Data\n{self.price_data}")
        if self.financial_data:
            sections.append(f"### Financial Data\n{self.financial_data}")
        if self.analyst_reports:
            sections.append(f"### Analyst Research\n{self.analyst_reports}")
        if self.social_feed:
            sections.append(f"### Social Feed\n{self.social_feed}")
        if self.order_flow_data:
            sections.append(f"### Order Flow\n{self.order_flow_data}")
        if self.macro_context:
            sections.append(f"### Macro Context\n{self.macro_context}")
        if self.risk_signals:
            sections.append(f"### Risk Signals\n{self.risk_signals}")
        if self.regulatory_context:
            sections.append(f"### Regulatory\n{self.regulatory_context}")
        if self.nav_data:
            sections.append(f"### NAV Data\n{self.nav_data}")
        return "\n\n".join(sections) if sections else "(No information available)"


# ── Access rules per agent role ──

# Each role gets access to specific information categories.
# True = has access, False = no access.
ACCESS_RULES: dict[AgentRole, dict[str, bool]] = {
    AgentRole.RETAIL: {
        "event_summary": True,
        "price_data": True,
        "social_feed": True,
        "analyst_reports": False,  # delayed access (Phase 4)
        "financial_data": False,
        "order_flow_data": False,
        "macro_context": False,
        "risk_signals": False,
        "regulatory_context": False,
        "nav_data": False,
    },
    AgentRole.KOL: {
        "event_summary": True,
        "price_data": True,
        "social_feed": True,
        "analyst_reports": True,   # KOLs read research
        "financial_data": False,
        "order_flow_data": False,
        "macro_context": False,
        "risk_signals": False,
        "regulatory_context": False,
        "nav_data": False,
    },
    AgentRole.ANALYST: {
        "event_summary": True,
        "price_data": True,
        "social_feed": False,      # analysts don't read Weibo
        "analyst_reports": True,
        "financial_data": True,    # full filings access
        "order_flow_data": False,
        "macro_context": True,
        "risk_signals": False,
        "regulatory_context": True,
        "nav_data": False,
    },
    AgentRole.INSTITUTIONAL: {
        "event_summary": True,
        "price_data": True,
        "social_feed": False,
        "analyst_reports": True,   # sell-side relationship
        "financial_data": True,
        "order_flow_data": True,   # can see aggregate flows
        "macro_context": True,
        "risk_signals": True,
        "regulatory_context": True,
        "nav_data": False,
    },
    AgentRole.QUANT: {
        "event_summary": False,    # quants don't read narratives
        "price_data": True,        # ONLY price/volume
        "social_feed": False,
        "analyst_reports": False,
        "financial_data": False,
        "order_flow_data": True,
        "macro_context": False,
        "risk_signals": False,
        "regulatory_context": False,
        "nav_data": False,
    },
    AgentRole.STRATEGIC: {
        "event_summary": True,
        "price_data": True,
        "social_feed": False,
        "analyst_reports": True,
        "financial_data": True,
        "order_flow_data": False,
        "macro_context": True,
        "risk_signals": False,
        "regulatory_context": True,
        "nav_data": False,
    },
    AgentRole.SHORT_FUND: {
        "event_summary": True,
        "price_data": True,
        "social_feed": False,
        "analyst_reports": True,
        "financial_data": True,
        "order_flow_data": True,
        "macro_context": True,
        "risk_signals": True,      # borrow cost, short interest
        "regulatory_context": True,
        "nav_data": False,
    },
    AgentRole.ETF_AP: {
        "event_summary": False,
        "price_data": True,
        "social_feed": False,
        "analyst_reports": False,
        "financial_data": False,
        "order_flow_data": False,
        "macro_context": False,
        "risk_signals": False,
        "regulatory_context": False,
        "nav_data": True,          # NAV, premium/discount
    },
}

# Default: media, policy, regulator get everything (they're info producers)
_DEFAULT_ACCESS = {k: True for k in [
    "event_summary", "price_data", "social_feed", "analyst_reports",
    "financial_data", "order_flow_data", "macro_context", "risk_signals",
    "regulatory_context", "nav_data",
]}


class InformationFilter:
    """Filters information context by agent role.

    Usage:
        filt = InformationFilter()
        ctx = filt.filter(
            agent_role=AgentRole.RETAIL,
            full_context=full_ctx,
        )
        # ctx.analyst_reports will be empty for retail
        prompt = ctx.to_prompt_section()
    """

    def filter(
        self,
        agent_role: AgentRole | str,
        full_context: dict[str, str],
        *,
        round_idx: int = 0,
    ) -> FilteredContext:
        """Filter full information context by agent role's access rules.

        Args:
            agent_role: the agent's role category
            full_context: dict mapping field names to content strings
            round_idx: current round (for delayed access)
        """
        if isinstance(agent_role, str):
            try:
                agent_role = AgentRole(agent_role.lower())
            except ValueError:
                agent_role = AgentRole.RETAIL  # safe fallback

        rules = ACCESS_RULES.get(agent_role, _DEFAULT_ACCESS)

        ctx = FilteredContext(agent_role=agent_role.value)

        for field_name, has_access in rules.items():
            if has_access and field_name in full_context:
                setattr(ctx, field_name, full_context[field_name])

        return ctx

    def classify_role(self, agent_type: str) -> AgentRole:
        """Map a persona's agent_type string to an AgentRole.

        Handles the various agent_type values from persona YAML.
        """
        mapping: dict[str, AgentRole] = {
            "retail": AgentRole.RETAIL,
            "kol": AgentRole.KOL,
            "analyst": AgentRole.ANALYST,
            "institutional": AgentRole.INSTITUTIONAL,
            "quant": AgentRole.QUANT,
            "strategic": AgentRole.STRATEGIC,
            "media": AgentRole.MEDIA,
            "policy": AgentRole.POLICY,
            "regulator": AgentRole.REGULATOR,
            "short_fund": AgentRole.SHORT_FUND,
            "etf_ap": AgentRole.ETF_AP,
            "market_maker": AgentRole.MARKET_MAKER,
        }
        return mapping.get(agent_type.lower(), AgentRole.RETAIL)


__all__ = [
    "ACCESS_RULES",
    "AgentRole",
    "FilteredContext",
    "InformationFilter",
]
