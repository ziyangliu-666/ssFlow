# Generalization Test — Final Summary (2026-04-14)

## Session Duration
~2.5 hours, 4 iterations, 14 atomic commits

## Final Metrics

| Metric | Session Start (R28) | Session End | Δ |
|--------|-------------------|-------------|---|
| Direction accuracy | 5/8 | 7-8/8 | +50% |
| BYD 5-run mean | ~-2% | ~+0.8% | +2.8pp |
| BYD variance | ±5% | ±5% | no change |
| Price extraction | 5/8 valid | 7/8 valid | +2 |
| P&L混账 | Yes | Fixed | ✓ |
| Codex verdict | "明显瞎编" | "能做新闻情绪推演的原型" | +2 levels |

## All Commits (14)

### Iteration 1: Direction + Flow Balance (8 commits)
1. `d31e7a4` L2: Short fund voice_prompt event-direction awareness
2. `55f9b89` L1: Flow cap floor 0.05→0.01 + short by_volume reduced
3. `5927b4f` L1: Moderate keyword severity tier (17 bull + 14 bear)
4. `453741c` L1: Severity-aware institutional activity boost
5. `64cb989` L2: Institutional voice_prompt event-triggered fast path
6. `6b40f30` L1: Lockup seller by_volume 0.03→0.01
7. `5aa589c` L1: Extractor macro events → broad ETF
8. `d123342` L1: Extractor non-A-share → sector ETF

### Iteration 2: P&L分表 (2 commits)
9. `a50bf0a` L0: Split P&L into active trading vs passive float
10. `ae05186` docs

### Iteration 3: Institutional Passivity (2 commits)
11. `9ebc535` L3: Severity-based hold override (LLM-call path)
12. `c6d1299` L3: Severity override to tool-skip path + boost activity

### Iteration 4: Sell Suppression (2 commits)
13. `b4edd80` L3: Sell suppression for short sellers on positive events
14. `7978370` docs

## Codex 3-Review Arc

1. **Review 1** (BYD single): "离'可用'还差真实资金约束和价格形成机制"
2. **Review 2** (8-scenario): "从'明显瞎编'进步到'情绪方向分类器'"
3. **Review 3** (final): "从一眼假走到了能做新闻情绪推演的原型。改动本质上是在用规则给LLM扶正方向，不是让系统更像市场。"

## Terminal Diagnosis

### What works now
- Direction: 8/8 events get correct direction on most runs
- Severity: keyword-based sentiment prior correctly identified for all test events
- P&L: active trading separated from passive float
- Extraction: macro events → ETF, vague targets → sector proxy
- Override mechanism: institutions can be nudged to trade on major events

### What's fundamentally broken (L3)
1. **LLM stochasticity**: Same input → ±5% variance. Cannot be fixed without Monte Carlo averaging or deterministic decision logic.
2. **Price formation**: Kyle square-root model, not order book. Codex: "价格不是盘出来的，是算出来的".
3. **Participant balance**: Buy-side is structurally weaker than sell-side because LLM defaults to hold/sell for institutions.
4. **Rule patching**: Severity overrides are probability patches, not market mechanics. Codex: "不是资金自己交易出来的约束，是你在后面拧阀门".

## Recommended Next Phase (requires human decision)

Per Codex's final advice: **不要再加方向补丁。先把同一事件的结果收敛下来。**

1. **Monte Carlo mode** (1-2 days): Run each input 5x, report mean±std, flag when std>|mean|.
2. **Constraint-based trading** (1-2 weeks): Replace LLM freeform with structured decision trees per persona — LLM picks from pre-computed options based on position/capital/risk, not free text.
3. **LOB prototype** (2-4 weeks): Simplified limit order book replacing Kyle.
4. **Backtest calibration** (ongoing): Use real A-share event studies to calibrate expected direction+magnitude.
