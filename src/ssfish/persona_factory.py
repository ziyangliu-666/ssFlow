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
1. **You MUST produce AT LEAST 12 distinct classes.** This is non-negotiable.
   The classes MUST cover (use null fields if the corpus lacks data — better
   null than missing class):
     - At least 3 RETAIL sub-classes:
       * retail short-term active (high-turnover speculator)
       * retail long-term passive (buy-and-hold mass affluent)
       * retail high-net-worth pro-am (value investor with concentrated positions)
     - At least 3-4 INSTITUTIONAL sub-classes:
       * mutual fund / active long-only equity
       * ETF / passive index / authorized participant
       * hedge fund / active long-short
       * insurance / pension / long-horizon institutional
     - At least 1 FOREIGN class (if the market has cross-border investors)
     - At least 1 QUANT / MARKET-MAKER / HFT class (if applicable)
     - At least 3 STRATEGIC sub-classes if the market has them:
       * industrial / corporate strategic holders (大股东, founders, controlling shareholders)
       * government / state holders (国资委, sovereign wealth, central banks)
       * cross-holding / related-party holders (公司互持, 关联方)
       * national stabilization fund / national team if exists (汇金, 证金)
       * individual major holders 5%+ if disclosed
   - **DO NOT collapse the panel into 4-7 generic classes. The whole point is granularity.**
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


# ─────────────────────── Stage 2: per-persona generation ───────────────────────


# Per-class generation prompt. Stage 2 generates ONLY the creative fields
# (voice_prompt, sandbox config, biases, behavior, information). The
# structural fields (id, market_share, decision_mode, role, capital_range)
# are passed through verbatim from Stage 1's research output. This prevents
# the LLM from hallucinating market_share values that don't sum to 1.0.
PERSONA_CREATIVE_FIELDS_SYSTEM_PROMPT = """You are a persona pack designer for ssFish, \
an agent-based market simulation tool. Given a participant class identified \
in market research, generate ONLY the creative fields for the persona:
sandbox config, voice_prompt, behavior, biases, information, knowledge.

Your output MUST be valid JSON with EXACTLY these keys (no others):

{
  "display_name": "One-line description (capital range, age, info source)",
  "behavior": {
    "avg_position_pct": <0-1>,
    "annual_turnover": <float>,
    "typical_holding_period_days": <int>,
    "stop_loss_discipline": "low | medium | high",
    "reaction_speed": "instant | fast | medium | slow"
  },
  "information": {
    "primary_sources": ["source_a", "source_b"],
    "secondary_sources": ["source_c"],
    "ignored": ["thing_x"],
    "english_capable": <bool>
  },
  "biases": {"bias_name_1": <0-1>, ...},
  "knowledge": {
    "typical_holdings": ["..."],
    "information_sources": ["..."],
    "ignores": ["..."]
  },
  "voice_prompt": "Multi-line voice prompt in second person",
  "sandbox": {
    "instance_count": <int>,
    "capital_distribution": {
      "type": "lognormal",
      "median_cny": <float>,
      "sigma": <0.5-1.5>,
      "floor_cny": <float>,
      "ceiling_cny": <float>
    },
    "initial_position_distribution": {
      "type": "bernoulli",
      "prob_holding": <0-1>,
      "position_size_pct_when_holding": {"type": "uniform", "min": <0-1>, "max": <0-1>}
    },
    "risk": {
      "max_position_pct": <0-1>,
      "margin_account_pct": <0-1>,
      "leverage_max": <float>,
      "stop_loss_threshold": <negative float>,
      "stop_loss_discipline": <0-1>
    },
    "action_space": [
      {"name": "hold", "side": "none", "pool": "none", "fraction": 0.0},
      {"name": "...", "side": "buy|sell", "pool": "cash|holdings_in_target", "fraction": <0-1>}
    ],
    "reaction_lag_rounds": {"type": "discrete", "values": [<int>, ...]}
  }
}

DO NOT include market_share, decision_mode, role, capital_range_cny,
time_horizon_days, sub_archetype, archetype, or id. Those will be merged
in by Python from the research data — your output is ONLY the fields above.

CRITICAL RULES:
1. **NEVER use forbidden vocabulary in voice_prompt or any text field**:
   买入, 卖出, 建议, 推荐, 应该, 必须, 减仓, 加仓, 建仓, 清仓, 目标价,
   评级, 止损位, 止盈位, BUY, SELL, target price, recommendation, advice.
   Use: 进入/离场/倾向/观望/分批减/加仓位/合规窗口.
2. **action_space items are dicts with explicit semantics** — name, side
   (none|buy|sell), pool (none|cash|holdings_in_target), fraction (0-1).
   Action names must be descriptive (e.g., "panic_sell_50pct"), NOT
   advisory (no "buy_signal", no "recommend_*").
3. **For strategic personas** (input class.is_strategic == true):
   - sandbox.action_space dominated by "do_nothing" (95%+ probability)
   - rare actions: "block_sale_5pct", "increase_holding_2pct", "pledge_increase"
4. **sandbox.instance_count** scales with class size:
   - Strategic / individual major holders: 2-5 instances
   - Institutional (mutual fund, hedge fund, insurance): 10-50
   - Retail active: 1000-5000
   - Retail passive: 1000-4000
   - Quant / market makers: 20-50
5. **sandbox.capital_distribution.median_cny**:
   - Retail: total account size (¥10k-¥10M for A-share retail)
   - Institutions: per-stock allocation, NOT total fund AUM (¥10M-¥1B per stock)
   - Strategic large holders: actual block value (¥1B-¥100B)
6. **risk.max_position_pct** is the HARD CAP enforced by the engine:
   - Retail: 0.95 (can go nearly all-in)
   - Institutions: 0.05-0.15 (concentration limits)
   - Strategic: 1.0 (already at target)
7. **voice_prompt** 100-300 chars, second person, market's source language.
8. Return ONLY the JSON, no commentary, no markdown fences."""


