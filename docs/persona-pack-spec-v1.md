# Persona Pack Spec v1

> **状态:** 设计草案, 等待 review 决定是否立项
> **日期:** 2026-04-08
> **作者:** ssFish 团队 (with research from Claude Opus 4.6)
> **目标:** 把 ssFish 的 persona panel 从"虚构原型"改成"真实市场结构对应的可插拔预设包", 让同一个引擎能跑 A 股 / 美股 / 大宗商品 / 任何有可观测参与者结构的市场.

---

## TL;DR

**问题:** 当前 `personas/ashare-v1.yaml` 是 10 个 vibes-based 抽象原型, 权重凭感觉, 不引用任何真实数据源. 而且当前架构是"sentiment aggregation 启发式" — 从 sentiment_mean 用拍脑袋公式映射到 implied_move, 没有任何市场机制依据.

**解决方案 (两阶段):**

**Phase A — Persona Pack v2 (输入侧):** 把 persona panel 升级成"市场预设包" (market preset pack) 系统. Schema v2 增加 `market` / `data_sources` / `market_share` / `initial_capital` / `initial_holdings` 等字段, 强制每个 persona 引用真实公开数据 (中证投保基金 / 深交所投教 / SIFMA / CFTC COT). 一个 market 一个 yaml, 通过 `--market ashare` 选择.

**Phase B — Sandbox Execution Mode (引擎侧):** 把 sentiment aggregation 替换成 agent-based market model. 每个 persona 类别有 N 个 stochastic agent 实例, 每个 agent 有真实资金和持仓. LLM 输出**动作分布**而不是 sentiment, sandbox 在这个分布下采样 N 个 agent 的具体动作 (买/卖/持有), 聚合成订单流, 然后用 square-root price impact (Kyle model) 从订单流推导价格变化. 价格反馈到下一轮, 形成真实的多轮动力学. **LLM 调用数和当前一样 (1 次/类别/轮), 但输出的是物理学意义上的真实价格轨迹.**

**为什么这两个一起做:**
- Phase A 没有 Phase B: 仍然是 sentiment 启发式, panel 真实但输出不真实
- Phase B 没有 Phase A: 沙箱跑出 emergent dynamics, 但 agent 是虚构的, 仍然 GIGO
- 两个一起: 真实 panel + 真实机制 = ssFish 第一次有了**可证伪的输出** (价格轨迹可以直接对账 T+1, T+5)

**为什么现在做这个:** 昨晚 scorecard entry #001 的 H3 finding (context completeness 是 sentiment 的主导变量, 比 architecture 重要得多) 暗示**输入质量 >> 引擎复杂度**. Persona 的真实性是输入质量的一半. 沙箱机制是另一半 — 让输出有物理意义而不是启发式. 在用户没动 multi-family 之前先把这两块做对, 性价比高得多.

**取消的工作:** Baseline eval (虚构 panel 上测多样性是浪费); multi-family 架构改写 (在 panel 真实 + 沙箱机制到位之前不 justified).

---

## 1. 当前 ashare-v1.yaml 的三条具体罪状

### 罪状 1: 权重和真实比例完全脱节

```
当前 panel weight 分布:
  散户大妈     1.0
  韭菜         0.8   ← 最低?? 真实韭菜贡献巨大成交量
  短线游资     1.3
  雪球大V      1.5   ← 最高?? 雪球大V是声音放大器, AUM 极小
  机构卖方     1.4
  北上资金     1.3
  政策观察者   1.2
  量化机器人   1.1
  自媒体投研   1.2
  私募调研员   1.4

  retail-ish 合计    5.8 / 12.2 = 47.5%
  institutional 合计 6.4 / 12.2 = 52.5%
```

**对比真实 A 股 (volume-weighted, 2024-2025):**
- 散户贡献 60-70% 成交量
- 机构 + 北上 + 量化 + 一般法人 合计 ~30-40% 成交量

**当前 panel 反过来了.** 雪球大V 不应该是权重最高的——他的 AUM 大概率比一个普通游资还小, 但因为"声音大"被赋予 1.5. 这是把"audibility"和"market impact"混为一谈.

### 罪状 2: 完全漏掉产业资本和政府队

按 2024 年末持股市值五分法 (来源: 新浪财经/华西证券):

| 类别 | 持流通市值占比 | ashare-v1 中是否存在 |
|---|---|---|
| **产业资本** (大股东、关联方、上市公司互持) | **34.4%** | ❌ 完全没有 |
| 一般个人投资者 | 32.3% | ✅ 散户大妈/韭菜 (权重错) |
| 专业投资机构 (公募+私募+险资+社保) | 19.2% | ✅ 卖方/私募 (但混乱) |
| 政府持股 (汇金、证金、地方国资) | 7.6% | ❌ 完全没有 |
| 个人大股东 | 6.4% | ❌ 没有 |

**最大的 single category (产业资本 34.4%) 整个不存在.** 这意味着 ssFish 在分析任何涉及大股东减持/增持/质押/对赌的事件时, 完全听不到这部分声音. 而 A 股最重要的事件类型之一恰好是大股东行为.

### 罪状 3: 权重凭感觉, 没有引用任何数据源

`ashare-v1.yaml` 里没有任何 `data_source:` 或 `citation:` 字段. 数字是怎么来的, 没人知道. 这违反了 ssFish 自己的 Live Scorecard 透明度承诺——一个声称"prospectively scoreable"的工具, 它的输入侧不可追溯, 是讽刺.

---

## 2. 真实 A 股投资者结构 (with citations)

### 2.1 账户数与资金量分布 (中登口径, 2025 年末)

| 资金量分档 | 账户占比 |
|---|---|
| < 1 万元 | 23.15% |
| 1 万 – 10 万元 | 48.48% |
| 10 万 – 50 万元 | 21.65% |
| 50 万 – 100 万元 | 3.75% |
| 100 万 – 500 万元 | 2.62% |
| > 500 万元 | 0.88% |
| **< 10 万累计** | **71.63%** |

**关键观察:** 71.63% 的散户账户资金量 < 10 万. ssFish panel 里"散户大妈"持工行+五粮液+中国平安, 这至少是几十万的配置, 实际只代表那 ~28% 的中产散户. 真正的"小散户"(< 10 万) 一个都没有出现.

来源: 新浪新闻引用中登公司数据 (cls.cn / sina.com.cn 转载, 2025-08).

### 2.2 持流通市值结构 (五分法, 2024 年末)

| 持有者类别 | 持流通市值占比 | 细分 |
|---|---|---|
| 产业资本 | 34.4% | 大股东、关联方、上市公司互持 |
| 一般个人投资者 | 32.3% | 散户 (剔除大股东、董监高) |
| 专业投资机构 | 19.2% | 公募 7.3% / 私募 4.1% / 保险 1.9% / 社保 / 其他 |
| 政府持股 | 7.6% | 汇金、证金、地方国资委 |
| 个人大股东 | 6.4% | 持股 5%+ 的自然人 |

来源: 华西证券《A 股投资者结构全景图 2024Q3》、新浪财经 2025-06-23.

### 2.3 主要机构具体仓位 (2024 年中)

