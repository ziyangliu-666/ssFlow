# ssFlow 历史回测诊断报告 v2 — 月度 6 轮复盘

**日期**: 2026-04-11
**对照**: `analysis/backtest_diagnostic_v1.md` (2026-04-10 · 日度 4-6 轮)
**方法**: 5 个历史事件, 每个事件用 `monthly-6m` 调度跑 6 轮 (= 6 个月),
与 Sina K-line 真实月末价对比
**成本**: 5 events × 6 rounds × gpt-4o-mini ≈ 1h 40min / ~$1.7 (其中 ~55 min
是 OASIS TwHIN rec model, 单轮 ~65s, 这不是 LLM 推理成本而是 rec 模型重建成本)

**目的**: 在 v1 诊断后上游已经做了多轮修复 (bearish persona, 取消
conviction locking, limit_board / T1, fill_engine, price anchor context),
验证系统是否还会产生单边行情, 找出还没解决的结构性偏差

---

## 0. 核心结论 · 先看这个

系统已经**从 v1 的 "永远看多" 过度旋转到 v2 的 "温和反向"**.

| 测试场景 | v1 表现 | v2 表现 |
|---|---|---|
| 强利空 (-46.5% 真实) | +18% (方向错) | 先跌 -17% 再涨回 +4% (**先对后错**) |
| 软利空 (-11.7% 真实) | +30% (方向错) | -10% (**方向+幅度都对**) |
| 波动侧向 (+8.5% 真实) | — | +0.5% (**方向对但无波动**) |
| 强政策利好 (+33.8% 真实) | +32% (巧合对) | -12% (**方向反**) |
| 超强政策利好 (+94.2% 真实) | — | -0.02% (**完全抹平**) |

**方向准确率**: v1 2/3 ≈ 67% (小样本, 有一次是 Kyle 上界撞中), v2 **2/5 = 40%**.

**但 v2 不是简单倒退**: 软利空 + 侧向事件 (五粮液 / 贝泰妮) 这类 "信号中等, 不偏不倚" 的场景, v2 首次达到了**方向 + 量级双匹配**. 问题集中在**两极**:
- 强利空被 "mean reversion" 机制过早拉回
- 强利好被 "价格上涨 → 风险" 的 price anchor 和 "月度重估" narrative 持续压制

## 1. v1 P0 修复的结构性审计

v1 提到的 P0 缺陷, 2026-04-11 实际状态:

| # | v1 缺陷 | v1 症状 | 当前代码状态 | 是否解决 |
|---|---|---|---|---|
| 1.1 | 全部 14 trader persona long-only | 利空事件下 12买/43持/1卖 | 16 traders 全部有 sell, 2 个 margin-short (event_driven_short_fund, macro_hedge_risk_off) | **✓ 结构已修** |
| 1.2 | Echo chamber — 77% 看多帖 | 所有 agent 读同一 feed | 新增 independent_bearish_research (Muddy Waters 风格, trigger_prob 0.30), cicc_deng 显式偏谨慎 (sellside_research_contrarian) | **△ 过度: 利好场景也保持看空音量** |
| 1.3 | Conviction locking "保持方向一致" | R1 后永不反向 | `oasis_persona_adapter.update_conviction_context` 改为 price-anchored 风险提示: "价格越高回调风险越大", "请基于当前价位独立判断" | **△ 过度: 单向锚变成双向 mean reversion** |
| 1.4 | LLM 推理-行为脱节 | 说卖不调工具 | `_patch_perform_action` 处理全部 tool call, trader 16/16 能成功调用 | **✓ 结构已修** |
| 2.1 | 无估值锚点 | 不知涨多了 | `compact_kline_summary` 有 30 日波动率/均线, 但**仍无 PE/PB/目标价/DCF 锚** | **△ 技术锚有, 基本面锚缺** |
| 2.2 | 无新信息注入 | R1+ 信息真空 | `ExternalEventSchedule` 支持注入, 但 monthly-6m 默认不用 | **△ 基础设施有, 未接入** |
| 2.4 | sandbox_templates 硬编码看多 | 每轮注 bullish 公告 | 已修为 "库存积压 → 去库存压力" (commit `b6f7e19`) | **✓ 已修** |
| Kyle | 压缩上界 +32% | 超级利好过不去 | MAX_DELTA_PCT_PER_ROUND = 0.10, FLOW_KNEE = 0.08 + dynamic_knee 随累计幅度下滑 | **✗ 仍卡, 且对月度尤其严重** |

