"""Tests for the persona pack calibration pipeline.

All web search + LLM calls are mocked. The pipeline's deterministic
parts (URL deduplication, structural merging, pack assembly, yaml
emission, loader validation roundtrip) are tested with no I/O.

For end-to-end tests against the real LLM, see the manual smoke run
in scripts/generate_pack.py — those are not in the unit test suite
because they cost ~$0.02 each.
"""

from __future__ import annotations

import json
import yaml
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from ssfish.persona import load_personas
from ssfish.persona_factory import (
    DEFAULT_ACTION_TEMPLATES,
    GENERIC_RESEARCH_QUESTIONS,
    KNOWN_MARKETS,
    LOCALE_NATIVE_QUERIES,
    MarketDescriptor,
    ParticipantClass,
    ResearchData,
    ResearchSource,
    _build_search_queries,
    _gather_unique_urls,
    _is_blocked,
    _merge_creative_into_structural,
    _slugify_url,
    assemble_pack,
    generate_persona_pack,
    generate_personas,
    research_market,
    validate_pack_loads,
    write_pack_yaml,
)


# ─────────────────────── Constants ───────────────────────


class TestConstants:

    def test_known_markets_have_required_fields(self):
        for slug, descriptor in KNOWN_MARKETS.items():
            assert descriptor.slug == slug
            assert descriptor.search_terms, f"{slug}: search_terms must be non-empty"
            assert descriptor.locale, f"{slug}: locale must be set"

    def test_generic_questions_use_term_placeholder(self):
        for q in GENERIC_RESEARCH_QUESTIONS:
            assert "{term}" in q, f"Question missing {{term}} placeholder: {q}"

    def test_locale_native_queries_use_term_native(self):
        for locale, queries in LOCALE_NATIVE_QUERIES.items():
            for q in queries:
                assert "{term_native}" in q, (
                    f"{locale} query missing {{term_native}} placeholder: {q}"
                )

    def test_default_action_templates_cover_all_decision_modes(self):
        from ssfish.persona import VALID_DECISION_MODES
        for mode in VALID_DECISION_MODES:
            assert mode in DEFAULT_ACTION_TEMPLATES, (
                f"DEFAULT_ACTION_TEMPLATES missing decision_mode: {mode}"
            )


# ─────────────────────── Helpers ───────────────────────


class TestHelpers:

    def test_is_blocked_catches_known_domains(self):
        assert _is_blocked("https://www.linkedin.com/posts/foo")
        assert _is_blocked("https://twitter.com/x/status/1")
        assert _is_blocked("https://x.com/foo/status/2")
        assert not _is_blocked("https://example.com/article")

    def test_slugify_url_produces_short_stable_id(self):
        url = "https://www.fxbaogao.com/detail/4612072"
        slug = _slugify_url(url)
        assert isinstance(slug, str)
        assert len(slug) <= 48
        # Stable: same url → same slug
        assert _slugify_url(url) == slug

    def test_build_search_queries_native_first(self):
        descriptor = KNOWN_MARKETS["ashare"]
        queries = _build_search_queries(descriptor)
        # First query should be native (zh-CN), not English
        assert any("A股" in q for q in queries[:6])
        # Total cap is 12
        assert len(queries) <= 12

    def test_build_search_queries_english_only_market(self):
        descriptor = MarketDescriptor(
            slug="test-market",
            locale="en-US",
            search_terms=["foo bar market"],
            native_search_terms=["foo bar market"],
        )
        queries = _build_search_queries(descriptor)
        assert all("foo bar market" in q for q in queries)


# ─────────────────────── _gather_unique_urls ───────────────────────


