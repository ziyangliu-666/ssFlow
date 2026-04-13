---
date: 2026-04-13
simulation_id: oasis_687b9dec7c6d
git_commit: c450fe3
round: 12
level: L1
codex_session: 019d863c-d285-7093-ae05-4c538e4f3638
---

**角度 5：可信度杀手 Top 3**

1. 159915 (创业板ETF) +4.12% 而 399006 (创业板指) -3.95%。ETF 和指数方向矛盾。
2. slow_react flow=+4.55億 但 price 不变。价格冲击缺乏单调性。
3. 全部18类P&L均为正——非事件标的上涨洗平了主事件跌幅，P&L失去可解释性。

**进展**：gap_open 移除解决了 flow/price 方向矛盾。

**根因**：spillover 模型把 399006 的跌幅错误传导为非事件标的的涨幅（或完全不传导），
导致 ETF 和指数反向、P&L 被非事件标的冲掉。
