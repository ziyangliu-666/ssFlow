"""Feed reader: pull recent posts from the OASIS SQLite db, filtered by follow graph.

Phase I — connects the OASIS social layer (which persists everything to SQLite
during `env.step()`) to our trading layer (which needs structured `Publication`
context to make order decisions). The trading layer doesn't read OASIS state
directly; it goes through this module.

Architecture:

  1. Each persona has a `follows: list[str]` from its YAML config.
  2. The `oasis_persona_adapter.build_agent_graph` wires those into the
     in-memory `AgentGraph` as directed edges (follower → followee).
  3. After every OASIS `env.step()`, this module:
       a. Resolves each persona's followee set via the AgentGraph
       b. Maps oasis agent_ids → OASIS user_ids via the user table
       c. SELECTs recent posts from those user_ids
       d. Wraps each row in a `Publication` dataclass
       e. Returns the list, ranked by (authority × recency)
  4. The trading layer formats the `list[Publication]` into a markdown feed
     block and passes it as user-message context to `chat_json_sync`.

The `content_type` of a publication is NOT stored in the OASIS db (which only
has `content` text). For Phase I, we use a **content type registry** owned by
the engine: when a publication is created (either by ManualAction or by tracking
which publishing component fired this round), the engine records
`(post_id, content_type, author_persona_id, authority_weight)` in a Python
dict that the feed reader joins against. This avoids forking OASIS to add a
column.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from oasis.social_agent.agent_graph import AgentGraph

from .information import Publication


log = logging.getLogger(__name__)


# ─────────────────────── Content type registry ───────────────────────


@dataclass
class PublicationMetadata:
    """Out-of-band metadata for a published post.

    OASIS only stores text in its post table; this metadata is what the engine
    keeps in memory so the feed reader can recover content type, authority
    weight, and the structured `references` list.
    """

    content_type: str
    author_persona_id: str
    author_archetype: str
    authority_weight: float = 0.5
    round_idx: int = 0
    references: list[str] = field(default_factory=list)


class PublicationRegistry:
    """Tracks `(oasis_post_id → PublicationMetadata)` for the duration of a sim.

    The engine populates this registry whenever a publication is created
    (either via ManualAction or after extracting LLM-driven posts). The feed
    reader queries it when rendering feed entries to recover the content type
    and authority weight that aren't in the OASIS db.
    """

    def __init__(self) -> None:
        self._by_post_id: dict[int, PublicationMetadata] = {}

    def register(self, post_id: int, meta: PublicationMetadata) -> None:
        self._by_post_id[post_id] = meta

    def get(self, post_id: int) -> Optional[PublicationMetadata]:
        return self._by_post_id.get(post_id)

    def all_metadata(self) -> dict[int, PublicationMetadata]:
        return dict(self._by_post_id)


# ─────────────────────── DB helpers ───────────────────────


def _agent_to_user_id_map(db_path: str | Path) -> dict[int, int]:
    """Build {oasis_agent_id → user_id} mapping by querying the user table.

    The OASIS user table is populated when env.reset() signs up agents. This
    is the canonical join from our integer agent_id (assigned by build_agent_graph)
    to the autoincrementing user_id used in the post and follow tables.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT agent_id, user_id FROM user WHERE agent_id IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    return {int(agent_id): int(user_id) for agent_id, user_id in rows}


def _followees_in_graph(
    follower_agent_id: int, agent_graph: AgentGraph
) -> set[int]:
    """Return the set of OASIS agent_ids that `follower_agent_id` follows
    in the in-memory AgentGraph.

    Edge direction in our adapter is `follower → followee`, matching how
    `oasis_persona_adapter.build_agent_graph` wires them.
    """
    out: set[int] = set()
    for src, dst in agent_graph.get_edges():
        if src == follower_agent_id:
            out.add(dst)
    return out


# ─────────────────────── Main entry: feed_for ───────────────────────


