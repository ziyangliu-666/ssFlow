"""Intent resolver: LLM call -> StructuredIntent.

Takes an agent's information context and portfolio state, calls the LLM,
and parses the response into a structured intent. The intent describes
WHAT the agent wants to do and WHY, without concern for whether it's
actually executable (that's the constraint solver's job).

Key difference from legacy `submit_order_distribution`:
- Returns a single intent, not a probability distribution
- Includes urgency and price preference, not just side + size
- Includes the agent's rationale (for reports)
- Portfolio-aware: the prompt includes the agent's specific state
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

log = logging.getLogger(__name__)


class Urgency(str, Enum):
    """How urgently the agent wants to execute."""
    IMMEDIATE = "immediate"   # market order — wants fills NOW
    PATIENT = "patient"       # limit order — willing to wait for price
    TWAP = "twap"            # slice over multiple rounds
    NONE = "none"            # hold — no trading intent


@dataclass(frozen=True)
class StructuredIntent:
    """What an agent wants to do, parsed from LLM output.

    This is INTENT, not an order. The constraint solver checks feasibility
    and the order generator converts it to actual LOB orders.
    """

    persona_id: str
    agent_id: str
    side: str                    # "buy", "sell", "hold", "short"
    size_fraction: float         # 0.0-1.0: fraction of available pool
    urgency: Urgency = Urgency.PATIENT
    price_preference: float = 0.0  # offset from last price (+ = willing to pay more)
    rationale: str = ""          # LLM's reasoning (for reports)
    target_ticker: str = ""      # which instrument (empty = primary)
    twap_rounds: int = 1         # for TWAP: how many rounds to slice over
    confidence: float = 0.5      # 0-1: how confident in the direction

    @property
    def is_active(self) -> bool:
        """True if this intent involves trading (not just holding)."""
        return self.side in ("buy", "sell", "short") and self.size_fraction > 0


# ── Intent parsing from LLM output ──


def parse_intent_from_json(
    raw: dict[str, Any],
    persona_id: str,
    agent_id: str,
) -> StructuredIntent:
    """Parse a StructuredIntent from LLM JSON output.

    Expected LLM response format:
    {
        "side": "buy" | "sell" | "hold" | "short",
        "size_fraction": 0.0-1.0,
        "urgency": "immediate" | "patient" | "twap" | "none",
        "price_preference": -0.01 to +0.01 (offset from last as fraction),
        "rationale": "...",
        "target_ticker": "002594.SZ",
        "twap_rounds": 1-5,
        "confidence": 0.0-1.0
    }

    Gracefully handles missing/malformed fields with safe defaults.
    """
    side = str(raw.get("side", "hold")).lower().strip()
    if side not in ("buy", "sell", "hold", "short"):
        log.warning("Unknown side %r from %s, defaulting to hold", side, persona_id)
        side = "hold"

    size_frac = float(raw.get("size_fraction", 0.0))
    size_frac = max(0.0, min(1.0, size_frac))

    try:
        urgency = Urgency(str(raw.get("urgency", "patient")).lower())
    except ValueError:
        urgency = Urgency.PATIENT

    price_pref = float(raw.get("price_preference", 0.0))
    price_pref = max(-0.05, min(0.05, price_pref))

    rationale = str(raw.get("rationale", ""))
    target = str(raw.get("target_ticker", ""))
    twap = max(1, int(raw.get("twap_rounds", 1)))
    confidence = max(0.0, min(1.0, float(raw.get("confidence", 0.5))))

    if side == "hold":
        size_frac = 0.0
        urgency = Urgency.NONE

    return StructuredIntent(
        persona_id=persona_id,
        agent_id=agent_id,
        side=side,
        size_fraction=size_frac,
        urgency=urgency,
        price_preference=price_pref,
        rationale=rationale,
        target_ticker=target,
        twap_rounds=twap,
        confidence=confidence,
    )


def parse_intent_from_text(
    text: str,
    persona_id: str,
    agent_id: str,
) -> StructuredIntent:
    """Parse a StructuredIntent from free-form LLM text.

    Tries JSON extraction first, falls back to keyword heuristics.
    """
    # Try to find JSON in the text
    for start, end in [("{", "}"), ("```json", "```")]:
        idx = text.find(start)
        if idx >= 0:
            end_idx = text.rfind(end) + len(end)
            candidate = text[idx:end_idx].strip()
            if candidate.startswith("```"):
                candidate = candidate[7:-3].strip()
            try:
                data = json.loads(candidate)
                if isinstance(data, dict):
                    return parse_intent_from_json(data, persona_id, agent_id)
            except (json.JSONDecodeError, ValueError):
                continue

    # Keyword fallback
    lower = text.lower()
    if any(w in lower for w in ["买入", "buy", "加仓", "建仓"]):
        side = "buy"
    elif any(w in lower for w in ["卖出", "sell", "减仓", "清仓"]):
        side = "sell"
    elif any(w in lower for w in ["做空", "short"]):
        side = "short"
    else:
        side = "hold"

    # Try to extract size
    size = 0.0
    if side != "hold":
        for pct in [0.1, 0.2, 0.3, 0.5, 1.0]:
            if f"{int(pct*100)}%" in text:
                size = pct
                break
        if size == 0.0:
            size = 0.1  # default small position

    return StructuredIntent(
        persona_id=persona_id,
        agent_id=agent_id,
        side=side,
        size_fraction=size,
        urgency=Urgency.PATIENT,
        rationale=text[:200],
    )


# ── Prompt templates ──


INTENT_SYSTEM_PROMPT = """\
You are a trading decision engine. Given the market context and your portfolio state,
output a JSON trading decision.