class TestGatherUniqueUrls:

    def test_round_robin_quota(self):
        """Each query should contribute its top per_query_quota URLs first."""
        query_results = [
            [{"url": "https://q1-a.com"}, {"url": "https://q1-b.com"}, {"url": "https://q1-c.com"}, {"url": "https://q1-d.com"}],
            [{"url": "https://q2-a.com"}, {"url": "https://q2-b.com"}],
            [{"url": "https://q3-a.com"}, {"url": "https://q3-b.com"}, {"url": "https://q3-c.com"}],
        ]
        urls = _gather_unique_urls(query_results, cap=20, per_query_quota=2)
        # First pass: 2 from each → 6 URLs
        # Second pass: leftover (q1-c, q1-d, q3-c) → 3 more = 9
        assert len(urls) == 9
        # First 6 should be the top-2 from each query, in query order
        assert urls[0] == "https://q1-a.com"
        assert urls[1] == "https://q1-b.com"
        assert urls[2] == "https://q2-a.com"
        assert urls[3] == "https://q2-b.com"
        assert urls[4] == "https://q3-a.com"
        assert urls[5] == "https://q3-b.com"

    def test_cap_respected(self):
        query_results = [
            [{"url": f"https://q{i}.com"}] for i in range(50)
        ]
        urls = _gather_unique_urls(query_results, cap=10, per_query_quota=1)
        assert len(urls) == 10

    def test_dedupe_across_queries(self):
        query_results = [
            [{"url": "https://shared.com"}, {"url": "https://q1-a.com"}],
            [{"url": "https://shared.com"}, {"url": "https://q2-a.com"}],
        ]
        urls = _gather_unique_urls(query_results, cap=20, per_query_quota=2)
        assert urls.count("https://shared.com") == 1

    def test_empty_results(self):
        urls = _gather_unique_urls([], cap=10)
        assert urls == []

    def test_blocked_url_excluded_via_blocklist(self):
        # Note: _gather doesn't itself block; that's done in search_web.
        # But empty/missing URLs are skipped.
        query_results = [
            [{"url": ""}, {"url": "https://valid.com"}],
        ]
        urls = _gather_unique_urls(query_results, cap=10)
        assert urls == ["https://valid.com"]


# ─────────────────────── _merge_creative_into_structural ───────────────────────


def _make_class(
    pid: str = "test",
    archetype: str = "Test Class",
    decision_mode: str = "discretionary",
    by_holdings: float | None = 0.10,
    by_volume: float | None = 0.05,
    by_account_count: float | None = None,
    is_strategic: bool = False,
    capital_min: int | None = 100000,
    capital_max: int | None = 1000000,
    horizon_min: int | None = 1,
    horizon_max: int | None = 30,
    cited_source_ids: list[str] | None = None,
) -> ParticipantClass:
    return ParticipantClass(
        id=pid,
        archetype=archetype,
        sub_archetype=None,
        description="test description",
        decision_mode=decision_mode,
        role="directional_speculator" if not is_strategic else "strategic_holder",
        by_volume=by_volume,
        by_holdings=by_holdings,
        by_account_count=by_account_count,
        typical_capital_min_cny=capital_min,
        typical_capital_max_cny=capital_max,
        typical_holding_period_days_min=horizon_min,
        typical_holding_period_days_max=horizon_max,
        is_strategic=is_strategic,
        cited_source_ids=cited_source_ids or [],
    )