# Mapping from research role/decision_mode to default action_space templates.
# These are seeds; the LLM may add more specific actions.
DEFAULT_ACTION_TEMPLATES = {
    "strategic": [
        {"name": "do_nothing", "side": "none", "pool": "none", "fraction": 0.0},
        {"name": "block_sale_5pct", "side": "sell", "pool": "holdings_in_target", "fraction": 0.05},
        {"name": "increase_holding_2pct", "side": "buy", "pool": "cash", "fraction": 0.02},
    ],
    "passive": [
        {"name": "hold", "side": "none", "pool": "none", "fraction": 0.0},
        {"name": "rebalance_trim_3pct", "side": "sell", "pool": "holdings_in_target", "fraction": 0.03},
        {"name": "rebalance_add_2pct", "side": "buy", "pool": "cash", "fraction": 0.02},
    ],
    "systematic": [
        {"name": "hold", "side": "none", "pool": "none", "fraction": 0.0},
        {"name": "model_buy_8pct", "side": "buy", "pool": "cash", "fraction": 0.08},
        {"name": "model_sell_8pct", "side": "sell", "pool": "holdings_in_target", "fraction": 0.08},
        {"name": "stop_loss_full", "side": "sell", "pool": "holdings_in_target", "fraction": 1.0},
    ],
    "discretionary": [
        {"name": "hold", "side": "none", "pool": "none", "fraction": 0.0},
        {"name": "trim_25pct", "side": "sell", "pool": "holdings_in_target", "fraction": 0.25},
        {"name": "add_15pct", "side": "buy", "pool": "cash", "fraction": 0.15},
        {"name": "exit_full", "side": "sell", "pool": "holdings_in_target", "fraction": 1.0},
    ],
}


