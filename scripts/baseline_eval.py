#!/usr/bin/env python
"""Baseline evaluation — the retrospective's Week-1 launch blocker.

This script exists because both the CEO phase dual voices (Codex + Claude
subagent) independently recommended running a cheap cross-family sentiment
diversity test BEFORE writing any multi-family LLM orchestration code. The
original smoke test produced 100% bearish output on an ambiguous beat-and-
miss event (sentiment_std=0.18) — the "correlated hallucination in costumes"
failure mode. Multi-family swap is the proposed fix, but both models argued
it's a HOPE not a proof: Gemini/Qwen/Claude share overlapping training data,
RLHF preferences, and legal landmines around Chinese equities.

The cheapest way to validate the multi-family hypothesis is to run the SAME
ambiguous event × 4 model families via a single-batched-prompt structure
(the current ssFish shape), measure within-family and cross-family sentiment
diversity, and decide:

  - If cross-family mean sentiment differs meaningfully AND at least one
    family lands positive on at least one persona: multi-family IS a fix,
    proceed with the architectural refactor.
  - If cross-family means all land within ~0.15 of each other AND all show
    low within-family std: multi-family is NOT the fix. Pivot to
    adversarial-by-construction persona pairing instead.

Usage:
    uv run python scripts/baseline_eval.py

Output: eval/baseline-eval-<timestamp>.{json,md}

Cost: ~$0.02-0.05 total across all 4 model calls.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Ensure src/ is on the path when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ssfish.llm_client import chat_json, cost_tracker  # noqa: E402


# ─────────────────────── Eval config ───────────────────────


# The model families to test. Each is what ssFish would switch to in the
# hypothetical multi-family swap. These are the exact strings yourapi.cn
# accepts per the PRICING_PER_M table in llm_client.py.
#
# Note: the user's yourapi.cn account may not have every family enabled.
# Failed models are recorded in the output with error_type, so the eval
# degrades gracefully and still produces useful data from whatever is
# accessible. If only the OpenAI family is enabled, we at least get
# WITHIN-OpenAI-family diversity data by testing multiple variants.
MODEL_FAMILIES = [
    # OpenAI family (confirmed accessible as of 2026-04-07)
    "gpt-4o-mini",
    "gpt-4o",
    "gpt-4.1-mini",
    "gpt-4.1-nano",
    # Other families (may be 503 if yourapi.cn account doesn't have them)
    "claude-haiku-4-5",
    "qwen-plus",
    "gemini-2.5-flash",
    "deepseek-chat",
]

# An intentionally ambiguous A-share event: beat on one metric, miss on
# another. Real markets split on this kind of event. If the simulated
# panel all converges one direction, the moat is broken.
AMBIGUOUS_EVENT_TEXT = """
比亚迪 (002594) 2026 年第一季度业绩公告:
  - 新能源汽车销量 132 万辆, 同比增长 +18% (市场一致预期 +12%, 超预期)
  - 毛利率 17.8%, 同比下降 2.3 个百分点 (市场一致预期持平, 低于预期)
  - 海外订单占比提升至 23%, 但汇率对冲成本上升
  - 公司管理层在问答环节表示 "下半年会通过产品结构优化逐步缓解毛利压力"
  - 同日宁德时代公布价格联盟协议, 行业整体成本端有松动预期
