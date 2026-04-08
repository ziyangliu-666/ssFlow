# A Share Investor Structure — Research Notes

> Source data backing every `market_share` number in `personas/ashare.yaml`.
> Last refresh: 2026-04-08.
>
> This file is the **truth-source** for the panel composition. When the
> structure of A-share market participants changes (e.g., new annual
> investor structure report), update this file FIRST, then refresh the
> `market_share` fields in `personas/ashare.yaml` to match. Both files
> should always be consistent.
>
> Cross-reference: `docs/persona-pack-spec-v1.md §2` has the same data
> in spec-document form. This file is the runtime / contributor reference.

## TL;DR (the four most important numbers)

| Statistic | Value | Source |
|---|---|---|
| Total A-share investors (accounts) | 2.4 亿 (240M) | 中登 / 财联社 2025 |
| Individual investor share of accounts | 99.76% | 财联社 2025 |
| Individual investor share of holdings | 32.3% (流通市值, 2024 末) | 华西证券 |
| Individual investor share of trading volume | 60-70% | 东方财富 / 多个行业研报 |

The single most important thing this tells you: **A股 by account count
is overwhelmingly retail; by trading volume is still majority retail; but
by holdings is a minority retail market** with strategic capital and
institutions holding the bulk of value. Any persona panel that ignores
this asymmetry is wrong.

## 1. Account distribution by capital (中登 2025-12)

| Capital range | Account share |
|---|---|
| < 1 万 | 23.15% |
| 1-10 万 | 48.48% |
| 10-50 万 | 21.65% |
| 50-100 万 | 3.75% |
| 100-500 万 | 2.62% |
| > 500 万 | 0.88% |
| **< 10 万 累计** | **71.63%** |

**Implication for personas:** more than 70% of A股 retail accounts have
< 10 万 in capital. Our `retail_short_term_chaser` persona uses a lognormal
distribution with median ¥120k to capture this — slightly higher than the
median because we filter to accounts that actually trade (the < 1 万 tier
is dominated by dormant accounts).

## 2. Persona class market share (持流通市值, 2024 末)

Source: 华西证券《A 股投资者结构全景图 2024Q3》, also reported in
新浪财经 2025-06-23.

