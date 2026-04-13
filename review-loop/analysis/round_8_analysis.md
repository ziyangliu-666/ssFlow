---
round: 8
date: 2026-04-13
escalation: L1 (engine — cross-round price continuity)
---

# 5 Whys: R0 close ≠ R1 open (价格跨轮断档)

## 问题
每轮的收盘价和下一轮的开盘价不一致。
R0 收 ¥3595.14, R1 开 ¥3613.55 (+¥18.41)。
R1 收 ¥3604.69, R2 开 ¥3624.63 (+¥19.94)。
Codex 在 round 4, 5, 6, 7 都指出此问题。

## 5 Whys

**Why 1**: 报告里 Round N 结束价 ≠ Round N+1 起始价
**Why 2**: 每轮的 `initial_price` 来源不一致——可能是从 phase 价格推导还是从 AdaptiveADV 推导
**Why 3**: fast_react 和 slow_react 的价格推进可能在轮次之间没有正确衔接
**Why 4**: oasis_engine 在推进轮次时可能重新读取了某个中间状态作为 initial_price
**Why 5**: 根因——需要检查 oasis_engine.py 中轮次推进时 price 的传递逻辑

## 修复

L1: 确保 round N+1 的 initial_price 直接取自 round N 最后一个 phase 的 final_price，
不经过任何中间计算或状态读取。

## 次要问题（本轮不修）

1. P&L 全正（capital/holdings baseline 不一致）
2. 399006 被当做可交易标的（需要 L2/L3 改造）
3. 价格冲击太大（ADV 可能需要 constituent-level 计算）
