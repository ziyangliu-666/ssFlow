"""Persona pack calibration pipeline.

Generates a v2 schema persona pack YAML from web research, with no human
authoring. The agent does the deep research, structured extraction, and
generation. Output is a `personas/<market>-auto-<date>.yaml` file that
loads cleanly through `persona.load_personas()` and runs through
`run_sandbox_simulation()` like any hand-authored pack.

Architecture (4 stages):

  Stage 1 — RESEARCH       Pure Python search + fetch + 1 LLM extraction
  Stage 2 — CLASSIFY       1 LLM call: identify the participant classes
  Stage 3 — GENERATE       N parallel LLM calls: 1 per persona class
  Stage 4 — VALIDATE+EMIT  Python sanity checks + write yaml file

Cost (default): ~$0.15 per market generation.
Wall clock: ~2-5 minutes per market (Stage 3 parallelized).

Generic research questions (Stage 1) are market-agnostic. The agent fills
in {market} and discovers the actual sources via DuckDuckGo search. No
per-market hardcoded URL list.

Design decisions (locked from the user-driven design discussion):
  - Agent autonomy: no per-market source list, just generic questions
  - Default per-market, --ticker optional override (Q2)
  - Output to <market>-auto-<date>.yaml, never overwrite hand-written (Q3)
  - Voice prompt = Python skeleton + LLM filler (Q4, Bouchaud 2018 style)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from bs4 import BeautifulSoup
from ddgs import DDGS

from .config import settings
from .llm_client import JsonChatResult, chat_json
from .persona import (
    SCHEMA_VERSION,
    VALID_DECISION_MODES,
    load_personas,
)


log = logging.getLogger(__name__)


# ─────────────────────── Constants ───────────────────────

# Generic research questions used in Stage 1. The agent fills in {term}
# (a free-form market name like "China A-share" or "WTI crude oil futures")
# and runs DDG search for each. These are the ONLY hardcoded inputs to the
# pipeline — everything else is discovered.
#
# Add a new question here ONLY if it generalizes across markets. Per-market
# specifics belong in the LLM extraction prompts, not here.
GENERIC_RESEARCH_QUESTIONS = [
    "{term} investor structure participant classes holdings share",
    "{term} retail vs institutional trading volume split",
    "{term} top institutional holders by AUM",
    "{term} retail investor demographics behavior",
    "{term} strategic holders corporate government share",
    "{term} regulator investor survey report 2024 2025",
]

# Per-locale native-language search query enrichment. These are filled with
# {term_native} (a native-language free-form name) so the searches actually
# hit the canonical local sources, not English academic papers.
LOCALE_NATIVE_QUERIES = {
    "zh-CN": [
        "{term_native} 投资者结构 持流通市值",
        "{term_native} 散户 机构 占比",
        "{term_native} 北上资金 公募 私募 占比",
        "{term_native} 中登 投资者数量",
        "{term_native} 持仓比例 五分法",
        "{term_native} 量化 占成交量",
    ],
    "en-US": [
        "{term_native} SIFMA investor structure",
        "{term_native} CFTC commitment of traders",
        "{term_native} institutional vs retail share",
    ],
}


@dataclass
class MarketDescriptor:
    """Identifies a market for both filename and search purposes.

    `slug` is used for files and cache keys (must be filesystem-safe).
    `search_terms` are free-form English search strings.
    `native_search_terms` are local-language search strings (used with
    LOCALE_NATIVE_QUERIES). The factory passes each term × each question
    template to DDG, then dedupes URLs.

    Examples:
        ashare:
            slug = "ashare"
            search_terms = ["China A-share", "Shanghai Stock Exchange"]
            native_search_terms = ["A股", "中国A股市场", "沪深股市"]
        us-equity:
            slug = "us-equity"
            search_terms = ["US equity market", "S&P 500", "NYSE NASDAQ"]
        crude-oil-wti:
            slug = "crude-oil-wti"
            search_terms = ["WTI crude oil futures", "NYMEX crude oil"]
    """

    slug: str
    search_terms: list[str]
    native_search_terms: list[str] = field(default_factory=list)
    locale: str = "en-US"


# Pre-defined descriptors for markets ssFish ships with.
KNOWN_MARKETS: dict[str, "MarketDescriptor"] = {}

# Sources to skip during fetch (paywalls / login walls / known noise)
FETCH_BLOCKLIST_DOMAINS = {
    "linkedin.com",
    "facebook.com",
    "twitter.com",
    "x.com",
    "instagram.com",
    "tiktok.com",
}

# Per-fetch limits
FETCH_TIMEOUT_SECONDS = 15
FETCH_MAX_BYTES = 500_000  # 500 KB cap on a single page
FETCH_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36 ssFish-research/0.1"
)

# Cost guard: cap on LLM calls per pipeline run
MAX_LLM_CALLS_PER_RUN = 50


# ─────────────────────── Dataclasses (research output) ───────────────────────


@dataclass
class ResearchSource:
    """One web source the research agent fetched + extracted from."""

    id: str           # short slug (e.g., "huaxi-2025q3")
    url: str
    title: str
    org: str | None = None
    accessed: str | None = None  # YYYY-MM-DD UTC+8
    excerpt: str = ""             # the cleaned text (truncated)
    fetch_status: str = "ok"      # ok | failed | skipped


@dataclass
class CapitalTier:
    """One bucket of the account-distribution histogram."""

    label: str          # e.g., "<1万", "1-10万", "10-50万"
    min_cny: float | None = None
    max_cny: float | None = None
    account_share: float | None = None
    cited_source_ids: list[str] = field(default_factory=list)


@dataclass
class ParticipantClass:
    """One identified market participant class with its rough stats.

    This is the output of Stage 1 (research) and the input to Stage 2/3
    (classify + generate). It carries enough info to write a v2 persona
    block, but not the polished sandbox config or voice prompt yet.
    """

    id: str               # snake_case slug
    archetype: str        # human label
    sub_archetype: str | None = None
    description: str = ""
    decision_mode: str = "discretionary"
    role: str = ""
    by_volume: float | None = None
    by_holdings: float | None = None
    by_account_count: float | None = None
    typical_capital_min_cny: float | None = None
    typical_capital_max_cny: float | None = None
    typical_holding_period_days_min: int | None = None
    typical_holding_period_days_max: int | None = None
    is_strategic: bool = False
    info_sources: list[str] = field(default_factory=list)
    biases: dict[str, float] = field(default_factory=dict)
    cited_source_ids: list[str] = field(default_factory=list)
    free_form_notes: str = ""


@dataclass
class ResearchData:
    """Full output of Stage 1 — everything Stage 2/3 needs to generate the pack."""

    market: str
    locale: str
    research_date: str           # YYYY-MM-DD UTC+8
    sources: list[ResearchSource] = field(default_factory=list)
    total_accounts: int | None = None
    retail_pct_by_count: float | None = None
    retail_pct_by_volume: float | None = None
    retail_pct_by_holdings: float | None = None
    capital_tiers: list[CapitalTier] = field(default_factory=list)
    classes: list[ParticipantClass] = field(default_factory=list)
    free_form_summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dump for caching."""
        from dataclasses import asdict
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResearchData":
        sources = [ResearchSource(**s) for s in data.get("sources", [])]
        tiers = [CapitalTier(**t) for t in data.get("capital_tiers", [])]
        classes = [ParticipantClass(**c) for c in data.get("classes", [])]
        return cls(
            market=data["market"],
            locale=data["locale"],
            research_date=data["research_date"],
            sources=sources,
            total_accounts=data.get("total_accounts"),
            retail_pct_by_count=data.get("retail_pct_by_count"),
            retail_pct_by_volume=data.get("retail_pct_by_volume"),
            retail_pct_by_holdings=data.get("retail_pct_by_holdings"),
            capital_tiers=tiers,
            classes=classes,
            free_form_summary=data.get("free_form_summary", ""),
        )


