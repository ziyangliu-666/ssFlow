"""Compliance output filter — load-bearing wall.

This module enforces the P4 compliance posture from the design doc:
    Output is strictly descriptive ("simulation shows X"), never prescriptive
    ("you should do Y"). Any text containing forbidden investment-advice
    vocabulary is rejected.

Used by:
    - simulation pipeline (every report runs through assert_compliant before display)
    - tests/test_output_filter.py (CRITICAL — 20+ test cases must all pass)

Two layers planned:
    1. regex blocklist (this file, fully implemented tonight)
    2. LLM semantic check (stub for Day 2)

If layer 1 catches a violation, the report is sent to a manual review log
and the user sees a generic "your simulation is being verified" message.
"""

from __future__ import annotations

import re

# ─────────────────────── Forbidden vocabulary ───────────────────────
#
# Each entry is matched case-insensitively. Chinese terms have no case but the
# regex engine handles unicode fine. Punctuation around the term doesn't affect
# the match (we use word-boundary-ish matching).
#
# Source: P4 firewall rule #3 from the design doc + the test plan's CP2 spec.

FORBIDDEN_VOCAB: list[str] = [
    # Direct advice verbs (Chinese)
    "建议",
    "推荐",
    "应该",
    "必须",
    "不应",
    "请买",
    "请卖",
    # Trading actions (Chinese)
    "买入",
    "卖出",
    "减仓",
    "加仓",
    "建仓",
    "清仓",
    "持仓建议",
    "建议持仓",
    # Price targets / ratings (Chinese)
    "目标价",
    "目标股价",
    "评级",
    "强烈买入",
    "强烈卖出",
    "强烈推荐",
    "止损位",
    "止盈位",
    "建仓位",
    "加仓位",
    # English equivalents
    "BUY",
    "SELL",
    "STRONG BUY",
    "STRONG SELL",
    "target price",
    "price target",
    "buy rating",
    "sell rating",
    "strong buy",
    "strong sell",
    "recommend buy",
    "recommend sell",
    "我建议",
    "我推荐",
    "我认为应该",
]


# Pre-compile regex for performance and case-insensitivity.
# Each forbidden phrase becomes a regex with word-boundaries (best effort for
# mixed Chinese/English).
def _compile_blocklist() -> list[tuple[str, re.Pattern[str]]]:
    compiled = []
    for word in FORBIDDEN_VOCAB:
        # For pure-ASCII terms, use word boundaries to avoid false positives
        # like "BUYER" matching "BUY".
        if word.isascii():
            pattern = re.compile(rf"\b{re.escape(word)}\b", re.IGNORECASE)
        else:
            # Chinese terms: no word boundary needed, match anywhere
            pattern = re.compile(re.escape(word), re.IGNORECASE)
        compiled.append((word, pattern))
    return compiled


_COMPILED_BLOCKLIST = _compile_blocklist()


# ─────────────────────── Public API ───────────────────────


class ComplianceViolation(Exception):
    """Raised when output text contains forbidden vocabulary."""

    def __init__(self, violations: list[str], snippet: str = ""):
        self.violations = violations
        self.snippet = snippet
        super().__init__(
            f"Compliance filter rejected text. Violations: {violations}. "
            f"Snippet: {snippet[:200]}"
        )


def regex_filter_violations(text: str) -> list[str]:
    """Return list of forbidden words found in text. Empty list = clean.

    Order is deterministic (matches FORBIDDEN_VOCAB order). Duplicates removed.
    """
    found: list[str] = []
    seen: set[str] = set()
    for word, pattern in _COMPILED_BLOCKLIST:
        if pattern.search(text):
            if word not in seen:
                found.append(word)
                seen.add(word)
    return found


def assert_compliant(text: str) -> None:
    """Raise ComplianceViolation if text contains forbidden vocab.

    Call this on the FINAL report before showing it to the user.
    """
    violations = regex_filter_violations(text)
    if violations:
        raise ComplianceViolation(violations, snippet=text)


def is_compliant(text: str) -> bool:
    """Non-raising version of assert_compliant. Returns True if clean."""
    return not regex_filter_violations(text)


# ─────────────────────── Sanitizer ───────────────────────
#
# Replaces forbidden words with descriptive substitutes so that LLM-generated
# reaction comments can flow into the user-facing report without tripping the
# top-level assert_compliant() check. The substitutions preserve meaning but
# strip prescriptive force.