| 机构 | 持 A 股市值 | 占流通市值 | 行为特征 |
|---|---|---|---|
| 公募基金 | 6.5 万亿 | 8.4% | 仓位 2024Q2 = 82.6%, 主动权益新发同比 -25% |
| 股票型 ETF | 1.80 万亿 | 2.7% | 2024H1 净流入 5778 亿, 沪深 300 ETF 占 73% |
| 北上资金 | 2.02 万亿 | 3.1% | 占外资 99%, 配置从蓝筹转"红利+科技"哑铃 |
| 融资融券 | 1.41 万亿 | 2.2% | 前五行业: 电子/非银/医药/电力设备/计算机 |
| 险资 | (12-14% 仓位) | — | 偏好市值 1000 亿+ 个股 (62%), 银行+非银 80% |

来源: 新浪财经《十五幅图了解 A 股各类资金行为》2024-08.

### 2.4 散户行为细分 (二八定律)

根据多个 2024-2025 调查 (东财财富号、证券时报):

- **30% 散户账户 (~7000 万户) 贡献 60-70% 成交量**, 集中在中小盘+题材, 平均持股周期 < 40 天
- **70% 散户账户 (~1.7 亿户) 是配置型**, 资金集中沪深 300, 低交易频率
- 散户交易频率是机构的 **3-5 倍**
- 量化占总成交量 **20-30%**, 其中外资量化通过北上 5-8%
- 散户 2025H1 年化亏损率 23.6%, 仅 18.9% 盈利
- 100 万+ 账户盈利比例 > 90%

**这意味着 panel 里"散户"应该明确分两类:**
- `retail_active_short_term`: 高频追热点, 高 weight (因为成交量贡献大)
- `retail_passive_holder`: 低频配置, 低 weight (但账户数多)

当前 ashare-v1 把所有散户混在"散户大妈/韭菜/雪球大V/自媒体"4 个原型里, 没有 active vs passive 的轴.

### 2.5 散户信息源生态 (2024-2025)

| 平台 | 用户群体 | 内容风格 |
|---|---|---|
| 雪球 | 中产散户、半专业 | 逻辑分析+数据推演, "讲道理多, 喊口号少" |
| 东方财富股吧 | 普通散户 | 情绪放大镜, 舆情拐点先在这里出现 |
| 抖音/快手财经号 | 新散户、年轻人 | 短视频带货式荐股, 是新韭菜主战场 |
| 小红书炒股笔记 | 女性散户、95+ | 生活化、笔记体 |
| 集思录 | 套利党、可转债玩家 | 高度技术化, 量化偏好 |
| 微信群 / QQ 群 | 中老年散户 | "内部消息", 经常被骗 |
| 大V (微博/微信公众号) | 跟随型散户 | 观点主导, 集中度高 |
| 券商研报 | 跟随型散户 | 卖方口径, 普遍偏多 |

**当前 ashare-v1 只覆盖了央视、雪球、微博/抖音、微信群——抖音+小红书+集思录+券商研报 这四类完全没有 persona 对应**.

---

## 3. 美股投资者结构 (sketch)

数据点 (来源: SIFMA 2024 Equity Market Structure Compendium):

| 维度 | 数字 |
|---|---|
| 散户交易量占比 | 17.9% (近三年稳定在 ~18%, 历史 ~10%) |
| 机构交易量占比 | ~82% |
| ETF 交易量占比 | 19.6% |
| 总 ADV | 12.2 billion shares (2024, +10.2% YoY) |
| 期权 ADV | 47.3M 合约 (+9% YoY) |

Robinhood 用户画像 (来源: businessofapps.com):
- 中位年龄 35 岁 (2025-03), 3/4 用户 ≤ 43 岁
- 主要 Gen Z + Millennial
- 75% 散户交易通过手机 app
- 受 Reddit/WSB 影响显著, 趋势跟随
- 种族: 67% 白人, 12% 亚裔, 12% 拉美裔, 6% 黑人

**美股 panel 的合理 archetype 列表:**
- `robinhood_genz_meme_trader` (~5% 成交量)
- `mass_affluent_etf_holder` (~8%)
- `boomer_blue_chip_holder` (~3%)
- `long_only_mutual_fund_pm` (~15%)
- `index_etf_authorized_participant` (~12%)
- `quant_market_maker` (~25%, Citadel/Virtu/Jane Street 系)
- `equity_l_s_hedge_fund` (~10%)
- `pension_fund_consultant` (~5%)
- `sovereign_wealth_long_only` (~3%)
- `prop_desk_intraday` (~5%)
- `corporate_buyback_program` (~5%)
- `short_seller_activist` (~2%)
- `passive_401k_dca` (~2%)

权重应该按成交量 (而非账户数), 因为 ssFish 测的是"事件后市场反应".

---

## 4. 大宗商品投资者结构 (sketch — WTI 原油)

数据点 (来源: CFTC Disaggregated COT, Oxford Institute for Energy Studies):

| 类别 (CFTC 标准) | 角色 | 仓位特征 |
|---|---|---|
| Producer/Merchant/Processor/User | 产业实体 (套保) | 油气公司、炼厂、航空、终端 |
| Swap Dealer | OTC 衍生品做市商 | 通过 futures 对冲 OTC swap 风险 |
| Managed Money | CTA / 对冲基金 | 趋势跟随者, 仓位最波动 |
| Other Reportables | 大型实体不属于上述 | 实物贸易商、产业基金 |
| Non-reportable | 小散户 | 持仓量 < 报告门槛 |

**关键数字 (2020-2025):**
- Money Managers + Other (long+short) ≈ 35-46% 未平仓合约
- WTI 2024 ADV = 3.4M 合约/日 (反弹自 2018 高点 3.0M)
- 速度: 商业套保慢、speculator 快、CTA 被技术信号触发

**WTI 原油 panel 的合理 archetype 列表:**
- `integrated_oil_major_hedger` (Exxon/Shell desk, 套保产销 mismatch)
- `independent_e_p_hedger` (页岩油商, 套保未来 12 个月产量)
- `refiner_crack_spread_trader` (Valero/Marathon, 交易 crack spread)
- `airline_jet_fuel_hedger` (Delta/United, 套保 jet fuel)
- `commodity_index_fund_long` (DJ-UBS, GSCI 被动多头)
- `cta_trend_following` (Winton, AHL 系统化趋势)
- `macro_hedge_fund_discretionary` (Brevan Howard 系)
- `swap_dealer_market_maker` (Goldman/Morgan Stanley 商品 desk)
- `physical_trader_arbitrageur` (Vitol/Trafigura/Glencore)
- `opec_strategic_reserve` (沙特/俄罗斯供给侧)
- `central_bank_strategic` (中国国储, 美国 SPR)
- `retail_uso_etf_holder` (USO ETF 散户)

权重按 open interest 比例分配.

---

## 5. 跨市场共同模式

把上面三个市场抽出来, 共同点很清晰:

### 5.1 任何市场都有的 5 个轴

1. **资金性质**: 自有资金 / 受托资金 / 套保资金 / 杠杆资金
2. **时间尺度**: 日内 / 周 / 月 / 季 / 年+
3. **决策模式**: 系统化 / 半系统化 / 主观自由 / 跟随
4. **信息源**: 一手 (调研/产业链) / 二手 (研报/媒体) / 三手 (社交/口碑)
5. **市场角色**: 单边方向 / 套利 / 做市 / 套保 / 投机

