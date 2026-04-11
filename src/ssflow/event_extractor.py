"""Stage 0 — Event extraction from free-form input.

The user provides ONE thing: a piece of text (URL, news headline, short
description of intent). The agent does the rest:
  - Identifies the market (ashare / us-equity / crude-oil / ...)
  - Identifies the instrument (ticker, asset, futures contract)
  - Classifies the event type (earnings / opec / supply_disruption / ...)
  - Extracts the date (or defaults to today)
  - Searches the web for prior consensus / recent price action / sector context
  - Returns an EventProposal that the user can review + edit + confirm

Price + ADV are NOT looked up here — those are populated per instrument
by the distillation step that builds the InstrumentUniverse.

Architecture (3 stages):

  Stage 0a — CLASSIFY    1 LLM call: parse input → market + instrument + event_type
  Stage 0b — RESEARCH    Web search + fetch for the identified instrument
  Stage 0c — SYNTHESIZE  1 LLM call: produce EventProposal from corpus

Cost per extraction: ~$0.02-0.04
Wall clock: ~20-40 seconds
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from .event import VALID_EVENT_TYPES, Event
from .llm_client import JsonChatResult, chat_json
from .persona_factory import (
    KNOWN_MARKETS,
    fetch_corpus,
    now_utc8_date,
    search_web,
)


log = logging.getLogger(__name__)


# ─────────────────────── Dataclasses ───────────────────────


@dataclass
class EventProposal:
    """Proposed Event with auto-extracted fields + per-field confidence.

    The user reviews this in the web UI / CLI, edits any field, and then
    confirms. The confirmed proposal is converted to an Event and runs
    through the sandbox simulation.

    Price/ADV/currency are NOT on the proposal — they live on the
    InstrumentUniverse built by distillation.
    """

    # Required fields (matching Event)
    ticker: str
    instrument: str            # human-readable display name
    market: str                # slug
    event_type: str
    event_date: str            # YYYY-MM-DD
    event_text: str

    # Optional context (auto-filled from web search)
    prior_consensus: str = ""
    recent_price_action: str = ""
    sector_context: str = ""

    # Per-field confidence (0-1) — surfaced to user so they know what to verify
    confidence: dict[str, float] = field(default_factory=dict)

    # Recommended persona pack path
    suggested_personas_path: str | None = None

    # Audit trail
    extracted_at: str = ""
    raw_input: str = ""
    sources_consulted: list[str] = field(default_factory=list)
    extraction_warnings: list[str] = field(default_factory=list)

    def to_event(self) -> Event:
        """Convert to a runnable Event after user confirmation."""
        return Event(
            ticker=self.ticker,
            event_text=self.event_text,
            event_type=self.event_type,
            event_date=self.event_date,
            prior_consensus=self.prior_consensus,
            recent_price_action=self.recent_price_action,
            sector_context=self.sector_context,
            market=self.market,
            instrument=self.instrument,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ─────────────────────── Stage 0a: classify ───────────────────────


CLASSIFY_SYSTEM_PROMPT = """You are an event classification agent for a market \
simulation tool. Given a free-form input from a user (URL, headline, news text, \
or short description), identify the market and instrument the event is about.

Output STRICT JSON matching this schema:

{
  "market": "ashare | us-equity | crude-oil-wti | hk-equity | jp-equity | btc-spot | gold-spot | (or a new slug if you recognize a different market)",
  "instrument": "Human-readable display name (e.g. 'BYD', 'NVIDIA', 'WTI Crude Oil', 'Bitcoin')",
  "ticker": "Symbol or contract code (e.g. '002594', 'NVDA', 'CL=F', 'BTC-USD')",
  "event_type": "earnings | ipo | dividend | shareholder_action | m_a | management_change | supply_disruption | demand_shock | opec_meeting | inventory_release | weather | geopolitical | halving | exchange_event | protocol_upgrade | policy | regulatory | lawsuit | macro | other",
  "event_date": "YYYY-MM-DD (date the event occurred or will occur — if no date in input, use today)",
  "event_text_summary": "1-3 sentence summary of the event in the source language",
  "price_currency": "CNY | USD | EUR | JPY | HKD | GBP | KRW | BTC (currency the instrument trades in)",
  "search_terms_for_context": [
    "specific search query 1 to fetch prior consensus",
    "specific search query 2 to fetch recent price action",
    "specific search query 3 to fetch sector / supply context"
  ],
  "confidence": {
    "market": 0.0-1.0,
    "instrument": 0.0-1.0,
    "ticker": 0.0-1.0,
    "event_type": 0.0-1.0,
    "event_date": 0.0-1.0
  }
}

RULES:
1. If you can't identify the market with confidence > 0.5, use "other" and
   set confidence accordingly.
2. For event_date: if the input doesn't mention a specific date, use today's
   date provided in the user message.
