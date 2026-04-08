"""Event input schema.

An Event is what the user uploads. The schema explicitly includes prior context
because, per Codex's review, "markets price surprise relative to consensus, not
events in isolation". Without prior_consensus / recent_price_action, the
simulation only sees ~25% of the actual signal.

All non-essential fields default to empty strings — the user can leave them
blank but the system warns about reduced confidence.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


VALID_EVENT_TYPES = {
    "earnings",
    "policy",
    "m_a",
    "management_change",
    "ipo",
    "dividend",
    "shareholder_action",
    "lawsuit",
    "regulatory",
    "other",
}


@dataclass
class Event:
    ticker: str
    event_text: str
    event_type: str
    event_date: str  # YYYY-MM-DD
    prior_consensus: str = ""
    recent_price_action: str = ""
    sector_context: str = ""

    def __post_init__(self) -> None:
        # Light validation, no exceptions on optional context (just warnings)
        if not self.ticker or not self.ticker.strip():
            raise ValueError("Event.ticker is required")
        if not self.event_text or not self.event_text.strip():
            raise ValueError("Event.event_text is required")
        if self.event_type not in VALID_EVENT_TYPES:
            raise ValueError(
                f"Event.event_type '{self.event_type}' not in {sorted(VALID_EVENT_TYPES)}"
            )
        if not self.event_date or len(self.event_date) != 10:
            raise ValueError("Event.event_date must be YYYY-MM-DD")

    @property
    def context_completeness(self) -> float:
        """Fraction of optional context fields populated. Used in confidence scoring."""
        optional = [self.prior_consensus, self.recent_price_action, self.sector_context]
        filled = sum(1 for f in optional if f.strip())
        return filled / len(optional)

    @property
    def text_hash(self) -> str:
        """Stable hash of the event text — for scorecard dedup and replay."""
        h = hashlib.sha256()
        h.update(self.ticker.encode())
        h.update(self.event_date.encode())
        h.update(self.event_text.encode())
        return h.hexdigest()[:16]

    def to_simulation_input(self) -> str:
        """Render the full event context as a prompt block for personas."""
        parts = [
            f"# Event being analyzed",
            f"  - Ticker: {self.ticker}",
            f"  - Date: {self.event_date}",
            f"  - Type: {self.event_type}",
            f"",
            f"## Event text",
            self.event_text.strip(),
        ]
        if self.prior_consensus.strip():
            parts.extend(["", "## Prior consensus / what was already priced in",
                         self.prior_consensus.strip()])
        if self.recent_price_action.strip():
            parts.extend(["", "## Recent price action", self.recent_price_action.strip()])
        if self.sector_context.strip():
            parts.extend(["", "## Sector context", self.sector_context.strip()])
        return "\n".join(parts)


__all__ = ["Event", "VALID_EVENT_TYPES"]
