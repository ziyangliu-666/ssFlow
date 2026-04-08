"""Canonical Publication dataclass for the Phase I information layer.

A `Publication` is one piece of content broadcast in a simulation round.
The OASIS social layer stores posts in its SQLite db; the `oasis_feed_reader`
module reads them out and converts them into `Publication` instances that
the engine, trading layer, and report renderer all consume.

This dataclass is intentionally framework-agnostic — no OASIS imports — so
that swapping the social-layer framework later is a drop-in replacement.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Publication:
    """One piece of content broadcast into the simulation's information stream.

    Attributes:
        publication_id: Stable id for the publication. Phase I uses the OASIS
            post_id (an integer) prefixed with `"post:"` so cross-references
            in `references` are unambiguous strings.
        author_persona_id: ssFlow persona id that authored the publication
            (e.g., `"sellside_analyst_citic"`). For the synthetic market
            broadcaster, this is the literal string `"__market__"`.
        author_archetype: human-readable archetype for display in reports
            (e.g., `"卖方分析师(中信建投)"`).
        content_type: one of the values in
            `ssflow.persona.VALID_CONTENT_TYPES`, plus the special type
            `"market_event"` used by the synthetic market broadcaster.
        text: the publication's body text. Already sanitized through
            `output_filter.sanitize_text` (the OASIS LM adapter handles this
            automatically for LLM-generated content; the engine handles it for
            ManualAction-injected content).
        round_idx: the simulation round in which this publication was emitted.
        authority_weight: 0.0-1.0, how much downstream readers' feeds prioritize
            this content. Pulled from the source persona's `PublishConfig`
            (or defaults to 1.0 for synthetic market events).
        references: list of `publication_id` values this publication cites,
            quotes, or reposts. Empty for original posts. The engine populates
            this from OASIS's `original_post_id` column where applicable.
        oasis_post_id: the raw OASIS post_id (integer) for cross-reference
            against the SQLite db.
        likes: cached like count at the time of read.
        reposts: cached repost count at the time of read.
    """

    publication_id: str
    author_persona_id: str
    author_archetype: str
    content_type: str
    text: str
    round_idx: int
    authority_weight: float = 0.5
    references: list[str] = field(default_factory=list)
    oasis_post_id: int | None = None
    likes: int = 0
    reposts: int = 0


__all__ = ["Publication"]