# ─────────────────────── Path helpers ───────────────────────


def research_cache_dir() -> Path:
    d = settings.project_root / "personas" / "_research"
    d.mkdir(parents=True, exist_ok=True)
    return d


def auto_pack_dir() -> Path:
    return settings.project_root / "personas"


def cache_path_for(market: str, date_str: str) -> Path:
    return research_cache_dir() / f"{market}-{date_str}.json"


def auto_pack_path_for(market: str, date_str: str) -> Path:
    return auto_pack_dir() / f"{market}-auto-{date_str}.yaml"


def now_utc8_date() -> str:
    """Return today's date in UTC+8 (China Standard Time)."""
    from datetime import timezone, timedelta
    cst = timezone(timedelta(hours=8))
    return datetime.now(cst).strftime("%Y-%m-%d")


# ─────────────────────── Stage 1 helpers (search + fetch) ───────────────────────


def _is_blocked(url: str) -> bool:
    for blocked in FETCH_BLOCKLIST_DOMAINS:
        if blocked in url.lower():
            return True
    return False


def _slugify_url(url: str) -> str:
    """Build a short stable slug from a URL for use as source_id."""
    h = re.sub(r"[^a-zA-Z0-9]+", "-", url[:80]).strip("-").lower()
    return h[:48] or "src"


