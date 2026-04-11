# ssFlow TODO — 下次一口气执行

**上下文**：2026-04-11 全流程跑通后发现的问题清单。场景 CATL Q1 earnings
（300750.SZ，ChiNext 20cm 板），走 `earnings-3d` 4 轮，价格 ¥218.50 → ¥245.54
（+12.38%），耗时 73.9s / $0.14，report `reports/oasis_738d59cb3ef4.md`。

跑通暴露的核心问题：
1. 合并遗漏 bug 已在本地修（未 commit）
2. P&L 账务可能有问题
3. Agent 完全看不到历史 K 线
4. Round 推进对 agent 没有真实时间意义

按优先级和执行顺序排列。每项都给了问题定位、改法方向、涉及文件、验收标准——
下次可以直接按顺序执行，不需要重新调研。

---

## P0-A 未 commit 的修复（先 commit 再继续）

### A1. `round_schedule.prompt_context()` 签名补参
- **现状**：本次跑的时候报错 `TypeError: prompt_context() got an unexpected
  keyword argument 'cumulative_delta_pct'`。已在本地修复。
- **根因**：上次合并冲突时 `round_schedule.py` 保留了本地旧签名，但
  `oasis_engine.py:1109` 的调用方用了新签名（带 `cumulative_delta_pct` +
  `current_price`）。
- **已改**：`src/ssflow/round_schedule.py` — `prompt_context()` 方法加了两个
  kwonly 参数，并把它们渲染进时间上下文（当前价格 + 事件以来累计涨跌）。
- **动作**：`git diff src/ssflow/round_schedule.py` 确认改动，commit 成
  `fix(schedule): restore cumulative_delta_pct + current_price kwargs in
  prompt_context`。

---

## P0-B 账务 bug（先查清楚，再继续）

### B1. QFII / 主动基金在涨势里亏损
- **现象**：CATL +12.38% 的单边上涨里，北上资金 -¥26.3億、公募主动 -¥7.4億、
  私募 -¥26.1億，而这些 class 每一轮都在净买入。strategic（产业资本）账面
  +¥205億合理，但 active 资金集体亏损不合理。
- **怀疑**：`compute_class_pnl()` 里均价或 mark-to-market 的会计不一致。可能
  在滚动建仓时把新买入的仓位按建仓价 P&L 算 0，但把老仓位按最新价 MTM，两套
  账混在一起；或者手续费 / 滑点被重复扣了。
- **定位**：
  1. 读 `src/ssflow/trading_layer.py` 里 `compute_class_pnl()` / `apply_action()`
  2. 读 `src/ssflow/oasis_engine.py` 调用 P&L 的地方
  3. 写一个最小单测：单 class 单 agent，初始持仓 0 + 初始现金 1000 元，
     R0 买 500 元，R1 买 500 元，价格从 10 → 12 单调上涨，验证 P&L = +¥200
- **改法**：等定位清楚。可能是均价累加公式错 / 可能是 cash + holdings 双扣
  一次 / 可能是未实现 P&L 被当成已实现。
- **验收**：纯多头单调上涨场景里 active fund 应该盈利，回跑 CATL 确认 QFII
  从 -¥26 变成正数。

---

## P0-C 时间感真实化（纯 prompt 改造，改动小、验证快）

核心思路：让 agent 在 prompt 里真正"感觉到"时间段的特殊含义，而不是看一段
机械的时间戳描述。

### C1. `prompt_context()` 升级为叙事型
- **现状**：`src/ssflow/round_schedule.py:62` 的 `prompt_context()` 只输出
  `label / calendar_start~end / 距事件 N 小时 / 上一轮 / 下一轮` 几行干巴巴的
  描述。
- **改法**：
  1. 按 round id 判定 session kind（"T+0 上午盘"、"T+0 尾盘"、"T+1 开盘"
     等），输出叙事背景：
     - T+0 上午：`"事件后首个交易时段，散户情绪最狂热，机构通常观望等数据
       验证，流动性充沛但价格发现剧烈"`
     - T+0 下午 / 尾盘：`"尾盘流动性收敛，大单进场冲击成本上升。日内高位
       获利了结压力增加"`
     - T+1 开盘：`"新的交易日。昨日累计 +X%。隔夜情绪衰减约 30%，机构
       今日进场"`
     - T+1 盘中 / T+2 及以后：`"机构分析完毕正式进场 / 散户情绪见顶后回落
       / 融资盘观察警戒"`
  2. 距收盘剩余时间：用 `calendar_end` 和当前 round 在一天内的占比，输出
     `"距本日收盘还有 X 小时 Y 分钟"`
  3. 跨日时追加 `"这是事件后第 N 个交易日 - 前 N-1 天累计走势 ±X%"`
