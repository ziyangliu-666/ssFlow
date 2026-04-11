# ssFlow 历史回测 v2 修复验证报告

**日期**: 2026-04-11
**修复 commit**: `76bdf57` (fix(bias): event-conditional conviction + cadence-aware Kyle + upside teeth)
**回测**: 3 个最差案例重跑 (603259 / 300750 / 300059)
**方法**: `scripts/backtest_monthly.py --only 603259,300750,300059`, monthly-6m schedule
**耗时**: ~65 min / $1.00 (gpt-4o-mini)

---

## 0. 核心结论: **一个胜利, 一个部分改善, 一个严重回归**

**Direction accuracy: 1/3** (v1 同子集 0/3, 绝对值上 +1). 但单看"相对 v1 好还是坏":

| 事件 | 真实 | v1 sim | v2 sim | 方向 | 评估 |
|---|---|---|---|---|---|
| 药明康德 BIOSECURE | -46.5% | **+3.9%** | **-59.95%** | **v1❌ → v2✅** | **巨大胜利** (修复跨幅 +63pp) |
| 宁德时代 924 政策 | +33.8% | -12.4% | -4.77% | v1❌ → v2❌ | 部分改善 (+7.6pp 往正确方向) |
| 东方财富 924 政策 | +94.2% | -0.02% | **-33.04%** | v1❌ → v2❌❌ | **严重回归** (-33pp) |

**核心发现**: **P0-a (事件条件 mean-reversion) 是 home run**, 把药明康德 sim 从 +3.9% 拉到 -59.95% — 首次成功表达了 -40% 级别的基本面利空. 但 **P0-c (cadence-aware Kyle, bypass daily limit board) + 遗留的 downside force_action** 组合创造了**非对称 doom loop**: 对强政策利好事件, 初始 M+0 冲击不够强, 任何微小下跌会被 -5% 阈值的 institutional risk-off 放大成级联抛售, 没有 daily 10% cap 作为缓冲.

## 1. v1 → v2 逐事件对比

### 1.1 药明康德 BIOSECURE (2024-01-25, real -46.5%)

```
         v1 sim    v2 sim    real
M0       73.29    73.29     73.29
M+1      65.96    53.54     54.20    ← v2 比 v1 多跌 12 点, 与 real 对齐到 1 分内
M+2      60.34    40.25     54.48    ← v2 已经跌到 -45% (monthly cap 允许)
M+3      62.56  ← v1 开始反弹      46.18    v2: 38.31 (-48%)
M+4      65.62  ← v1 继续升        43.67    v2: 32.24 (-56%)
M+5      69.75  ← v1 +5%           42.11    v2: 31.85 (-57%)
M+6      76.18 (+4% 最终)          39.19    v2: 29.35 (-60%)
```

**胜利原因**:
1. **P0-a** (event-type-aware mean reversion): regulatory 事件不再触发 "价格越低反弹可能越大". agent 正确认识这是永久性 risk repricing.
2. **P0-c** (monthly cadence 解除 daily 10% cap): M+1 可以直接跌 -27%, 不像 v1 被 +-10% 夹住每月小动.
3. **P0-c** (bypass limit_board): 限价板状态机不干扰月度价格演进.

**仍有问题**: sim -60% vs real -47%, **超射 13pp**. 原因:
- 一旦跌穿 -5%, institutional 强制减仓 20% (downside force_action), 每轮都在追加抛售, 没有反弹机制
- 药明康德真实轨迹里有几次机构 "企稳" 回购信号, sim 里完全没有"跌深反弹"力量  
- sim 帖子情绪 4% bull / 71% bear — 比真实市场更极端, 加剧了单边

**评估**: 方向正确, 幅度只过度 28%. **远优于 v1 的 +50pp 方向错误**. 可接受.

### 1.2 宁德时代 924 政策 (2024-09-24, real +33.8%)

```
         v1 sim    v2 sim    real
M0      197.52   197.52    197.52
M+1     201.94   194.01    251.89    ← real M+1 直接 +27%, sim 两者都未抓住
M+2     194.18   177.23    245.98    v2 在 M+2 直接触底 -10.27%
M+3     186.88   163.28    261.24    v2 最低点 M+3 -17.33%
M+4     191.78   168.09    266.00    v2 开始反弹 (首次)
M+5     183.08   175.45    257.00    v2 继续反弹
M+6     173.09   188.11    264.30    v2 收 -4.77% (v1 收 -12.4%)
```

**部分改善原因**:
1. P0-a 的正向版本开始起作用: 当 cum 跌破 -15% 门槛, **policy 事件不再触发 "回调" 继续卖出**
2. sim 在 M+4-M+6 出现首次反弹, buy_sell_ratio **3.27 (净买 3 倍于卖)** 显示 trader 层最终认可了 bull narrative
3. P1-b 上行 force_action 在 M+5-M+6 可能开始发力 (sim 价格从 163 反弹到 188, 约 +15%)