def _merge_creative_into_structural(
    class_def: ParticipantClass,
    creative: dict[str, Any],
) -> dict[str, Any]:
    """Merge LLM-generated creative fields with structural fields from research.

    The structural fields (id, archetype, decision_mode, role, market_share)
    come from the research data and are NOT overwritten by the LLM. Only
    the LLM-generated fields (sandbox, voice_prompt, biases, behavior,
    information, knowledge, display_name) are taken from `creative`.
    """
    is_strategic = class_def.decision_mode == "strategic"

    # Build market_share from structural data only (no LLM hallucination)
    market_share: dict[str, Any] = {}
    if class_def.by_volume is not None:
        market_share["by_volume"] = round(float(class_def.by_volume), 4)
    if class_def.by_holdings is not None:
        market_share["by_holdings"] = round(float(class_def.by_holdings), 4)
    if class_def.by_account_count is not None:
        market_share["by_account_count"] = round(float(class_def.by_account_count), 4)
    if class_def.cited_source_ids:
        market_share["citations"] = [
            {"source_id": sid} for sid in class_def.cited_source_ids[:5]
        ]
    # If still empty, use a placeholder so the loader passes
    if not market_share or not any(
        k in market_share for k in ("by_volume", "by_holdings", "by_account_count")
    ):
        market_share["by_volume"] = 0.01
        market_share["citations"] = [{"source_id": "fallback", "note": "auto-fallback, no source"}]

    # Capital range: from structural data, fall back to LLM output sandbox if missing
    capital_range = None
    if class_def.typical_capital_min_cny and class_def.typical_capital_max_cny:
        capital_range = [
            int(class_def.typical_capital_min_cny),
            int(class_def.typical_capital_max_cny),
        ]

    # Time horizon: from structural data
    time_horizon = None
    if (
        class_def.typical_holding_period_days_min is not None
        and class_def.typical_holding_period_days_max is not None
    ):
        time_horizon = [
            int(class_def.typical_holding_period_days_min),
            int(class_def.typical_holding_period_days_max),
        ]

    # Sandbox: take from creative, but ensure action_space is non-empty
    # (fall back to default template if missing)
    sandbox = creative.get("sandbox", {})
    if not sandbox.get("action_space"):
        sandbox["action_space"] = DEFAULT_ACTION_TEMPLATES.get(
            class_def.decision_mode, DEFAULT_ACTION_TEMPLATES["discretionary"]
        )
    # Ensure required sandbox sub-fields exist with sensible defaults
    sandbox.setdefault("instance_count", 100)
    sandbox.setdefault("capital_distribution", {
        "type": "lognormal",
        "median_cny": 100000,
        "sigma": 0.8,
    })
    sandbox.setdefault("initial_position_distribution", {
        "type": "bernoulli",
        "prob_holding": 0.5,
        "position_size_pct_when_holding": {"type": "uniform", "min": 0.05, "max": 0.30},
    })
    sandbox.setdefault("risk", {"max_position_pct": 0.95})

    persona = {
        "id": class_def.id,
        "archetype": class_def.archetype,
        "sub_archetype": class_def.sub_archetype,
        "display_name": creative.get("display_name") or class_def.archetype,
        "model": None,
        "decision_mode": class_def.decision_mode,
        "role": class_def.role or "directional_speculator",
        "market_share": market_share,
        "voice_prompt": creative.get("voice_prompt", f"You represent {class_def.archetype}."),
        "biases": creative.get("biases", class_def.biases or {}),
        "knowledge": creative.get("knowledge", {}),
        "behavior": creative.get("behavior"),
        "information": creative.get("information"),
        "sandbox": sandbox,
    }
    if capital_range is not None:
        persona["capital_range_cny"] = capital_range
    if time_horizon is not None:
        persona["time_horizon_days"] = time_horizon

    # Strategic flag override (loader auto-sets, but be explicit)
    if is_strategic:
        persona["contributes_to_sentiment_mean"] = False
        persona["contributes_to_strategic_signal"] = True

    return persona