## 2. 真实 vs 模拟 — 完整数据

### 真实 6 月走势 (Sina K-line)

| # | 事件 | Ticker | 锚点 | M+0 | M+1 | M+2 | M+3 | M+4 | M+5 | M+6 | 6M累计 | 区间 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 药明康德 BIOSECURE | 603259 | 73.29 | — | 54.20 | 54.48 | 46.18 | 43.67 | 42.11 | 39.19 | **-46.5%** | [-50, +2] |
| 2 | 五粮液 Q3 miss | 000858 | 148.80 | — | 146.83 | 146.77 | 140.04 | 127.20 | 131.58 | 131.35 | **-11.7%** | [-17, +11] |
| 3 | 贝泰妮 Q1 | 300957 | 57.51 | — | 57.16 | 53.40 | 48.32 | 48.40 | 42.05 | 62.42 | **+8.5%** | [-32, +29] |
| 4 | 宁德时代 924 | 300750 | 197.52 | — | 251.89 | 245.98 | 261.24 | 266.00 | 257.00 | 264.30 | **+33.8%** | [+1, +53] |
| 5 | 东方财富 924 | 300059 | 11.96 | — | 20.30 | 23.19 | 27.26 | 25.82 | 22.94 | 23.23 | **+94.2%** | [+2, +159] |

### 模拟 6 月走势 (monthly-6m, 5 events)

| # | 事件 | 锚点 | M+0 | M+1 | M+2 | M+3 | M+4 | M+5 | M+6 | sim 累计 | Up/Dn/Rev |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 药明康德 | 73.29 | 73.29 | 65.96 | 60.34 | 62.56 | 65.62 | 69.75 | 76.18 | **+3.94%** | 4/2/1 |
| 2 | 五粮液 | 148.80 | 148.80 | 149.10 | 154.75 | 150.72 | 146.63 | 140.62 | 133.92 | **-10.00%** | 2/4/1 |
| 3 | 贝泰妮 | 57.51 | 57.51 | 55.44 | 57.12 | 58.98 | 61.08 | 59.87 | 57.80 | **+0.50%** | 3/3/2 |
| 4 | 宁德时代 | 197.52 | 197.52 | 201.94 | 194.18 | 186.88 | 191.78 | 183.08 | 173.09 | **-12.37%** | 2/4/3 |
| 5 | 东方财富 | 11.96 | 11.96 | 12.40 | 11.96 | 12.42 | 12.05 | 11.53 | 11.96 | **-0.02%** | 3/3/4 |

### 对齐度表

| 事件 | 真实 6M | sim 6M | 方向 | 幅度比 sim/real | 月内反转 sim/? | 帖子 Bull%/Bear% | 买卖比 |
|---|---|---|---|---|---|---|---|
| 药明康德 | -46.5% | +3.9% | **NO** | -0.08× | 1 rev | 10% / 60% ✓ | 2.18 (看空大但执行是买) |
| 五粮液 | -11.7% | -10.0% | YES | **0.85× ✓** | 1 rev | 41% / 38% | 0.50 |
| 贝泰妮 | +8.5% | +0.5% | YES | 0.06× (量级缺) | 2 rev | 34% / 36% | 1.08 |
| 宁德时代 | +33.8% | -12.4% | **NO** | -0.37× | 3 rev | 26% / 42% | 0.42 |
| 东方财富 | +94.2% | -0.02% | **NO** | -0.00× | 4 rev | — | 0.82 |

