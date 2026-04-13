"""Round adapter: maps micro-step events back to round-level records.

The new LOB engine runs on micro-steps (many ticks per trading day).
The legacy report format and frontend expect round-level records. This
adapter accumulates micro-step results into round-level summaries.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .clock import SessionEvent
from ..market.ashare_rules import SessionPhase

log = logging.getLogger(__name__)


@dataclass
class PhaseResult:
    """Aggregated results for one phase within a round."""

    phase: str                    # "fast_react", "slow_react", "settle" or session phase name
    net_flow: float = 0.0        # net buy - sell value
    n_fills: int = 0
    total_volume: float = 0.0    # shares traded
    total_value: float = 0.0     # currency traded
    price_start: float = 0.0
    price_end: float = 0.0
    delta_pct: float = 0.0


@dataclass
class RoundRecord:
    """One round's aggregated results (compatible with legacy format).

    Maps a trading day (or portion of one) to the round-level data
    that report.py and the frontend expect.
    """

    round_idx: int
    day_idx: int
    price_before: float
    price_after: float = 0.0
    delta_pct: float = 0.0
    net_flow: float = 0.0
    n_fills: int = 0
    total_volume: float = 0.0
    total_value: float = 0.0
    phase_results: list[PhaseResult] = field(default_factory=list)
    # Per-persona flow breakdown (for report)
    class_flows: list[dict] = field(default_factory=list)
    # Publications collected during this round
    publications: list[dict] = field(default_factory=list)

    def finalize(self, price_after: float) -> None:
        """Compute derived fields after all phases are processed."""
        self.price_after = price_after
        if self.price_before > 0:
            self.delta_pct = (price_after / self.price_before) - 1.0
        self.net_flow = sum(p.net_flow for p in self.phase_results)
        self.n_fills = sum(p.n_fills for p in self.phase_results)
        self.total_volume = sum(p.total_volume for p in self.phase_results)
        self.total_value = sum(p.total_value for p in self.phase_results)


class RoundAdapter:
    """Accumulates micro-step results into round-level records.

    One round = one trading day. The adapter accumulates fills, flows,
    and price updates across all micro-steps in a day, then produces
    a RoundRecord when the day ends.

    Usage:
        adapter = RoundAdapter()
        for event in clock:
            adapter.on_event(event, exchange_state)
            if event.is_day_end:
                round_record = adapter.finalize_round(exchange.last_price)
    """

    def __init__(self) -> None:
        self._rounds: list[RoundRecord] = []
        self._current: RoundRecord | None = None
        self._current_phase: PhaseResult | None = None

    def on_day_start(self, day_idx: int, price: float) -> None:
        """Start accumulating a new round (trading day)."""
        self._current = RoundRecord(
            round_idx=len(self._rounds),
            day_idx=day_idx,
            price_before=price,
        )

    def on_phase_start(self, phase_name: str, price: float) -> None:
        """Start a new phase within the current round."""
        if self._current is None:
            return
        self._current_phase = PhaseResult(
            phase=phase_name,
            price_start=price,
        )

    def on_fill(self, value: float, quantity: float, is_buy: bool) -> None:
        """Record a fill in the current phase."""
        if self._current_phase is None:
            return
        self._current_phase.n_fills += 1
        self._current_phase.total_volume += quantity
        self._current_phase.total_value += abs(value)
        if is_buy:
            self._current_phase.net_flow += value
        else:
            self._current_phase.net_flow -= value

    def on_phase_end(self, price: float) -> None:
        """Finalize the current phase."""
        if self._current_phase is None:
            return
        self._current_phase.price_end = price
        if self._current_phase.price_start > 0:
            self._current_phase.delta_pct = (
                price / self._current_phase.price_start - 1.0
            )
        if self._current is not None:
            self._current.phase_results.append(self._current_phase)
        self._current_phase = None

    def finalize_round(self, price: float) -> RoundRecord | None:
        """Finalize the current round and return the record."""
        if self._current is None:
            return None
        self._current.finalize(price)
        self._rounds.append(self._current)
        record = self._current
        self._current = None
        return record

    @property
    def rounds(self) -> list[RoundRecord]:
        return list(self._rounds)

    @property
    def current_round_idx(self) -> int:
        return len(self._rounds)


__all__ = ["PhaseResult", "RoundAdapter", "RoundRecord"]
