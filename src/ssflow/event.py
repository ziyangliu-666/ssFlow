"""Event input schema.

An Event is what the user uploads. The schema explicitly includes prior context
because, per Codex's review, "markets price surprise relative to consensus, not
events in isolation". Without prior_consensus / recent_price_action, the
simulation only sees ~25% of the actual signal.

This module is currency-agnostic. The Event carries:
  - `current_price`     in the instrument's native currency
  - `adv_value`         in the same currency as current_price
  - `price_currency`    e.g. "CNY", "USD", "JPY", "BTC"
  - `market`            slug like "ashare", "us-equity", "crude-oil-wti"
  - `instrument`        display name (e.g. "BYD" or "比亚迪")

VALID_EVENT_TYPES is intentionally broad — covers equity events, commodity
events, crypto events, policy events, etc. Add new types here as new
markets need them.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any


VALID_EVENT_TYPES = {
    # Equity-flavored
    "earnings",
    "ipo",
    "dividend",
    "shareholder_action",
    "m_a",
    "management_change",
    # Commodity-flavored
    "supply_disruption",
    "demand_shock",
    "opec_meeting",
    "inventory_release",
    "weather",
    "geopolitical",
    # Crypto-flavored
    "halving",
    "exchange_event",
    "protocol_upgrade",
    # Universal
    "policy",
    "regulatory",
    "lawsuit",
    "macro",
    "other",
}


@dataclass
class Event:
    ticker: str               # symbol or contract code (e.g. "002594", "NVDA", "CL=F", "BTC-USD")
    event_text: str
    event_type: str
    event_date: str           # YYYY-MM-DD

    # ── Optional context fields (boost simulation accuracy when filled) ──
    prior_consensus: str = ""
    recent_price_action: str = ""
    sector_context: str = ""

    # ── Sandbox required fields (current state of the instrument) ──
    current_price: float | None = None
    adv_value: float | None = None  # average daily volume in the same currency as price

    # ── Market metadata (for currency formatting + persona pack selection) ──
    market: str | None = None        # slug like "ashare", "us-equity"
    price_currency: str = "CNY"      # currency code; default CNY for backward compat
    instrument: str | None = None    # human-readable display name (e.g. "BYD")

    # ── Reserved for future cross-asset spillover modeling ──
    sector_etf_ticker: str | None = None

    # ── Cross-market context (overnight US/HK/commodity moves) ──
    # Operator-provided structured data. Format per entry:
    #   {"market": "us_equity", "ticker": "NVDA", "change_pct": -17.0, ...}
    # Auto-extracted from event_text when not provided.
    cross_market_context: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
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
        if self.current_price is not None and self.current_price <= 0:
            raise ValueError(
                f"Event.current_price must be > 0 if set, got {self.current_price}"
            )
        if self.adv_value is not None and self.adv_value <= 0:
            raise ValueError(
                f"Event.adv_value must be > 0 if set, got {self.adv_value}"
            )
        # Default instrument to ticker if not set
        if self.instrument is None:
            self.instrument = self.ticker

    @property
    def adv_cny(self) -> float | None:
        """Backward-compat alias for adv_value (deprecated)."""
        return self.adv_value

    @property
    def is_sandbox_ready(self) -> bool:
        """True iff the event has the fields required for sandbox-mode simulation."""
        return self.current_price is not None and self.adv_value is not None

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
        market_label = self.market or "unknown market"
        instrument = self.instrument or self.ticker
        parts = [
            f"# Event being analyzed",
            f"  - Market: {market_label}",
            f"  - Instrument: {instrument} ({self.ticker})",
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