## 3. 五大结构性偏差 — v2 版本

### 3.1 `update_conviction_context` 过度对称化 → 强利空事件被 "反弹预期" 拉回

**代码位置**: `src/ssflow/oasis_persona_adapter.py:91-104`

```python
if cum_pct > 3.0:
    price_comment = "价格越高, 继续上涨的空间可能越小, 回调的风险越大"
elif cum_pct < -3.0:
    price_comment = "价格越低, 继续下跌的空间可能越小, 反弹的可能性越大"
```

**症状** (药明康德轨迹):
```
M+0: 73.29
M+1: 65.96  (-10%, 触发 M+1 的 price_comment: "反弹的可能性越大")
M+2: 60.34  (-17.67%, 继续触发)
M+3: 62.56  (+2.56%, 反弹开始)
M+4: 65.62  (+10.47%, 加速反弹)
M+5: 69.75  (+14%)
M+6: 76.18  (最终 sim +3.94% vs real -46.5%)
```

**根因**: 真实市场里, "价格跌多了反弹的概率" 取决于**跌的原因**. 基本面恶化 / 地缘风险的跌是**永久性**的, 不反弹; 技术性超卖反弹才是 mean reverting. 现在的 prompt 是**无条件**的 mean reversion hint, 让 agent 在**任何**下跌场景都期待反弹.

**修复方向**:
- 把 price_comment 改成 **conditional on event polarity**. 事件类型是 regulatory / delisting / bankruptcy 时, 跌下去的 mean reversion 不生效 (应改成 "基本面风险, 下跌可能远未结束").
- 或者把 mean reversion 门槛从 cum_pct ±3% 拉大到 ±15%, 让真正 "短期超卖" 才触发.

### 3.2 月度 `主动考虑减仓/加仓/翻转` 指令是无条件的 → 强利好事件被月度重估吞掉

**代码位置**: `src/ssflow/round_schedule.py` 新加的 monthly 分支

```python
elif is_monthly and round_idx > 0:
    lines.append(
        "  月度重估: 距上月月线已有约 30 个交易日, "
        "上月末的叙事是否仍然成立? 基本面/估值是否已经验证? "
        "请主动考虑减仓/加仓/翻转 — 不要仅因上月方向而默认延续"
    )
```

**症状** (宁德时代轨迹):
```
M+0: 197.52
M+1: 201.94 (+2.24%, 924 政策效应只体现了 2.24%/10% 的 cap)
M+2: 194.18 (-1.69%, 月度重估触发, 部分 agent 翻转)
M+3: 186.88 (-5.39%, 翻转加速)
M+4: 191.78 (+2.91%, 反弹失败)
M+5: 183.08 (-7.31%)
M+6: 173.09 (最终 -12.37% vs real +33.81%)
```

**根因**: 这条 prompt 是**我今天写的**. 写它的动机是 v1 的 "保持方向一致" 问题, 但完全矫枉过正 — 它**命令** agent 每轮考虑翻转, 无论事件驱动是否仍然有效. 对政策级强利好 (单次事件驱动的长期 re-rating), 翻转指令直接撕裂了持续看多的 narrative.

**修复方向**:
- 去掉 "请主动考虑减仓/加仓/翻转" 的**祈使句**, 改成开放式判断: "评估上月走势是否已充分 priced in"
- 让 agent **自主决定**是否翻转, 而不是由 prompt 命令
- 或者把月度重估做成 conditional: 只在 cum > 20% 或 < -20% 时提醒 "空间可能变小", 小幅场景不提

### 3.3 Kyle ±10% / round cap 是日级规则 → 月频无法表达政策级单月 +30%+

**代码位置**: `src/ssflow/market_dynamics.py:41`

```python
MAX_DELTA_PCT_PER_ROUND: float = 0.10
```

