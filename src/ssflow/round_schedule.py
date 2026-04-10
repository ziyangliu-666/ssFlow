"""Time-aware round schedule for simulations.

Maps abstract round indices to real calendar periods so agents perceive
the passage of time. Different agent types have different activity levels
per time period (retail active T+0, institutional T+1-T+2, strategic T+5+).

Schedules can be built from:
  1. A named preset  — ``make_schedule("earnings-5d", event_date)``
  2. A custom spec list — ``make_schedule(spec=[...], event_date=...)``
  3. Full YAML / JSON — ``RoundSchedule.from_serializable(data)``
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


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


# ── Presets ──────────────────────────────────────────────────────────

# Each preset is a list of round specs. Fields:
#   id, label, hours, active  (calendar_start/end are filled from event_date)
# "hours" is hours_since_event; "active" is active_agent_types.

_PRESETS: dict[str, list[dict[str, Any]]] = {
    # ── Flash — single trading day, 1 round ──
    "flash-1d": [
        {"id": "T0",  "label": "T+0 全天",  "hours": 0.0,
         "start": "09:30", "end": "15:00",
         "active": ["retail", "kol", "media", "quant"]},
    ],

    # ── Earnings short — 3 trading days, 4 rounds ──
    "earnings-3d": [
        {"id": "T0_AM",  "label": "T+0 上午盘", "hours": 0.0,
         "start": "09:30", "end": "11:30",
         "active": ["retail", "kol", "media", "news_wire"]},
        {"id": "T0_PM",  "label": "T+0 下午盘", "hours": 3.5,
         "start": "13:00", "end": "15:00",
         "active": ["retail", "kol", "analyst"]},
        {"id": "T1",     "label": "T+1 全天",   "hours": 24.0,
         "start": "09:30", "end": "15:00",
         "active": ["retail", "analyst", "institutional"]},
        {"id": "T2",     "label": "T+2 全天",   "hours": 48.0,
         "start": "09:30", "end": "15:00",
         "active": ["retail", "analyst", "institutional"]},
    ],

    # ── A-share earnings / major event — 5 trading days, 6 rounds ──
    "earnings-5d": [
        {"id": "T0_AM",  "label": "T+0 上午盘", "hours": 0.0,
         "start": "09:30", "end": "11:30",
         "active": ["retail", "kol", "media", "news_wire"]},
        {"id": "T0_PM",  "label": "T+0 下午盘", "hours": 3.5,
         "start": "13:00", "end": "15:00",
         "active": ["retail", "kol", "analyst"]},
        {"id": "T1",     "label": "T+1 全天",   "hours": 24.0,
         "start": "09:30", "end": "15:00",
         "active": ["retail", "analyst", "institutional"]},
        {"id": "T2",     "label": "T+2 全天",   "hours": 48.0,
         "start": "09:30", "end": "15:00",
         "active": ["retail", "analyst", "institutional"]},
        {"id": "T3",     "label": "T+3 全天",   "hours": 72.0,
         "start": "09:30", "end": "15:00",
         "active": ["institutional", "strategic"]},
        {"id": "T4",     "label": "T+4 全天",   "hours": 96.0,
         "start": "09:30", "end": "15:00",
         "active": ["institutional", "strategic"]},
    ],

    # ── Fast intraday — T+0 only, 4 rounds ──
    "intraday": [
        {"id": "T0_open",  "label": "T+0 开盘竞价", "hours": 0.0,
         "start": "09:15", "end": "09:30",
         "active": ["retail", "quant", "media"]},
        {"id": "T0_AM1",   "label": "T+0 上午前段", "hours": 0.5,
         "start": "09:30", "end": "10:30",
         "active": ["retail", "kol", "quant"]},
        {"id": "T0_AM2",   "label": "T+0 上午后段", "hours": 1.5,
         "start": "10:30", "end": "11:30",
         "active": ["retail", "analyst", "institutional"]},
        {"id": "T0_PM",    "label": "T+0 下午盘",   "hours": 3.5,
         "start": "13:00", "end": "15:00",
         "active": ["retail", "analyst", "institutional"]},
    ],

    # ── Short — 3 rounds, quick check ──
    "quick-3r": [
        {"id": "T0",  "label": "T+0 全天",  "hours": 0.0,
         "start": "09:30", "end": "15:00",
         "active": ["retail", "kol", "media", "quant"]},
        {"id": "T1",  "label": "T+1 全天",  "hours": 24.0,
         "start": "09:30", "end": "15:00",
         "active": ["retail", "analyst", "institutional"]},
        {"id": "T2",  "label": "T+2 全天",  "hours": 48.0,
         "start": "09:30", "end": "15:00",
         "active": ["institutional", "strategic"]},
    ],

    # ── Policy / slow-burn — 10 trading days ──
    "policy-10d": [
        {"id": "T0_AM",  "label": "T+0 上午盘", "hours": 0.0,
         "start": "09:30", "end": "11:30",
         "active": ["retail", "kol", "media", "news_wire"]},
        {"id": "T0_PM",  "label": "T+0 下午盘", "hours": 3.5,
         "start": "13:00", "end": "15:00",
         "active": ["retail", "kol", "analyst"]},
    ] + [
        {"id": f"T{d}", "label": f"T+{d} 全天", "hours": 24.0 * d,
         "start": "09:30", "end": "15:00",
         "active": (
             ["retail", "analyst", "institutional"] if d <= 3
             else ["institutional", "strategic"]
         )}
        for d in range(1, 10)
    ],

    # ── Extended — 10 trading days (alias for policy-10d) ──
    "extended-10d": [
        {"id": "T0_AM",  "label": "T+0 上午盘", "hours": 0.0,
         "start": "09:30", "end": "11:30",
         "active": ["retail", "kol", "media", "news_wire"]},
        {"id": "T0_PM",  "label": "T+0 下午盘", "hours": 3.5,
         "start": "13:00", "end": "15:00",
         "active": ["retail", "kol", "analyst"]},
    ] + [
        {"id": f"T{d}", "label": f"T+{d} 全天", "hours": 24.0 * d,
         "start": "09:30", "end": "15:00",
         "active": (
             ["retail", "analyst", "institutional"] if d <= 3
             else ["institutional", "strategic"]
         )}
        for d in range(1, 10)
    ],
}

PRESET_NAMES: list[str] = sorted(_PRESETS.keys())


def _build_from_spec(
    spec: list[dict[str, Any]],
    event_date: str,
) -> RoundSchedule:
    """Build a RoundSchedule from a list of round spec dicts.

    Each dict should have at minimum: ``id``, ``label``, ``hours``.
    Optional: ``start``, ``end`` (HH:MM), ``active`` (list[str]).
    ``calendar_start`` / ``calendar_end`` are derived from event_date + start/end.
    """
    rounds: list[RoundDef] = []
    for s in spec:
        rid = s["id"]
        label = s["label"]
        hours = float(s.get("hours", 0.0))
        start_time = s.get("start", "09:30")
        end_time = s.get("end", "15:00")
        active = list(s.get("active", []))

        # For T+0 rounds, use event_date directly; for later rounds use
        # the label as a proxy (actual calendar math not needed here — the
        # label and hours_since_event carry the semantic weight).
        day_offset = int(hours // 24) if hours >= 24 else 0
        if day_offset == 0:
            cal_start = f"{event_date} {start_time}"
            cal_end = f"{event_date} {end_time}"
        else:
            cal_start = f"T+{day_offset} {start_time}"
            cal_end = f"T+{day_offset} {end_time}"

        rounds.append(RoundDef(
            id=rid,
            label=label,
            calendar_start=cal_start,
            calendar_end=cal_end,
            hours_since_event=hours,
            active_agent_types=active,
        ))
    return RoundSchedule(rounds=rounds, event_datetime=event_date)


def make_schedule(
    preset: str = "earnings-5d",
    event_date: str = "",
    *,
    spec: list[dict[str, Any]] | None = None,
) -> RoundSchedule:
    """Build a RoundSchedule from a named preset or a custom spec.

    Args:
        preset: one of PRESET_NAMES. Ignored when ``spec`` is provided.
        event_date: "YYYY-MM-DD" string for calendar_start/end derivation.
        spec: if provided, overrides the preset. A list of round dicts, each
            with at least ``id``, ``label``, ``hours``. Optional: ``start``,
            ``end`` (HH:MM strings), ``active`` (list of agent type prefixes).

    Returns:
        A RoundSchedule with the requested rounds.

    Raises:
        ValueError: if ``preset`` is unknown and ``spec`` is None.

    Examples::

        # Named preset
        schedule = make_schedule("intraday", "2026-04-10")

        # Custom spec
        schedule = make_schedule(spec=[
            {"id": "R0", "label": "开盘", "hours": 0, "active": ["retail"]},
            {"id": "R1", "label": "午盘", "hours": 2, "active": ["retail", "institutional"]},
            {"id": "R2", "label": "收盘", "hours": 5, "active": ["institutional"]},
        ], event_date="2026-04-10")
    """
    if spec is not None:
        return _build_from_spec(spec, event_date)
    if preset not in _PRESETS:
        raise ValueError(
            f"Unknown schedule preset {preset!r}. "
            f"Available: {', '.join(PRESET_NAMES)}"
        )
    return _build_from_spec(_PRESETS[preset], event_date)


def make_default_schedule(
    event_date: str,
    n_trading_days: int = 5,
) -> RoundSchedule:
    """Build a sensible default schedule for A-share simulations.

    Backward-compatible wrapper around ``make_schedule``. Maps to:
    T+0 AM, T+0 PM, T+1, ..., T+(n-1).

    For n_trading_days=5 this produces 6 rounds (same as "earnings-5d" preset).
    For other values, generates a custom spec dynamically.
    """
    if n_trading_days == 5:
        return make_schedule("earnings-5d", event_date)

    # Dynamic: T+0 AM/PM + T+1 through T+(n-1)
    spec: list[dict[str, Any]] = [
        {"id": "T0_AM", "label": "T+0 上午盘", "hours": 0.0,
         "start": "09:30", "end": "11:30",
         "active": ["retail", "kol", "media", "news_wire"]},
        {"id": "T0_PM", "label": "T+0 下午盘", "hours": 3.5,
         "start": "13:00", "end": "15:00",
         "active": ["retail", "kol", "analyst"]},
    ]
    for day in range(1, n_trading_days):
        spec.append({
            "id": f"T{day}", "label": f"T+{day} 全天",
            "hours": 24.0 * day,
            "start": "09:30", "end": "15:00",
            "active": (
                ["retail", "analyst", "institutional"]
                if day <= 2
                else ["institutional", "strategic"]
            ),
        })
    return _build_from_spec(spec, event_date)


__all__ = [
    "PRESET_NAMES",
    "RoundDef",
    "RoundSchedule",
    "make_default_schedule",
    "make_schedule",
]
