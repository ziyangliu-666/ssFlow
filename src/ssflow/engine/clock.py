"""A-share session clock — schedules micro-steps within trading sessions.

Models the intra-day structure of A-share trading:
  09:15-09:20  Pre-open (orders accepted, can cancel)
  09:20-09:25  Call auction (orders accepted, NO cancel)
  09:25-09:30  Match pause (compute opening price)
  09:30-11:30  Continuous AM trading
  11:30-13:00  Lunch break
  13:00-14:57  Continuous PM trading
  14:57-15:00  Closing call auction

For simulation purposes, each "round" in the legacy engine maps to one
trading day. Within each day, the clock produces a sequence of SessionEvents
that the orchestrator processes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from ..market.ashare_rules import SessionPhase

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionEvent:
    """A point in the trading day that triggers orchestrator action."""

    tick: int                     # monotonic simulation tick
    phase: SessionPhase           # current session phase
    day_idx: int                  # trading day index (0 = event day)
    minutes_into_day: int         # minutes from 09:15
    is_phase_start: bool = False  # True on phase transitions
    is_day_start: bool = False    # True on first event of the day
    is_day_end: bool = False      # True on last event of the day


class AShareClock:
    """Generates SessionEvents for A-share trading days.

    Usage:
        clock = AShareClock(n_days=5, ticks_per_session=20)
        for event in clock:
            orchestrator.process(event)
    """

    # Session schedule: (phase, start_minute, end_minute)
    SESSION_SCHEDULE: list[tuple[SessionPhase, int, int]] = [
        (SessionPhase.PRE_OPEN,       0,   5),    # 09:15-09:20
        (SessionPhase.CALL_AUCTION,   5,  10),    # 09:20-09:25
        (SessionPhase.MATCH_PAUSE,   10,  15),    # 09:25-09:30
        (SessionPhase.CONTINUOUS_AM, 15, 135),    # 09:30-11:30 (120 min)
        (SessionPhase.LUNCH_BREAK,  135, 225),    # 11:30-13:00 (90 min)
        (SessionPhase.CONTINUOUS_PM, 225, 342),   # 13:00-14:57 (117 min)
        (SessionPhase.CLOSING_CALL, 342, 345),    # 14:57-15:00 (3 min)
        (SessionPhase.CLOSED,       345, 360),    # 15:00+
    ]

    # Only these phases get simulation ticks (the rest are instant transitions)
    ACTIVE_PHASES = {
        SessionPhase.CALL_AUCTION,
        SessionPhase.CONTINUOUS_AM,
        SessionPhase.CONTINUOUS_PM,
        SessionPhase.CLOSING_CALL,
    }

    def __init__(
        self,
        n_days: int = 5,
        ticks_per_session: int = 40,   # total active ticks per day
    ) -> None:
        self.n_days = n_days
        self.ticks_per_session = ticks_per_session
        self._tick = 0

    def __iter__(self):
        """Yield SessionEvents across all trading days."""
        for day in range(self.n_days):
            yield from self._generate_day(day)

    def _generate_day(self, day_idx: int) -> list[SessionEvent]:
        """Generate events for one trading day."""
        events: list[SessionEvent] = []

        # Compute tick budget per active phase
        total_active_minutes = sum(
            end - start
            for phase, start, end in self.SESSION_SCHEDULE
            if phase in self.ACTIVE_PHASES
        )

        for phase, start_min, end_min in self.SESSION_SCHEDULE:
            if phase not in self.ACTIVE_PHASES:
                # Emit one event for phase transition (no ticks spent)
                events.append(SessionEvent(
                    tick=self._tick,
                    phase=phase,
                    day_idx=day_idx,
                    minutes_into_day=start_min,
                    is_phase_start=True,
                    is_day_start=(phase == SessionPhase.PRE_OPEN),
                    is_day_end=(phase == SessionPhase.CLOSED),
                ))
                continue

            # Active phase: distribute ticks proportionally
            phase_minutes = end_min - start_min
            phase_ticks = max(1, int(
                self.ticks_per_session * phase_minutes / total_active_minutes
            ))

            for i in range(phase_ticks):
                minute = start_min + int(phase_minutes * i / phase_ticks)
                events.append(SessionEvent(
                    tick=self._tick,
                    phase=phase,
                    day_idx=day_idx,
                    minutes_into_day=minute,
                    is_phase_start=(i == 0),
                ))
                self._tick += 1

        return events

    @property
    def total_ticks(self) -> int:
        """Estimate total ticks across all days."""
        return self.n_days * self.ticks_per_session


__all__ = ["AShareClock", "SessionEvent"]