每一个 persona 都应该在这 5 个轴上有明确坐标, 而不是一段 vibes-based 的 voice prompt.

### 5.2 任何市场都有的 4 类参与者

| 抽象类别 | A 股例子 | 美股例子 | 商品例子 |
|---|---|---|---|
| **Retail (small)** | 散户大妈 / 韭菜 | Robinhood Gen Z | USO ETF holder |
| **Retail (sophisticated) / Pro-am** | 雪球大V / 集思录 | 自营券商客户 | 商品 ETF holder |
| **Institution (long-only)** | 公募 / 险资 / 社保 | mutual fund / pension | index fund / ETF |
| **Institution (active/levered)** | 私募 / 量化 / 北上 | hedge fund / prop / HFT | CTA / swap dealer / physical trader |
| **Strategic** | 产业资本 / 政府队 | 公司回购 / sovereign wealth | OPEC / SPR / state oil |

**当前 ashare-v1 完全漏掉了 Strategic 这一类**. 而 Strategic 在所有市场里都是 single-largest 持有者之一.

### 5.3 真实 panel 必须满足的三个性质

- **Coverage**: 每个 ≥5% 的市场参与者类别都至少有一个 persona
- **Weight fidelity**: persona 的 `weight` 字段对应**该 persona 在事件反应中的市场影响力**, 不是声音大小
- **Source-grounded**: 每个 persona 都引用至少一个公开数据源, 写明 `accessed:` 日期

---

## 6. Schema v2 设计

### 6.1 整体结构

```yaml
schema_version: 2
market: ashare           # 必填: ashare | us-equity | crude-oil-wti | gold | hsi | ...
locale: zh-CN            # voice prompt 语言
last_updated: 2026-04-08

# ── 数据源 ─────────────────────────────────────────
data_sources:
  - id: cdc-2025          # 引用时用 source_id
    name: 中国证券登记结算有限责任公司 投资者数量月报
    org: 中登公司
    url: https://www.chinaclear.cn/...
    accessed: 2026-04-08
    coverage: 2025-12 数据
  - id: huaxi-2024q3
    name: 华西证券 A股投资者结构全景图 2024Q3
    org: 华西证券研究所
    url: https://www.fxbaogao.com/detail/4612072
    accessed: 2026-04-08

# ── 市场结构汇总 (用于 sanity check) ───────────────
structure_summary:
  total_accounts_count: 240000000     # 2.4 亿账户
  retail_pct_by_account_count: 0.9976
  retail_pct_by_holdings: 0.323
  retail_pct_by_volume: 0.65          # 中点估算
  institutional_pct_by_holdings: 0.192
  strategic_pct_by_holdings: 0.484    # 产业资本 + 政府 + 个人大股东

  # cross-check: 所有 personas 的 market_share.by_volume 相加应该接近 1.0
  expected_volume_share_sum: 1.0
  tolerance: 0.05

# ── 默认聚合方式 ────────────────────────────────────
aggregation:
  default_dimension: by_volume   # 或 by_holdings | by_account_count
  # 引擎根据这个字段决定 weighted_mean 用哪一列
  # 如果场景是"事件后短期价格反应", 用 by_volume
  # 如果场景是"长期估值锚", 用 by_holdings

# ── personas ──────────────────────────────────────
personas:
  - id: retail_short_term_chaser
    archetype: retail_active
    sub_archetype: short_term_momentum
    display_name: 短线追涨散户 (资金 5-30 万, 25-40 岁)

    # ── 真实市场份额 (cite!) ──
    market_share:
      by_account_count: 0.21       # 30% 活跃账户的子集
      by_holdings: 0.10
      by_volume: 0.30              # ~30% 散户成交量集中
      citations:
        - source_id: cdc-2025
        - source_id: stcn-202508
          note: "30% 活跃账户贡献 60-70% 成交量"

    # ── 5-axis demographics ──
    capital_range_cny: [50000, 300000]
    age_range: [25, 40]
    time_horizon_days: [1, 14]
    decision_mode: discretionary
    leverage: low                  # 偶尔融资, 主要现金
    role: directional_speculator

    # ── behavior fingerprint ──
    behavior:
      avg_position_pct: 0.85
      annual_turnover: 8.0          # 8x/year
      max_concentration_top1_pct: 0.40
      typical_holding_period_days: 5
      stop_loss_discipline: low
      reaction_speed: fast          # within hours of news

    # ── information environment ──
    information:
      primary_sources: [douyin_finance, xiaohongshu_notes, eastmoney_guba]
      secondary_sources: [wechat_groups, mass_kol]
      ignored: [sell_side_research, annual_reports, conference_calls]
      english_capable: false

    # ── psychological biases (0.0-1.0) ──
    biases:
      momentum_chasing: 0.90
      loss_aversion: 0.70
      herd_following: 0.85
      recency: 0.85
      overconfidence_after_wins: 0.75
      anchoring_to_entry_price: 0.80

    # ── voice prompt (specific to this persona, NOT generic) ──
    voice_prompt: |
      你是一个 28 岁前端程序员, 工资 2 万, 股票账户 12 万 ...
      [voice prompt 必须和上面的 demographics/behavior/biases 一致, 不能凭空发挥]

    # ── model assignment (留给 multi-family swap) ──
    model: null   # 默认 = settings.default_model. Multi-family 时填具体 family.

  - id: industrial_capital_strategic
    archetype: strategic
    sub_archetype: industrial_capital
    display_name: 大股东 / 关联方 / 上市公司互持

    market_share:
      by_holdings: 0.344    # 五分法第一大类
      by_volume: 0.05       # 不频繁交易但单次量大
      citations:
        - source_id: huaxi-2024q3
        - source_id: sina-2025q1

    decision_mode: strategic       # 不是交易型决策
    time_horizon_days: [180, 1825] # 半年到 5 年
    role: strategic_holder
    
    behavior:
      annual_turnover: 0.15        # 偶尔减持/质押/增持
      reaction_speed: slow         # 看季度而非日内
      stop_loss_discipline: na     # 没有止损概念

    information:
      primary_sources: [internal_management, board_meetings, regulator_briefings]
      ignored: [retail_sentiment, technical_analysis]

    biases:
      long_horizon_bias: 0.95
      cash_flow_focus: 0.80
      regulatory_risk_focus: 0.85

    voice_prompt: |
      你代表 [公司名] 控股股东方的资本运作团队.
      你考虑的不是"明天涨跌", 而是"未来 18 个月的减持窗口、
      质押融资成本、和监管对大股东行为的态度". 你说话谨慎,
      多用财务术语, 几乎不评论日内波动.
```

### 6.2 每个 persona 的最小字段集合

**必填:**
- `id` (unique slug)
- `archetype` + `sub_archetype` (供 aggregation 分组)
- `market_share` (至少一个 dimension + citations)
- `decision_mode`
- `time_horizon_days`
- `role`
- `voice_prompt`

**可选 (但建议都填):**
- `capital_range_cny` (或对应市场货币)
- `behavior.{avg_position_pct, annual_turnover, reaction_speed}`
- `information.{primary_sources, ignored}`
- `biases` (dict, 0.0-1.0)
- `model` (留给 multi-family)