**症状** (东方财富 M+1 对比):
- 真实 M+1: +70% (924 后一个月即翻倍的券商行情)
- sim M+1: +3.68% (远未触 +10% cap, 净 flow 就先被 dynamic_knee 压缩了)

即使强制把 cap 打开到 ±30%, sim 还是打不到, 因为:
1. **`compute_dynamic_knee` 用累计幅度衰减 base_knee** — 对月频来说, M+2 时 cum_abs_delta 都 >5%, knee 已经被打压
2. **flow_knee=0.08 是日级经验值** — 月度情绪聚集导致的 flow/ADV 比日度高一个数量级, knee 被压缩的系数更狠

**修复方向**:
- `compute_price_impact` 和 `compute_dynamic_knee` 需要接一个 `cadence` 参数 (daily / monthly), 月频场景:
  - `max_delta_pct` 放宽到 ±30% 甚至 ±50%
  - `flow_knee` 放宽到 0.3 左右
  - `cumulative_abs_delta` resistance 的阈值提高 2-3 倍
- 或者 `monthly-6m` preset 自动注入这些 overrides

### 3.4 bearish analyst 无条件的 trigger_prob → 利好事件里空头研究照样刷屏

**代码位置**: `personas/ashare.yaml:1455-1457`

```yaml
- id: independent_bearish_research
  publishes:
    - content_type: research_note
      trigger_prob: 0.30
```

**症状** (宁德时代帖子情绪):
真实 +33.8% 的强利好, 帖子分布 26% 看多 / 42% 看空 — **看空帖比看多帖多 60%**.

Muddy Waters 风格的 "独立做空研究" 会每轮 30% 概率发帖. 对强利好事件, 这些帖子注入了无法回避的看空信号, 影响了 feed 里 high-authority research_note 的数量.

**修复方向**:
- `trigger_prob` 应该条件于**事件极性**: 强利好 (overnight_sentiment > 0.5) 降到 0.05, 强利空 (< -0.5) 维持 0.30, 中性维持 0.15
- 或者: Muddy Waters-style 空头研究的内容生成时, 应该显式承认 "主流 narrative 与我相反"
- 更一般地: persona 的发声频率应该根据事件是否**对它的论点有利**动态调整

### 3.5 force_action 只有下行触发 → 没有对应的上行 FOMO 机制

**代码位置**: `src/ssflow/sandbox_templates.py:161-176`

```python
# 融资利用率逼近警戒线 → 券商强平卖出 50%
{"forced_side": "sell", "forced_quantity_pct": 0.50},
# 累计跌超 5% → 机构合规风控减仓 20%
{"forced_side": "sell", "forced_quantity_pct": 0.20},
```

**症状**: 强利空事件 (药明康德) 一旦 -5%, 机构被强制减仓 — 这**放大**了跌势, 让 sim 能跟上 v1 所不能的下跌. 但对等的**上行机制完全缺失**:
- retail 短线追涨没有 "cum > 20% → 散户 FOMO 追加 30%"
- 机构没有 "突破 250 日新高 → 增仓 20%"
- 没有 "短期融资余额上升 → 市场情绪传染" 之类的正反馈

**修复方向**: 补一套对称的 forced_action for 上行场景
```python
# 累计涨超 20% → 机构跑步进场 (补仓增仓)
{"entity_slot": "institutional_class", "condition": "price_change_pct > 20.0",
 "effect_type": "force_action", "forced_side": "buy", "forced_quantity_pct": 0.15},
# 散户融资余额上升 → 短线追涨
{"entity_slot": "retail_class", "condition": "margin_utilization < 0.30 and price_change_pct > 10.0",
 "effect_type": "force_action", "forced_side": "buy", "forced_quantity_pct": 0.20},
```

### 3.6 补充: OASIS TwHIN rec model 是慢速瓶颈