# ddgs search backends (as of v9.x): brave, duckduckgo, google, grokipedia,
# mojeek, wikipedia, yahoo, yandex. The default 'auto' picks dynamically and
# turns out to give the best quality for mixed Chinese+English queries.
# We explicitly use 'auto' (None) to let ddgs choose.
def search_web(query: str, max_results: int = 6) -> list[dict[str, str]]:
    """ddgs search wrapper. Returns list of {title, url, body}.

    Uses ddgs default 'auto' backend, which dynamically picks among brave/
    duckduckgo/yandex/etc. Per-call timeout is enforced by the caller via
    asyncio.wait_for.
    """
    log.info("[research] search: %s", query[:80])

    def _normalize(raw_results: list[dict]) -> list[dict[str, str]]:
        normalized = []
        for r in raw_results:
            normalized.append({
                "title": r.get("title", ""),
                "url": r.get("href", "") or r.get("url", ""),
                "body": r.get("body", "") or r.get("snippet", ""),
            })
        return [r for r in normalized if r["url"] and not _is_blocked(r["url"])]

    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        return _normalize(results)
    except Exception as exc:
        log.warning("[research] search failed for %r: %s", query[:60], exc)
        return []


async def fetch_clean(url: str, client: httpx.AsyncClient) -> tuple[str, str]:
    """Fetch a URL and return (title, cleaned_body_text). Raises on failure."""
    if _is_blocked(url):
        raise ValueError(f"blocked domain: {url}")
    resp = await client.get(
        url,
        timeout=FETCH_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers={"User-Agent": FETCH_USER_AGENT},
    )
    resp.raise_for_status()
    raw = resp.content[:FETCH_MAX_BYTES]
    soup = BeautifulSoup(raw, "html.parser")
    # Strip nav / script / style noise
    for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
        tag.decompose()
    title = (soup.title.string if soup.title and soup.title.string else "").strip()
    text = soup.get_text(separator=" ", strip=True)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text)
    return title, text


async def fetch_corpus(
    urls: list[str],
    max_concurrent: int = 5,
    per_url_max_chars: int = 8000,
) -> list[ResearchSource]:
    """Fetch all URLs in parallel and return a list of ResearchSource objects."""
    sem = asyncio.Semaphore(max_concurrent)
    accessed = now_utc8_date()
    sources: list[ResearchSource] = []

    async with httpx.AsyncClient() as client:
        async def _one(url: str) -> None:
            async with sem:
                src = ResearchSource(
                    id=_slugify_url(url),
                    url=url,
                    title="",
                    accessed=accessed,
                )
                try:
                    title, text = await fetch_clean(url, client)
                    src.title = title[:200]
                    src.excerpt = text[:per_url_max_chars]
                    src.fetch_status = "ok"
                except Exception as exc:
                    src.fetch_status = f"failed: {type(exc).__name__}: {str(exc)[:100]}"
                    log.warning("[research] fetch failed %s: %s", url, src.fetch_status)
                sources.append(src)

        await asyncio.gather(*(_one(u) for u in urls))

    return sources


# ─────────────────────── Stage 1: research_market ───────────────────────