### 6.3 关键设计选择

**为什么 `market_share` 是 dict 而不是 single weight:**
不同分析场景需要不同的权重维度. 短期事件反应用 `by_volume`, 长期估值锚用 `by_holdings`. 一个 panel 同时配三种, aggregation 时按需选择. 这把"权重该是多少"从主观判断变成"看场景查表".

**为什么 `data_sources` 是顶层字段而不是每个 persona 内嵌:**
DRY. 多个 personas 引用同一个 source, 通过 `source_id` 关联. 这让"换个数据源"变成改一处而不是十处.

**为什么有 `structure_summary` 字段:**
做 sanity check. CI 应该跑一个测试: `sum(personas[*].market_share.by_volume) ∈ [1-tolerance, 1+tolerance]`. 这防止有人加 persona 时把权重加到 > 1.0 或 < 0.8.

**为什么 `aggregation.default_dimension` 在 pack 里而不是 hardcode:**
不同市场默认权重维度不同. 商品市场 (主要看 open interest) 默认 `by_open_interest`, 股票市场默认 `by_volume`, 长期分析默认 `by_holdings`. Pack 自描述比代码硬编码好.

---

## 7. 迁移路径: ashare-v1 → ashare-v2

### 7.1 不要原地改 v1, 写新的 v2

`personas/ashare-v1.yaml` 保持不动 (作为对比基线), 写一个全新的 `personas/ashare-v2.yaml`. 这样:
- Live Scorecard 可以同时跑 v1 和 v2 比较
- v1 留下来作为"correlated hallucination"问题的历史样本
- 用户可以 `--personas ashare-v1` 或 `--personas ashare-v2` 切换对比

### 7.2 Ashare-v2 推荐 panel 组成 (12-15 personas)

按真实持流通市值五分法 + 散户细分:

| # | Persona | archetype | market_share.by_holdings | market_share.by_volume |
|---|---|---|---|---|
| 1 | 大股东资本运作团队 | strategic | 0.20 | 0.03 |
| 2 | 上市公司互持 / 关联方 | strategic | 0.10 | 0.01 |
| 3 | 国资委 / 地方政府队 | strategic | 0.05 | 0.01 |
| 4 | 汇金 / 证金 / 国家队 | strategic | 0.03 | 0.02 |
| 5 | 个人大股东 (5%+) | strategic | 0.06 | 0.01 |
| 6 | 短线追涨散户 (< 30 万) | retail_active | 0.10 | 0.30 |
| 7 | 中产配置型散户 (30-300 万) | retail_passive | 0.15 | 0.10 |
| 8 | 高净值价投散户 (> 300 万) | retail_pro_am | 0.07 | 0.05 |
| 9 | 公募基金经理 (主动权益) | institution_long_only | 0.07 | 0.10 |
| 10 | 公募 ETF AP / passive | institution_passive | 0.03 | 0.08 |
| 11 | 私募基金经理 (主动多空) | institution_active | 0.04 | 0.10 |
| 12 | 险资 / 社保 / 养老 | institution_long_horizon | 0.04 | 0.03 |
| 13 | 北上资金 / QFII | foreign_long_short | 0.03 | 0.08 |
| 14 | 量化 (国内 + 北上) | quant | 0.02 | 0.25 |
| 15 | 卖方分析师 / 券商研报 | sell_side | 0.00 | 0.00 |

注: #15 不持仓也不直接交易, 但是**信息节点**, 影响 #6/#7/#8 的方向. Aggregation 时单独处理 (类似"channel" 而非"trader").

`by_holdings` 合计 ≈ 0.99 ✓
`by_volume` 合计 ≈ 1.27 (over 100% 因为换手率不同, 高换手类别成交量贡献被放大). 这正常, 不是 bug.

### 7.3 Voice prompt 重写原则

当前 ashare-v1 的 voice prompt 是 vibes-based, 写得有戏剧感但和 demographics 数据脱节. 新 prompt 应该:

1. **第一句话明确身份的 5-axis 坐标**: 资金量 / 时间尺度 / 决策模式 / 信息源 / 市场角色
2. **第二段提供具体生活细节** (这是 LLM 真正用来产生 voice 的部分)
3. **第三段约束反应模式**: "你看到突发新闻的第一反应是 X, 第二天才会 Y"
4. **不能引用真实公司名** (合规风险)
5. **语言风格匹配信息源**: 抖音用户的说话方式 ≠ 雪球用户

---

## 8. 引擎需要改的地方

### 8.1 必改 (~200 LOC)

| 文件 | 改动 | 原因 |
|---|---|---|
| `src/ssfish/persona.py` | 加 `MarketShare`, `BehaviorProfile`, `InformationProfile` dataclasses; loader 兼容 v1 + v2 | schema v2 |
| `src/ssfish/persona.py` | `_validate_persona_dict` 增强: 检查 market_share 引用的 source_id 存在 | 数据完整性 |
| `src/ssfish/persona.py` | 新增 `load_persona_pack(path)` 返回 `PersonaPack` (带 data_sources, structure_summary) | 包级元数据 |
| `src/ssfish/aggregation.py` | `aggregate(reactions, personas, dimension="by_volume")` — 按指定 dimension 加权 | 多维权重 |
| `src/ssfish/scorecard.py` | 新增列 `persona_pack_id`, `persona_pack_version`, `aggregation_dimension` | 可重现性 |
| `scripts/run_one.py` | 新增 `--market` flag, 默认根据 `--personas` 路径推断 | CLI |

### 8.2 选改 (~100 LOC)

| 文件 | 改动 |
|---|---|
| `src/ssfish/persona.py` | `validate_pack(pack)`: 跑 sanity checks (market_share 合计 within tolerance) |
| `tests/test_persona.py` | 加 schema v2 round-trip 测试, sanity check 测试 |
| `personas/SCHEMA.md` | 新建: 完整字段文档 |
| `personas/_template.yaml` | 新建: 模板 + 注释 |
| `personas/RESEARCH_ASHARE.md` | 新建: 这个 spec 里的数据汇总, 留作引用 |

### 8.3 不需要改

- `simulation.py` (orchestration), `llm_client.py`, `output_filter.py`, `event.py`, `report.py`, `api/*` 全部不动. Persona 是引擎的输入, 不是引擎本身.

---

## 9. Phase B — 沙箱执行模式 (Sandbox Execution Mode)

> **这一节描述把 ssFish 从"sentiment 启发式"升级成"agent-based market model"的架构. 是 Persona Pack v2 的下一层 — 没有它, panel 真实但输出仍然是启发式; 有了它, ssFish 第一次产生可证伪的价格轨迹.**

### 9.1 概念

当前 ssFish:

```
event → personas → 每人输出 sentiment ∈ [-1, +1]
              → 加权平均 = sentiment_mean
              → 启发式公式 implied_move = f(sentiment_mean)
              → 输出 "-6.3% to -4.1%" (假精度, 不可证伪)
```

沙箱版 ssFish:

```
event → personas → 每个 persona 类别有 N 个 stochastic agent 实例
                   每个 agent 有: 初始资金 + 初始持仓 + 风险约束
              → LLM 调用 (1 次/类别/轮): 输出 action distribution
              → sandbox 在 distribution 下采样 N 个 agent 的具体动作
              → 聚合所有 agent 的动作 = 净订单流 (net flow in CNY)
              → square-root price impact: ΔP/P = λ × sign(flow) × sqrt(|flow|/ADV)
              → 新价格反馈到 R+1 轮, agents 看到新价格再决策
              → 多轮迭代后输出价格轨迹 [P0, P1, P2, ..., Pn]
              → 也输出每类 persona 的盈亏 + 仓位变化
```

**输出从"sentiment 描述"变成"价格 + 成交量 + 持仓变动 + 盈亏"**. 全部可以和真实市场 T+1, T+5 数据对账.

### 9.2 Stochastic instance spawning per archetype

**核心 trick**: LLM 调用次数和 agent 数量解耦.

| | 当前架构 | 朴素 ABM | 沙箱 ABM (我们要的) |
|---|---|---|---|
| Persona 类别数 | 10 | — | 12-15 |
| Agent 实例数 | 10 | 100,000 (真实账户数级) | 10,000 (per archetype × 类别数 ≈ 总量真实级) |
| LLM 调用/轮 | 10 | 100,000 ❌ 完全不可行 | 12-15 (一次输出整个类别的动作分布) |
| 1 sim 成本 | $0.005 | $50 | $0.005-0.01 |

**LLM 调用 1 次, 给类别**:

```python
prompt = f"""
你代表一个 persona 类别: retail_short_term_chaser
该类别在 A 股的特征:
  - 资金量: lognormal(median=12万, sigma=0.8)
  - 平均仓位: 85%
  - 平均持有期: 5 天
  - 信息源: 抖音/小红书/股吧
  - 当前持仓: 60% 持有题材股 (含本事件标的), 40% 观望
  - 当前账户盈亏: 平均 -8% (年初至今)

事件: BYD 2024 Q1 营收+18% YoY (beat), 毛利率 -2.3pp (miss)
当前价格变化: 相比昨收 -3%
盘口: 卖一压力大, 买一稀疏

输出 JSON: {{
  "action_distribution": {{
    "panic_sell_50pct": 0-1,    # 卖出当前持仓的 50%
    "stop_loss_full_exit": 0-1, # 触发止损全部清仓
    "hold": 0-1,                # 不动
    "average_down_10pct": 0-1,  # 用 10% 现金加仓
    "fomo_buy_30pct": 0-1       # 用 30% 现金追涨
  }},
  "rationale": "...",
  "confidence": 0-1
}}
"""

# LLM 返回:
{
  "action_distribution": {
    "panic_sell_50pct": 0.30,
    "stop_loss_full_exit": 0.10,
    "hold": 0.40,
    "average_down_10pct": 0.15,
    "fomo_buy_30pct": 0.05
  },
  "rationale": "Q1 beat 但 margin miss 引发分歧, 加上 -3% 已经触发部分恐慌",
  "confidence": 0.7
}
```

**Sandbox 后处理 (纯 Python, 0 LLM 调用)**:

```python
# 这一类 spawn 10000 个 agent
agents = []
for _ in range(10000):
    capital = lognormal(median=120000, sigma=0.8)
    holdings = sample_initial_holdings(class="retail_short_term_chaser",
                                        target_position=0.60)
    action = sample_from_distribution(distribution)
    agents.append(Agent(capital, holdings, action))

# 聚合订单流
net_flow = 0
for agent in agents:
    if agent.action == "panic_sell_50pct":
        net_flow -= agent.holdings_in_target * agent.target_price * 0.5
    elif agent.action == "stop_loss_full_exit":
        net_flow -= agent.holdings_in_target * agent.target_price
    elif agent.action == "average_down_10pct":
        net_flow += agent.cash * 0.10
    elif agent.action == "fomo_buy_30pct":
        net_flow += agent.cash * 0.30
    # hold: 不变

# net_flow 是这一类 persona 对该事件标的的净订单流 (CNY)
class_flows["retail_short_term_chaser"] = net_flow
```

对所有 12-15 个类别同样处理, 得到 `class_flows: dict[str, float]`. 总净流 = `sum(class_flows.values())`.

### 9.3 Square-root price impact (Kyle model)

学术上经过 30 年验证 (Kyle 1985, Almgren-Chriss 2001, Bouchaud 2010):

```
ΔP/P = λ × sign(net_flow) × sqrt(|net_flow| / ADV)
```

| 符号 | 含义 | 来源 |
|---|---|---|
| `net_flow` | 单期净订单流, CNY (正=买, 负=卖) | 沙箱聚合 |
| `ADV` | 标的真实日均成交额, CNY | Tushare/Wind, 事件前 30 日均值 |
| `λ` | 市场冲击系数, 无量纲 | 校准 (见 9.9) |

**经验值**: A 股 λ ≈ 0.4-0.6, 美股 λ ≈ 0.2-0.4, 商品 λ ≈ 0.3-0.5. 这是经过几千篇论文校准的范围.

**例子**: BYD ADV ≈ ¥80 亿/日. 沙箱推演出某轮净流 -¥3 亿:

```
ΔP/P = 0.5 × (-1) × sqrt(3亿 / 80亿)
     = -0.5 × sqrt(0.0375)
     = -0.5 × 0.194
     = -0.097
     ≈ -9.7% (单轮单类别)
```

**实际多类别合并 + 多轮反馈后**, 单轮影响会被买盘抵消一部分. 真实输出可能是 R0 -2%, R1 -3.2%, R2 -4.1%, 收敛在 -5% 到 -8% 区间.

**为什么是 sqrt 而不是 linear**: 实证. 大单的冲击是次线性的——市场会吸收 (做市商提供流动性, 反向交易者出现). Linear 会高估极端情况, sqrt 拟合更好.

### 9.4 多轮反馈动力学

这是沙箱模式相对当前架构最大的提升点之一. 当前 ssFish 的"多轮"是假的——上一轮的"价格"只是文字描述, 没影响下一轮的决策. 沙箱的多轮是真的:

```
R0 (initial):
  P_0 = current_market_price
  agents 看到 event + P_0
  → class_flows_R0
  → ΔP_0 = λ × sqrt(|net_flow_R0|/ADV)
  → P_1 = P_0 × (1 + ΔP_0)

R1:
  agents 看到 event + P_1 (变化了的价格) + 自己上一轮的盈亏
  → class_flows_R1 (不同的, 因为价格信号变了)
  → ΔP_1 = ...
  → P_2 = P_1 × (1 + ΔP_1)

R2:
  agents 看到 P_2 + 自己累计盈亏 + 是否触发 margin call
  ...

R5 (terminal):
  输出 [P_0, P_1, P_2, P_3, P_4, P_5] 作为价格轨迹
  也输出每个类别的最终持仓 + 盈亏
```

**这才是真正的多轮 simulation**. 它能涌现:
- **羊群崩溃**: 价格 -3% → 散户恐慌 → 净流加速 → -5% → 更多人恐慌 → -8% (自反性)
- **抄底反弹**: 价格 -8% → 公募觉得便宜 → 大单买入 → +2% → 散户 fomo → +4% (反向反馈)
- **margin call cascade**: 融资盘价格触发 105% 平仓线 → 强平卖出 → 价格再跌 → 更多融资盘平仓 (Black Monday 1987 的机制)
- **流动性枯竭**: 卖盘集中 / 没有买盘 → 交易暂停 / 价格断层