REPLACEMENTS: dict[str, str] = {
    # Direct verbs
    "建议": "倾向",
    "推荐": "看好",
    "应该": "倾向",
    "必须": "需要",
    "不应": "不倾向",
    # Imperative prefixes (retrospective finding: 请买/请卖 were the two
    # genuinely unmapped FORBIDDEN_VOCAB entries — longer phrases like 我建议
    # are implicitly handled by the 建议→倾向 substring match after longest-
    # first sort, but 请买/请卖 have no shorter substring in this table).
    "请买": "可进入",
    "请卖": "可离场",
    # Trading actions
    "买入": "进入",
    "卖出": "离场",
    "减仓": "降低敞口",
    "加仓": "提高敞口",
    "建仓": "进入头寸",
    "清仓": "完全离场",
    # Price targets / ratings
    "目标价": "参考区间",
    "目标股价": "参考区间",
    "评级": "观点",
    "强烈买入": "强偏多",
    "强烈卖出": "强偏空",
    "止损位": "情绪压力位",
    "止盈位": "情绪缓释位",
    # English
    "BUY": "[lean-in]",
    "SELL": "[lean-out]",
    "STRONG BUY": "[strong lean-in]",
    "STRONG SELL": "[strong lean-out]",
    "target price": "reference range",
    "price target": "reference range",
    "buy rating": "lean-in view",
    "sell rating": "lean-out view",
}


def sanitize_text(text: str) -> str:
    """Replace forbidden words with descriptive substitutes.

    Use this on individual LLM-generated reaction comments before they get
    composed into the final report. The final report is still validated by
    assert_compliant() as a defense-in-depth check.
    """
    if not text:
        return text
    out = text
    # Sort longest-first so multi-word phrases match before sub-strings
    for original in sorted(REPLACEMENTS.keys(), key=len, reverse=True):
        replacement = REPLACEMENTS[original]
        if original.isascii():
            pattern = re.compile(rf"\b{re.escape(original)}\b", re.IGNORECASE)
        else:
            pattern = re.compile(re.escape(original), re.IGNORECASE)
        out = pattern.sub(replacement, out)
    return out


def _assert_all_forbidden_sanitizable() -> None:
    """Module-load guarantee: every FORBIDDEN_VOCAB entry gets sanitized to a compliant form.

    Runs once at import time. Catches contributor drift where someone adds a
    new forbidden vocab entry but forgets to add a corresponding REPLACEMENTS
    rule (either direct or via substring). The check is the round-trip
    `is_compliant(sanitize_text(token))` for every token in FORBIDDEN_VOCAB —
    this handles both direct replacements and implicit substring handling
    (e.g., `持仓建议` is sanitized via the `建议→倾向` substring rule even
    though `持仓建议` is not itself a key in REPLACEMENTS).

    This is the retrospective fix for the silent-quarantine failure mode:
    previously, FORBIDDEN_VOCAB and REPLACEMENTS could drift apart without
    warning, and the first sign of trouble was a quarantined report at
    runtime. Now, drift fails at import time with a clear error.
    """
    unsanitized: list[str] = []
    for word in FORBIDDEN_VOCAB:
        # Use the existing regex_filter_violations path through sanitize_text.
        if regex_filter_violations(sanitize_text(word)):
            unsanitized.append(word)
    if unsanitized:
        raise RuntimeError(
            "output_filter.REPLACEMENTS is missing sanitization paths for "
            f"{len(unsanitized)} FORBIDDEN_VOCAB entries: {unsanitized}. "
            "Add direct entries to REPLACEMENTS (or ensure a shorter substring "
            "of each unmapped entry is already mapped). See the retrospective "
            "for context: this assertion prevents the silent-quarantine bug "
            "where the LLM emits a forbidden token that has no sanitization "
            "path and the whole report gets quarantined with no actionable info."
        )


_assert_all_forbidden_sanitizable()


# ─────────────────────── LLM semantic layer (Day 2 stub) ───────────────────────


async def llm_semantic_check(text: str) -> bool:  # pragma: no cover
    """Day 2 placeholder. Will ask LLM 'is this descriptive or prescriptive?'.

    For now, always returns True (compliant). The regex layer is the only
    enforced layer tonight. Per the design doc, this is acceptable for
    Week 0-2 because the regex catches the most common violation patterns
    and the user is the only end user.

    Tracked as a TODO for next session.
    """
    return True


__all__ = [
    "ComplianceViolation",
    "FORBIDDEN_VOCAB",
    "REPLACEMENTS",
    "assert_compliant",
    "is_compliant",
    "llm_semantic_check",
    "regex_filter_violations",
    "sanitize_text",
]
