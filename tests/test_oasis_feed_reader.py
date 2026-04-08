"""Tests for oasis_feed_reader.

Doesn't actually run OASIS — instead seeds a SQLite db with the same schema
shape OASIS uses, plus a tiny mock AgentGraph, and exercises the query +
filtering logic directly.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from oasis.social_agent.agent_graph import AgentGraph

from ssflow.information import Publication
from ssflow.oasis_feed_reader import (
    PublicationMetadata,
    PublicationRegistry,
    _agent_to_user_id_map,
    _followees_in_graph,
    feed_for,
    render_feed_as_text,
)


# ─────────────────────── Fixtures ───────────────────────


def _seed_db(db_path: Path, agent_user_pairs: list[tuple[int, int, str]]) -> None:
    """Create a minimal OASIS-shaped SQLite db.

    `agent_user_pairs` is [(agent_id, user_id, user_name), ...].
    """
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE user (
            user_id INTEGER PRIMARY KEY,
            agent_id INTEGER,
            user_name TEXT,
            name TEXT,
            bio TEXT,
            created_at DATETIME,
            num_followings INTEGER DEFAULT 0,
            num_followers INTEGER DEFAULT 0
        );
        CREATE TABLE post (
            post_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            original_post_id INTEGER,
            content TEXT DEFAULT '',
            quote_content TEXT,
            created_at DATETIME,
            num_likes INTEGER DEFAULT 0,
            num_dislikes INTEGER DEFAULT 0,
            num_shares INTEGER DEFAULT 0,
            num_reports INTEGER DEFAULT 0
        );
        """
    )
    for agent_id, user_id, user_name in agent_user_pairs:
        conn.execute(
            "INSERT INTO user (user_id, agent_id, user_name, name, bio) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, agent_id, user_name, user_name, ""),
        )
    conn.commit()
    conn.close()


def _add_post(db_path: Path, user_id: int, content: str, original_post_id: int | None = None) -> int:
    conn = sqlite3.connect(str(db_path))
    cur = conn.execute(
        "INSERT INTO post (user_id, content, original_post_id) VALUES (?, ?, ?)",
        (user_id, content, original_post_id),
    )
    post_id = cur.lastrowid
    conn.commit()
    conn.close()
    return post_id


def _make_graph(edges: list[tuple[int, int]], num_nodes: int) -> AgentGraph:
    """Build an AgentGraph manually with `num_nodes` nodes (no SocialAgents,
    just vertices) and the given follow edges.

    The feed_reader doesn't dereference the AgentGraph's `agent_mappings`
    dict — it only calls `get_edges()` — so we can leave it empty.
    """
    graph = AgentGraph()
    for i in range(num_nodes):
        graph.graph.add_vertex(str(i))  # use string names per igraph 0.11 advice
    # Map node names back to indices for add_edge
    for src, dst in edges:
        try:
            graph.graph.add_edge(str(src), str(dst))
        except Exception:
            pass
    return graph


# ─────────────────────── _agent_to_user_id_map ───────────────────────


class TestAgentToUserMapping:

    def test_basic_mapping(self, tmp_path):
        db = tmp_path / "test.db"
        _seed_db(db, [(0, 100, "alice"), (1, 101, "bob"), (2, 102, "carol")])
        result = _agent_to_user_id_map(db)
        assert result == {0: 100, 1: 101, 2: 102}

    def test_skips_null_agent_id(self, tmp_path):
        db = tmp_path / "test.db"
        _seed_db(db, [(0, 100, "a")])
        # Insert a row with NULL agent_id
        conn = sqlite3.connect(str(db))
        conn.execute("INSERT INTO user (user_id, agent_id, user_name) VALUES (200, NULL, 'orphan')")
        conn.commit()
        conn.close()

        result = _agent_to_user_id_map(db)
        assert 0 in result
        # Orphan with NULL agent_id is filtered out
        assert None not in result


# ─────────────────────── _followees_in_graph ───────────────────────


class TestFolloweesInGraph:

    def test_simple_follow(self):
        # Note: igraph .add_edge takes vertex names as strings since we used
        # add_vertex(str(i)). The edge indices are still the 0-based positions.
        graph = _make_graph(edges=[(0, 1), (0, 2)], num_nodes=3)
        # _followees_in_graph reads from graph.get_edges() which returns
        # (source, target) integer position tuples. With our string-named
        # vertices, the indices are 0,1,2 in insertion order.
        followees = _followees_in_graph(0, graph)
        assert followees == {1, 2}

    def test_no_follows(self):
        graph = _make_graph(edges=[], num_nodes=3)
        assert _followees_in_graph(0, graph) == set()

    def test_only_outgoing_edges(self):
        # 0 follows 1 and 2; 1 follows 0. From 0's perspective, only 1 and 2.
        graph = _make_graph(edges=[(0, 1), (0, 2), (1, 0)], num_nodes=3)
        assert _followees_in_graph(0, graph) == {1, 2}
        assert _followees_in_graph(1, graph) == {0}
        assert _followees_in_graph(2, graph) == set()


# ─────────────────────── feed_for end-to-end ───────────────────────