这些都是真实市场的特征. 当前的 sentiment_mean 永远捕捉不到.

### 9.5 Persona schema 增量 (在 §6 基础上加)

```yaml
personas:
  - id: retail_short_term_chaser
    # ... (§6 已有的所有字段) ...

    # ── Phase B 新增字段 ──
    sandbox:
      # 实例化参数
      instance_count: 10000          # 这一类在 sim 里 spawn 多少个 agent
      capital_distribution:
        type: lognormal
        median_cny: 120000
        sigma: 0.8
        floor_cny: 10000             # 截断
        ceiling_cny: 1000000

      # 初始持仓 (相对于事件标的)
      initial_position_distribution:
        type: bernoulli              # 60% 概率持有, 40% 不持
        prob_holding: 0.60
        position_size_pct_when_holding:
          type: uniform
          min: 0.05                  # 持仓 5-30%
          max: 0.30
        avg_entry_price_offset:      # 相对当前价格的入场成本
          type: normal
          mean: -0.05                # 平均成本比现价低 5%
          sigma: 0.15                # ±15% 散布

      # 风险约束
      risk:
        max_position_pct: 0.95       # 最高仓位
        margin_account_pct: 0.10     # 10% 的人有融资账户
        leverage_max: 2.0            # 融资倍数上限
        stop_loss_threshold: -0.15   # 散户的止损线 (实际很多人没有)
        stop_loss_discipline: 0.30   # 触发后实际执行的概率

      # 可选动作集合 (LLM 输出 distribution 必须在这里面)
      action_space:
        - panic_sell_50pct
        - stop_loss_full_exit
        - hold
        - average_down_10pct
        - fomo_buy_30pct

      # 反应速度 (决定 agent 在哪些 R 轮活跃)
      reaction_lag_rounds:
        type: discrete
        values: [0, 0, 0, 1, 1, 2]   # 50% R0 立即反应, 33% R1, 17% R2
```

机构类 persona 的 action_space 不同:

```yaml
  - id: institution_long_only_pm
    sandbox:
      instance_count: 30             # 公募基金经理人数级
      capital_distribution:
        type: lognormal
        median_cny: 5000000000       # 5亿管理规模
        sigma: 1.5                   # 跨度大 (从 1亿到 500亿)

      action_space:
        - hold
        - rebalance_underweight_10pct  # 减少 10% 配置
        - rebalance_overweight_10pct
        - swap_to_sector_peer
        - exit_full_position
        - initiate_new_position_5pct
```

### 9.6 Engine changes for sandbox

| 文件 | 改动 | LOC |
|---|---|---|
| `src/ssfish/sandbox.py` | **新建**. `Agent`, `OrderBook`, `MarketImpact` 类. 实例化 + 聚合 + 价格演化 | ~400 |
| `src/ssfish/persona.py` | 加 `SandboxConfig` dataclass, parser 兼容 sandbox 字段 | ~100 |
| `src/ssfish/llm_client.py` | 加 `chat_action_distribution()` 方法, JSON schema 强制 action_space 内 | ~50 |
| `src/ssfish/simulation.py` | 加 `run_sandbox_simulation()` (并存于 `run_simulation`), 调用 sandbox.py | ~150 |
| `src/ssfish/aggregation.py` | 加 `aggregate_sandbox_result()`, 输出价格轨迹 + 类别盈亏 | ~150 |
| `src/ssfish/report.py` | 报告新增"价格轨迹"和"类别盈亏"段落 | ~100 |
| `src/ssfish/scorecard.py` | schema v3: 加 `price_trajectory_json`, `class_pnl_json`, `lambda_used`, `adv_used` 字段 | ~50 |
| `src/ssfish/event.py` | `Event` 加 `adv_cny` 和 `current_price` 字段 (沙箱必需) | ~30 |
| `tests/test_sandbox.py` | **新建**. 单元测试 + 1 个 end-to-end 测试 | ~300 |

**总计**: ~1300 LOC + 1 个新模块. 比 Phase A 大约一倍.

**关键设计选择**:
- 新建 `sandbox.py` 而不是改 `simulation.py`: 沙箱模式和 sentiment 模式可以并存. CLI 用 `--mode sentiment | sandbox` 切换. v1 的 yaml 仍然能跑 sentiment 模式.
- `Agent` 是纯数据类, 不调 LLM. LLM 只负责类别级 action distribution.
- `OrderBook` 只是 net flow 聚合器, 不做撮合 (撮合是更进一步的复杂度, Phase C 再说).
- `MarketImpact` 是 stateless 函数, 接收 `(net_flow, adv, lambda)` 返回 `delta_p_pct`.

### 9.7 Cost analysis

| 模式 | LLM 调用/sim | 1 sim 成本 (gpt-4o-mini) | 1 sim 时长 |
|---|---|---|---|
| 当前 sentiment (5 轮 × 10 类别 batched) | 5 (batched) | $0.005 | ~30s |
| Phase A persona-pack v2 (sentiment 模式, 5 轮 × 12-15 类别 batched) | 5 (batched) | $0.005-0.008 | ~35s |
| Phase B sandbox 模式 (5 轮 × 12-15 类别, 1 调用/类别/轮) | 60-75 | $0.030-0.060 | ~60-90s |
| Phase B sandbox + multi-family (3 family × 同上) | 180-225 | $0.10-0.20 | ~120-180s |

**沙箱模式比当前贵 6-12 倍**, 但仍然在"日常使用 ok"范围 ($0.05/sim ≈ 一杯咖啡能跑 100 次). 如果担心成本, 可以:
- 减少轮数 (5 → 3)
- 减少类别 (15 → 10)
- 用更便宜的模型做 stochastic instance 类别 (haiku-4-5 < gpt-4o-mini)
- 缓存类似事件的 action distribution

### 9.8 校准 λ 系数

λ 是市场冲击系数, 一个市场一个值, 一次性校准.

**校准方法**: 拿 30 个历史事件 (业绩、并购、政策、暴雷), 每个事件知道 (a) 事件前 ADV, (b) 事件后真实 ΔP/P, (c) 事件后真实 net flow (从 Tick 数据 / Level 2 / 龙虎榜推算). 反解 λ:

```
λ_i = ΔP_i / (sign(flow_i) × sqrt(|flow_i| / ADV_i))

λ_market = median(λ_1, ..., λ_30)
```

数据源: Tushare Pro (sgt.dat 历史 Level 2), 或者上交所/深交所的 Tick 数据 API.

**这是一次性研究项目**, 不是 ssFish 的核心. 第一版可以直接用文献值 (A 股 0.5, 美股 0.3) , 之后慢慢校准.

### 9.9 这套架构能让 ssFish 真正可证伪

当前 sentiment_mean = -0.665 没法和任何真实数据对账. 沙箱版输出:

