---
round: 4
date: 2026-04-13
escalation: L0 → L2 (LLM prompt constraints)
---

# 5 Whys: 参与者行为与标签/理由方向矛盾

## 问题
Codex 连续 3 轮指出同一类可信度杀手：
- 事件驱动做空基金：看涨理由推出做空动作
- 北上资金(外资减配)：标签说减配，实际大买
- 产业资本(解禁减持)：说要进入但观望

## 5 Whys

**Why 1**: 报告里 rationale 文本和 net_flow 方向不一致
→ Round 2-3 在这个层级修（L0 报告层注释），没用

**Why 2**: ClassFlowResult 的 rationale 和 net_flow 是独立字段，引擎不校验一致性
→ 可以在 trading_layer.py 加校验（L1），但这只是补丁

**Why 3**: LLM 在 `submit_trading_decision` tool call 里独立指定 side 和 rationale，
schema 没有约束两者方向一致。LLM 可以写"看涨"但选 side="sell"
→ 可以改 tool schema 加 validation（L1），但 LLM 仍然会产出矛盾

**Why 4**: `_user_info_for()` 构建的 system prompt 给了 LLM 事件历史先例
（"政策利好通常+20-50%"），但完全没有基于 persona 的行为约束。
一个"做空基金"收到的 prompt 和"散户"几乎一样——都是"政策利好"叙事。
→ 这是根因之一：prompt 没有 role-specific 约束

**Why 5**: 设计上 persona 的 `role` 和 `decision_mode` 是纯元数据标签，
不参与 prompt 构建也不约束 tool schema 的合法输出。
LLM 被赋予了一个角色身份但没有被告知这个身份的行为边界。

## 根因

`oasis_persona_adapter.py` 的 `_user_info_for()` 不根据 persona 的 role/archetype
注入行为约束指令。所有角色收到近似相同的 system prompt（只有 voice_prompt 不同），
导致 LLM 输出同质化且角色-行为脱节。

## 修复层级

**L2 (LLM 提示词)**：在 `_user_info_for()` 中，根据 persona 的 role 注入
角色特定的行为约束：
- short_seller/hedge_fund_short: "你只能做空或观望，不能做多"
- strategic_holder (减配类): "你的默认倾向是减持，需要极强理由才能增持"
- 所有角色: "你的交易理由必须和交易方向一致——如果你认为会涨但选择卖出，必须解释为什么"

## 之前尝试的修复（为什么不够）

- Round 2 (L0): 修了价格数据一致性 → 和行为矛盾无关
- Round 3 (L0): 加了报告注释说明矛盾 → 标注问题不等于解决问题