- **文件**：`src/ssflow/round_schedule.py` — 扩展 `prompt_context()` 方法
  签名接收更多上下文（当前价、累计涨跌、前一日收盘等），给 RoundDef 加
  `narrative_hint` 或 `session_kind` 字段。
- **验收**：跑一次，grep report 里的 `时间 / Time` 段，应该能看到叙事性描述
  而不只是时间戳。

### C2. T+1 锁定写进 agent 决策上下文
- **现状**：`T1Ledger` 机械上阻止当日卖出，但 agent prompt 里完全没说
  "今日买入明日才能卖"。agent 决策时根本不考虑这一成本。
- **改法**：
  1. 在 prompt 组装层（`oasis_persona_adapter.py` 里构建 user_profile 的
     地方）加一段：`"交易规则：A 股 T+1 - 今日买入的仓位明日方可卖出。
     请基于你对【明日】而不是【今日】价格的判断做买入决策。"`
  2. 散户 class 尤其要加这条，strategic 类可以省（他们本来就不做日内）
  3. 跨日开盘时 prompt 里追加 `"你昨日有 X 股今日可以自由卖出"` 做显式提示
- **文件**：`src/ssflow/oasis_persona_adapter.py` — 找到构建 agent prompt
  的地方（约 `build_oasis_agent_from_persona` 或 user_profile 组装处）
- **验收**：grep 跑完的 agent 决策理由里应出现 "T+1 / 明日 / 隔夜" 等词频
  明显上升

### C3. 时段敏感性硬信号
- **改法**：在每轮 prompt 开头加一行明显的 session 标签：
  - `[盘中 · 充裕流动性]`
  - `[尾盘 · 流动性收敛]`
  - `[开盘竞价 · 价格发现]`
  - `[隔夜休市 · 情绪衰减]`
  这个是简单字符串插入，不涉及任何逻辑改动。
- **文件**：`src/ssflow/round_schedule.py` + `oasis_persona_adapter.py`
- **验收**：report 里每轮的时间块能看到这个标签

---

## P0-D 历史 K 线 / 资产宇宙一体化（核心设计级改动）

核心思路：让 `run_one.py` 默认走 `distillation` 路径拉多标的 universe +
历史 K 线，在 agent prompt 里暴露紧凑的历史走势数据。

### D1. `run_one.py` 默认走 distillation 路径
- **现状**：`scripts/run_one.py` 从单个 `Event` 直接进 `run_simulation`，
  跳过了 `distillation.py` 里的 `distill_universe()`。结果是 `Instrument.
  kline_30d` 永远是空列表，`InstrumentUniverse` 根本没被构建。
- **改法**：
  1. 在 `run_one.py` 里，event 准备好后调用 `distill_universe()`（或
     `distill_from_event()`）构建 universe
  2. 默认拉 3-5 个关联标的：事件主体 + 1 板块 ETF + 1-2 竞品 + 1 上/下游
  3. LLM 生成关联标的列表（给 event_text + sector），然后并行拉各自的
     real-time quote + kline_30d
  4. 把 universe 传给 `run_simulation(instrument_universe=universe)`
  5. 加 CLI flag `--no-universe` 保留单标的兼容路径
- **文件**：`scripts/run_one.py` + 可能需要小改 `src/ssflow/distillation.py`
- **验收**：跑 CATL 应该看到 `# universe: 300750 (event_subject), 159813
  (板块ETF), 002709 (peer), 002460 (upstream), ...` 之类的日志，并且
  `report.price_range` 段会显示多标的价格

### D2. `InstrumentUniverse.prompt_summary()` 塞紧凑 K 线
- **现状**：`src/ssflow/instrument.py:275` 的 `prompt_summary()` 每个标的
  只输出 `"名称 (ticker) [关系] 当前价 ¥XXX"`——K 线数据被 compute
  pairwise beta 内部用但 agent 自己看不到任何一根柱。