def _build_search_queries(descriptor: MarketDescriptor, max_total: int = 12) -> list[str]:
    """Build a capped set of DDG queries for a market.

    Strategy: prioritize native-language queries first (they hit canonical
    local sources), then English queries. Cap to `max_total` to avoid DDG
    rate-limiting. Use only the FIRST search term (most representative)
    × all questions, instead of combinatorial term × question explosion.
    """
    queries: list[str] = []

    # Native first (higher quality for non-English markets)
    native_templates = LOCALE_NATIVE_QUERIES.get(descriptor.locale, [])
    if descriptor.native_search_terms and native_templates:
        primary_native = descriptor.native_search_terms[0]
        for q in native_templates:
            queries.append(q.format(term_native=primary_native))

    # English: use the first search term × all generic questions
    if descriptor.search_terms:
        primary_en = descriptor.search_terms[0]
        for q in GENERIC_RESEARCH_QUESTIONS:
            queries.append(q.format(term=primary_en))

    return queries[:max_total]


def _gather_unique_urls(
    query_results: list[list[dict]],
    cap: int = 25,
    per_query_quota: int = 3,
) -> list[str]:
    """Dedupe URLs across queries, with per-query quota to ensure each query is
    represented in the final list (instead of "first N" which lets the first
    1-2 queries dominate).

    Algorithm:
        1. Round-robin: take top `per_query_quota` URLs from each query in order
        2. After all queries contribute their quota, fill remaining slots from
           the leftover URLs in original query order
        3. Always dedupe globally
        4. Cap final list to `cap`
    """
    seen: set[str] = set()
    out: list[str] = []

    # Pass 1: round-robin top-N per query
    for results in query_results:
        added = 0
        for r in results:
            if added >= per_query_quota:
                break
            url = r.get("url", "")
            if not url or url in seen:
                continue
            seen.add(url)
            out.append(url)
            added += 1
            if len(out) >= cap:
                return out

    # Pass 2: fill remaining with leftover URLs from each query
    if len(out) < cap:
        for results in query_results:
            for r in results:
                url = r.get("url", "")
                if not url or url in seen:
                    continue
                seen.add(url)
                out.append(url)
                if len(out) >= cap:
                    return out
    return out


# Stage 1 LLM extraction prompt
RESEARCH_EXTRACTION_SYSTEM_PROMPT = """You are a market microstructure researcher. \
Given a corpus of web pages about a financial market, extract a structured \
summary of the market's investor structure.

Your output MUST be valid JSON matching this schema:

{
  "total_accounts": <int or null>,
  "retail_pct_by_count": <0.0-1.0 or null>,
  "retail_pct_by_volume": <0.0-1.0 or null>,
  "retail_pct_by_holdings": <0.0-1.0 or null>,
  "capital_tiers": [
    {"label": "...", "min_cny": <float or null>, "max_cny": <float or null>,
     "account_share": <0.0-1.0 or null>, "cited_source_ids": ["src-id", ...]}
  ],
  "classes": [
    {
      "id": "snake_case_slug",
      "archetype": "Human label",
      "sub_archetype": "more_specific_or_null",
      "description": "1-2 sentences in plain language",
      "decision_mode": "discretionary | systematic | strategic | passive",
      "role": "directional_speculator | long_term_holder | strategic_holder | commercial_hedger | market_maker_arb | passive_market_maker | quant | sell_side_analyst | short_seller | institutional_long_only | institutional_long_horizon | foreign_active | active_long_short",
      "by_volume": <0.0-1.0 or null>,
      "by_holdings": <0.0-1.0 or null>,
      "by_account_count": <0.0-1.0 or null>,
      "typical_capital_min_cny": <float or null>,
      "typical_capital_max_cny": <float or null>,
      "typical_holding_period_days_min": <int or null>,
      "typical_holding_period_days_max": <int or null>,
      "is_strategic": <true if decision_mode == strategic else false>,
      "info_sources": ["source_a", "source_b"],
      "biases": {"momentum_chasing": 0.0-1.0, "loss_aversion": 0.0-1.0, ...},
      "cited_source_ids": ["src-id", ...]
    },
    ...
  ],
  "free_form_summary": "3-5 sentence overview of this market's structure"
}

RULES:
1. Return BETWEEN 10 AND 16 classes covering all major market participant types.
   - Retail subdivided into at least 2-3 sub-classes (active short-term, passive long-term, high-net-worth value)
   - Institutional subdivided into at least 3-4 sub-classes (mutual funds, ETF/index, hedge funds, insurance/pension, foreign)
   - Strategic / corporate / government holders if the market has them (e.g., 大股东 / 国资委 / 国家队 / 个人大股东 in A股, sovereign wealth in US, OPEC in oil)
   - Market-specific archetypes if applicable (e.g., 量化 quant, market makers, swap dealers)
   - **DO NOT collapse the panel into 4-5 generic classes. The whole point is granularity.**
2. by_holdings sum across all classes should be close to 1.0 (within 0.10 tolerance).
3. by_volume sum may exceed 1.0 (high-turnover classes contribute disproportionately).
4. cited_source_ids MUST reference IDs that appear in the corpus you were given. Empty list if no source supports the number — better than fake citation.
5. If a number is not supportable from the corpus, return null for that field. **DO NOT hallucinate round numbers (0.1, 0.2, 0.3) when you don't have evidence.** It is better to return null than to invent numbers.
6. AVOID returning numbers ending in exactly .0 or .5 unless the source literally says so — those are usually a sign of guessing.
7. If the corpus is in Chinese, keep the archetype field in Chinese.
8. Return ONLY the JSON, no commentary, no markdown fences."""