| 五分法分类 | 持流通市值占比 | ashare.yaml personas |
|---|---|---|
| 产业资本 | 34.4% | `industrial_capital_strategic` (20%) + `cross_holding_strategic` (10%) + `major_individual_holder` (6%-ish via separate persona) |
| 一般个人投资者 | 32.3% | `retail_short_term_chaser` + `retail_passive_holder` + `retail_pro_am_value` (sums to ~32%) |
| 专业投资机构 | 19.2% | `mutual_fund_active_pm` (7.3%) + `etf_authorized_participant` (~3%) + `private_equity_active` (4.1%) + `insurance_pension` (~4%) + `northbound_qfii` (~3%) (sums to ~21%, slight over because we double-count northbound which isn't in the 五分法) |
| 政府持股 | 7.6% | `government_state_strategic` (5%) + `national_team_strategic` (3%) (sums to 8%, slight over for the same reason) |
| 个人大股东 (5%+) | 6.4% | `major_individual_holder` (6%) |

`ashare.yaml`'s by_holdings sum is **0.992 ≈ 1.0**, which matches reality.

## 3. Trading volume contribution (流量, ~2024)

Source: 东方财富 2025-08, 证券时报, 多个行业研报.

| 类别 | 成交量贡献 |
|---|---|
| 散户 (个人投资者) | 60-70% |
| 公募 + 私募 + 险资 + 一般法人 | 20-25% |
| 北上资金 (含外资量化) | 5-8% |
| 国内量化 (主动+高频) | 15-25% (overlap with above) |

注: 量化总和 (国内 + 北上量化) 约 20-30% of total volume.

**Implication for personas:** retail's by_volume share is much higher than
its by_holdings share (60-70% vs 32%) because retail churns much faster
(annual turnover ~8x for short-term traders, 1.5x for passive holders).
This is why our panel needs DIFFERENT weights along by_volume vs by_holdings
dimensions — `retail_short_term_chaser` has by_volume=0.30 but by_holdings=0.10,
while `industrial_capital_strategic` has by_volume=0.03 but by_holdings=0.20.

`ashare.yaml`'s by_volume sum is **1.170 (slightly > 1.0)** because high-turnover
classes (散户 + 量化) contribute disproportionately to volume.

## 4. Behavioral split within retail (二八定律)

Source: 多个 2024-2025 调查 (东财财富号, 证券时报):

- ~30% 散户账户 (~7000万户) 贡献 60-70% 成交量
  - 集中在中小盘 + 题材股
  - 平均持股周期 < 40 天
  - 高换手率, 跟风炒作明显
  - This is `retail_short_term_chaser`'s class

- ~70% 散户账户 (~1.7亿户) 是配置型
  - 资金集中沪深 300
  - 低交易频率
  - This is `retail_passive_holder` + `retail_pro_am_value` (the 高净值 tail)

- 散户 2025H1 年化亏损率: 23.6%
- 散户 2025H1 盈利账户比例: 仅 18.9%
- 100 万+ 账户盈利比例: > 90%

**Implication:** the `retail_pro_am_value` class is small in count but
disproportionately profitable. Their `position_size_pct_when_holding` and
`stop_loss_discipline` are both higher than the short-term chaser class.

## 5. Major institution per-stock allocation calibration

The big pitfall when authoring institutional personas is that
**`capital_distribution.median_cny` is the fund's TOTAL AUM, not its
position in this single stock.** The per-stock exposure is computed
via `position_size_pct_when_holding`. For institutions:

| Institution type | Typical fund AUM | Typical per-stock allocation | ashare.yaml setting |
|---|---|---|---|
| 公募主动权益 | ¥5-200 亿 | 1-4% of fund | `position_size_pct_when_holding: [0.01, 0.04]` |
| 私募主动多空 | ¥10-100 亿 | 2-8% of fund | `[0.02, 0.08]` |
| 险资 | ¥1000-5000 亿 | 0.1-0.5% of fund | `[0.001, 0.005]` |
| 北上资金 (per book) | ¥5-300 亿 | 1-5% of book | `[0.01, 0.05]` |
| 量化 (factor models) | ¥10-300 亿 | 0.05-0.5% of fund | `[0.0005, 0.005]` |

`max_position_pct` in the `risk` block is the HARD CAP — buy actions
get clamped to respect this. Mutual funds have `max_position_pct = 0.05`
because of fund concentration limits (5% rule).

For strategic personas, `capital` represents the CNY value of their
actual block holding in this specific stock (not their total wealth).
E.g., 王传福 holds ~17% of BYD ≈ ¥100B at current market cap, so the
`industrial_capital_strategic` median capital is ¥30B for an "average"
stock and the position_size is fixed at 0.95 (nearly all wealth in
this single name).

## 6. Information source ecosystem (2024-2025)

Source: 多个调查 + 知乎 + 集思录用户 reports.

| 平台 | 用户群体 | ashare.yaml `information.primary_sources` 引用 |
|---|---|---|
| 雪球 | 中产散户、半专业 | `xueqiu_long`, `xueqiu_long_form` |
| 东方财富股吧 | 普通散户 | `eastmoney_guba` (for chaser), `eastmoney_articles` (for passive) |
| 抖音 / 快手 财经号 | 新散户、年轻人 | `douyin_finance` (chaser only) |
| 小红书 炒股笔记 | 女性散户、95+ | `xiaohongshu_notes` |
| 集思录 | 套利党、可转债玩家 | `jisilu_arbitrage` (pro-am only) |
| 微信群 / QQ 群 | 中老年散户 | `wechat_groups` (chaser) |
| 大V (微博/微信公众号) | 跟随型散户 | `mass_kol`, `weibo_finance` |
| 券商研报 | 跟随型散户 | `sell_side_research` (passive + pro-am) |
| 公司年报 / 公告原文 | 价投 | `annual_reports`, `conference_calls` (pro-am) |

The pro-am persona has `english_capable: true` because they read
international research; the short-term chaser has `english_capable: false`.

## 7. References (full)

1. 中国证券登记结算有限责任公司, 投资者数量月报 2025-12  
   https://www.chinaclear.cn/

2. 华西证券研究所《A 股投资者结构全景图 2024Q3》  
   https://www.fxbaogao.com/detail/4612072

3. 新浪财经《从投资者结构变化看资本市场投资端改革 — 2024 年投资者结构全景分析》2025-06-23  
   https://finance.sina.com.cn/jjxw/2025-06-23/doc-infcanva9299037.shtml

4. 新浪财经《十五幅图了解 A 股各类资金行为和偏好》2024-08-08  
   https://finance.sina.com.cn/roll/2024-08-08/doc-inchwxxk1914485.shtml

5. 证券时报《深交所摸底 A 股个人投资者状况调查》2024-03-20  
   https://www.stcn.com/article/detail/1152319.html

6. 财联社《A 股投资者首超 2.4 亿, 个人投资者超 99%》  
   https://www.cls.cn/detail/2086830

7. 东方财富财富号《散户在 A 股市场中的人数和资金量占比》2025-08  
   https://caifuhao.eastmoney.com/news/20250824214141911949940

8. 深圳证券交易所投资者教育《个人投资者状况调查报告》系列  
   https://investor.szse.cn/institute/bookshelf/report/index.html

9. 中国证券业协会  
   https://www.amac.org.cn/

10. 毕马威《2024 年度中国证券业调查报告》  
    https://assets.kpmg.com/content/dam/kpmg/cn/pdf/zh/2024/09/mainland-china-securities-survey-2024.pdf

11. 中国证券投资者保护基金  
    http://www.sipf.com.cn/

## When to refresh this file

- **Annually** when the 五分法 numbers update (typically Q1 of the following year)
- **Quarterly** if the by_volume splits are showing meaningful drift
- **Whenever** a new persona class is added to `ashare.yaml`
- **Whenever** the 中登 monthly report shows a major shift in account distribution

Update this file FIRST, then update `ashare.yaml` to match. Run
`uv run python -c "from ssfish.persona import load_personas; ps = load_personas('personas/ashare.yaml'); print(sum(p.market_share.by_holdings for p in ps))"`
to verify the holdings sum is still ≈ 1.0.