- **改法**：每个 instrument 的渲染行追加一段 K 线统计量。30 根柱全塞
  prompt 太长，做聚合更好：
  ```
  - CATL (300750) [事件相关] 当前价 ¥218.50
    30日: +8.3% | 波动率 2.1%/日 | 最高¥245 / 最低¥198
    20日均线¥218.4 | 成交额均值 85億 | 近5日换手率 +23%
    近5日趋势: +1.2% / -0.8% / +2.1% / -0.4% / +3.3%
  ```
- **做法**：在 `Instrument` 上加一个 `compact_kline_summary()` 方法，
  从 `kline_30d` 算：
  - 30 日累计涨跌 %
  - 30 日日收益率的标准差（波动率）
  - 30 日最高 / 最低
  - 20 日简单均线
  - 30 日成交额均值
  - 近 5 日换手率趋势
  - 最近 5 日日涨跌幅列表
- **文件**：`src/ssflow/instrument.py` — 新增 `compact_kline_summary()`
  方法，修改 `prompt_summary()` 调用它
- **验收**：跑一次，agent prompt 里（或 debug dump）应能看到每个标的的
  历史统计块

### D3. 事件主体的 OHLCV 最近 5 日完整显示
- **改法**：对 `event_subject` 关系的 instrument 特别待遇，显示最近 5 根
  完整日线：`日期 / 开 / 高 / 低 / 收 / 成交额`。其他标的只显示紧凑摘要。
- **文件**：`src/ssflow/instrument.py` `prompt_summary()`
- **验收**：report 里事件主体那行下面有最近 5 日 OHLCV 表

### D4. Persona-specific 信息过滤（可选，难度高）
- **思路**：不是所有 class 看到同样的数据——量化看完整 OHLCV + 技术指标，
  散户只看"近3日涨跌 + 是否破位"，strategic 看月线 + 持仓成本。
- **改法**：在 `persona.yaml` 里加 `information_filter: [technical|retail|
  strategic|fundamental]` 字段，prompt_summary 根据 filter 输出不同内容。
- **优先级**：可以放 P1，D1-D3 完成后再做。
- **文件**：`personas/ashare.yaml`, `src/ssflow/persona.py`,
  `src/ssflow/instrument.py`

---

## P0-E 极端场景压测（前面改完后必跑）

P0-C 和 P0-D 改完后，跑几个极端场景验证限价板机制 + 时间感 + 多标的
traversal 都能在真实 run 里工作：

### E1. 一字跌停场景
- 场景：某 ST 股突发退市警告，prev_close ¥10.00，`event_type=delisting_risk`
- 预期：pre-open auction 触发 gap_open，R0 开盘就是 LIMIT_DOWN，
  execution state 面板显示 "跌停 / 封单强度 X.X"
- 验收：report 里有 `执行状态: 跌停` 字样，log 里有 `PRE-OPEN AUCTION: gap
  -9.XX% → board LIMIT_DOWN`

### E2. 连板场景
- 场景：政策利好引发的情绪炒作（比如"央行降准"），主板标的连续 2 天涨停
- 预期：R0 收盘价 = prev_close × 1.10，R1 开盘 gap +5% 后继续上攻涨停
- 验收：两日都触发 LIMIT_UP，seal_strength 逐日变化可见

### E3. ETF / 多标的联动（P0-D 改完后）
- 场景：CATL 涨停，板块 ETF（159813 电池 ETF）和同板块个股应有正向联动
- 验收：`InstrumentUniverse.compute_spillover()` 给相关标的的价格确实被
  推高，report 里多个标的的价格都显示变动

---

## P1 路径到 8/10（AUTO_REVIEW.md 里 Codex 给的缺口）

### P1-1. 多标的因子层（D1 之后的自然延伸）
- 基础已有：`InstrumentUniverse.compute_spillover()` + pairwise beta
- 缺：spillover 生效路径没在主引擎端到端接通——universe 被构建但 engine
  只 trade 单标的。需要改 `oasis_engine.py` 主循环，每轮算完事件主体的
  delta 后调用 `compute_spillover()` 给其他标的报价