3. For search_terms_for_context: produce 3 specific queries that would help
   pull the right web results. Use the instrument name + relevant terms.
4. Return ONLY the JSON, no commentary."""


async def _classify_input(raw_input: str, today: str) -> dict[str, Any]:
    """Stage 0a: classify the input into market + instrument + event type."""
    log.info("[extract] Classifying input (%d chars)", len(raw_input))
    user_msg = (
        f"Today's date (UTC+8): {today}\n\n"
        f"User input:\n{raw_input}\n\n"
        f"Classify this input. Return the JSON per the schema."
    )
    json_result: JsonChatResult = await chat_json(
        messages=[
            {"role": "system", "content": CLASSIFY_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        max_tokens=800,
    )
    if not isinstance(json_result.parsed, dict):
        raise RuntimeError(
            f"classify returned non-dict: {type(json_result.parsed).__name__}"
        )
    return json_result.parsed


# ─────────────────────── Stage 0b: research context ───────────────────────


async def _research_context(
    classification: dict[str, Any],
    max_urls_per_query: int = 4,
    max_total_urls: int = 12,
) -> list[dict[str, str]]:
    """Stage 0b: web search + fetch for context related to the classified event.

    Uses the search_terms_for_context the LLM proposed in stage 0a.
    Returns a list of {url, title, excerpt} dicts.
    """
    queries = classification.get("search_terms_for_context", [])
    if not queries:
        # Fall back to a generic query
        instrument = classification.get("instrument", "")
        event_type = classification.get("event_type", "")
        queries = [f"{instrument} {event_type} 2025 2026"]

    log.info("[extract] Researching with %d queries", len(queries))

    loop = asyncio.get_running_loop()
    all_results: list[list[dict[str, str]]] = []
    for i, q in enumerate(queries):
        try:
            results = await asyncio.wait_for(
                loop.run_in_executor(None, search_web, q, max_urls_per_query),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            log.warning("[extract] search timeout: %s", q[:60])
            results = []
        all_results.append(results)
        if i < len(queries) - 1:
            await asyncio.sleep(1.0)

    # Round-robin dedupe
    seen: set[str] = set()
    urls: list[str] = []
    for results in all_results:
        for r in results[:max_urls_per_query]:
            url = r.get("url", "")
            if url and url not in seen:
                seen.add(url)
                urls.append(url)
                if len(urls) >= max_total_urls:
                    break
        if len(urls) >= max_total_urls:
            break

    log.info("[extract] Fetching %d URLs", len(urls))
    sources = await fetch_corpus(urls)
    return [
        {
            "url": s.url,
            "title": s.title,
            "excerpt": s.excerpt,
        }
        for s in sources
        if s.fetch_status == "ok" and s.excerpt
    ]


# ─────────────────────── Stage 0c: synthesize EventProposal ───────────────────────


SYNTHESIZE_SYSTEM_PROMPT = """You are a market event synthesis agent. Given a \
preliminary classification and a corpus of web pages about an instrument, produce \
a complete EventProposal that ssFlow can run through its sandbox simulation.

Price, ADV, and currency are NOT your job — those are populated separately
by the distillation step that hits real market data APIs per instrument.
Focus on the narrative fields.

Output STRICT JSON matching this schema:

{
  "event_text": "Polished 2-5 sentence event description in the source language",
  "prior_consensus": "What the market expected before this event (1-3 sentences from the corpus)",
  "recent_price_action": "Recent price + volume context (1-3 sentences from the corpus)",
  "sector_context": "Industry / supply-demand context (1-3 sentences from the corpus)",
  "suggested_personas_path": "personas/ashare.yaml | personas/us-equity-v1.yaml | personas/crude-oil-wti-v1.yaml | (or another path)",
  "confidence_context": 0.0-1.0,
  "warnings": ["list of any caveats — missing data, conflicting sources, etc."]
}

CRITICAL RULES:
1. **DO NOT use forbidden vocabulary** in any text field: 建议, 推荐, 应该,
   买入, 卖出, BUY, SELL, target price, recommend, should buy, etc.
   Use descriptive language: "市场预期", "consensus expected", "趋势", etc.
2. suggested_personas_path: pick the closest match from the available packs:
   - ashare market → personas/ashare.yaml (canonical hand-written) OR
                      personas/ashare-auto-2026-04-08.yaml (auto-generated)
   - us-equity     → personas/us-equity-v1.yaml OR personas/us-equity-auto-2026-04-08.yaml
   - crude-oil-wti → personas/crude-oil-wti-v1.yaml OR personas/crude-oil-wti-auto-2026-04-08.yaml
   - other markets → "personas/<market>.yaml" (may not exist yet)