""".strip()


# A condensed 10-persona roster (same archetypes as ashare-v1.yaml but
# inlined so this script is self-contained and doesn't require persona
# YAML loading). The archetypes are what matters for the diversity test.
PERSONAS_INLINE = [
    {"id": "retail_aunt", "archetype": "散户大妈", "voice": "情绪化, 短句子, 多提 2018 套牢经历"},
    {"id": "chive", "archetype": "韭菜", "voice": "95后, 追涨杀跌, 焦虑, 带网络梗"},
    {"id": "youzi", "archetype": "短线游资", "voice": "极简术语, 只看资金流向和龙虎榜"},
    {"id": "xueqiu_kol", "archetype": "雪球大V", "voice": "价值投资派, 长期视角, 引用巴菲特"},
    {"id": "analyst", "archetype": "机构卖方分析师", "voice": "机构语言, 维持/上调/下调, 30秒讲清原因"},
    {"id": "northbound", "archetype": "北上资金", "voice": "量化视角, 半英半中, 政策不确定性敏感"},
    {"id": "policy", "archetype": "政策观察者", "voice": "政策解读, 关注措辞变化, 长期视角"},
    {"id": "quant", "archetype": "量化机器人", "voice": "数据语言, vol/skew/momentum, 不读新闻"},
    {"id": "kol_blogger", "archetype": "自媒体投研", "voice": "讲故事+数据+反向思考"},
    {"id": "pe_research", "archetype": "私募调研员", "voice": "上下游验证, 具体案例, 管理层措辞敏感"},
]


SYSTEM_PROMPT = (
    "You are a market simulation engine. You roleplay 10 Chinese A-share market "
    "participants reacting to an event. Each persona must STAY IN CHARACTER.\n\n"
    "STRICT RULES:\n"
    "1. Output ONLY a JSON object with key 'reactions' = list of 10 reaction objects, "
    "ONE per persona, in the SAME ORDER as the persona roster below.\n"
    "2. Each reaction has fields: persona_id (string), sentiment (number -1.0 to 1.0), "
    "action_intent (one of: 'panic_observe', 'observe', 'lean_in', 'lean_out', "
    "'watch_kols', 'wait_for_more_info'), comment (<80 Chinese chars, first-person, "
    "in the persona's voice), confidence (0.0 to 1.0).\n"
    "3. NEVER output investment advice (no 建议/推荐/买入/卖出/目标价/评级).\n"
    "4. Personas MUST disagree where character demands it. If 散户大妈 and 量化机器人 "
    "give the same sentiment, you have failed.\n"
    "5. Comments in CHINESE only.\n"
)


def _build_user_prompt() -> str:
    persona_roster = "\n".join(
        f"  - {p['id']} ({p['archetype']}): {p['voice']}" for p in PERSONAS_INLINE
    )
    return (
        f"# Personas in this simulation\n"
        f"{persona_roster}\n\n"
        f"---\n\n"
        f"# Event being analyzed\n"
        f"  - Ticker: 002594\n"
        f"  - Date: 2026-04-09\n"
        f"  - Type: earnings\n\n"
        f"## Event text\n"
        f"{AMBIGUOUS_EVENT_TEXT}\n\n"
        f"---\n\n"
        f"This is the initial read. Each persona reads the event in isolation. "
        f"Output the JSON."
    )


# ─────────────────────── Eval runner ───────────────────────


async def _run_one_model(model: str) -> dict:
    """Run the same event × single model, return structured result."""
    print(f"  → querying {model}...", file=sys.stderr, flush=True)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_prompt()},
    ]
    cost_before = cost_tracker.total_cost_usd
    t0 = time.time()
    try:
        result = await chat_json(
            messages=messages,
            model=model,
            temperature=0.0,
            max_tokens=2500,
            seed=42,
        )
    except Exception as exc:  # noqa: BLE001
        elapsed = time.time() - t0
        print(f"  ✗ {model} FAILED: {type(exc).__name__}: {str(exc)[:200]}", file=sys.stderr)
        return {
            "model": model,
            "status": "error",
            "error_type": type(exc).__name__,
            "error_message": str(exc)[:500],
            "elapsed_seconds": round(elapsed, 2),
            "cost_usd": round(cost_tracker.total_cost_usd - cost_before, 4),
        }
    elapsed = time.time() - t0
    cost = round(cost_tracker.total_cost_usd - cost_before, 4)

    # Extract reactions + sentiments
    parsed = result.parsed
    if isinstance(parsed, dict):
        reactions = parsed.get("reactions") or parsed.get("data") or []
    else:
        reactions = parsed if isinstance(parsed, list) else []

    sentiments: list[float] = []
    intents: list[str] = []
    comments: list[dict] = []
    for r in reactions:
        if not isinstance(r, dict):
            continue
        try:
            s = float(r.get("sentiment", 0.0))
        except (TypeError, ValueError):
            s = 0.0
        sentiments.append(max(-1.0, min(1.0, s)))
        intents.append(str(r.get("action_intent", "observe")))
        comments.append(
            {
                "persona_id": str(r.get("persona_id", "?")),
                "sentiment": s,
                "action_intent": str(r.get("action_intent", "observe")),
                "comment": str(r.get("comment", ""))[:200],
            }
        )

    mean = round(statistics.mean(sentiments), 3) if sentiments else 0.0
    std = round(statistics.pstdev(sentiments) if len(sentiments) > 1 else 0.0, 3)

    print(
        f"  ✓ {model}: mean={mean:+.3f} std={std:.3f} n={len(sentiments)} "
        f"cost=${cost:.4f} elapsed={elapsed:.1f}s fingerprint={result.system_fingerprint}",
        file=sys.stderr,
    )

    return {
        "model": model,
        "status": "ok",
        "system_fingerprint": result.system_fingerprint,
        "n_reactions": len(sentiments),
        "sentiment_mean": mean,
        "sentiment_std": std,
        "sentiments": sentiments,
        "intents": intents,
        "reactions": comments,
        "elapsed_seconds": round(elapsed, 2),
        "cost_usd": cost,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
    }


async def run_eval() -> dict:
    print(f"# baseline_eval: {len(MODEL_FAMILIES)} model families × 1 ambiguous event", file=sys.stderr)
    print(f"# event hash: {hash(AMBIGUOUS_EVENT_TEXT) % (10**16):016x}", file=sys.stderr)
    print(f"# starting at cost={cost_tracker.total_cost_usd:.4f}", file=sys.stderr)

    results = []
    for model in MODEL_FAMILIES:
        result = await _run_one_model(model)
        results.append(result)

    # Cross-family analysis (only over successful runs)
    ok_results = [r for r in results if r["status"] == "ok"]
    if len(ok_results) >= 2:
        cross_family_means = [r["sentiment_mean"] for r in ok_results]
        cross_family_mean = round(statistics.mean(cross_family_means), 3)
        cross_family_std = round(
            statistics.pstdev(cross_family_means) if len(cross_family_means) > 1 else 0.0, 3
        )
        within_family_stds = [r["sentiment_std"] for r in ok_results]
        avg_within_family_std = round(statistics.mean(within_family_stds), 3)
    else:
        cross_family_mean = 0.0
        cross_family_std = 0.0
        avg_within_family_std = 0.0

    verdict = _verdict(ok_results, cross_family_std, avg_within_family_std)

    summary = {
        "event_hash": f"{hash(AMBIGUOUS_EVENT_TEXT) % (10**16):016x}",
        "event_text": AMBIGUOUS_EVENT_TEXT,
        "models_tested": MODEL_FAMILIES,
        "models_ok": [r["model"] for r in ok_results],
        "models_failed": [r["model"] for r in results if r["status"] != "ok"],
        "per_model": results,
        "cross_family_mean": cross_family_mean,
        "cross_family_std": cross_family_std,
        "avg_within_family_std": avg_within_family_std,
        "verdict": verdict,
        "total_cost_usd": round(sum(r.get("cost_usd", 0.0) for r in results), 4),
        "run_at": datetime.now(timezone.utc).isoformat(),
    }

    return summary


def _verdict(ok_results: list[dict], cross_family_std: float, avg_within_family_std: float) -> dict:
    """Apply the decision rule from the retrospective.

    The retrospective specified two thresholds:
      - cross_family_std > 0.15 AND at least one family has mean on opposite
        side of zero from another → multi-family IS a fix, proceed.
      - cross_family_std ≤ 0.15 AND avg within-family std < 0.30 → multi-family
        is NOT the fix, pivot to adversarial-by-construction.
      - Anything else is ambiguous → need more events.
    """
    if len(ok_results) < 2:
        return {
            "outcome": "insufficient_data",
            "reason": "fewer than 2 models completed successfully",
            "next_step": "investigate the failed model(s), retry",
        }

    means = [r["sentiment_mean"] for r in ok_results]
    max_mean = max(means)
    min_mean = min(means)
    has_opposite_signs = max_mean > 0.1 and min_mean < -0.1

    if cross_family_std > 0.15 and has_opposite_signs:
        return {
            "outcome": "multi_family_helps",
            "reason": (
                f"cross_family_std={cross_family_std} > 0.15 AND at least one family "
                f"landed positive ({max_mean:+.3f}) while another landed negative "
                f"({min_mean:+.3f}) — multi-family injection would meaningfully widen "
                f"the simulated panel's diversity"
            ),
            "next_step": "proceed with multi-family architectural refactor (wire persona.model through)",
        }

    if cross_family_std <= 0.15 and avg_within_family_std < 0.30:
        return {
            "outcome": "multi_family_does_not_help",
            "reason": (
                f"cross_family_std={cross_family_std} ≤ 0.15 AND avg within-family "
                f"std={avg_within_family_std} < 0.30 — all 4 families converge on the "
                f"same read AND none of them produce much intra-panel diversity. "
                f"The correlated hallucination is not a yourapi.cn routing artifact; "
                f"it's a fundamental property of same-training-data LLMs on ambiguous "
                f"A-share events"
            ),
            "next_step": (
                "pivot architecture: inject disagreement by construction — force one "
                "persona to steel-man the bear case, another to steel-man the bull case, "
                "using prompt engineering rather than hoping models disagree"
            ),
        }

    return {
        "outcome": "ambiguous",
        "reason": (
            f"cross_family_std={cross_family_std}, avg_within_family_std={avg_within_family_std}, "
            f"max_mean={max_mean:+.3f}, min_mean={min_mean:+.3f} — results are mixed. "
            f"Need more events to decide."
        ),
        "next_step": "run baseline_eval on 3-5 more ambiguous events before committing to multi-family",
    }


# ─────────────────────── Output writers ───────────────────────


def _write_json(summary: dict, path: Path) -> None:
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_markdown(summary: dict, path: Path) -> None:
    verdict = summary["verdict"]
    lines = [
        "# ssFish Baseline Eval — Cross-Family Sentiment Diversity",
        "",
        f"**Run at:** `{summary['run_at']}`  ",
        f"**Event hash:** `{summary['event_hash']}`  ",
        f"**Total cost:** `${summary['total_cost_usd']:.4f}`  ",
        f"**Models tested:** {', '.join(f'`{m}`' for m in summary['models_tested'])}  ",
        f"**Models OK:** {', '.join(f'`{m}`' for m in summary['models_ok']) or 'none'}  ",
        f"**Models FAILED:** {', '.join(f'`{m}`' for m in summary['models_failed']) or 'none'}",
        "",
        "## Event",
        "",
        "```",
        summary["event_text"],
        "```",
        "",
        "## Per-model results",
        "",
        "| Model | Status | sentiment_mean | within-family std | n | cost_usd | elapsed_s |",
        "|-------|--------|----------------|-------------------|---|----------|-----------|",
    ]
    for r in summary["per_model"]:
        if r["status"] == "ok":
            lines.append(
                f"| `{r['model']}` | ✓ ok | `{r['sentiment_mean']:+.3f}` | `{r['sentiment_std']:.3f}` | {r['n_reactions']} | `${r['cost_usd']:.4f}` | `{r['elapsed_seconds']:.1f}s` |"
            )
        else:
            lines.append(
                f"| `{r['model']}` | ✗ {r['status']} | — | — | — | `${r.get('cost_usd', 0):.4f}` | `{r.get('elapsed_seconds', 0):.1f}s` |"
            )

    lines.extend(
        [
            "",
            "## Cross-family summary",
            "",
            f"- **cross_family_mean:** `{summary['cross_family_mean']:+.3f}` (mean of per-model means)",
            f"- **cross_family_std:** `{summary['cross_family_std']:.3f}` (std of per-model means — how much the models disagree with each other)",
            f"- **avg_within_family_std:** `{summary['avg_within_family_std']:.3f}` (how diverse each model's internal panel is, on average)",
            "",
            "## Verdict",
            "",
            f"**Outcome:** `{verdict['outcome']}`",
            "",
            f"**Reason:** {verdict['reason']}",
            "",
            f"**Next step:** {verdict['next_step']}",
            "",
            "## Per-model sample comments",
            "",
        ]
    )
    for r in summary["per_model"]:
        if r["status"] != "ok":
            continue
        lines.append(f"### `{r['model']}`")
        lines.append("")
        for rxn in r.get("reactions", [])[:3]:
            lines.append(
                f"- **{rxn['persona_id']}** (sentiment `{rxn['sentiment']:+.2f}`, intent `{rxn['action_intent']}`): {rxn['comment']}"
            )
        lines.append("")

    lines.extend(
        [
            "---",
            "",
            "_This eval was generated by `scripts/baseline_eval.py` per the retrospective's Week-1 launch blocker. It answers: does multi-family LLM injection actually produce meaningful cross-family diversity, or is 'correlated hallucination in costumes' a fundamental property of same-training-data LLMs on A-share events?_",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


# ─────────────────────── Main ───────────────────────


def main() -> None:
    summary = asyncio.run(run_eval())

    # Write outputs
    eval_dir = Path(__file__).resolve().parents[1] / "eval"
    eval_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    json_path = eval_dir / f"baseline-eval-{ts}.json"
    md_path = eval_dir / f"baseline-eval-{ts}.md"
    _write_json(summary, json_path)
    _write_markdown(summary, md_path)

    print(file=sys.stderr)
    print(f"# baseline_eval complete.", file=sys.stderr)
    print(f"#   verdict: {summary['verdict']['outcome']}", file=sys.stderr)
    print(f"#   cross_family_std: {summary['cross_family_std']}", file=sys.stderr)
    print(f"#   avg_within_family_std: {summary['avg_within_family_std']}", file=sys.stderr)
    print(f"#   total cost: ${summary['total_cost_usd']:.4f}", file=sys.stderr)
    print(f"#   results: {json_path}", file=sys.stderr)
    print(f"#   report:  {md_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