**仍有问题**:
- **M+1 完全没抓住 +27% 的 gap up**: sim M+1 只有 -1.78%. 原因:
  - 初始 sentiment 还没完全转多 (bear posts 50% vs bull 21%)
  - Kyle cap 虽然放到 +40%, 但实际 raw flow × lambda 远未触顶
  - 政策事件需要的是**瞬时高强度冲击**, 而 sim 是逐步扩散的
- **doom loop 在 M+2-M+3 触发**: 从 +0 掉到 -17%, 是 downside force_action (-5% threshold) 追着卖出导致的, 不是 bull narrative 真的失败
- sim 最终收 -4.77%, 虽然比 v1 的 -12.4% 强, 但仍方向错

**评估**: 部分胜利, 最终反弹表明 conviction 机制起作用, 但早期 doom loop 把分数吃掉了.

### 1.3 东方财富 924 政策 (2024-09-24, real +94.2%)

```
         v1 sim    v2 sim    real
M0      11.96    11.96     11.96
M+1     12.40    11.98     20.30    ← real M+1 +70%, sim 几乎 flat
M+2     11.96    10.97     23.19    v2 开始下跌 -8%
M+3     12.42    10.06     27.26    v2 继续跌
M+4     12.05    10.42     25.82    v2 小反弹失败
M+5     11.53    9.32      22.94    v2 跌到最低 -22%
M+6     11.96    8.01      23.23    v2 收 -33% (v1 收 -0.02%)
```

**严重回归原因**:
1. **M+1 依然未抓住**: sim 只 +0.17% 对应 real +70%. 这是 Kyle 无论 cap 多高都表达不出的 — 真实场景每天涨停板直到 M+1 末端, 用日度 cap 重复触发; 月度模式一个月只有一次 Kyle call, 即使不 clip, 也追不上真实连续涨停的累积效果.
2. **doom loop 从 M+2 开始全面失控**: -8% → -16% → -12% → -22% → -33%. 复盘原因:
   - buy_sell_ratio 0.46 (卖流量是买流量的 ~2 倍), trader 整体净卖出
   - 帖子情绪 24% bull / 43% bear, 媒体+分析师层仍在输出看空
   - downside force_action 在 -5% 阈值**每轮都触发**, 机构合规风控持续减仓 20%
   - 没有 daily 限价板做缓冲 (我 bypass 了)
   - 上行 FOMO force_action 的 +15% 阈值**从未被触及** (sim 最高才 +0.17%)
3. **v1 没这么糟**: v1 sim 在 ±4% 内横盘, 因为 daily 限价板 + 日度 Kyle cap 提供双重 brake, 不会出现月度 -33% 这种级联崩溃.

**评估**: **v2 的修复组合对东方财富造成了严重回归**. 我们把保护机制 (limit board) 拆了却没补上对应的上行机制, 同时保留了 sensitive 的下行 force_action — 非对称.

## 2. 各项修复的实际效果评分

| 修复 | 目标问题 | 实际效果 | 评分 |
|---|---|---|---|
| **P0-a** 事件条件 mean-reversion | 药明康德跌到 -17% 后反弹 | **完美**: 药明康德 sim -60%, 不再反弹 | ⭐⭐⭐⭐⭐ |
| **P0-b** 去掉月度翻转祈使句 | 宁德时代被月度重估打脸 | **部分**: 宁德 sim 后半段反弹, 但早期仍跌 | ⭐⭐⭐ |
| **P0-c** monthly Kyle cap + bypass limit_board | 东方财富 +94% 打不出来 | **负面**: 放开幅度解除了 brake, 反而给 doom loop 更大空间 | ⭐ |
| **P1-a** bearish analyst 闭嘴 | 宁德时代 42% bear 帖子 | **未起效**: 宁德 v2 仍 50% bear; 东方财富 43% bear | ⭐ |
| **P1-b** 对称上行 force_action | 机构动量追涨 / 散户 FOMO | **未触发**: sim 达不到 +15% 阈值 → FOMO 从未发火 | ⭐ |

**加权**: **P0-a 是唯一无可争议的胜利**. P0-c 产生了最大的副作用. P1-a/b 基本 **未触发**.

## 3. 根本原因: 非对称 doom loop

```
                    v2 现状
                    
政策利好事件  ─┬─► M+0 sentiment_modifier 还是偏空 (bear posts 50%)
               ├─► M+1 净 flow 微正, Kyle 只打出 +0.17%
               │    (远未触发 FOMO +15% 门槛)
               │
               ├─► 噪声把 M+2 打到 -5%
               │       ↓
               └─► institutional risk-off force_action (-5% 阈值) 触发
                       ↓
                   强制减仓 20%
                       ↓
                   M+3 进一步跌 (无 daily limit board 缓冲)
                       ↓
                   force_action 再次触发
                       ↓
                   级联直到 -20% / -30%
```

