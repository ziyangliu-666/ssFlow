"""Overnight sentiment + gap volatility resolution from event metadata.

This logic used to live inline in ``oasis_engine.run_simulation``'s pre-open
auction block. It was extracted in Round 6 because:

1. The severity/volatility maps started drifting against the calibration
   backtest harness, and reviewers could not find a single place to inspect
   the mapping.
2. Inline bear/bull keyword sniffing had a conflict-resolution bug: a text
   containing *both* bear and bull keywords would apply the bull clamp on
   top of the bear clamp, silently erasing the bearish signal.
3. Testing the resolution rules directly requires it to be a pure function
   that doesn't depend on an engine round loop.

The resolver is the single source of truth for:
- overnight sentiment in ``[-1, 1]`` conditioned on event_type + text
- gap_vol in ``[0.03, 0.30]``
- ``terminal_risk`` flag used downstream for forced-seller cascades

Calibration events with a known ``day1_open`` override the heuristic with
the implied gap (scaled to roughly match overnight sentiment magnitude).
"""

from __future__ import annotations

import re
from dataclasses import dataclass


__all__ = [
    "SeverityResolution",
    "resolve_event_severity",
]


# Directional prior per canonical VALID_EVENT_TYPES entry. Unknown keys
# fall through to 0.0. Keep in sync with ``event.VALID_EVENT_TYPES``.
_SEVERITY_MAP: dict[str, float] = {
    # Strongly bearish
    "delisting_risk": -0.9,
    "regulatory": -0.6,
    "lawsuit": -0.5,
    "geopolitical": -0.7,
    "demand_shock": -0.6,
    # Mildly bearish
    "shareholder_action": -0.3,
    "management_change": -0.2,
    "exchange_event": -0.3,
    "inventory_release": -0.3,
    # Neutral / context-dependent
    "earnings": 0.0,
    "macro": 0.0,
    "m_a": 0.0,
    "ipo": 0.0,
    "other": 0.0,
    "weather": 0.0,
    "opec_meeting": 0.0,
    # Mildly bullish
    "dividend": 0.2,
    "protocol_upgrade": 0.3,
    # Strongly bullish
    "policy": 0.6,
    "supply_shock": 0.5,
    "supply_disruption": 0.5,
    "mania": 0.8,
    "halving": 0.7,
}

# Gap volatility per canonical event type. Extreme events can gap to the
# board limit on day 1; moderate events produce small gaps.
_VOL_BY_TYPE: dict[str, float] = {
    "delisting_risk": 0.25,
    "regulatory": 0.15,
    "lawsuit": 0.12,
    "geopolitical": 0.15,
    "supply_shock": 0.10,
    "supply_disruption": 0.10,
    "mania": 0.12,
    "policy": 0.10,
    "earnings": 0.05,
    "shareholder_action": 0.08,
    "exchange_event": 0.10,
    "demand_shock": 0.12,
    "management_change": 0.06,
    "inventory_release": 0.08,
    "dividend": 0.04,
    "protocol_upgrade": 0.08,
    "halving": 0.15,
    "m_a": 0.08,
    "ipo": 0.08,
    "macro": 0.08,
    "other": 0.05,
    "weather": 0.06,
    "opec_meeting": 0.08,
}

# Extreme keyword lists. Compiled as a single regex per side for speed and
# to guarantee match ordering when both sides hit. The resolution rule is:
# when BOTH sides match, choose the side with the larger magnitude clamp —
# so terminal bear risk is never silently erased by a stray bull phrase in
# the same text, and vice versa.
#
# Chinese keywords are matched as raw substrings (Chinese has no inter-word
# whitespace and ordinary substring match is the conventional tokenisation
# fallback). English keywords use ``\b`` word boundaries — otherwise "st"
# matches "stock" and blows up every English description that mentions an
# English word beginning with `st`. ``*ST`` is a special case: it's a
# stand-alone Chinese ticker-prefix marker (e.g. "*ST 华讯") and must match
# regardless of whitespace around it.
_EXTREME_BEAR_KEYWORDS_CN = (
    "退市", "强制退市", "立案", "造假", "停牌", "破产", "重整", "终止上市",
)
_EXTREME_BEAR_KEYWORDS_EN = (
    "delisting", "fraud", "suspension", "bankruptcy",
)
_EXTREME_BULL_KEYWORDS_CN = (
    "一字涨停", "连板", "涨停板", "核准注册", "国家队",
    "全面降准", "迎来爆发", "龙头",
)

_BEAR_PATTERN = (
    # Chinese substring matches
    "|".join(re.escape(kw) for kw in _EXTREME_BEAR_KEYWORDS_CN)
    # *ST ticker-prefix marker, insensitive to surrounding whitespace
    + r"|\*st"
    # English keywords as whole words
    + "|" + "|".join(rf"\b{re.escape(kw)}\b" for kw in _EXTREME_BEAR_KEYWORDS_EN)
)
_EXTREME_BEAR_RE = re.compile(_BEAR_PATTERN, re.IGNORECASE)
_EXTREME_BULL_RE = re.compile(
    "|".join(re.escape(kw) for kw in _EXTREME_BULL_KEYWORDS_CN), re.IGNORECASE
)

_BEAR_CLAMP = -0.85
_BULL_CLAMP = 0.75