async def _llm_generate_creative_fields(
    class_def: ParticipantClass,
    research: ResearchData,
    seed: int | None = None,
) -> dict[str, Any]:
    """Stage 2 — single LLM call to generate the CREATIVE fields for one persona.

    Returns a dict with: display_name, behavior, information, biases,
    knowledge, voice_prompt, sandbox.

    Does NOT return structural fields (id, market_share, decision_mode,
    role, capital_range_cny, time_horizon_days). Those are merged in by
    `_merge_creative_into_structural` from the Stage 1 research output.
    """
    class_context = json.dumps({
        "id": class_def.id,
        "archetype": class_def.archetype,
        "sub_archetype": class_def.sub_archetype,
        "description": class_def.description,
        "decision_mode": class_def.decision_mode,
        "role": class_def.role,
        "is_strategic": class_def.is_strategic,
        "by_volume_share": class_def.by_volume,
        "by_holdings_share": class_def.by_holdings,
        "typical_capital_min_cny": class_def.typical_capital_min_cny,
        "typical_capital_max_cny": class_def.typical_capital_max_cny,
        "info_sources_hint": class_def.info_sources,
        "biases_hint": class_def.biases,
        "free_form_notes": class_def.free_form_notes,
    }, ensure_ascii=False, indent=2)

    market_context = (
        f"Market: {research.market}\n"
        f"Locale: {research.locale}\n"
        f"Total accounts in market: {research.total_accounts}\n"
        f"Retail by count: {research.retail_pct_by_count}\n"
        f"Free-form market summary: {research.free_form_summary[:400]}"
    )

    user_msg = (
        f"Generate the creative fields (display_name, behavior, information, "
        f"biases, knowledge, voice_prompt, sandbox) for the following persona class.\n\n"
        f"# Market context\n{market_context}\n\n"
        f"# Class to generate creative fields for\n{class_context}\n\n"
        f"Return ONLY the creative fields as JSON. Do NOT include id, "
        f"market_share, decision_mode, role, capital_range_cny, time_horizon_days, "
        f"sub_archetype, or archetype — those will be merged in by code."
    )

    json_result = await chat_json(
        messages=[
            {"role": "system", "content": PERSONA_CREATIVE_FIELDS_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        seed=seed,
        max_tokens=2000,
    )
    if not isinstance(json_result.parsed, dict):
        raise RuntimeError(
            f"LLM creative-gen returned non-dict for {class_def.id}: "
            f"{type(json_result.parsed).__name__}"
        )
    return json_result.parsed


async def generate_personas(
    research: ResearchData,
    seed: int | None = None,
    max_concurrent: int = 5,
) -> list[dict[str, Any]]:
    """Stage 2 — generate all persona blocks in parallel.

    Each class gets one LLM call for creative fields, then Python merges
    structural fields from research data deterministically. This prevents
    the LLM from hallucinating market_share values that don't sum to 1.0.
    """
    log.info(
        "[generate] Producing %d persona blocks for market=%s",
        len(research.classes), research.market,
    )
    sem = asyncio.Semaphore(max_concurrent)

    async def _one(class_def: ParticipantClass) -> dict[str, Any] | None:
        async with sem:
            try:
                creative = await _llm_generate_creative_fields(
                    class_def, research, seed=seed
                )
                merged = _merge_creative_into_structural(class_def, creative)
                log.info("[generate]   ✓ %s", class_def.id)
                return merged
            except Exception as exc:
                log.warning("[generate]   ✗ %s: %s", class_def.id, exc)
                # Fallback: build a minimal persona with just structural data
                try:
                    return _merge_creative_into_structural(class_def, {})
                except Exception:
                    return None

    results = await asyncio.gather(*(_one(c) for c in research.classes))
    successful = [r for r in results if r is not None]
    log.info("[generate] %d/%d personas generated", len(successful), len(results))
    return successful


# ─────────────────────── Stage 3: assemble + validate + emit ───────────────────────


def _normalize_market_share_sums(persona_blocks: list[dict[str, Any]]) -> None:
    """Mutate persona_blocks in place to normalize market_share sums to ~1.0.

    The Stage 1 LLM estimates each class's share independently, often
    producing sums like 1.4 or 1.85 instead of ~1.0. This is the calibration
    flaw the user pointed out: GIGO at the input layer. We fix it deterministically
    here by scaling each dimension to sum to 1.0 (or close to it for by_volume,
    which can legitimately exceed 1.0 because of high-turnover classes).

    Per-dimension targets:
      - by_holdings: must sum to ~1.0 (zero-sum across all holders)
      - by_account_count: must sum to ~1.0
      - by_volume: capped at 1.5 (high-turnover classes contribute more)
    """
    for dim, target_sum in (
        ("by_holdings", 1.0),
        ("by_account_count", 1.0),
        ("by_volume", 1.5),
    ):
        values = []
        for block in persona_blocks:
            ms = block.get("market_share", {})
            v = ms.get(dim)
            if v is not None:
                values.append((block, float(v)))

        if not values:
            continue
        current_sum = sum(v for _, v in values)
        if current_sum <= 0:
            continue

        # Only normalize if the sum is meaningfully off from target (>20% off)
        if abs(current_sum - target_sum) / target_sum > 0.20:
            scale = target_sum / current_sum
            log.info(
                "[normalize] Rescaling %s: sum=%.3f → %.3f (scale=%.3f)",
                dim, current_sum, target_sum, scale,
            )
            for block, _ in values:
                block["market_share"][dim] = round(
                    block["market_share"][dim] * scale, 4
                )


def assemble_pack(
    research: ResearchData,
    persona_blocks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Stage 3 — assemble the final pack dict (ready for yaml dump).

    Runs market_share sum normalization before assembly so the emitted yaml
    has by_holdings ≈ 1.0 by construction.
    """
    # Normalize sums in place
    _normalize_market_share_sums(persona_blocks)

    # Build data_sources from the fetched corpus
    data_sources = []
    for s in research.sources:
        if s.fetch_status != "ok":
            continue
        data_sources.append({
            "id": s.id,
            "name": s.title or s.url,
            "url": s.url,
            "accessed": s.accessed,
            "note": "auto-extracted by persona_factory.research_market",
        })

    pack = {
        "schema_version": SCHEMA_VERSION,
        "market": research.market,
        "locale": research.locale,
        "last_updated": research.research_date,
        "_generated_by": "ssfish.persona_factory",
        "_generation_summary": research.free_form_summary,
        "data_sources": data_sources,
        "personas": persona_blocks,
    }
    return pack


def write_pack_yaml(pack: dict[str, Any], path: Path) -> None:
    """Write the pack dict to a YAML file with sensible formatting."""
    import yaml as yamllib
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yamllib.safe_dump(
            pack,
            f,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
            width=120,
        )
    log.info("[emit] Wrote pack to %s", path)


def validate_pack_loads(path: Path) -> tuple[bool, str]:
    """Run the pack through load_personas to confirm it's schema-valid.

    Returns (success, message).
    """
    try:
        personas = load_personas(path)
        n = len(personas)
        sums = {
            "by_volume": sum((p.market_share.by_volume or 0) for p in personas),
            "by_holdings": sum((p.market_share.by_holdings or 0) for p in personas),
        }
        return True, (
            f"loaded {n} personas successfully; "
            f"sum_by_volume={sums['by_volume']:.3f}, "
            f"sum_by_holdings={sums['by_holdings']:.3f}"
        )
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


# ─────────────────────── Top-level orchestrator ───────────────────────


async def generate_persona_pack(
    descriptor: MarketDescriptor | str,
    output_path: Path | None = None,
    use_cache: bool = True,
    seed: int | None = None,
    max_urls: int = 25,
) -> tuple[Path, dict[str, Any]]:
    """End-to-end pipeline: research → generate → assemble → validate → emit.

    Args:
        descriptor: market descriptor (or known slug)
        output_path: where to write the yaml. Defaults to
            personas/<slug>-auto-<date>.yaml
        use_cache: reuse cached research for the day if available
        seed: deterministic seed for LLM calls
        max_urls: cap on URLs to fetch in research stage

    Returns:
        (output_path, summary_dict). The summary contains stats useful for
        showing the user a diff or sanity check (n_personas, sums, cost,
        elapsed, etc.).
    """
    import time
    from .llm_client import cost_tracker

    if isinstance(descriptor, str):
        if descriptor not in KNOWN_MARKETS:
            raise ValueError(f"Unknown market: {descriptor}")
        descriptor = KNOWN_MARKETS[descriptor]

    cost_at_start = cost_tracker.total_cost_usd
    t0 = time.time()
    date_str = now_utc8_date()

    if output_path is None:
        output_path = auto_pack_path_for(descriptor.slug, date_str)

    log.info("[pipeline] Starting persona pack generation for market=%s", descriptor.slug)

    # Stage 1: research
    research = await research_market(
        descriptor=descriptor,
        use_cache=use_cache,
        cache_date=date_str,
        seed=seed,
        max_urls=max_urls,
    )
    log.info("[pipeline] Stage 1 done: %d classes from %d sources",
             len(research.classes),
             sum(1 for s in research.sources if s.fetch_status == "ok"))

    # Stage 2: generate persona blocks in parallel
    persona_blocks = await generate_personas(research, seed=seed)
    log.info("[pipeline] Stage 2 done: %d persona blocks generated", len(persona_blocks))

    # Stage 3: assemble + write yaml
    pack = assemble_pack(research, persona_blocks)
    write_pack_yaml(pack, output_path)

    # Stage 4: validate via the loader
    valid, msg = validate_pack_loads(output_path)
    log.info("[pipeline] Stage 4 validation: %s — %s",
             "OK" if valid else "FAILED", msg)

    elapsed = time.time() - t0
    cost = cost_tracker.total_cost_usd - cost_at_start

    summary = {
        "market": descriptor.slug,
        "output_path": str(output_path),
        "n_personas": len(persona_blocks),
        "n_sources_fetched": sum(1 for s in research.sources if s.fetch_status == "ok"),
        "n_classes_from_research": len(research.classes),
        "elapsed_seconds": round(elapsed, 1),
        "cost_usd": round(cost, 4),
        "validates": valid,
        "validation_message": msg,
    }
    log.info("[pipeline] DONE in %.1fs ($%.4f): %s", elapsed, cost, summary)
    return output_path, summary


__all__ = [
    "GENERIC_RESEARCH_QUESTIONS",
    "KNOWN_MARKETS",
    "LOCALE_NATIVE_QUERIES",
    "MarketDescriptor",
    "ResearchData",
    "ResearchSource",
    "CapitalTier",
    "ParticipantClass",
    "assemble_pack",
    "fetch_corpus",
    "generate_persona_pack",
    "generate_personas",
    "research_market",
    "search_web",
    "now_utc8_date",
    "auto_pack_path_for",
    "cache_path_for",
    "validate_pack_loads",
    "write_pack_yaml",
]