```json
{
  "price_trajectory": [
    {"t": "2024-04-29 09:30", "price": 218.50, "delta": 0.000},
    {"t": "2024-04-29 10:00", "price": 213.80, "delta": -0.022},
    {"t": "2024-04-29 11:00", "price": 209.10, "delta": -0.043},
    {"t": "2024-04-29 14:00", "price": 207.20, "delta": -0.052},
    {"t": "2024-04-29 15:00", "price": 211.30, "delta": -0.033}
  ],
  "predicted_close_t1": 211.30,
  "predicted_low_intraday": 206.80,
  "predicted_volume_cny": 9_500_000_000,
  "class_pnl": {
    "retail_short_term_chaser": -8_200_000,
    "retail_passive_holder": -1_300_000,
    "institution_long_only_pm": -3_500_000,
    "quant": +12_400_000,
    "industrial_capital_strategic": 0  // 没交易
  }
}
```

这个 JSON 的**每一个字段**都可以和真实数据 1:1 对账:
- `predicted_close_t1` vs 真实 T+1 收盘
- `predicted_low_intraday` vs 真实日内低点
- `predicted_volume_cny` vs 真实成交额
- `class_pnl["quant"]` vs 真实量化机构当日盈亏 (需要数据源, 但概念上可对账)

**Scorecard entry 现在变成: ssFish 预测 X, 真实 Y, 误差 |X - Y| / Y**. 这是真正的 Live Scorecard, 不是当前的"sentiment 描述 + 启发式区间".

---

## 10. 工作量估算

### Phase A — Persona Pack v2 (输入侧, sentiment 模式仍可用)

| 阶段 | 工作 | 时间 |
|---|---|---|
| A1 | Schema v2 + persona.py loader + tests | 3-4 hr |
| A2 | 写 ashare-v2.yaml (15 personas, 6-8 个数据源) | 4-6 hr |
| A3 | 引擎 aggregation 改造 (多维 weight) + scorecard schema migration | 2 hr |
| A4 | 跑 BYD smoke test 对比 v1 vs v2, 写到 scorecard entry #002 | 1 hr |
| A5 (可选) | 写 us-equity-v1.yaml 和 crude-oil-wti-v1.yaml 验证 schema 跨市场 | 4-6 hr |
| A6 (可选) | `personas/SCHEMA.md` + `_template.yaml` + RESEARCH 文档 | 2 hr |

**Phase A 核心 (A1-A4): ~10-13 hr** ≈ 一个完整工作日.
**Phase A 完整 (A1-A6): ~16-21 hr** ≈ 一个周末.

### Phase B — Sandbox Execution Mode (引擎侧, agent-based market model)

| 阶段 | 工作 | 时间 |
|---|---|---|
| B1 | 沙箱基础: `sandbox.py` (Agent / OrderBook / MarketImpact 类) + 单元测试 | 4-5 hr |
| B2 | `chat_action_distribution()` LLM 调用接口 + JSON schema 强约束 | 2 hr |
| B3 | `simulation.run_sandbox_simulation()` 多轮反馈循环 + λ-based 价格更新 | 3-4 hr |
| B4 | Persona schema 增加 sandbox 字段 (capital/holdings/action_space 分布), 重写 ashare-v2 sandbox 段落 | 3-4 hr |
| B5 | aggregation/report/scorecard 改造: 输出价格轨迹 + 类别盈亏 | 3 hr |
| B6 | 端到端测试: BYD smoke test sandbox 模式, 对比 sentiment 模式, 写到 scorecard entry #003 | 2 hr |
| B7 (可选) | λ 系数校准: 拿 30 个历史事件反解 λ_ashare | 4-6 hr |
| B8 (可选) | Event 增加 ADV/current_price 字段 + Tushare 接入 | 3-4 hr |

**Phase B 核心 (B1-B6): ~17-20 hr** ≈ 两个工作日.
**Phase B 完整 (B1-B8): ~24-30 hr** ≈ 一周晚上.

### 总计

| 路径 | 时间 | 输出 |
|---|---|---|
| 仅 Phase A 核心 | 10-13 hr | 真实 panel + sentiment 模式 (仍是启发式) |
| Phase A 核心 + Phase B 核心 | 27-33 hr | 真实 panel + 沙箱模式 (可证伪价格轨迹) |
| 完整 (A 全 + B 全) | 40-51 hr | 跨市场 + 校准 + 数据接入, 接近"v2 产品级" |

**强依赖关系**: B 必须在 A 之后 (沙箱依赖真实 capital/holdings 字段, 这些只在 v2 schema 里). 但 A 不强制要求 B (A 单独 ship 也是改进).

---

## 11. Open questions (需要 Ziyang 决策)

### Q1: 是否立即立项做 ashare-v2

- **A**: 是, 完整做 Phase 1-6, 按上述计划
- **B**: 是, 但只做 Phase 1-4 (核心 ashare-v2), 跨市场 sketch 推迟
- **C**: 否, 推迟到 ssFish 有第一个外部用户之后再说

我的推荐: **B**. 跨市场 sketch 是验证 schema 的好方法, 但不是非做不可. 先证明 ashare-v2 能产生比 v1 显著不同的 BYD smoke test 结果, 再扩展.

### Q2: ashare-v2 panel 大小

- **A**: 12-15 personas (上面表格的版本) — coverage 够用, 单次 sim 成本可控 (~$0.005-0.01)
- **B**: 25-30 personas (每个 archetype 2-3 个变体) — 更细致, 但单次 sim 成本 ~$0.02-0.04
- **C**: 50+ personas — 接近真实市场参与者多样性, 但成本 ~$0.10/sim 且需要并行调度

推荐: **A 起步**, 跑通后看效果决定是否扩到 B.

### Q3: Strategic 类 (产业资本/政府/个人大股东) 的 voice prompt 怎么写

这是最难的部分. 真实大股东不会公开发表事件反应——他们的"反应"是行为 (减持/增持/质押/对赌). LLM 模拟"大股东 PR 团队的内部 memo"是接近的, 但仍然是间接的.

- **A**: voice_prompt 写"内部资本运作 memo 风格", 标记 `output_type: internal_memo`
- **B**: 直接让 strategic personas 输出"行为意图" (减持窗口 / 质押额度 / 增持信号) 而不是 "sentiment"
- **C**: Strategic personas 不输出 sentiment, 只输出"对该事件的战略分类" (利好减持窗口/不影响/触发增持考虑)

推荐: **C**. Strategic 持有者本来就不应该被聚合到 sentiment_mean 里. 他们应该作为**单独的 strategic_signal 字段**附在报告里, 而不是参与 sentiment 加权.

这意味着 aggregation.py 需要改: sentiment 只对 retail/institutional 加权, strategic 单独输出.

### Q4: 数据源更新频率

- **A**: 数据源每年手动更新一次 (年报季)
- **B**: 数据源半年更新一次 (年报+中报)
- **C**: 抓取 RSS / API 自动更新 (复杂, 需要数据管道)

推荐: **A**. ssFish 不是数据公司, 投资者结构数据本来就年度更新. 在 yaml 里写 `data_sources[*].accessed` 和 `last_updated`, 用户跑 sim 时如果数据 > 12 个月旧给个 warning.

### Q5: 是否引入"event-conditional weight"

不同事件类型, 不同 persona 影响力不同. 例如:
- 大股东减持事件: strategic personas 权重应该高得多
- 美联储加息: 北上资金/外资权重应该高得多
- 业绩暴雷: 量化/做空相关权重应该高得多