class TestMergeCreativeIntoStructural:

    def test_market_share_carried_through_from_class(self):
        """The merged persona's market_share comes from the class def, not the LLM creative output."""
        class_def = _make_class(by_holdings=0.073, by_volume=0.10, by_account_count=0.0001)
        creative = {
            # LLM tries to set market_share — should be IGNORED
            "market_share": {"by_holdings": 0.99, "by_volume": 0.99},
            "voice_prompt": "test prompt",
            "biases": {"loss_averse": 0.5},
            "sandbox": {
                "instance_count": 50,
                "capital_distribution": {"type": "lognormal", "median_cny": 100000, "sigma": 0.8},
                "initial_position_distribution": {"type": "bernoulli", "prob_holding": 0.5},
                "risk": {"max_position_pct": 0.95},
                "action_space": [
                    {"name": "hold", "side": "none", "pool": "none", "fraction": 0.0}
                ],
            },
        }
        merged = _merge_creative_into_structural(class_def, creative)
        # Structural fields from class_def, not creative
        assert merged["market_share"]["by_holdings"] == 0.073
        assert merged["market_share"]["by_volume"] == 0.10
        assert merged["market_share"]["by_account_count"] == 0.0001
        # Creative fields from creative
        assert merged["voice_prompt"] == "test prompt"
        assert merged["biases"] == {"loss_averse": 0.5}

    def test_strategic_persona_auto_flags(self):
        class_def = _make_class(decision_mode="strategic", is_strategic=True)
        creative = {"voice_prompt": "you are strategic"}
        merged = _merge_creative_into_structural(class_def, creative)
        assert merged["contributes_to_sentiment_mean"] is False
        assert merged["contributes_to_strategic_signal"] is True

    def test_capital_range_carried_from_class(self):
        class_def = _make_class(capital_min=50000, capital_max=300000)
        merged = _merge_creative_into_structural(class_def, {})
        assert merged["capital_range_cny"] == [50000, 300000]

    def test_time_horizon_carried_from_class(self):
        class_def = _make_class(horizon_min=1, horizon_max=14)
        merged = _merge_creative_into_structural(class_def, {})
        assert merged["time_horizon_days"] == [1, 14]

    def test_action_space_falls_back_to_default_template(self):
        """If LLM returns empty/missing action_space, use default for the decision_mode."""
        class_def = _make_class(decision_mode="systematic")
        creative = {"sandbox": {"instance_count": 30}}
        merged = _merge_creative_into_structural(class_def, creative)
        assert len(merged["sandbox"]["action_space"]) >= 1
        names = [a["name"] for a in merged["sandbox"]["action_space"]]
        assert "hold" in names
        assert any("model" in n for n in names)

    def test_strategic_uses_strategic_default_actions(self):
        class_def = _make_class(decision_mode="strategic", is_strategic=True)
        merged = _merge_creative_into_structural(class_def, {})
        names = [a["name"] for a in merged["sandbox"]["action_space"]]
        assert "do_nothing" in names

    def test_market_share_with_no_dimensions_gets_fallback(self):
        """If class has no market_share at all, merge inserts a fallback so loader passes."""
        class_def = _make_class(by_holdings=None, by_volume=None, by_account_count=None)
        merged = _merge_creative_into_structural(class_def, {})
        assert "by_volume" in merged["market_share"]  # fallback inserted
        assert merged["market_share"]["by_volume"] == 0.01

    def test_citations_included_when_present(self):
        class_def = _make_class(cited_source_ids=["src-a", "src-b"])
        merged = _merge_creative_into_structural(class_def, {})
        cites = merged["market_share"]["citations"]
        assert {c["source_id"] for c in cites} == {"src-a", "src-b"}


# ─────────────────────── assemble_pack + write_pack_yaml + validate ───────────────────────


def _make_research(market: str = "test-market", n_classes: int = 3) -> ResearchData:
    return ResearchData(
        market=market,
        locale="en-US",
        research_date="2026-04-08",
        sources=[
            ResearchSource(
                id="src-a",
                url="https://a.com",
                title="A",
                accessed="2026-04-08",
                excerpt="...",
                fetch_status="ok",
            ),
            ResearchSource(
                id="src-b",
                url="https://b.com",
                title="B",
                accessed="2026-04-08",
                excerpt="...",
                fetch_status="failed",  # this one is dropped from data_sources
            ),
        ],
        classes=[
            _make_class(pid=f"class_{i}", by_holdings=0.3, by_volume=0.3)
            for i in range(n_classes)
        ],
        free_form_summary="Test market summary",
    )


class TestAssembleAndEmit:

    def test_assemble_pack_basic_shape(self):
        research = _make_research(n_classes=2)
        # Build minimal persona blocks via merge
        blocks = [
            _merge_creative_into_structural(c, {"voice_prompt": "test"})
            for c in research.classes
        ]
        pack = assemble_pack(research, blocks)
        assert pack["schema_version"] == 2
        assert pack["market"] == "test-market"
        assert pack["locale"] == "en-US"
        assert len(pack["personas"]) == 2
        # Only successful sources are in data_sources
        assert len(pack["data_sources"]) == 1
        assert pack["data_sources"][0]["id"] == "src-a"

    def test_write_and_validate_roundtrip(self, tmp_path):
        research = _make_research(n_classes=3)
        blocks = [
            _merge_creative_into_structural(c, {"voice_prompt": f"test {c.id}"})
            for c in research.classes
        ]
        pack = assemble_pack(research, blocks)
        out_path = tmp_path / "test-auto.yaml"
        write_pack_yaml(pack, out_path)
        assert out_path.exists()
        # File should re-load via the real loader
        valid, msg = validate_pack_loads(out_path)
        assert valid, f"Auto-emitted pack failed loader validation: {msg}"
        assert "loaded 3 personas" in msg

    def test_emitted_pack_loads_via_load_personas(self, tmp_path):
        research = _make_research(n_classes=4)
        blocks = [
            _merge_creative_into_structural(c, {"voice_prompt": "x"})
            for c in research.classes
        ]
        pack = assemble_pack(research, blocks)
        out_path = tmp_path / "roundtrip.yaml"
        write_pack_yaml(pack, out_path)
        personas = load_personas(out_path)
        assert len(personas) == 4
        for p in personas:
            assert p.market_share is not None
            assert p.sandbox is not None
            assert p.sandbox.action_space