### P1-2. 差异化资产负债表
- 现状：per-class 里每个 agent 的 cash / holdings 是同质的
- 改：从 persona 采样资金规模的对数正态分布（散户 1-50 万、私募 5000 万-
  5 亿、社保 10-1000 亿），体现长尾。`src/ssflow/oasis_persona_adapter.py`
  agent 实例化处

### P1-3. 空头 / 融券
- 现状：`event_driven_short_fund` 6 agents 都说 sell 但 net_flow ¥0（没仓可
  卖）
- 改：允许负持仓 + 融券成本（按年化 8-12% 计算每日持有成本），或给初始
  short interest。涉及 `trading_layer.py` 持仓模型改造

### P1-4. 文本→overnight sentiment 信号提取
- 现状：`_severity_map` 按 `event_type` 死映射（earnings=0.0）。scenario
  文本里写"预期开盘冲击涨停"完全没被 parse
- 改：在 `event_extractor.py` 合成 EventProposal 时多产一个
  `overnight_sentiment: -1~+1` 字段，`oasis_engine.py` 里 prefer 这个
  字段而不是 severity_map

---

## P2 扩充锐度

### P2-1. 扩充 calibration library
- 现在 10 个事件，回测 direction accuracy 52%（刚过随机）
- 扩到 30+ 事件，CI gate 卡 60% 阈值
- `src/ssflow/calibration_library.py`

### P2-2. 出版物效果触发覆盖测试
- 8 类 publication 各编一个对应的 LLM-free 测试场景，确认 `EffectTracker`
  在真实 run 里能被每类触发至少一次
- `tests/test_publication_effects.py` 新增 integration 级测试

### P2-3. 报告执行面板 always-render
- 现在只有非 NORMAL 状态才显示 execution state panel
- 改成始终渲染（NORMAL 时也显示 fill_rate=100%, seal_strength=0, T+1 锁定
  = 0），便于调试确认"系统真的在跑"
- `src/ssflow/report.py:174+` 的 `_render_execution_state()`

---

## P3 产品化

### P3-1. 前端接入新字段
- fill_rate / board_state / T+1 lockup / publication effects 这些新字段暴露
  给 UI，让用户能把机制外显看到
- `api/app.py` + `frontend/`

---

## 执行顺序建议

**一口气模式**（下次直接让 agent 执行）：

```
1. P0-A commit schedule fix                       # 5 min
2. P0-B 写 P&L 最小单测 + 定位 + 修               # 30-60 min
3. P0-C1/C2/C3 时间感改造（纯 prompt 改动）       # 30 min
4. P0-D1 run_one.py 接 distillation               # 30-60 min
5. P0-D2/D3 instrument.compact_kline_summary      # 30 min
6. P0-E1/E2 极端场景跑一遍 + 补 regression tests  # 60 min
7. 回跑 CATL 场景，确认 QFII P&L 正常、时间感     # 20 min
   叙事在 report 里可见、K 线数据在 prompt 里可见
8. commit + push
```

预计总工作量：4-6 小时真实编码 + 20 min LLM 跑 run 的成本。

**验收标准总清单**（执行完之后必须通过）：
- [ ] A1 schedule fix commit
- [ ] B1 QFII P&L 在单调上涨场景 > 0
- [ ] C1 report 里能看到叙事性时间描述（不只是时间戳）
- [ ] C2 grep agent 决策理由，"T+1 / 明日 / 隔夜" 词频 > 3 次
- [ ] C3 report 里能看到 session 标签
- [ ] D1 run_one.py 跑 CATL 能自动构建 4+ 标的 universe
- [ ] D2 agent prompt 里能 grep 到 K 线统计块（波动率 / 均线 / 换手率等）
- [ ] D3 event_subject 下面能看到最近 5 日 OHLCV 表
- [ ] E1 退市场景 report 显示 "跌停" 字样
- [ ] E2 连板场景 2 天都触发 LIMIT_UP
- [ ] 所有相关单测绿 (`tests/test_limit_board.py`,
      `tests/test_calibration_backtest.py`, `tests/test_round_schedule.py`)

---

**最后注意**：`pending_experiments` 在 `REVIEW_STATE.json` 里已经是空，但
这份 TODO 执行完后，跑一轮 `/auto-review-loop` 让 Codex 再锐评一次，看分数
从 6.9 能推到多少。目标 8.0/10。