**观察**: log 显示 `social.rec INFO twhin model cost time: 65s` 每轮. 这是 OASIS 的推荐系统嵌入模型重建, 和 LLM 无关.

- 6 轮 × 65s = 390s / 事件 = 6.5 min 纯在 TwHIN
- 5 事件 = 32.5 min 在 TwHIN
- 加上 LLM 推理, 单个 sim 真实耗时 20-27 min

**影响**: monthly-6m backtest 的单次迭代成本 (时间) 太高, 不适合做密集参数扫描. 建议:
- 月度 sim 应该 bypass TwHIN (monthly 尺度下 recommendation refresh 没必要每轮做)
- 或者缓存 rec 结果
- 或者只在 M+0 和 M+3 refresh

## 4. 修复优先级 (v2)

| 优先级 | 修复 | 预期影响 | 工作量 |
|---|---|---|---|
| P0 | `update_conviction_context` 的 mean reversion 改为 **conditional on event_type** (regulatory/delisting 时不提 "反弹可能") | 救回强利空事件 (药明康德从 +3% 回到 -30%+) | 1h |
| P0 | 去掉 monthly narrative 里的 **祈使句** ("请主动考虑翻转"), 改为 "评估叙事是否已充分消化" | 救回强利好事件 (宁德时代 / 东方财富) | 30m |
| P0 | `compute_price_impact` / `compute_dynamic_knee` 接 `cadence` 参数, `monthly-6m` 自动放宽 cap 到 ±30% + knee 到 0.3 | 让强政策事件能表达真实幅度 | 2h |
| P1 | bearish persona 的 `trigger_prob` 条件于事件极性 (`overnight_sentiment`) | 利好事件里压制 Muddy Waters 噪声 | 1h |
| P1 | 对称补 **上行 force_action** (institutional momentum chase, retail FOMO) | 强利好事件加速 | 1h |
| P1 | OASIS TwHIN rec model 月度 bypass | 单次 backtest 从 25 min → ~5 min | 2h |
| P2 | 估值锚点 (PE/PB/目标价) 注入 prompt | 让 agent 区分 "涨到估值高" vs "涨到估值合理" | 重构级 |
| P2 | `ExternalEventSchedule` 自动注入月度新事件 (季报 / 宏观数据) | 让 M+2-M+5 有新信息驱动 | 3h |
| P2 | 统一 bias detection → 让 monthly 的 active_agent_types 包含全部 class | 月度尺度下不应该按日内 session 筛选活跃度 | 30m |

## 5. 几个容易混淆的结论

1. **不是 "agent 都看空"**: 药明康德帖子 60% 看空是对的, 因为事件真的是利空; 问题在**信念执行**环节 — agents 认同 bear narrative 但交易层被 mean reversion 拉回.

2. **不是 "Kyle cap 就卡死"**: 对 bull 事件 sim 甚至没打到 10% cap, 是 buy flow 本身被 bearish voices 稀释. 日频 cap 对月频确实是问题, 但不是全部问题.

3. **不是 "persona 不平衡"**: 16 个 trader 已经全部能 sell, 2 个显式 short. 问题不在 persona 配置, 在 **prompt-level 压力和 info-layer 偏差**的组合效应.

4. **v2 软利空事件成功了**: 五粮液 -10% vs real -11.7%, 方向 + 幅度双 align. 说明系统在**中等信号 + 中等情绪**区间 work. 极端区间 (强利空 / 强利好) 两端都出问题.

## 6. 两种设计哲学的取舍

v1 和 v2 的偏差本质上是**两种极端的 prompt design**:

**v1 风格**: "保持方向一致, 不要翻转" → 单调 monotonic, 无 mean reversion, 正反馈锁定
**v2 风格**: "价格越高风险越大, 主动考虑翻转" → 过度 mean reverting, 吞掉真实趋势

