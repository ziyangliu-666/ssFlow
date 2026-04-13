---
date: 2026-04-13
simulation_id: oasis_24210d687179
git_commit: 8d548c5
round: 15
level: L1
codex_session: 019d866a-908a-7303-87ce-b28c407743fc
---

**角度 5：可信度杀手 Top 3**

1. 159915 +0.15% vs 399006 -2.15%——ETF和指数反向，持续性问题。
2. northbound_qfii 买入53.59億但只亏-1.76 ¥M——P&L量级对不上。
3. "国家队/险资/大股东等(观望): 无恐慌信号"——模板话。

**根因**：agents 直接交易 159915 (ETF) 产生独立正向 Kyle 冲击，
覆盖了事件主标的的负向 spillover。需要 post-phase 约束让 sector_etf 跟踪其指数。