# ── Moderate keywords: common positive/negative indicators ──
# These nudge sentiment by ±0.3 instead of clamping to extremes.
# Applied AFTER extreme keywords — if extreme already matched, moderate is skipped.
_MODERATE_BULL_KEYWORDS_CN = (
    "超预期", "超出预期", "增长", "突破", "新高", "纪录",
    "合并", "收购", "重组", "吸收合并",
    "扶持", "补贴", "纾困", "取消限购",
    "降息", "降准", "宽松",
    "超越", "领先", "创新高",
    "beat",
)
_MODERATE_BEAR_KEYWORDS_CN = (
    "暴雷", "下滑", "亏损", "罚款", "处罚",
    "减持", "解禁", "负增长", "下降", "首次下滑",
    "打压", "制裁", "实体清单", "加征关税",
    "下修", "miss", "不及预期",
)
_MODERATE_BULL_NUDGE = 0.35
_MODERATE_BEAR_NUDGE = -0.35
_MODERATE_BULL_RE = re.compile(
    "|".join(re.escape(kw) for kw in _MODERATE_BULL_KEYWORDS_CN), re.IGNORECASE
)
_MODERATE_BEAR_RE = re.compile(
    "|".join(re.escape(kw) for kw in _MODERATE_BEAR_KEYWORDS_CN), re.IGNORECASE
)


@dataclass(frozen=True)
class SeverityResolution:
    """Pre-open auction sentiment + gap volatility derived from an event.

    Attributes:
        overnight_sentiment: clamped to [-1, 1]. Negative = bearish.
        gap_vol: scalar in [0.03, 0.30] passed to ``gap_open``.
        terminal_risk: True when extreme-bear keywords fired — downstream
            code uses this to trigger forced-seller cascades and suppress
            retail dip-buying.
        bull_keyword_match: True when extreme-bull keywords fired.
        source: "day1_open" when calibration data overrode the heuristic,
            "severity_map" otherwise. Useful for diagnostics.
    """

    overnight_sentiment: float
    gap_vol: float
    terminal_risk: bool
    bull_keyword_match: bool
    source: str


def _resolve_keyword_adjustment(
    base_sentiment: float, event_text: str
) -> tuple[float, bool, bool]:
    """Apply keyword-based sentiment adjustments.

    Two tiers:
      1. Extreme keywords → clamp to ±0.75/0.85 (terminal risk / mania).
      2. Moderate keywords → nudge by ±0.35 (common event indicators).

    Extreme keywords take priority. When both sides match within a tier,
    choose the side with the larger magnitude.
    """
    if not event_text:
        return base_sentiment, False, False
    text_l = event_text.lower()

    # ── Tier 1: Extreme keywords (hard clamp) ──
    bear_hit = bool(_EXTREME_BEAR_RE.search(text_l))
    bull_hit = bool(_EXTREME_BULL_RE.search(text_l))

    if bear_hit and bull_hit:
        bear_candidate = min(base_sentiment, _BEAR_CLAMP)
        bull_candidate = max(base_sentiment, _BULL_CLAMP)
        if abs(bear_candidate) >= abs(bull_candidate):
            return bear_candidate, True, bull_hit
        return bull_candidate, bear_hit, True

    if bear_hit:
        return min(base_sentiment, _BEAR_CLAMP), True, False
    if bull_hit:
        return max(base_sentiment, _BULL_CLAMP), False, True

    # ── Tier 2: Moderate keywords (soft nudge) ──
    mod_bull = bool(_MODERATE_BULL_RE.search(text_l))
    mod_bear = bool(_MODERATE_BEAR_RE.search(text_l))

    if mod_bull and mod_bear:
        # Both matched — pick the side with more keyword matches
        bull_count = len(_MODERATE_BULL_RE.findall(text_l))
        bear_count = len(_MODERATE_BEAR_RE.findall(text_l))
        if bull_count >= bear_count:
            nudged = max(-1.0, min(1.0, base_sentiment + _MODERATE_BULL_NUDGE))
            return nudged, False, False
        nudged = max(-1.0, min(1.0, base_sentiment + _MODERATE_BEAR_NUDGE))
        return nudged, False, False

    if mod_bull:
        nudged = max(-1.0, min(1.0, base_sentiment + _MODERATE_BULL_NUDGE))
        return nudged, False, False
    if mod_bear:
        nudged = max(-1.0, min(1.0, base_sentiment + _MODERATE_BEAR_NUDGE))
        return nudged, False, False

    return base_sentiment, False, False


def resolve_event_severity(
    event_type: str,
    event_text: str | None,
    *,
    day1_open: float | None = None,
    current_price: float | None = None,
) -> SeverityResolution:
    """Resolve overnight sentiment + gap volatility for the pre-open auction.

    Precedence:
      1. If ``day1_open`` and ``current_price`` are both provided (calibration
         path), the implied gap drives sentiment directly.
      2. Otherwise the severity map provides a directional prior keyed on
         ``event_type``.
      3. Extreme-bear / extreme-bull keyword matches on ``event_text`` clamp
         the sentiment to the appropriate extreme. Conflicting matches
         resolve in favour of the larger-magnitude clamp.
    """
    et = (event_type or "").lower()
    base = _SEVERITY_MAP.get(et, 0.0)

    source = "severity_map"
    if day1_open is not None and current_price is not None and current_price > 0:
        implied = (day1_open - current_price) / current_price * 10.0
        base = max(-1.0, min(1.0, implied))
        source = "day1_open"
        # We still run the keyword sniff — calibration data can coexist
        # with an event text that carries terminal-risk signalling.

    sent, bear_hit, bull_hit = _resolve_keyword_adjustment(base, event_text or "")
    sent = max(-1.0, min(1.0, sent))

    gap_vol = _VOL_BY_TYPE.get(et, 0.05)
    if abs(sent) >= 0.8:
        gap_vol = max(gap_vol, 0.20)
    # Floor + ceiling so extreme data cannot blow up the auction
    gap_vol = max(0.03, min(0.30, gap_vol))

    return SeverityResolution(
        overnight_sentiment=sent,
        gap_vol=gap_vol,
        terminal_risk=bear_hit,
        bull_keyword_match=bull_hit,
        source=source,
    )