def feed_for(
    follower_persona_id: str,
    persona_id_to_oasis_id: dict[str, int],
    agent_graph: AgentGraph,
    db_path: str | Path,
    publication_registry: PublicationRegistry,
    *,
    recent_limit: int = 12,
    include_self_posts: bool = False,
) -> list[Publication]:
    """Build a feed for one persona by querying the OASIS db.

    Steps:
      1. Translate `follower_persona_id` → oasis agent_id via the id map
      2. Find followee agent_ids from the AgentGraph follow edges
      3. Translate followee agent_ids → user_ids via the user table
      4. SELECT recent posts from those user_ids (limit, ordered by created_at DESC)
      5. Wrap each post in a Publication, joining metadata from the registry
      6. Sort by (authority_weight × recency), return top N

    Args:
        follower_persona_id: ssFish persona id (NOT the oasis agent_id) of the
            entity we're building a feed for. Typically a trader.
        persona_id_to_oasis_id: returned from `build_agent_graph`.
        agent_graph: the in-memory AgentGraph (source of truth for static follows).
        db_path: path to the OASIS SQLite db (`env.platform.db_path`).
        publication_registry: shared registry that the engine populates as
            publications are created. Used to recover content type / authority.
        recent_limit: cap on returned publications (default 12).
        include_self_posts: if True, include posts authored by the follower
            themselves (rare; defaults False).

    Returns:
        list[Publication], sorted descending by (authority_weight × recency_score).
    """
    if follower_persona_id not in persona_id_to_oasis_id:
        log.warning(
            "feed_for: unknown persona id '%s'; returning empty feed",
            follower_persona_id,
        )
        return []

    follower_agent_id = persona_id_to_oasis_id[follower_persona_id]
    followee_agent_ids = _followees_in_graph(follower_agent_id, agent_graph)
    if not followee_agent_ids:
        return []

    agent_to_user = _agent_to_user_id_map(db_path)
    follower_user_id = agent_to_user.get(follower_agent_id)
    followee_user_ids = {
        agent_to_user[a] for a in followee_agent_ids if a in agent_to_user
    }
    if not followee_user_ids:
        return []

    if not include_self_posts and follower_user_id is not None:
        followee_user_ids.discard(follower_user_id)
        if not followee_user_ids:
            return []

    # Query recent posts from followees
    placeholders = ",".join("?" * len(followee_user_ids))
    sql = f"""
        SELECT post_id, user_id, original_post_id, content,
               quote_content, created_at, num_likes, num_shares
        FROM post
        WHERE user_id IN ({placeholders})
        ORDER BY post_id DESC
        LIMIT ?
    """
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            sql, [*followee_user_ids, recent_limit * 3]  # over-fetch then re-rank
        ).fetchall()
    finally:
        conn.close()

    # Build user_id → persona_id reverse lookup for the post authors
    user_to_agent = {v: k for k, v in agent_to_user.items()}
    oasis_id_to_persona_id = {v: k for k, v in persona_id_to_oasis_id.items()}

    publications: list[Publication] = []
    for row in rows:
        post_id, user_id, original_post_id, content, _quote, _ts, num_likes, num_shares = row
        author_agent_id = user_to_agent.get(user_id)
        if author_agent_id is None:
            continue
        author_persona_id = oasis_id_to_persona_id.get(author_agent_id, f"agent_{author_agent_id}")

        meta = publication_registry.get(int(post_id))
        if meta is not None:
            content_type = meta.content_type
            archetype = meta.author_archetype
            authority_weight = meta.authority_weight
            round_idx = meta.round_idx
            references = list(meta.references)
        else:
            # Unregistered post — happens for OASIS LLM-driven posts that the
            # engine didn't pre-categorize. Fall back to "social_post" + default
            # weight.
            content_type = "social_post"
            archetype = author_persona_id
            authority_weight = 0.3
            round_idx = 0
            references = []

        references_with_original = list(references)
        if original_post_id is not None:
            references_with_original.append(f"post:{int(original_post_id)}")

        publications.append(
            Publication(
                publication_id=f"post:{int(post_id)}",
                author_persona_id=author_persona_id,
                author_archetype=archetype,
                content_type=content_type,
                text=str(content or ""),
                round_idx=round_idx,
                authority_weight=authority_weight,
                references=references_with_original,
                oasis_post_id=int(post_id),
                likes=int(num_likes or 0),
                reposts=int(num_shares or 0),
            )
        )

    # Re-rank by (authority_weight × recency_score). Recency: newer = higher.
    # We approximate recency by the row order (already DESC by post_id) — give
    # earlier rows a higher score.
    n = len(publications)
    if n == 0:
        return []
    ranked = []
    for idx, pub in enumerate(publications):
        recency_score = 1.0 - (idx / n)
        score = pub.authority_weight * recency_score
        ranked.append((score, pub))
    ranked.sort(key=lambda x: x[0], reverse=True)
    return [pub for _, pub in ranked[:recent_limit]]


def render_feed_as_text(publications: list[Publication]) -> str:
    """Render a list of Publications as a compact markdown block for prompts.

    Used by the trading layer to feed the OASIS social context into a
    structured trading-decision LLM call.
    """
    if not publications:
        return "(no publications in your feed this round)"
    lines = []
    for pub in publications:
        prefix = f"[{pub.content_type}]"
        if pub.references:
            cite_marker = f" (citing {pub.references[0]})"
        else:
            cite_marker = ""
        body = pub.text.replace("\n", " ").strip()[:240]
        lines.append(
            f"{prefix} {pub.author_archetype}{cite_marker}: {body}"
        )
    return "\n".join(lines)


__all__ = [
    "Publication",
    "PublicationMetadata",
    "PublicationRegistry",
    "feed_for",
    "render_feed_as_text",
]
