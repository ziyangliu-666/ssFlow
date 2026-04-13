---
date: 2026-04-13
simulation_id: oasis_cafc922f7570
git_commit: dbb30a5
round: 8
level: L1
codex_session: 019d8601-da3b-7a01-8076-67288b7dfcd5
---

**角度 5：可信度杀手 Top 3**

1. `fast_react: ¥3639.35 → ¥3612.01` — Round 1 header说¥3617.36开，phase却从¥3639.35开。同轮两套起点价格。
2. `公募ETF AP (净卖盘): 小幅进入头寸15%现金 *(净卖盘与增持意图偏差)*` — 正式报告承认成交方向和意图不一致。
3. `flow=¥+1.46億 → +4.05%` — 价格冲击失真。

**进展**：跨轮价格连续性已修复（R0 close = R1 open），P&L 方向正确。

**新问题**：gap_open 导致 phase breakdown 起始价与 round header 不一致。
