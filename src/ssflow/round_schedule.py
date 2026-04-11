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

    @property
    def session_kind(self) -> str:
        """Classify the round into a named trading session for narrative use."""
        rid = (self.id or "").upper()
        start = self.calendar_start or ""
        end = self.calendar_end or ""
        if "OPEN" in rid or "竞价" in self.label or " 09:15" in start:
            return "pre_open"
        if "AM" in rid or "上午" in self.label:
            return "morning"
        if "PM" in rid or "下午" in self.label:
            return "afternoon"
        # Dayparts beyond T+0 use calendar_start to decide morning/afternoon,
        # otherwise treated as a whole-day session.
        if " 09:30" in start and " 15:00" in end:
            return "whole_day"
        if " 09:30" in start and " 11:30" in end:
            return "morning"
        if " 13:00" in start and " 15:00" in end:
            return "afternoon"
        return "whole_day"

    @property
    def trading_day_index(self) -> int:
        """Which trading day this round belongs to (0 = T+0, 1 = T+1, ...)."""
        return max(0, int(self.hours_since_event // 24))


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

    def prompt_context(
        self,
        round_idx: int,
        *,
        cumulative_delta_pct: float | None = None,
        current_price: float | None = None,
        prev_day_close_delta_pct: float | None = None,
    ) -> str:
        """Time context string for injection into agent prompts.

        Produces a narrative block that goes beyond the bare timestamp:
          - Hard session tag (e.g. [盘中·充裕流动性] / [尾盘·流动性收敛])
          - Session-kind flavour text (T+0 morning / close / cross-day etc.)
          - Time-to-close remaining inside the current session
          - T+1 settlement reminder (A-share specific)
          - Cross-day reset message with yesterday's close and overnight drift

        Optional kwargs:
          cumulative_delta_pct: % change since the event (e.g. 5.4 for +5.4%)
          current_price: last traded price in the event currency
          prev_day_close_delta_pct: % move recorded on the previous trading
            day; injected when round_idx crosses a day boundary so agents see
            "昨日收盘 +3.2%" framed as a fresh-start reference point.
        """
        rd = self.get_round(round_idx)
        if rd is None:
            return f"\n# 时间 / Time: 第 {round_idx + 1} 轮"

        session = rd.session_kind
        day_idx = rd.trading_day_index
        is_first_round_of_day = round_idx == 0 or (
            self.rounds[round_idx - 1].trading_day_index != day_idx
        )

        # Detect monthly cadence: any inter-round gap in this schedule
        # >= 15 days means we are running a month-scale scenario. When
        # that is true we relabel the day-scale chrome into month-scale
        # language so agents don't think they are debating an intraday
        # print when the schedule actually spans half a year.
        is_monthly = self._is_monthly_cadence()
        if is_monthly:
            month_idx = self._month_index(round_idx)

        # ── Session tag (hard signal agents can key off) ──
        if is_monthly:
            tag = "[月度 · 中长期定价]"
        else:
            tag_map = {
                "pre_open":   "[开盘竞价 · 价格发现]",
                "morning":    "[盘中 · 充裕流动性]",
                "afternoon":  "[尾盘 · 流动性收敛]",
                "whole_day":  "[全日 · 常规流动性]",
            }
            tag = tag_map.get(session, "[全日 · 常规流动性]")

        # ── Narrative flavour by (session, day_idx) ──
        # Pulled into a separate method so the logic stays declarative.
        if is_monthly:
            narrative = _MONTHLY_NARRATIVES[
                min(len(_MONTHLY_NARRATIVES) - 1, month_idx)
            ]
        else:
            narrative = _session_narrative(session, day_idx, is_first_round_of_day)

        if is_monthly:
            elapsed_line = (
                f"  距事件发生: {rd.hours_since_event / 24:.0f} 天  ·  "
                f"事件后第 {month_idx + 1} 个月"
            )
        else:
            elapsed_line = (
                f"  距事件发生: {rd.hours_since_event:.0f} 小时  ·  "
                f"事件后第 {day_idx + 1} 个交易日"
            )
        lines = [
            f"\n# 时间 / Time: {rd.label}  {tag}",
            f"  时间段: {rd.calendar_start} ~ {rd.calendar_end}",
            elapsed_line,
        ]
        if narrative:
            lines.append(f"  情境: {narrative}")

        # ── Price anchor ──
        if current_price is not None and cumulative_delta_pct is not None:
            lines.append(
                f"  当前价格: {current_price:.2f}  "
                f"(事件以来 {cumulative_delta_pct:+.2f}%)"
            )
        elif current_price is not None:
            lines.append(f"  当前价格: {current_price:.2f}")
        elif cumulative_delta_pct is not None:
            lines.append(f"  事件以来累计涨跌: {cumulative_delta_pct:+.2f}%")

        # ── Cross-day reset message ──
        if is_first_round_of_day and day_idx > 0 and not is_monthly:
            if prev_day_close_delta_pct is not None:
                lines.append(
                    f"  新交易日开盘: 昨日收盘 {prev_day_close_delta_pct:+.2f}%, "
                    f"隔夜情绪理应部分衰减 — 重新审视持仓和仓位"
                )
            else:
                lines.append(
                    "  新交易日开盘: 隔夜情绪理应部分衰减 — "
                    "重新审视持仓和仓位"
                )
        elif is_monthly and round_idx > 0:
            # Open-ended reassessment — no imperative. Backtest v2
            # (§3.2) showed that the prior "请主动考虑翻转" phrasing
            # systematically destroyed conviction in strong-bull events
            # (宁德时代 924 real +34% → sim -12%) because agents
            # read it as a standing instruction to reverse. This
            # version lets the agent decide based on new information.
            lines.append(
                "  月度重估: 距上月已有约 30 个交易日, "
                "评估原始事件的 narrative 是否仍在验证, "
                "新的月度数据/研报/宏观事件是否带来新的 catalyst. "
                "若叙事仍成立且无反证, 保持方向; "
                "若已充分 priced in 或出现反证, 相应调整"
            )

        # ── T+1 settlement reminder (A-share) ──
        # On monthly cadence this is trivially satisfied, so skip the
        # line to save prompt budget for the monthly-specific context.
        if not is_monthly:
            lines.append(
                "  交易规则: A 股 T+1 — 今日买入的仓位明日方可卖出, "
                "请基于你对【下一个交易日】而非【今日收盘】的价格判断做买入决策"
            )

        if round_idx > 0:
            prev = self.rounds[round_idx - 1]
            lines.append(f"  上一轮: {prev.label}")
        if round_idx + 1 < len(self.rounds):
            nxt = self.rounds[round_idx + 1]
            lines.append(f"  下一轮: {nxt.label}")

        return "\n".join(lines)

    def _is_monthly_cadence(self) -> bool:
        """True if this schedule steps in month-scale jumps (≥15 days)."""
        if len(self.rounds) < 2:
            return False
        for i in range(1, len(self.rounds)):
            gap = self.rounds[i].hours_since_event - self.rounds[i - 1].hours_since_event
            if gap >= 15 * 24:
                return True
        return False

    def _month_index(self, round_idx: int) -> int:
        """Zero-based month offset of `round_idx` within a monthly schedule.

        Computed from `hours_since_event` so callers can use non-uniform
        monthly spacing. Falls back to the raw round index for schedules
        that don't actually step monthly.
        """
        if not 0 <= round_idx < len(self.rounds):
            return 0
        hours = self.rounds[round_idx].hours_since_event
        if hours < 15 * 24:
            return 0
        return int(round(hours / (30 * 24)))

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


# ── Session narrative helpers ────────────────────────────────────────


_MONTHLY_NARRATIVES: list[str] = [
    "事件当月: 一级消化期. 财报/政策冲击集中定价, 机构调研集中, "
    "分析师模型快速更新, 散户情绪从爆发到初步消化, 波动率显著抬升",
    "事件后第二个月: 二级消化期. 趋势确认或证伪, 分析师评级调整潮, "
    "板块联动开始显现, 融资/融券余额明显变动, 估值锚点重新讨论",
    "事件后第三个月: 中期博弈期. 部分机构兑现/加仓, 新的宏观或行业月度数据验证叙事, "
    "短线动量开始衰减, 长线逻辑是否成立成为焦点",
    "事件后第四个月: 季度末调仓期. 长线资金决定是否重仓, 短期动量基本衰减, "
    "基金季报持仓披露, 风险溢价重新校准",
    "事件后第五个月: 催化酝酿期. 新一季财报预期升温, 新催化剂开始酝酿, "
    "原事件本身的影响基本 priced in, 叙事切换到下一季的新变量",
    "事件后第六个月: 半年业绩验证期. 事件的长期影响正式兑现到业绩, "
    "市场开始用数据而非叙事定价. 错判的方向在此刻被强制修正",
]


def _session_narrative(
    session: str, day_idx: int, is_first_round_of_day: bool
) -> str:
    """Pick a short narrative line describing this session's character.

    Designed to give agents behavioural context — not just a timestamp —
    so they can reason about liquidity, info velocity, and the typical
    participant mix. Deliberately terse: one sentence per (session, day)
    pair so it fits inside prompt budgets without crowding out the event.

    For multi-month schedules (day_idx ≥ 15) we branch into a dedicated
    month-scale narrative bank — the daily templates are tuned for
    intraday/T+N cadences and degenerate into "长期消化" repetition
    once the schedule moves past the first week.
    """
    if day_idx >= 15:
        month_idx = min(len(_MONTHLY_NARRATIVES) - 1, day_idx // 30)
        return _MONTHLY_NARRATIVES[max(0, month_idx)]
    if day_idx == 0:
        # Event day: fast, noisy, retail-dominated in the morning
        if session == "pre_open":
            return (
                "事件后首次开盘竞价. 隔夜情绪集中爆发, "
                "成交稀薄但价格发现极端"
            )
        if session == "morning":
            return (
                "事件后首个交易时段. 散户情绪最狂热, 机构通常观望等数据, "
                "流动性充沛但价格发现剧烈"
            )
        if session == "afternoon":
            return (
                "事件日下午盘. 尾盘流动性收敛, 日内高位获利了结压力抬升, "
                "机构开始试探性建仓或减仓"
            )
        return "事件当日常规交易. 散户驱动, 机构观望"
    if day_idx == 1:
        # T+1: institutional entry begins
        if is_first_round_of_day or session in ("pre_open", "morning"):
            return (
                "事件后第二个交易日开盘. 机构完成内部分析, 开始正式进场/调仓, "
                "散户隔夜情绪部分衰减"
            )
        return (
            "T+1 盘中. 机构流入主导, 散户开始根据昨日浮盈/浮亏决定加仓或止盈"
        )
    if day_idx == 2:
        return (
            "事件后第三个交易日. 分析师研报集中发布, 机构持仓明显调整, "
            "短线散户情绪见顶后回落"
        )
    if day_idx <= 4:
        return (
            f"事件后第 {day_idx + 1} 个交易日. 战略资金进场, 长线机构评估基本面, "
            "融资盘/融券观察警戒"
        )
    return f"事件后第 {day_idx + 1} 个交易日. 行情已进入长期消化阶段"


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

    # ── Monthly-6m — 6 rounds, one per month, 6-month horizon ──
    #
    # For long-horizon backtesting where each round represents a whole
    # trading month. `hours_since_event = 720 * N` means `day_idx` lands
    # at multiples of 30, which triggers the _MONTHLY_NARRATIVES branch
    # inside _session_narrative and gives agents month-scale context
    # instead of intraday session tags.
    "monthly-6m": [
        {"id": "M0",  "label": "事件当月 (M+0)", "hours": 0.0,
         "start": "09:30", "end": "15:00",
         "active": ["retail", "kol", "media", "news_wire",
                    "analyst", "institutional"]},
        {"id": "M1",  "label": "事件后一个月 (M+1)", "hours": 720.0,
         "start": "09:30", "end": "15:00",
         "active": ["institutional", "analyst", "retail"]},
        {"id": "M2",  "label": "事件后两个月 (M+2)", "hours": 1440.0,
         "start": "09:30", "end": "15:00",
         "active": ["institutional", "strategic", "analyst"]},
        {"id": "M3",  "label": "事件后三个月 (M+3)", "hours": 2160.0,
         "start": "09:30", "end": "15:00",
         "active": ["institutional", "strategic", "analyst"]},
        {"id": "M4",  "label": "事件后四个月 (M+4)", "hours": 2880.0,
         "start": "09:30", "end": "15:00",
         "active": ["strategic", "institutional"]},
        {"id": "M5",  "label": "事件后五个月 (M+5)", "hours": 3600.0,
         "start": "09:30", "end": "15:00",
         "active": ["strategic", "institutional"]},
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