Output format (JSON only, no other text):
{{
  "side": "buy" | "sell" | "hold" | "short",
  "size_fraction": 0.0 to 1.0,
  "urgency": "immediate" | "patient" | "twap" | "none",
  "price_preference": -0.02 to +0.02,
  "rationale": "brief explanation",
  "confidence": 0.0 to 1.0
}}

Rules:
- size_fraction is fraction of available cash (buy) or holdings (sell)
- price_preference is offset from current price as fraction (+0.01 = willing to pay 1% more)
- urgency "immediate" = market order, "patient" = limit order, "twap" = multi-round
- confidence 0 = random guess, 1 = very sure
"""


def build_intent_prompt(
    persona_voice: str,
    market_context: str,
    portfolio_state: str,
    information_summary: str,
) -> list[dict[str, str]]:
    """Build the LLM prompt messages for intent resolution.

    Returns a list of message dicts suitable for the OpenAI chat API.
    """
    return [
        {"role": "system", "content": f"{INTENT_SYSTEM_PROMPT}\n\n{persona_voice}"},
        {"role": "user", "content": (
            f"## Market Context\n{market_context}\n\n"
            f"## Your Portfolio\n{portfolio_state}\n\n"
            f"## Information\n{information_summary}"
        )},
    ]


class IntentResolver:
    """Resolves agent intent from LLM calls.

    This is a stateless helper — it builds prompts and parses responses.
    The actual LLM call is done by the caller (engine/orchestrator) to
    allow batching and cost control.
    """

    def build_prompt(
        self,
        persona_voice: str,
        market_context: str,
        portfolio_state: str,
        information_summary: str,
    ) -> list[dict[str, str]]:
        """Build the prompt for one agent's intent resolution."""
        return build_intent_prompt(
            persona_voice=persona_voice,
            market_context=market_context,
            portfolio_state=portfolio_state,
            information_summary=information_summary,
        )

    def parse_response(
        self,
        response_text: str,
        persona_id: str,
        agent_id: str,
    ) -> StructuredIntent:
        """Parse an LLM response into a StructuredIntent."""
        return parse_intent_from_text(response_text, persona_id, agent_id)

    def hold_intent(self, persona_id: str, agent_id: str, reason: str = "") -> StructuredIntent:
        """Create a hold intent (no trading action)."""
        return StructuredIntent(
            persona_id=persona_id,
            agent_id=agent_id,
            side="hold",
            size_fraction=0.0,
            urgency=Urgency.NONE,
            rationale=reason or "No trading action this round",
        )


__all__ = [
    "IntentResolver",
    "StructuredIntent",
    "Urgency",
    "build_intent_prompt",
    "parse_intent_from_json",
    "parse_intent_from_text",
]
