---
round: 12
date: 2026-04-13
escalation: L1 (engine — disable gap_open for rounds > 0)
---

# 5 Whys: 正流入却跌价 (flow=+0.09億 → -0.20%)

## 问题
R2 fast_react 显示 flow=+0.09億 但价格 ¥3364.67 → ¥3357.82 (-0.20%)。
正向资金流却产生负向价格变动，自相矛盾。

## 5 Whys

**Why 1**: 显示的 price_before 是 pre-gap (3364.67)，price_after 是 post-gap+post-trade (3357.82)
**Why 2**: gap_open 在 R2 开盘时把 current_price 从 3364.67 推到 ~3354（负向gap）
**Why 3**: fast_react 的 +0.09億 flow 把价格从 3354 推到 3357.82（正确方向）
**Why 4**: 但 phase 显示的 delta 从 3364.67(pre-gap) 到 3357.82 = -0.20%（包含了 gap）
**Why 5**: 根因——gap_open 的隔夜情绪模型产生的价格跳变被混入了 first phase 的显示中

## 修复

L1: 禁用 R1+ 轮次的 gap_open。理由：
1. gap_open 产生的隔夜情绪已经由 LLM personas 的反应隐式建模
2. 无论怎么修复显示，gap 总会在某个维度导致状态不一致
3. R0 的 gap_open（事件初始冲击）也可以移除——LLM 在 fast_react 会自己反应