# ─────────────────────── generate_personas (mocked LLM) ───────────────────────


class TestGeneratePersonasMocked:

    @pytest.mark.asyncio
    async def test_generate_calls_llm_per_class_and_merges(self, monkeypatch):
        """Mock _llm_generate_creative_fields to return a stub creative dict."""
        research = _make_research(n_classes=3)

        async def fake_creative(class_def, research, seed=None):
            return {
                "voice_prompt": f"voice for {class_def.id}",
                "biases": {"momentum": 0.7},
                "sandbox": {
                    "instance_count": 200,
                    "capital_distribution": {"type": "lognormal", "median_cny": 50000, "sigma": 0.6},
                    "initial_position_distribution": {"type": "bernoulli", "prob_holding": 0.5},
                    "risk": {"max_position_pct": 0.8},
                    "action_space": [
                        {"name": "hold", "side": "none", "pool": "none", "fraction": 0.0},
                        {"name": "trim_20pct", "side": "sell", "pool": "holdings_in_target", "fraction": 0.2},
                    ],
                },
            }

        monkeypatch.setattr(
            "ssfish.persona_factory._llm_generate_creative_fields", fake_creative
        )

        blocks = await generate_personas(research, seed=42)
        assert len(blocks) == 3
        for block in blocks:
            assert "voice for class_" in block["voice_prompt"]
            assert block["sandbox"]["instance_count"] == 200
            # market_share carried through from research
            assert block["market_share"]["by_volume"] == 0.3

    @pytest.mark.asyncio
    async def test_generate_recovers_from_per_class_failure(self, monkeypatch):
        """If one class fails, the others should still complete; failed class
        falls back to a structural-only persona."""
        research = _make_research(n_classes=3)

        async def fake_creative(class_def, research, seed=None):
            if class_def.id == "class_1":
                raise RuntimeError("simulated LLM failure")
            return {"voice_prompt": "ok"}

        monkeypatch.setattr(
            "ssfish.persona_factory._llm_generate_creative_fields", fake_creative
        )

        blocks = await generate_personas(research, seed=42)
        # All 3 should still be present (failed one falls back to structural-only)
        assert len(blocks) == 3
        ids = [b["id"] for b in blocks]
        assert "class_1" in ids


# ─────────────────────── End-to-end pipeline (mocked) ───────────────────────


class TestEndToEndPipelineMocked:

    @pytest.mark.asyncio
    async def test_pipeline_skipped_research_via_cache(self, monkeypatch, tmp_path):
        """If research is cached, the pipeline skips Stage 1 and runs Stage 2-4."""
        # Place a cached research file
        research = _make_research(n_classes=3)
        from ssfish.persona_factory import cache_path_for, now_utc8_date

        # Monkey-patch cache_path_for + auto_pack_path_for to use tmp_path
        date_str = now_utc8_date()
        cache_file = tmp_path / f"{research.market}-{date_str}.json"
        cache_file.write_text(
            json.dumps(research.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        monkeypatch.setattr(
            "ssfish.persona_factory.cache_path_for",
            lambda market, date_str: cache_file,
        )

        out_yaml = tmp_path / "test-auto.yaml"
        monkeypatch.setattr(
            "ssfish.persona_factory.auto_pack_path_for",
            lambda market, date_str: out_yaml,
        )

        # Mock the LLM creative call
        async def fake_creative(class_def, research, seed=None):
            return {
                "voice_prompt": f"v {class_def.id}",
                "sandbox": {"instance_count": 10},
            }

        monkeypatch.setattr(
            "ssfish.persona_factory._llm_generate_creative_fields", fake_creative
        )

        # Build a descriptor for the test market
        descriptor = MarketDescriptor(
            slug="test-market",
            locale="en-US",
            search_terms=["foo"],
        )

        out_path, summary = await generate_persona_pack(
            descriptor=descriptor,
            output_path=out_yaml,
            use_cache=True,
            seed=42,
        )
        assert summary["validates"] is True
        assert summary["n_personas"] == 3
        assert out_path.exists()

        # Verify the file loads via the real loader
        personas = load_personas(out_path)
        assert len(personas) == 3
