---
round: 5
date: 2026-04-13
escalation: L2 → L1 (engine constraint — post-LLM validation + flow impact)
---

# 5 Whys: 散户"观望"却是净买盘 (出现两次)

## 问题
散户(中产配置型) R1 净买盘 ¥0.92億, R2 净买盘 ¥1.18億,
理由都写"决定暂时观望，保持现有持仓"。出现两次，自毁信用。

## 5 Whys

**Why 1**: 报告里 rationale 说"观望"但 net_flow > 0
**Why 2**: LLM 在 submit_trading_decision 里选了 side="buy" + 小 qty,
但 rationale 文本写了"观望"
**Why 3**: L2 prompt 约束说"如果 hold，不要用动作词"，但没有反向约束：
"如果 rationale 说观望，side 必须是 hold"
**Why 4**: LLM 的 side 和 rationale 是独立生成的，没有后校验
**Why 5**: 根因——缺少 post-LLM 校验层，当 rationale 明确表达
hold/观望意图时，应该覆盖 side 为 hold

## 修复

L1: 在 oasis_trading_tool.py 的 OrderCollector.add() 添加
rationale-side 一致性校验。当 rationale 明确说"观望"/"保持"
且不含任何进入/离场动作词时，强制 side="hold", quantity=0。

## 次要问题：Kyle 冲击太小

¥93億 净卖出 (ADV ¥9.69億 = 9.6x) 只产生 -0.06%。
flow compression knee=1.5% 把 460% 压到 1.45%，太激进。
但这是参数调优问题，本轮先解决 rationale-side 一致性。
