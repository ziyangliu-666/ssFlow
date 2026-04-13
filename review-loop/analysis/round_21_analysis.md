# Round 21 Analysis — Per-class flow exceeds ADV by 15x

## Problem
全球宏观对冲基金单轮做空 ¥142亿，标的 ADV 才 ¥9.45亿（15x ADV），物理上不可能。

## 5 Whys

**问题**: 单一参与者的交易量远超标的日均成交

**Why 1**: 宏观对冲 15 个 agent 全部做空 15%，合计流量 ¥142亿
→ 每个 agent 中位 capital ¥80亿 × 15% = ¥12亿，15 个 agent = ¥180亿

**Why 2**: 引擎没有按 ADV 限制单个 class 的每轮流量
→ apply_distribution_to_agent_pop 只看 agent 自身的仓位/资金约束

**Why 3**: Kyle 模型的输入是净流量，不管流量是否可达
→ 现实中 142亿 的做空需要大量券源 + 对手方承接，根本执行不了

**Why 4**: 引擎信任 LLM 决策 + agent 资金，不校验市场容量
→ 缺少"市场能吸收多少"这一层约束

**Why 5**: 根因——**没有 per-class flow cap relative to ADV**

## 根因层级判定

Why 2-4 → **L1（引擎约束层）**

## 本轮修复

在 oasis_engine.py 中，apply_distribution_to_agent_pop 返回后、
flow 进入 phase summation 之前，添加 per-class ADV flow cap：

- 每个 class 每个 phase 的 |net_flow| 不超过 ADV_FLOW_CAP * effective_adv
- ADV_FLOW_CAP = 1.0 (最大 = 当天全部成交)
- 超过时 scale down flow.net_flow，log 警告
- Agent state 暂不调整（未来可做 partial fill）