**正解**: agent 的方向应该由**证据驱动**而非 prompt 驱动.
- 收到新的 catalyzing 信息 → 保持方向 or 反向
- 价格已经 price-in 原事件 → 逐步减仓
- 出现反向 catalyst → 明确翻转

这需要的不是 prompt "请你 X", 而是 **round-level 的新信息注入 + 基本面/估值锚点作为客观参考**. 落到代码上是 P2 的两项 (估值锚 + external event schedule).

## 附录 A: 每轮价格轨迹 ASCII chart

```
药明康德 BIOSECURE 法案 (real -46.5%, sim +3.94%)
  M0   ¥73.29  │        real → ¥54.20 (-26%)
  M+1  ¥65.96  ▇▇▇▇▇▇▇▇▇▇ -10.0%  (sim 抛售)   real ¥54.48 (-26%)
  M+2  ¥60.34  ▇▇▇▇▇▇▇▇ -17.67% (继续跌)       real ¥46.18 (-37%)
  M+3  ¥62.56  ▆▆▆▆▆▆▆ -14.64% (反弹开始)      real ¥43.67 (-40%)
  M+4  ¥65.62  ▅▅▅▅▅ -10.47%                  real ¥42.11 (-43%)
  M+5  ¥69.75  ▃▃▃ -4.83%                     real ¥39.19 (-47%)
  M+6  ¥76.18  ▁ +3.94%                       (sim 完全背离)

宁德时代 924 政策组合拳 (real +33.8%, sim -12.37%)
  M0   ¥197.52 │        real → ¥251.89 (+27%)  ← M+1 即涨 27%
  M+1  ¥201.94 ▁ +2.24% (远不及 cap)           real ¥245.98 (+25%)
  M+2  ¥194.18 ▁ -1.69% (翻转开始)              real ¥261.24 (+32%)
  M+3  ¥186.88 ▂ -5.39%                        real ¥266.00 (+35%)
  M+4  ¥191.78 ▁ -2.91% (反弹失败)              real ¥257.00 (+30%)
  M+5  ¥183.08 ▂ -7.31%                        real ¥264.30 (+34%)
  M+6  ¥173.09 ▃ -12.37%                      (sim 方向完全反)

东方财富 924 政策组合拳 (real +94%, sim -0.02%)
  M0   ¥11.96  │        real → ¥20.30 (+70%!)   ← M+1 即涨 70%
  M+1  ¥12.40  ▁ +3.68%                         real ¥23.19 (+94%)
  M+2  ¥11.96  ▫ +0.00%                         real ¥27.26 (+128%)
  M+3  ¥12.42  ▁ +3.85%                         real ¥25.82 (+116%)
  M+4  ¥12.05  ▁ +0.75%                         real ¥22.94 (+92%)
  M+5  ¥11.53  ▁ -3.60%                         real ¥23.23 (+94%)
  M+6  ¥11.96  ▫ +0.00%                        (sim 在 ±4% 内横盘)
```

## 附录 B: 帖子情绪 (post sentiment) 汇总

| 事件 | Real | Posts | Bull% | Bear% | Neutral% | Tilt |
|---|---|---|---|---|---|---|
| 药明康德 BIOSECURE | -46.5% | 176 | 10% | **60%** | 28% | ✓ BEAR |
| 五粮液 Q3 | -11.7% | 172 | 41% | 38% | 20% | ~ flat |
| 贝泰妮 Q1 | +8.5% | 202 | 34% | 36% | 29% | ~ flat |
| 宁德时代 924 | **+33.8%** | 187 | **26%** | **42%** | 31% | ✗ **wrong** |
| 东方财富 924 | **+94.2%** | — | — | — | — | (未统计) |

药明康德 60% 看空 + Muddy Waters 级 bearish analyst + price-anchor caution = 多重 bearish 叠加, 但**交易层的 mean reversion 把下跌拉回**. 宁德时代则是反过来: 强利好 event text, 但 bearish persona + 月度翻转指令 + Kyle cap = 让看多情绪无法穿透.
