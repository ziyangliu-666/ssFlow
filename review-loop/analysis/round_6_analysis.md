---
round: 6
date: 2026-04-13
escalation: L1 (engine data — ADV unit for indices)
---

# 5 Whys: ADV = ¥636099.40億 (63.6万亿, 量纲爆炸)

## 问题
报告 metadata 显示 `ADV = ¥636099.40億`，相当于 63.6 万亿元日成交额。
真实 399006 创业板指 ADV 约 500-2000 亿。差 100-1000 倍。
Codex 将此列为 #1 可信度杀手。

## 5 Whys

**Why 1**: 报告格式化正确（value / 1e8 + "億"），问题在上游数值
**Why 2**: `distillation.py` 从 `fetch_market_quote()` 拿到 adv_value 直接存入 Instrument
**Why 3**: `market_data.py` 的 `_fetch_sina_kline_adv()` 对所有标的统一计算 `close × volume`
**Why 4**: Sina 对股票返回的 volume 是股数（需要 × close 转成交额），
但对指数 volume 字段本身就已经是成交额（CNY），不需要再乘
**Why 5**: 根因 — `_fetch_sina_kline_adv()` 没有区分股票和指数的 volume 语义，
对指数做了多余的 `close × volume` 操作，导致 ADV 被放大 ~index_points 倍

## 修复

L1: 在 `market_data.py` 的 `_fetch_sina_kline_adv()` 中，
检测 ticker 是否为指数（399xxx, 000001 等），
如果是指数则直接用 volume 作为 turnover，不乘 close。

## 影响链

ADV 被放大 → Kyle formula 中 flow/ADV ratio 被压小 →
价格冲击被严重低估 → "¥113億 净卖出但价格只跌 -0.67%"
→ 同时导致跨标的价格变动被压成几档固定值

## 次要问题（本轮不修）

1. P&L 全部为正（价格跌了但所有人赚钱）— spawn 时 capital 与 holdings 不一致
2. "35 personas" vs "9991 agents" — 报告头 scale 描述不清晰