class TestFeedFor:

    def test_basic_feed(self, tmp_path):
        db = tmp_path / "test.db"
        _seed_db(db, [
            (0, 100, "trader_alice"),
            (1, 101, "analyst_bob"),
            (2, 102, "news_carol"),
        ])
        # alice (0) follows bob (1) and carol (2)
        graph = _make_graph(edges=[(0, 1), (0, 2)], num_nodes=3)

        # Bob and Carol post things
        post1 = _add_post(db, user_id=101, content="Bob says: BYD Q1 strong")
        post2 = _add_post(db, user_id=102, content="Carol reports: market reacts to Q1")

        registry = PublicationRegistry()
        registry.register(post1, PublicationMetadata(
            content_type="research_note", author_persona_id="bob",
            author_archetype="分析师", authority_weight=0.8,
        ))
        registry.register(post2, PublicationMetadata(
            content_type="news_brief", author_persona_id="carol",
            author_archetype="新闻", authority_weight=0.6,
        ))

        id_map = {"alice": 0, "bob": 1, "carol": 2}
        result = feed_for("alice", id_map, graph, db, registry)
        assert len(result) == 2
        # Should be sorted by authority × recency; bob (0.8) likely first
        assert any(p.author_persona_id == "bob" for p in result)
        assert any(p.author_persona_id == "carol" for p in result)
        # Content type and authority recovered from registry
        bob_pub = next(p for p in result if p.author_persona_id == "bob")
        assert bob_pub.content_type == "research_note"
        assert bob_pub.authority_weight == 0.8

    def test_excludes_non_followed_authors(self, tmp_path):
        db = tmp_path / "test.db"
        _seed_db(db, [
            (0, 100, "alice"),
            (1, 101, "followed"),
            (2, 102, "not_followed"),
        ])
        # Alice only follows agent 1
        graph = _make_graph(edges=[(0, 1)], num_nodes=3)

        _add_post(db, user_id=101, content="from followed")
        _add_post(db, user_id=102, content="from not_followed")

        registry = PublicationRegistry()
        id_map = {"alice": 0, "followed": 1, "not_followed": 2}
        result = feed_for("alice", id_map, graph, db, registry)

        assert len(result) == 1
        assert result[0].author_persona_id == "followed"

    def test_unregistered_post_falls_back_to_social_post(self, tmp_path):
        """A post created without a registry entry defaults to social_post type."""
        db = tmp_path / "test.db"
        _seed_db(db, [(0, 100, "alice"), (1, 101, "bob")])
        graph = _make_graph(edges=[(0, 1)], num_nodes=2)
        _add_post(db, user_id=101, content="some post")

        registry = PublicationRegistry()  # empty
        id_map = {"alice": 0, "bob": 1}
        result = feed_for("alice", id_map, graph, db, registry)
        assert len(result) == 1
        assert result[0].content_type == "social_post"
        assert result[0].authority_weight == 0.3  # fallback

    def test_recent_limit_respected(self, tmp_path):
        db = tmp_path / "test.db"
        _seed_db(db, [(0, 100, "alice"), (1, 101, "bob")])
        graph = _make_graph(edges=[(0, 1)], num_nodes=2)
        for i in range(20):
            _add_post(db, user_id=101, content=f"post {i}")

        registry = PublicationRegistry()
        id_map = {"alice": 0, "bob": 1}
        result = feed_for("alice", id_map, graph, db, registry, recent_limit=5)
        assert len(result) == 5

    def test_unknown_persona_returns_empty(self, tmp_path):
        db = tmp_path / "test.db"
        _seed_db(db, [(0, 100, "alice")])
        graph = _make_graph(edges=[], num_nodes=1)
        result = feed_for(
            "nonexistent",
            {"alice": 0},
            graph,
            db,
            PublicationRegistry(),
        )
        assert result == []

    def test_no_followees_returns_empty(self, tmp_path):
        db = tmp_path / "test.db"
        _seed_db(db, [(0, 100, "alice")])
        graph = _make_graph(edges=[], num_nodes=1)
        result = feed_for("alice", {"alice": 0}, graph, db, PublicationRegistry())
        assert result == []

    def test_repost_includes_original_in_references(self, tmp_path):
        db = tmp_path / "test.db"
        _seed_db(db, [(0, 100, "alice"), (1, 101, "bob")])
        graph = _make_graph(edges=[(0, 1)], num_nodes=2)
        original_id = _add_post(db, user_id=101, content="original")
        _add_post(db, user_id=101, content="repost", original_post_id=original_id)

        result = feed_for(
            "alice", {"alice": 0, "bob": 1}, graph, db, PublicationRegistry(),
        )
        # Both posts should appear; the repost should reference the original
        repost = next(p for p in result if "repost" in p.text)
        assert f"post:{original_id}" in repost.references


# ─────────────────────── render_feed_as_text ───────────────────────


class TestRenderFeedAsText:

    def test_empty_feed(self):
        assert "no publications" in render_feed_as_text([])

    def test_renders_publications_with_metadata(self):
        pubs = [
            Publication(
                publication_id="post:1",
                author_persona_id="bob",
                author_archetype="分析师",
                content_type="research_note",
                text="Test research note",
                round_idx=0,
                authority_weight=0.8,
            ),
            Publication(
                publication_id="post:2",
                author_persona_id="carol",
                author_archetype="新闻",
                content_type="news_brief",
                text="Test news",
                round_idx=0,
                authority_weight=0.6,
            ),
        ]
        text = render_feed_as_text(pubs)
        assert "[research_note]" in text
        assert "[news_brief]" in text
        assert "分析师" in text
        assert "Test research note" in text

    def test_truncates_long_text(self):
        long_text = "a" * 500
        pub = Publication(
            publication_id="x", author_persona_id="x", author_archetype="x",
            content_type="news_brief", text=long_text, round_idx=0,
        )
        rendered = render_feed_as_text([pub])
        assert len(rendered) < 350  # truncated to 240 + prefix

    def test_includes_citation_marker_when_references_present(self):
        pub = Publication(
            publication_id="post:5", author_persona_id="x", author_archetype="X",
            content_type="news_brief", text="repost text",
            round_idx=0, references=["post:1"],
        )
        rendered = render_feed_as_text([pub])
        assert "citing post:1" in rendered