Schema v2 可以加 `personas[*].event_modifiers: {event_type → weight_multiplier}`. 但这会显著增加复杂度.

推荐: **Phase A 不做, 直接进 Phase B 之后再说**. 沙箱模式下 event-conditional weight 自然 emerge — 不同事件触发不同 persona 的不同 action distribution, 不需要硬编码权重.

### Q6: Phase A vs Phase B 的执行顺序

**这是这次决策最重要的问题.** 三个选项:

- **A**: Phase A 全做完 (~13 hr) → 跑一次 BYD 对比 v1/v2 → 如果效果显著, 再开 Phase B (~20 hr)
- **B**: Phase A 只做 schema 部分 (A1, ~4 hr) + 直接进 Phase B (~20 hr), 跳过 sentiment 模式的完整实现
- **C**: Phase A + Phase B 一次性合并做完 (~33 hr), 一个长周末

我的推荐: **B**. 理由:
1. Sentiment 模式是当前架构的延续, 改进有限. 即使 Phase A 跑完, 输出仍然是 sentiment_mean 的小数点, 不是可证伪的价格轨迹. 投入 13 小时只换来 panel 真实, 不值.
2. Phase B (沙箱) 才是质变. 它把 ssFish 从"启发式工具"变成"agent-based market model", 这是真正能拉开和任何竞品的距离.
3. Phase A1 (schema v2 loader) 是 Phase B 的前提, 必须做. 但 Phase A2-A4 (写 ashare-v2.yaml + sentiment 模式 aggregation) 可以推迟——直接在 Phase B 里写包含 sandbox 字段的 ashare-v2.yaml, 一次到位.
4. 跳过 Phase A 的对比测试 (A4) 意味着我们没有 v1 vs v2 sentiment 模式的对照数据. 但 Phase B 的对照可以是更有意义的: sandbox-v2 vs 真实 BYD T+1 价格, 直接对账.

如果选 B, 总工作量从 33 hr 降到 ~24 hr, 而且产出更有意义.

如果担心 24 hr 太重, 备选方案: **B'** = Phase A1 (4hr) + Phase B 简化版 (跳过 B7, B8, 用文献 λ 值 + 手动填 ADV) ≈ 16-18 hr ≈ 两个晚上.

---

## 12. 如果立项, 下一步

如果用户选 Q1=A 或 Q1=B, 推荐用 `/plan-eng-review` 把这个 spec 变成正式的工程实施计划:

1. 读这个 spec
2. 跑 plan-eng-review 的 dual voices (Codex + Claude subagent) 评估 schema 设计
3. 输出一个分阶段实施 plan, 标记 critical path
4. 然后开 implementation 会话执行

如果选 Q1=C (推迟), 这个 spec 留在 docs/ 作为未来引用, 当前 session 直接结束.

---

## 引用源汇总

### A 股
- 中国证券登记结算有限责任公司, 投资者数量月报 2025-12
- 华西证券研究所《A 股投资者结构全景图 2024Q3》, [fxbaogao.com/detail/4612072](https://www.fxbaogao.com/detail/4612072)
- 新浪财经《从投资者结构变化看资本市场投资端改革 — 2024 年投资者结构全景分析》2025-06-23, [finance.sina.com.cn](https://finance.sina.com.cn/jjxw/2025-06-23/doc-infcanva9299037.shtml)
- 新浪财经《十五幅图了解 A 股各类资金行为和偏好》2024-08-08, [finance.sina.com.cn](https://finance.sina.com.cn/roll/2024-08-08/doc-inchwxxk1914485.shtml)
- 证券时报《深交所摸底 A 股个人投资者状况》2024-03-20, [stcn.com](https://www.stcn.com/article/detail/1152319.html)
- 财联社《A 股投资者首超 2.4 亿, 个人投资者超 99%》, [cls.cn/detail/2086830](https://www.cls.cn/detail/2086830)
- 东方财富财富号《散户在 A 股市场中的人数和资金量占比》2025-08, [caifuhao.eastmoney.com](https://caifuhao.eastmoney.com/news/20250824214141911949940)
- 深圳证券交易所投资者教育《个人投资者状况调查报告》系列, [investor.szse.cn](https://investor.szse.cn/institute/bookshelf/report/index.html)
- 中国证券业协会, [amac.org.cn](https://www.amac.org.cn/index/tzzfw/202405/t20240517_25560.html)
- 毕马威《2024 年度中国证券业调查报告》, [kpmg.com/cn](https://assets.kpmg.com/content/dam/kpmg/cn/pdf/zh/2024/09/mainland-china-securities-survey-2024.pdf)
- 中国证券投资者保护基金, [sipf.com.cn](http://www.sipf.com.cn/)

### 美股
- SIFMA Insights: Equity Market Structure Compendium 2024, [sifma.org](https://www.sifma.org/research/insights/insights-equity-market-structure-compendium)
- SIFMA 2024 Capital Markets Fact Book, [sifma.org](https://www.sifma.org/wp-content/uploads/2023/07/2024-SIFMA-Capital-Markets-Factbook.pdf)
- Business of Apps, Robinhood Statistics 2026, [businessofapps.com](https://www.businessofapps.com/data/robinhood-statistics/)
- ScienceDirect, "Robinhood, Reddit, and the news: The impact of traditional and social media on retail investor trading"
- World Economic Forum, 2024 Global Retail Investor Outlook, [weforum.org](https://www.weforum.org/publications/global-retail-investor-outlook-2025/key-insights-global-retail-investor-outlook-2025/)

### 商品
- CFTC Commitments of Traders, [cftc.gov](https://www.cftc.gov/MarketReports/CommitmentsofTraders/index.htm)
- CFTC Disaggregated COT — Petroleum (Futures Only), [cftc.gov/dea/futures/petroleum_lf.htm](https://www.cftc.gov/dea/futures/petroleum_lf.htm)
- Oxford Institute for Energy Studies, "The State of Speculative Positions in Oil Derivatives" 2025, [oxfordenergy.org](https://www.oxfordenergy.org/wpcms/wp-content/uploads/2025/12/Comment-The-State-of-%E2%80%98Speculative-positions-in-Oil-Derivatives.pdf)
- CFTC, "The Role of Speculators in the Crude Oil Futures Market", [cftc.gov](https://www.cftc.gov/sites/default/files/idc/groups/public/@swaps/documents/file/plstudy_19_cftc.pdf)

---

## 附录 A: 当前 ashare-v1.yaml 与 v2 的偏差

| 维度 | ashare-v1 | 真实 A 股 | ashare-v2 (拟) |
|---|---|---|---|
| Persona 数量 | 10 | — | 12-15 |
| Strategic 类覆盖 | 0% | 48.4% 持股 | 5 个 personas |
| Retail 类细分 | 4 personas (混乱) | 32.3% 持股 / 60-70% 成交量 | 3 personas (active/passive/pro-am) |
| 数据源引用 | 0 | — | 6-10 个公开数据源 |
| 权重维度 | single `weight: float` | volume/holdings/count 三维 | `market_share` dict |
| 引用 by_volume vs by_holdings 的能力 | 无 | — | aggregation.default_dimension 配置 |
| Voice prompt 数据一致性 | 凭感觉 | — | 强制和 5-axis 坐标对齐 |