async def _llm_extract_research(
    market: str,
    locale: str,
    sources: list[ResearchSource],
    seed: int | None = None,
) -> dict[str, Any]:
    """Single LLM call: extract structured research data from the corpus."""
    # Build the user message: corpus with source IDs
    corpus_blocks = []
    for s in sources:
        if s.fetch_status != "ok" or not s.excerpt:
            continue
        corpus_blocks.append(
            f"## Source: {s.id}\n"
            f"URL: {s.url}\n"
            f"Title: {s.title}\n"
            f"Content: {s.excerpt}"
        )
    corpus_text = "\n\n---\n\n".join(corpus_blocks)
    if not corpus_text:
        raise RuntimeError("No usable sources to extract from (all fetches failed)")

    user_msg = (
        f"Market: {market}\n"
        f"Locale: {locale}\n"
        f"Date: {now_utc8_date()}\n\n"
        f"Corpus:\n\n{corpus_text}\n\n"
        f"Extract the structured research summary as JSON per the schema."
    )

    log.info(
        "[research] Extracting from %d sources, %d total chars",
        sum(1 for s in sources if s.fetch_status == "ok"),
        len(corpus_text),
    )

    json_result: JsonChatResult = await chat_json(
        messages=[
            {"role": "system", "content": RESEARCH_EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        seed=seed,
        max_tokens=4000,
    )
    if not isinstance(json_result.parsed, dict):
        raise RuntimeError(
            f"LLM extraction returned non-dict: {type(json_result.parsed).__name__}"
        )
    return json_result.parsed


async def research_market(
    descriptor: MarketDescriptor | str,
    use_cache: bool = True,
    cache_date: str | None = None,
    seed: int | None = None,
    max_urls: int = 30,
) -> ResearchData:
    """Stage 1 — research orchestrator.

    Runs:
        1. DDG searches for each term × each generic question (+ native queries)
        2. Dedupe URLs, drop blocked domains
        3. Parallel fetch of top ~30 URLs
        4. Single LLM call to extract structured ResearchData
        5. Cache the result to personas/_research/<slug>-<date>.json

    Args:
        descriptor: MarketDescriptor object or a slug string for KNOWN_MARKETS
        use_cache: if True and a cached file exists, return it
        cache_date: override the date used for cache lookup (YYYY-MM-DD)
        seed: deterministic seed for the LLM extraction call
        max_urls: cap on URLs to fetch
    """
    if isinstance(descriptor, str):
        if descriptor not in KNOWN_MARKETS:
            raise ValueError(
                f"Unknown market slug: {descriptor!r}. "
                f"Either pass a MarketDescriptor object or use one of: "
                f"{sorted(KNOWN_MARKETS.keys())}"
            )
        descriptor = KNOWN_MARKETS[descriptor]

    date_str = cache_date or now_utc8_date()
    cache_file = cache_path_for(descriptor.slug, date_str)
    if use_cache and cache_file.exists():
        log.info("[research] Cache hit: %s", cache_file)
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        return ResearchData.from_dict(data)

    # Stage 1a: search SERIALLY with delay to avoid DDG rate limiting
    queries = _build_search_queries(descriptor)
    log.info("[research] %d queries built for market=%s", len(queries), descriptor.slug)
    query_results: list[list[dict[str, str]]] = []
    loop = asyncio.get_running_loop()
    for i, q in enumerate(queries):
        # search_web is sync (DDG client) — run in executor with timeout
        try:
            results = await asyncio.wait_for(
                loop.run_in_executor(None, search_web, q, 6),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            log.warning("[research] DDG search timeout for %r", q[:60])
            results = []
        query_results.append(results)
        # Polite delay between queries
        if i < len(queries) - 1:
            await asyncio.sleep(1.0)

    urls = _gather_unique_urls(query_results, cap=max_urls)
    log.info("[research] %d unique URLs across %d queries", len(urls), len(queries))

    # Stage 1b: fetch
    sources = await fetch_corpus(urls)
    ok_count = sum(1 for s in sources if s.fetch_status == "ok")
    log.info("[research] Fetched %d/%d sources successfully", ok_count, len(sources))

    # Stage 1c: LLM extraction
    extracted = await _llm_extract_research(
        descriptor.slug, descriptor.locale, sources, seed=seed
    )

    research = ResearchData(
        market=descriptor.slug,
        locale=descriptor.locale,
        research_date=date_str,
        sources=sources,
        total_accounts=extracted.get("total_accounts"),
        retail_pct_by_count=extracted.get("retail_pct_by_count"),
        retail_pct_by_volume=extracted.get("retail_pct_by_volume"),
        retail_pct_by_holdings=extracted.get("retail_pct_by_holdings"),
        capital_tiers=[CapitalTier(**t) for t in extracted.get("capital_tiers", [])],
        classes=[ParticipantClass(**c) for c in extracted.get("classes", [])],
        free_form_summary=extracted.get("free_form_summary", ""),
    )

    cache_file.write_text(
        json.dumps(research.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info("[research] Cached to %s", cache_file)
    return research


# ─────────────────────── KNOWN_MARKETS registry (filled at module bottom) ───────────────────────

KNOWN_MARKETS["ashare"] = MarketDescriptor(
    slug="ashare",
    locale="zh-CN",
    search_terms=[
        "China A-share market",
        "Shanghai Stock Exchange Shenzhen Stock Exchange",
        "Chinese mainland equity",
    ],
    native_search_terms=[
        "A股",
        "中国A股市场",
        "沪深股市",
        "A股投资者",
    ],
)

KNOWN_MARKETS["us-equity"] = MarketDescriptor(
    slug="us-equity",
    locale="en-US",
    search_terms=[
        "US equity market",
        "S&P 500",
        "NYSE NASDAQ",
        "American stock market",
    ],
    native_search_terms=["US equity market"],
)

KNOWN_MARKETS["crude-oil-wti"] = MarketDescriptor(
    slug="crude-oil-wti",
    locale="en-US",
    search_terms=[
        "WTI crude oil futures",
        "NYMEX crude oil",
        "petroleum futures market",
    ],
    native_search_terms=["WTI crude oil futures"],
)


# ─────────────────────── Stage 2: classify (deferred to F-next) ───────────────────────
# ─────────────────────── Stage 3: generate per persona (deferred) ───────────────────────
# ─────────────────────── Stage 4: validate + emit (deferred) ───────────────────────


__all__ = [
    "GENERIC_RESEARCH_QUESTIONS",
    "KNOWN_MARKETS",
    "LOCALE_NATIVE_QUERIES",
    "MarketDescriptor",
    "ResearchData",
    "ResearchSource",
    "CapitalTier",
    "ParticipantClass",
    "fetch_corpus",
    "research_market",
    "search_web",
    "now_utc8_date",
    "auto_pack_path_for",
    "cache_path_for",
]