3. Return ONLY the JSON, no commentary, no markdown fences."""


async def _synthesize_proposal(
    classification: dict[str, Any],
    sources: list[dict[str, str]],
    today: str,
) -> dict[str, Any]:
    """Stage 0c: produce the synthesized EventProposal."""
    if not sources:
        log.warning("[extract] No sources to synthesize from — proposal will be sparse")

    # Build the corpus string
    corpus_blocks = []
    for s in sources:
        corpus_blocks.append(
            f"## URL: {s['url']}\n"
            f"## Title: {s['title']}\n"
            f"## Content: {s['excerpt'][:3000]}"
        )
    corpus_text = "\n\n---\n\n".join(corpus_blocks) or "(no corpus available)"

    user_msg = (
        f"Today's date (UTC+8): {today}\n\n"
        f"Preliminary classification:\n{json.dumps(classification, ensure_ascii=False, indent=2)}\n\n"
        f"Web research corpus:\n\n{corpus_text}\n\n"
        f"Synthesize the EventProposal as JSON per the schema."
    )
    json_result: JsonChatResult = await chat_json(
        messages=[
            {"role": "system", "content": SYNTHESIZE_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        max_tokens=2000,
    )
    if not isinstance(json_result.parsed, dict):
        raise RuntimeError(
            f"synthesize returned non-dict: {type(json_result.parsed).__name__}"
        )
    return json_result.parsed


# ─────────────────────── Top-level orchestrator ───────────────────────


async def extract_event(
    raw_input: str,
    *,
    pre_fetched_corpus: list[dict[str, str]] | None = None,
) -> EventProposal:
    """Stage 0 — full pipeline.

    Input: a free-form string (URL, news text, headline, short description).
    Output: an EventProposal with all fields auto-filled, ready for the user
    to review + edit + confirm before running through the sandbox.

    Args:
        raw_input: the user's free-form text prompt.
        pre_fetched_corpus: optional list of ``{"url", "title", "excerpt"}``
            dicts that the caller already fetched (e.g. via
            `document_ingestion`). When provided, Stage 0b web research is
            **skipped** entirely — the user's own docs are assumed to be
            richer primary sources than whatever DDG would find. This
            both speeds up extraction and keeps first-party context in
            the loop.

    Cost: ~$0.02-0.04 per extraction (2 LLM calls + ~12 web fetches).
    Wall clock: ~20-40 seconds (much faster with pre_fetched_corpus — no web).
    """
    today = now_utc8_date()
    log.info("[extract] Starting extraction for input: %r", raw_input[:120])

    # Stage 0a: classify
    classification = await _classify_input(raw_input, today)
    log.info(
        "[extract] Classified: market=%s instrument=%s event_type=%s",
        classification.get("market"),
        classification.get("instrument"),
        classification.get("event_type"),
    )

    # Stage 0b: research (or skip if caller supplied a corpus)
    if pre_fetched_corpus is not None:
        sources = [
            {
                "url": str(s.get("url", "")),
                "title": str(s.get("title", "")),
                "excerpt": str(s.get("excerpt", "")),
            }
            for s in pre_fetched_corpus
            if s.get("excerpt")
        ]
        log.info(
            "[extract] Stage 0b skipped: using %d pre-fetched sources", len(sources)
        )
    else:
        sources = await _research_context(classification)
        log.info("[extract] Research complete: %d sources", len(sources))

    # Stage 0c: synthesize
    synth = await _synthesize_proposal(classification, sources, today)

    # Combine into EventProposal
    market = classification.get("market", "other")
    instrument = classification.get("instrument") or classification.get("ticker", "unknown")
    ticker = classification.get("ticker", instrument)
    event_type = classification.get("event_type", "other")
    if event_type not in VALID_EVENT_TYPES:
        event_type = "other"
    event_date = classification.get("event_date") or today

    confidence = dict(classification.get("confidence", {}))
    confidence["context"] = synth.get("confidence_context", 0.0)

    warnings = list(synth.get("warnings", []))

    proposal = EventProposal(
        ticker=ticker,
        instrument=instrument,
        market=market,
        event_type=event_type,
        event_date=event_date,
        event_text=synth.get("event_text") or classification.get("event_text_summary", ""),
        prior_consensus=synth.get("prior_consensus", ""),
        recent_price_action=synth.get("recent_price_action", ""),
        sector_context=synth.get("sector_context", ""),
        confidence=confidence,
        suggested_personas_path=synth.get("suggested_personas_path"),
        extracted_at=today,
        raw_input=raw_input,
        sources_consulted=[s["url"] for s in sources],
        extraction_warnings=warnings,
    )

    log.info(
        "[extract] DONE: %s · %s · context_confidence=%.2f",
        proposal.market, proposal.instrument,
        confidence.get("context", 0.0),
    )
    return proposal


__all__ = [
    "EventProposal",
    "extract_event",
]
