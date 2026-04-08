"""Tests for oasis_persona_adapter.build_agent_graph."""

from __future__ import annotations

import pytest

from oasis.social_platform.channel import Channel
from oasis.social_platform.typing import ActionType

from ssfish.oasis_persona_adapter import (
    INFO_ACTIONS,
    MARKET_AGENT_ACTIONS,
    MARKET_AGENT_ID_NAME,
    TRADER_ACTIONS,
    _actions_for,
    _resolve_follows,
    build_agent_graph,
)
from ssfish.persona import MarketShare, Persona, PublishConfig, SandboxConfig


# ─────────────────────── Fixtures ───────────────────────


def _make_trader(pid: str, follows: list[str] | None = None) -> Persona:
    return Persona(
        id=pid,
        archetype="测试 trader",
        display_name="测试散户",
        voice_prompt="test voice",
        decision_mode="discretionary",
        role="directional_speculator",
        market_share=MarketShare(by_volume=0.10),
        entity_role="trader",
        follows=follows or [],
        sandbox=SandboxConfig(
            instance_count=10,
            capital_distribution={"type": "fixed", "value_cny": 100000},
            initial_position_distribution={"type": "none"},
            risk={"max_position_pct": 0.95},
            action_space=[
                {"name": "hold", "side": "none", "pool": "none", "fraction": 0.0},
            ],
        ),
    )


def _make_news_wire(pid: str, follows: list[str] | None = None) -> Persona:
    return Persona(
        id=pid,
        archetype="财经媒体",
        display_name="财经新闻媒体",
        voice_prompt="客观陈述事实",
        decision_mode="discretionary",
        role="information_intermediary",
        market_share=MarketShare(by_volume=0.0),
        entity_role="media",
        follows=follows or [],
        publishes=[
            PublishConfig(
                content_type="news_brief",
                style_hint="客观, 简短",
                trigger_prob=0.5,
                authority_weight=0.7,
            )
        ],
    )


# ─────────────────────── Action set selection ───────────────────────


class TestActionSetSelection:

    def test_trader_gets_full_action_set(self):
        p = _make_trader("t1")
        actions = _actions_for(p)
        assert ActionType.CREATE_POST in actions
        assert ActionType.FOLLOW in actions
        assert ActionType.LIKE_POST in actions
        assert ActionType.REPOST in actions

    def test_info_entity_gets_restricted_set(self):
        p = _make_news_wire("news1")
        actions = _actions_for(p)
        assert ActionType.CREATE_POST in actions
        assert ActionType.REPOST in actions
        # No FOLLOW for info entities (their follow graph is static)
        assert ActionType.FOLLOW not in actions


# ─────────────────────── Graph build ───────────────────────


class TestBuildAgentGraph:

    def test_empty_personas_raises(self):
        with pytest.raises(ValueError, match="at least one"):
            build_agent_graph([], Channel())

    def test_single_trader_graph(self):
        personas = [_make_trader("alice")]
        graph, id_map = build_agent_graph(personas, Channel())
        # 1 trader + 1 market agent = 2 nodes
        assert graph.get_num_nodes() == 2
        assert "alice" in id_map
        assert MARKET_AGENT_ID_NAME in id_map
        # Trader follows market → 1 edge
        assert graph.get_num_edges() == 1

    def test_persona_id_mapping_is_unique(self):
        personas = [_make_trader("a"), _make_trader("b"), _make_news_wire("c")]
        graph, id_map = build_agent_graph(personas, Channel())
        ids = list(id_map.values())
        assert len(ids) == len(set(ids))  # all unique
        assert len(id_map) == 4  # 3 personas + market

    def test_traders_follow_market_implicitly(self):
        personas = [_make_trader("a"), _make_trader("b"), _make_news_wire("c")]
        graph, id_map = build_agent_graph(personas, Channel())
        edges = graph.get_edges()
        market_idx = id_map[MARKET_AGENT_ID_NAME]
        a_idx = id_map["a"]
        b_idx = id_map["b"]
        c_idx = id_map["c"]
        # Both traders should have an edge to market
        assert (a_idx, market_idx) in edges
        assert (b_idx, market_idx) in edges
        # The news_wire (non-trader) should NOT auto-follow market
        assert (c_idx, market_idx) not in edges

    def test_explicit_follows_resolved(self):
        personas = [
            _make_news_wire("news_source"),
            _make_trader("trader_x", follows=["news_source"]),
        ]
        graph, id_map = build_agent_graph(personas, Channel())
        edges = graph.get_edges()
        assert (id_map["trader_x"], id_map["news_source"]) in edges

    def test_wildcard_follows_all(self):
        personas = [
            _make_trader("a"),
            _make_trader("b"),
            _make_news_wire("c", follows=["*"]),
        ]
        graph, id_map = build_agent_graph(personas, Channel())
        edges = set(graph.get_edges())
        c_idx = id_map["c"]
        a_idx = id_map["a"]
        b_idx = id_map["b"]
        # c should follow both a and b (but not itself, not market)
        assert (c_idx, a_idx) in edges
        assert (c_idx, b_idx) in edges
        # c should NOT follow itself
        assert (c_idx, c_idx) not in edges

    def test_market_special_value_follows_market_agent(self):
        personas = [_make_news_wire("news", follows=["__market__"])]
        graph, id_map = build_agent_graph(personas, Channel())
        edges = graph.get_edges()
        assert (id_map["news"], id_map[MARKET_AGENT_ID_NAME]) in edges

    def test_self_follow_filtered(self):
        # Even if a persona claims to follow itself, the adapter should filter
        personas = [_make_trader("a", follows=["a"])]
        graph, id_map = build_agent_graph(personas, Channel())
        edges = graph.get_edges()
        a_idx = id_map["a"]
        assert (a_idx, a_idx) not in edges


# ─────────────────────── Follow graph utility ───────────────────────


class TestResolveFollows:

    def test_known_id(self):
        personas = [_make_trader("a"), _make_news_wire("b")]
        id_map = {"a": 0, "b": 1, MARKET_AGENT_ID_NAME: 2}
        a = personas[0]
        a.follows = ["b"]
        result = _resolve_follows(a, id_map, personas)
        assert result == {1}

    def test_wildcard_returns_all_personas(self):
        personas = [_make_trader("a"), _make_news_wire("b"), _make_trader("c")]
        id_map = {"a": 0, "b": 1, "c": 2, MARKET_AGENT_ID_NAME: 3}
        a = personas[0]
        a.follows = ["*"]
        result = _resolve_follows(a, id_map, personas)
        # Wildcard returns ALL personas (the engine filters self-follow elsewhere)
        assert result == {0, 1, 2}

    def test_market_special_value(self):
        personas = [_make_trader("a")]
        id_map = {"a": 0, MARKET_AGENT_ID_NAME: 1}
        a = personas[0]
        a.follows = [MARKET_AGENT_ID_NAME]
        result = _resolve_follows(a, id_map, personas)
        assert result == {1}

    def test_unknown_id_skipped(self, caplog):
        personas = [_make_trader("a")]
        id_map = {"a": 0, MARKET_AGENT_ID_NAME: 1}
        a = personas[0]
        a.follows = ["nonexistent"]
        result = _resolve_follows(a, id_map, personas)
        assert result == set()