**核心机制**:
1. **M+0 冲击不足**: 月度 Kyle 单次 call 无法表达 "连续 5 个涨停板" 的累积效果
2. **M+1 信息反馈被延误**: 帖子反映的 "政策伟大" 需要几轮才能消化, 而下行阈值只需要一轮触发
3. **非对称阈值**: 上行 +15% vs 下行 -5% = **3× 不对称**
4. **失去日度 brake**: 为了月度能表达大幅度, 把 daily limit board bypass 了, 但没补上任何月度 brake
5. **force_action 无极性感知**: 政策利好事件里, -5% 其实是 "跌出黄金坑" 而不是 "风险触发", 但 force_action 不知道

## 4. 下一步修复清单 (v3)

### P0 (立即必做)

**v3-1**: 对齐 force_action 阈值, 极性感知
- 下行 institutional risk-off: **-5% → -15%**, 量级 **0.20 → 0.10**
- 上行 institutional chase: **+20% → +8%**, 量级 **0.15 → 0.20**
- 上行 retail FOMO: **+15% → +5%**
- 对 `policy / supply_disruption / demand_shock` 事件**跳过下行 force_action**
- 对 `regulatory / lawsuit / shareholder_action / geopolitical` 事件**跳过上行 force_action**

文件: `src/ssflow/sandbox_templates.py` (条件) + `src/ssflow/entity_engine.py` (评估时 gate event_type)

**v3-2**: 重建月度 "soft brake" 替代日度 limit board
- 在月度模式下, 连续下跌 (Δpct 连续 N 轮 < 0) 的 marginal Δ 需要 decay
- 或者: 重新启用 limit_board 但 board_type 改为 "wide" (± 30% monthly cap)
- 或者: `compute_price_impact` 加参数 "trend_persistence_penalty" — 连续同方向的 round 边际 impact 下降

文件: `src/ssflow/market_dynamics.py` + `src/ssflow/oasis_engine.py`

**v3-3**: 对 policy 类事件 M+0 注入强力 initial impulse
- `event_severity.resolve_event_severity` 已经返回 overnight_sentiment; 当 > 0.5 且事件是 policy 时, 对 M+0 额外乘以 1.5-2× 的 raw_delta 冲击
- 这是对 "月度无法表达连续涨停" 的 workaround

文件: `src/ssflow/oasis_engine.py` (应用 sentiment_modifier 的地方)

### P1 (短期)

**v3-4**: 验证 P1-a 的 voice_prompt 实际是否生效
- 看 v2 DB 里 Muddy Waters / CICC contrarian 分析师 M+0/M+1 是否仍然发看空帖
- 如果仍然发, 需要从 engine 层**直接禁止** (强制 DO_NOTHING), 不能靠 voice_prompt 自觉

**v3-5**: 提高基础 bull voice 的音量
- 现状: Muddy Waters 这种明确空头 persona 是 1 个, 但"偏向看空的其他 persona" (retail KOL pessimistic, media with neutral tilt) 占了大头
- 补一个 `independent_bullish_research` (牛市旗手 persona) 对称 Muddy Waters, trigger 条件相反

### P2 (中期)

**v3-6**: 让 agent 直接读**真实市场动量** 而不是只读 rumor feed
- `instrument.compact_kline_summary` 现在有 30 日技术指标, 但 agent 很少引用它
- 在 prompt 里强化: "观察近 5 日真实走势", 如果 +8% 以上必须把"动量追随"作为优先选项

## 5. 最终评估: v2 是正确方向但不完整

**正面**:
- P0-a 证明了 **event-type-aware 的 conviction 机制有效**: 药明康德从 "不知道怎么跌" 跳到 "正确跌到 -60%"
- 宁德时代后半段反弹证明 **买入流量最终能跑赢噪声**, 只是被早期 doom loop 吃掉
- 5/5 → 1/3 方向准确率看起来差, 但 specifically 修复的那类 (药明康德) 成功了

**负面**:
- P0-c 解除 daily brake 的副作用比预想的大: 月度尺度需要一个 **新的月度 brake**, 不能简单 "拿掉日度的等着 Kyle 管所有事"
- P1-a 的 voice_prompt 修改没起作用; 空头 persona 仍然主导了 50%+ 的 feed
- P1-b 的 FOMO 阈值 +15% 对月度模式来说永远不会触发

**下一步**:
- v3 必须先修 force_action 非对称 + 引入月度 soft brake, 否则 monthly 模式无法用来回测政策利好事件
- v3 之后再跑一次 3-event 子集, 目标: **方向 ≥ 2/3, 且 东方财富不再 -33%**
