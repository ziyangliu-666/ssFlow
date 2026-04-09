"""Time-aware round schedule for simulations.

Maps abstract round indices to real calendar periods so agents perceive
the passage of time. Different agent types have different activity levels
per time period (retail active T+0, institutional T+1-T+2, strategic T+5+).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RoundDef:
    """One round in the schedule, bound to a real time period."""

    id: str                          # "T0_AM", "T1", "T5"
    label: str                       # "T+0 上午盘"
    calendar_start: str              # "2026-04-09 09:30"
    calendar_end: str                # "2026-04-09 11:30"
    hours_since_event: float         # 0.0, 2.0, 24.0, etc.
    # Which agent types are most active this round.
    # Empty list = all active equally.
    active_agent_types: list[str] = field(default_factory=list)

    def to_serializable(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "calendar_start": self.calendar_start,
            "calendar_end": self.calendar_end,
            "hours_since_event": self.hours_since_event,
            "active_agent_types": list(self.active_agent_types),
        }


@dataclass
class RoundSchedule:
    """Ordered list of rounds for a simulation, with real time mapping."""

    rounds: list[RoundDef] = field(default_factory=list)
    event_datetime: str = ""         # when the event happened

    @property
    def n_rounds(self) -> int:
        return len(self.rounds)

    def get_round(self, round_idx: int) -> RoundDef | None:
        if 0 <= round_idx < len(self.rounds):
            return self.rounds[round_idx]
        return None

    def prompt_context(self, round_idx: int) -> str:
        """Time context string for injection into agent prompts."""
        rd = self.get_round(round_idx)
        if rd is None:
            return f"\n# 时间 / Time: 第 {round_idx + 1} 轮"

        lines = [
            f"\n# 时间 / Time: {rd.label}",
            f"  时间段: {rd.calendar_start} ~ {rd.calendar_end}",
            f"  距事件发生: {rd.hours_since_event:.0f} 小时",
        ]
        if round_idx > 0:
            prev = self.rounds[round_idx - 1]
            lines.append(f"  上一轮: {prev.label}")
        if round_idx + 1 < len(self.rounds):
            nxt = self.rounds[round_idx + 1]
            lines.append(f"  下一轮: {nxt.label}")

        return "\n".join(lines)

    def to_serializable(self) -> dict[str, Any]:
        return {
            "event_datetime": self.event_datetime,
            "rounds": [r.to_serializable() for r in self.rounds],
        }

    @classmethod
    def from_serializable(cls, data: dict[str, Any]) -> "RoundSchedule":
        rounds = [
            RoundDef(**{
                k: v for k, v in r.items()
                if k in RoundDef.__dataclass_fields__
            })
            for r in data.get("rounds", [])
        ]
        return cls(rounds=rounds, event_datetime=data.get("event_datetime", ""))


def make_default_schedule(
    event_date: str,
    n_trading_days: int = 5,
) -> RoundSchedule:
    """Build a sensible default schedule for A-share simulations.

    Maps to: T+0 AM, T+0 PM, T+1, T+2, T+3, T+4, T+5.
    For n_trading_days=5 this produces 7 rounds.
    """
    rounds: list[RoundDef] = []

    # T+0 is split into AM and PM sessions
    rounds.append(RoundDef(
        id="T0_AM",
        label="T+0 上午盘",
        calendar_start=f"{event_date} 09:30",
        calendar_end=f"{event_date} 11:30",
        hours_since_event=0.0,
        active_agent_types=["retail", "kol", "media", "news_wire"],
    ))
    rounds.append(RoundDef(
        id="T0_PM",
        label="T+0 下午盘",
        calendar_start=f"{event_date} 13:00",
        calendar_end=f"{event_date} 15:00",
        hours_since_event=3.5,
        active_agent_types=["retail", "kol", "analyst"],
    ))

    # T+1 through T+n as full-day rounds
    for day in range(1, n_trading_days):
        hours = 24.0 * day
        rounds.append(RoundDef(
            id=f"T{day}",
            label=f"T+{day} 全天",
            calendar_start=f"T+{day} 09:30",
            calendar_end=f"T+{day} 15:00",
            hours_since_event=hours,
            active_agent_types=(
                ["retail", "analyst", "institutional"]
                if day <= 2
                else ["institutional", "strategic"]
            ),
        ))

    return RoundSchedule(rounds=rounds, event_datetime=event_date)


__all__ = [
    "RoundDef",
    "RoundSchedule",
    "make_default_schedule",
]
