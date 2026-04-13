# Loop Summary: Rounds 20-28 (2026-04-13)

## Objective
降低 Codex review 中的可信度杀手数量（目标 ≤1）。

## Result
**killer_count: 3 → 3（未达标），但杀手性质从 L0/L1 → L3 架构级**
**angle4_count: 3 → 2（改善）**

## What changed (Round 20-28 累计)

| Fix | Level | Commit | Effect |
|-----|-------|--------|--------|
| 机构 voice_prompt 加事件触发器 | L2 | e836850 | 参与定价力量 3→7 |
| Per-class ADV flow cap (1x) | L1 | 30827a4 | 价格方向翻转 -2.71%→+12.41% |
| Market-share-scaled cap | L1 | 2854d65 | 消除"统一盒饭" |
| 系统后台泄露移除 | L0 | 85c196d | "(no tool call)" → 自然语言 |
| Volume-share mult 5→3 | L1 | b2c1c25 | 消除量化=ADV |
| 游资 persona 新增 | L2 | 4396c2a | A股小盘特色参与者 |
| Net-flow direction guard | L1 | a88396a | 消除"说卖但净买"矛盾 |
| Clamped rationale suppression | L0 | 7b43b72 | 消除补丁痕迹泄露 |
| Sell-only hard constraint | L1 | f26f3aa | 解禁减持不再买入 |

## What's still broken (L3 — 需要人工介入)

### 1. 价格形成机制
- 当前: Kyle square-root flow impact model
- 问题: 净流量→价格，不是订单簿撮合→价格
- Codex 原话: "价格不是盘出来的，是算出来的"
- 修复方向: 引入 order book matching engine 或 at minimum limit-order-book simulation
- 难度: 高（需要重写 trading_layer 核心）

### 2. Round-based "三幕剧"架构
- 当前: fast_react → slow_react 每轮按 agent_type 分批执行
- 问题: 参与者按角色轮次排队，不是并发博弈
- Codex 原话: "不会按'角色轮次'排队发言"
- 修复方向: 连续拍卖 / 事件驱动执行 / agent-level tick
- 难度: 极高（OASIS 架构限制）

### 3. A 股交易制度缺失
- 缺少: 涨跌停（10%/20%）、集合竞价、T+1 约束（已部分实现）
- 缺少: 封单/炸板/回封动态、换手率
- 修复方向: LimitBoard 已有基础框架，需要扩展为真实涨停约束
- 难度: 中等（现有 LimitBoard 可扩展）

### 4. P&L 混账
- 存量持仓浮盈 vs 交易实现收益未区分
- 非交易参与者（major_holder）的 P&L 主导全表
- 修复方向: 分离"被动浮盈"和"交易 P&L"两张表
- 难度: 低-中

## Recommended next steps (按优先级)

1. **P&L 分表**（L0，1-2h）: 分离被动浮盈和主动交易 P&L，只展示主动交易者
2. **涨跌停约束**（L1，4-8h）: 扩展 LimitBoard 为真实 A 股涨停板
3. **Adaptive ADV 标定**（L1，2-4h）: 修复 R1 冲击>R0 的不一致
4. **Order book prototype**（L3，2-4 weeks）: 替换 Kyle 为简化 LOB

## Metrics trend

```
Round  Killers  Angle4  Improved  Level
1-18   3        3       shifting  L0/L1
20     3        2       shifted   L2     ← prompt activation
21     3        2       improved  L1     ← ADV cap, price flipped
22     3        2       improved  L1     ← market-share cap
23-24  3        2       shifted   L0/L1
25     3        2       shifted   L2     ← 游资 added
26     3        2       shifted   L1     ← direction guard
27     3        2       shifted   L0     ← rationale suppression
28     3        2       no        L1     ← sell-only constraint (L3 ceiling)
```
