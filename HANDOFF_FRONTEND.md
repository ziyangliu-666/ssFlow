# ssFlow 前端对齐 — Handoff 文档

**目的**：把 Round 5-8 后端新增机制全部接上前端，让后端跑了但前端看不见的 8 件事变成用户能在 UI 里看到、理解、校验的东西。

**执行方式**：另一个 Claude 读完这份文档即可独立完成，**不需要任何人介入**。所有自测必须走 playwright MCP，完成后自己 commit + push。

**基线 commit**：`e8ab9a4` (2026-04-11)

---

## 0. 极速上下文（读完才能开工）

### 0.1 项目是什么

ssFlow 是单标的 A 股事件压力演练引擎。核心流程：用户输入一个市场事件（文字/PDF）→ 事件抽取 + 多标的 Instrument Universe 构建 → 角色选型 + 轮次安排 → OASIS 社交模拟（LLM 扮演散户/机构/媒体）+ 每轮聚合订单流 → Kyle 平方根冲击算新价 + LimitBoard 涨跌停板约束 → 报告。技术栈：Flask + ssflow 包（Python）+ Vue 3 + Vite 前端。

### 0.2 最近 8 轮（R1-R8）发生了什么

自动 review loop 把分数从 4/10 推到 8/10。`AUTO_REVIEW.md` 有完整记录。R5-R8 做的**后端**新增/修复：

1. **P&L 双记账修复** (`trading_layer.py:93-104`)：`holdings_value(scalar)` 之前只看 `_default` ticker，任何非默认 key 被静默丢弃。Single-instrument 模式下初始持仓和订单落到了不同 key 上 → 主动基金在 +12% 涨势里账面 -¥26亿。已修。
2. **叙事化 per-round context** (`round_schedule.py` + `round_context.py` + `oasis_engine.py`)：agents 每轮现在会收到 session 标签 `[盘中·充裕流动性]` / `[尾盘·流动性收敛]` / 情境描述 / A 股 T+1 提醒 / 跨日重置。**新的注入路径**是 `set_round_context()` → user-instruction prepend，因为 OASIS/CAMEL 在 agent init 时把 system prompt 烤死了，写 `user_info.profile` 根本不生效（R1-R4 做的都白做了）。
3. **紧凑 K 线统计 + 5 日 OHLCV** (`instrument.py:132-224`)：`Instrument.compact_kline_summary()` 返回 30 日累计涨跌/波动率/最高最低/MA20/成交额均值/近 5 日走势；`recent_ohlcv_table()` 返回 5 日 OHLCV 表。后端 `prompt_summary()` 里渲染给 agents 看。**前端 `to_serializable` 还没给出结构化字段**。
4. **Distillation 默认走** (`scripts/run_one.py` + `/distill` 路由)：默认构建 3-6 标的 universe + 实时 Sina K 线。
5. **做空工具路由** (`oasis_trading_tool.py:147-220`)：`short_seller` / `active_long_short` / `long_short` / `hedge_fund_short` 这些 role 的 sell 订单默认走 `pool=margin`；其他 role 默认 `holdings_in_target`。显式 `pool=` kwarg 永远赢。
6. **事件严重性解析器** (`event_severity.py`)：新模块。`resolve_event_severity(event_type, event_text, day1_open, current_price) → SeverityResolution(overnight_sentiment, gap_vol, terminal_risk, bull_keyword_match, source)`。覆盖所有 `VALID_EVENT_TYPES`。极端 bear 关键词（退市/立案/造假/\*ST/delisting/fraud/bankruptcy）钳制到 -0.85 并设置 `terminal_risk=True`。`oasis_engine` 的 pre-open auction 用这个。
7. **终局风险级联** (`oasis_engine.py`)：持久化的 `_sim_terminal_risk` flag；被触发后 (a) 每个 trader 的 `set_round_context(terminal_risk_ctx=...)` 注入"基本面已不可交易，不要抄底"的指令文本；(b) 每轮向所有 long-only persona 注入合成强制卖单（normal 板 30%，limit-down 板 40%）绕过 LLM。这是 R8 把 000687 退市场景从 -3.42% 干到 -15.68% 的关键。
8. **Round context 清理** (`round_context.py` + `oasis_engine.py`)：`clear_round_context(agent)` 每轮最开始调一次，防止上一轮的 `conviction_ctx` / `pub_effects_ctx` 泄漏到下一轮。

### 0.3 当前前端对这些改动的感知程度

**完全不感知**：叙事时间上下文、K 线统计结构化字段、严重性解析结果、终局风险 flag、级联汇总、做空 `pool=margin` 显示、session_kind、gap/pre-open auction 诊断。

**后端已发射但前端忽略**（最浪费、最快能修）：`EVENT_ROUND_COMPLETE` payload 里 `limit_board_state` / `fill_rate` / `unfilled_volume` / `t1_blocked` / `seal_strength` 五个字段前端就差 `v-if` 没接。

**前端已经对了的部分**：`kline_30d` 数组在 `Instrument.to_serializable()` 里已经有了，`SetupView.vue` + `ExtractPipeline.vue` 已经画出了 sparkline。`EVENT_FORCE_ACTION_OVERRIDE` 已经在 `TimelineEvent.vue:126-133` 渲染了。

---

## 1. 前端当前状态（视觉基线）

所有基线截图通过 playwright MCP 在 `e8ab9a4` commit 的 checkout 上抓取。目录下 `docs/handoff-baselines/handoff-*.png`。

### 1.1 Home (`frontend/src/views/Home.vue`, 787 行) → `docs/handoff-baselines/handoff-01-home.png`

**路由**：`/`  
**当前呈现**：左侧 `<WorkflowRail>` 竖向流程条（01 开始 / 02 确认抽取 / 03 推演 / 04 报告），右侧主区域是大标题 "一条消息，如何在市场里<em>兑现</em>。" + 拖拽上传区 + 三个示例按钮。

**关键 DOM / ref**（playwright 命名）：
- `heading [level=1]`: "一条消息，如何在市场里兑现。"
- `textbox`: placeholder "描述你想跟踪的市场走势，或拖入研报 / 新闻 / 财报 PDF…"
- `button 添加文件` / `button 开始抽取 →`（默认 disabled，有输入后启用）
- 三个 example 按钮：CATL、白酒 miss、央行降准

**行为**：点示例按钮 → 只填充 textbox（不会自动推进）。必须点 `开始抽取 →` 才会触发 `Home.vue:177 onStart()` → 调 `ensureSession()` → `/extract` → 构建 event proposal → 最终 `router.push('/setup')`。

**当前问题**：没有任何诊断展示位。用户点 "开始抽取" 后不知道 distillation 成功与否（失败会静默 fallback 到 single-instrument，console.warn 一下，用户看不见）。

### 1.2 SetupView (`frontend/src/views/SetupView.vue`, 770 行) → `docs/handoff-baselines/handoff-02-setup-empty.png`

**路由**：`/setup`  
**无上下文时**：显示 "还没有抽取结果。 先从首页抽取一次事件再来推演。" + 回首页按钮。

**有上下文时的布局**（从代码推导）：
1. 顶部 `<ExtractPipeline>` 状态条
2. `<section class="universe-section">` 标的宇宙卡片网格（`SetupView.vue:24-73`）：
   - 每卡显示：`inst-name` / `inst-ticker` / `relLabel(inst.relationship)` / `inst-price`
   - 如果 `inst.kline_30d?.length` → 画 30 px 高的 SVG 折线 sparkline
   - 点击卡片展开 → 显示 `inst-detail`：日均成交额 + 30日高/低 + `inst.financials` 字典项
3. `<section class="entity-section">` 实体关系（可选，template-generated 时常为空）
4. `<section class="schedule-section">` 时间轴：一行 `rd.label` chip，`SetupView.vue:108-113`。**只显示 label**，不显示 `session_kind` / `trading_day_index` / 叙事
5. 事件摘要 textarea
6. Persona 选型
7. 开始推演按钮

**关键组件**：`<PersonaCard>`（`frontend/src/components/PersonaCard.vue`, 260 行）

**当前问题**：
- K 线卡片只有 sparkline + 日均成交额 + 30日高/低。**没有** 累计涨跌 / 波动率 / MA20 / 近 5 日走势字段
- 事件主体没有 5 日 OHLCV 表
- 时间轴 chip 只是 label 字符串，看不到 session 类型 / 叙事
- 没有事件诊断条（terminal_risk / 严重性 source / overnight_sent）
- distillation 失败时只有控制台警告

### 1.3 SimulationRunView (`frontend/src/views/SimulationRunView.vue`, 999 行) → `docs/handoff-baselines/handoff-03-run-empty.png`

**路由**：`/run/:streamId`  
**正常行为**：SSE 流连接到 `/simulate-stream/<stream_id>`，实时推时间轴 + 价格线 + 流向 + 强制事件。

**布局**：
- 左列：事件头 + 价格卡片 + 价格折线 + 分组净流入面板（本轮实时）
- 右列：事件轴 flex scroll，顶部过滤按钮：`想法 / 交易 / 流向 / 价格 / 轮次 / 强制`（`SimulationRunView.vue:286-294`）
- 底部控制：`自动推进` toggle + `查看报告 →` 按钮

**Reducer 逻辑**：`SimulationRunView.vue:292-488` 按事件 `type` 分派状态更新。关键切片：
```js
case 'round_complete':
  currentRound.value = (payload.round_idx || 0) + 1
  latestFlows.value = {}
  // ← limit_board_state / fill_rate / unfilled_volume / t1_blocked / seal_strength 全部丢弃
```
```js
case 'force_action_override':
  // Rendered by TimelineEvent — the forced trade shows in the timeline
  break  // ← 没有汇总指标，没有级联总数
```

**关闭后**（用户访问已完成的 `/run/:id`）：显示 "这条推演 已过去。" + 错误面板。

### 1.4 ReplayView (`frontend/src/views/ReplayView.vue`, 966 行) → `docs/handoff-baselines/handoff-05-replay.png`

**路由**：`/replay/:simulationId`  
**数据源**：`GET /simulation/:id/timeline` → 读 `reports/<sim_id>.events.json`

**布局**（从 `docs/handoff-baselines/handoff-05e-replay-leftcol.png` 看得最清楚）：
- 左列：
  - 标题 "重看这次 <em>推演</em>。"
  - 副标题 "标的 · event_type · N 轮 · sim <hash>"
  - 现价（大字）+ "较开盘" 涨跌百分比 + 开盘价
  - `<PriceChart>` 价格折线图（R0-R5 x 轴）
  - `分组净流入` 面板（本轮）— 我抓的 screenshot 里显示 "(暂无流向)"
  - 底部小字：`事件 134 · 当前 R6 · 速度 2×`
- 右列：
  - 顶部控制栏：`⏸ ⏮ ⏭` + `速度 1×/2×/4×/瞬间` + `查看报告 →`
  - 过滤按钮：`显示 想法 / 交易 / 流向 / 价格 / 轮次`（注意：**没有** 强制按钮，SimulationRunView 有）
  - `<article>` 列表：每条事件一个 `<TimelineEvent>` 卡

**Reducer** (`ReplayView.vue:300-333`) 和 SimulationRunView 几乎重复，也丢弃 `round_complete` 的执行状态字段。

### 1.5 ReportView (`frontend/src/views/ReportView.vue`, 1122 行) → `docs/handoff-baselines/handoff-04-report.png`

**路由**：`/reports/:simulationId`（注意复数 `/reports/`，API 是单数 `/report/:id`）  
**数据源**：`GET /report/:id`

**布局**：
- 左列窄侧栏：4 个指标格（初始价 / 终价 / Δ% / net flow）+ 第二组格子（事件 / 类型 / N 轮 / N 角色）+ 第三组（λ / ADV 等）
- 主区域：渲染后的 markdown `<article>`，包含 H1 "ssFlow Market Simulation Report"、Round 0..N 段、Order flow 表、Per-class P&L 表、Simulation Metadata
- 右上角工具按钮：`+ 新推演 / ▶ 重播推演 / ↓ 下载 .md / 设置`
- 底部免责 "研究工具。... · 成本 $X · 耗时 Ys"

**关键渲染调用**：`v-html="rendered"` 用 marked 把 markdown 展成 HTML。执行状态面板（报告里的 `### 执行状态` 区块）**已经在 markdown 里**并能正常渲染，但 JSON 响应里没有结构化字段，无法用于左侧指标格。

**当前问题**：`summary.per_class_pnl` 只有 `{persona_id, archetype, pnl}` 聚合字段，没有 per-instrument 拆分；左侧指标格没有 terminal_risk / severity / overnight_sent 展示位。

---

## 2. API / 后端字段清单（改动的时候要动这些）

### 2.1 SSE 事件（`src/ssflow/event_bus.py` + `oasis_engine.py` 发射）

| 事件 type | 发射位置 | 已有字段 | **新字段要加** |
|---|---|---|---|
| `simulation_start` | 流开始 | simulation_id, n_personas, n_rounds, initial_price | `severity_resolution: {overnight_sentiment, gap_vol, terminal_risk, source}` |
| `round_start` | `oasis_engine.py:763-769` | simulation_id, round_idx, current_price | `session_kind, trading_day_index, round_label, narrative, is_new_trading_day, prev_day_close_delta_pct` |
| `pre_open_auction` | **新事件**，在 `oasis_engine.py:903` 那段 log 处 | — | `round_idx, prev_close, open_price, gap_pct, board_state, sentiment, source` |
| `terminal_risk_flagged` | **新事件**，在 `oasis_engine.py:842-845` 新增 flag 处 | — | `round_idx, overnight_sentiment, source, keyword_matches` |
| `terminal_risk_cascade` | **新事件**，在 `oasis_engine.py:1511` log 处 | — | `round_idx, forced_persona_count, fraction, board_state` |
| `trade_submitted` | `oasis_engine.py:1596-1606` | persona_id, archetype, distribution{side, quantity_pct, pool}, rationale, instrument | ✅ 已够用。frontend 需要把 `pool=margin` 渲染成 "融券做空" badge |
| `force_action_override` | 已有 | persona_id, forced_side, forced_quantity_pct, reason, replaced_llm_order | **加** `trigger_source: "policy:stop_loss"|"terminal_risk_cascade"|"threshold"` |
| `round_complete` | `oasis_engine.py:1948-1961` | simulation_id, round_idx, publications_count, orders_count, class_flows_count, price_after, net_flow_total, **limit_board_state**, **fill_rate**, **unfilled_volume**, **t1_blocked**, **seal_strength** | 已经全有了！前端就是不读 |

### 2.2 REST 路由（`api/app.py`）

| 路由 | 行号 | 当前返回 | 要加 |
|---|---|---|---|
| `POST /distill` | `app.py:1145-1199` | `{instrument_universe, round_schedule}` | `instrument_universe.instruments[].compact_summary: {cum_return_pct, vol_pct, high, low, ma20, mean_turnover, recent_returns[]}`；`instrument_universe.instruments[].ohlcv_5d[]`（event_subject 专享）；`round_schedule.rounds[].session_kind` 和 `trading_day_index` |
| `GET /report/:id` | `app.py:824-970` | `{simulation_id, markdown, summary, meta}` | `summary.execution_state: {board_state, seal_strength, avg_fill_rate, total_unfilled, total_t1_blocked}`（结构化，不要让前端从 markdown 里 regex）；`summary.terminal_risk_triggered: bool`；`summary.cascade_summary: {rounds_fired, total_forced_sells}` |
| `GET /simulation/:id/timeline` | `app.py:972-1012` | `{simulation_id, n_rounds, n_personas, events[], source}` | 不用改，新事件通过 `event_log.events` 自动持久化 |

### 2.3 `Instrument.to_serializable()`（`src/ssflow/instrument.py:100-118`）

**已有**：`ticker, name, market, relationship, current_price, adv_value, price_currency, financials, kline_30d[], holdings_by_persona, margin_long_balance, margin_short_balance`

**要加**：`compact_summary: {cum_return_pct, vol_pct, high, low, ma20, mean_turnover, recent_returns}` — 从 `compact_kline_summary()` 提取出结构化字段（不要只 return 文本！文本是给 LLM 的）；`ohlcv_5d: [{date, open, high, low, close, volume}]` — event_subject 专享，从 `recent_ohlcv_table()` 的 bar list 直接返回。

### 2.4 `RoundDef.to_serializable()`（`src/ssflow/round_schedule.py:35-43`）

**已有**：`id, label, calendar_start, calendar_end, hours_since_event, active_agent_types[]`

**要加**：`session_kind` 和 `trading_day_index`（已经是 `RoundDef` 的 `@property`，只需要加进 dict）；`narrative: str`（可选，调 `_session_narrative(session_kind, day_idx, is_first_round_of_day)` 预先算好塞进去）

---

## 3. 改进清单（按执行顺序，共 5 个 Batch）

**所有 file:line 均相对 `e8ab9a4` commit。**

### Batch 1 — 后端已发射 / 前端只差 v-if（1 个 session）

最高杠杆：改完这个立刻能看到涨跌停、封单强度、成交率、强制原因，后端完全不动。

#### 1.1 `TimelineEvent.vue` 扩展 `round_complete` 分支

**文件**：`frontend/src/components/TimelineEvent.vue:94-97`

**当前**：
```vue
<div v-else-if="type === 'round_complete'" class="t-text subdued">
  第 {{ (payload.round_idx ?? 0) + 1 }} 轮结束 · {{ payload.publications_count }} 篇发布 · {{ payload.orders_count }} 笔订单
</div>
```

**改成**：增加条件渲染，当 `payload.limit_board_state !== 'normal'` 时高亮显示 "涨停 / 跌停 / 一字" 状态 + 封单强度 + 成交率；`payload.t1_blocked > 0` 时显示 T+1 拦截数。参考样式：已有的 `.force-line` / `.held-chip` 样式可复用。新加一个 `.limit-chip` CSS class。不要破坏现有的基础显示，只做增量。

**加样式**（文件内 `<style scoped>` 段末尾）：
```css
.limit-chip {
  display: inline-block;
  padding: 1px 6px;
  font-size: 9px;
  border-radius: 3px;
  margin-left: 6px;
  font-family: 'JetBrains Mono', monospace;
}
.limit-chip.up      { background: #ffece9; color: #d32f2f; }
.limit-chip.down    { background: #e7f5ec; color: #2e7d32; }
.limit-chip.one-word{ background: #d32f2f; color: #fff; }
.exec-line {
  margin-top: 4px;
  font-size: 10px;
  color: var(--ss-fg-faint);
  font-family: 'JetBrains Mono', monospace;
  display: flex;
  gap: 10px;
}
```

**验收**：在 Replay 页（拿一个有 events.json 的 sim），当某个 `round_complete` 事件有 `limit_board_state="limit_down"` 时，时间轴上该行要显示"跌停 · 封单强度 X.XX · 成交率 Y%"。

#### 1.2 `TimelineEvent.vue` 扩展 `trade_submitted` 识别 `pool=margin`

**文件**：`frontend/src/components/TimelineEvent.vue:274-279`

**当前**：
```js
const orderPoolLabel = computed(() => {
  const pool = props.payload?.distribution?.pool || ''
  if (pool === 'cash') return '可用资金'
  if (pool === 'holdings_in_target') return '持仓'
  return pool
})
```

**改成**：加 `if (pool === 'margin') return '融券做空'`。然后在模板 `:class="orderSideClass"` 的 `<span class="order-side">` 旁边，如果 `orderPool.value === 'margin'` 就额外渲染一个 `<span class="short-badge">融券</span>`。

**验收**：Replay 一个有 short fund 出现的 sim（历史报告可能没有；可以在 Batch 3 跑一个新的）。或者写一个 playwright 单元脚本直接访问 `/replay/:id` 对 force_action_override 事件做可见性检查。

#### 1.3 `TimelineEvent.vue` 扩展 `force_action_override` 展示 `trigger_source`

**前提**：后端 Batch 2 会在 `force_action_override` payload 里加 `trigger_source`。这里前端可以先无条件渲染，显示 payload.trigger_source 文字；后端没发时 fallback 到 reason 字段。

**文件**：`frontend/src/components/TimelineEvent.vue:126-133`

**改动**：在 `<span class="force-reason">` 前加 `<span v-if="payload.trigger_source" class="trigger-chip">{{ triggerLabel(payload.trigger_source) }}</span>`，`triggerLabel` map：
```js
{
  "policy:stop_loss":        "止损触发",
  "policy:profit_take":      "止盈触发",
  "policy:time_exit":        "时限出清",
  "terminal_risk_cascade":   "终局风险",
  "threshold":               "阈值触发",
}
```

**验收**：playwright 在 Replay 页检查，`force_action_override` 事件有 trigger_source 时显示对应中文标签。

---

### Batch 2 — 新事件类型 + 前端时间轴新分支（1-2 个 sessions）

让 R5-R8 的严重性解析 / pre-open auction / terminal cascade 在 UI 上可见。

#### 2.1 后端：新增 `EVENT_SIMULATION_DIAGNOSIS` 事件

**文件**：`src/ssflow/event_bus.py`（事件常量）+ `src/ssflow/oasis_engine.py`（发射）

1. 在 `event_bus.py` 里新增常量 `EVENT_SIMULATION_DIAGNOSIS = "simulation_diagnosis"`；加进 `ALL_EVENT_TYPES` frozenset；`test_event_bus.py:85` 的断言会同步更新（它是已知的 pre-existing failure，修它一起）
2. 在 `oasis_engine.py:765` 第一次调用 `safe_emit(EVENT_ROUND_START)` **之前**，emit 一次 diagnosis：
   ```python
   if round_idx == 0:
       from .event_severity import resolve_event_severity
       _diag_sev = resolve_event_severity(
           event_type=event.event_type or "",
           event_text=event.event_text or "",
           day1_open=getattr(event, "day1_open", None),
           current_price=event.current_price,
       )
       safe_emit(
           event_sink,
           EVENT_SIMULATION_DIAGNOSIS,
           simulation_id=simulation_id,
           event_type=event.event_type,
           overnight_sentiment=float(_diag_sev.overnight_sentiment),
           gap_vol=float(_diag_sev.gap_vol),
           terminal_risk=bool(_diag_sev.terminal_risk),
           bull_keyword_match=bool(_diag_sev.bull_keyword_match),
           source=_diag_sev.source,
       )
   ```

#### 2.2 后端：新增 `EVENT_PRE_OPEN_AUCTION`

**文件**：`src/ssflow/event_bus.py` + `oasis_engine.py:903`

1. 常量 `EVENT_PRE_OPEN_AUCTION = "pre_open_auction"`
2. 把现有 log `R%d PRE-OPEN AUCTION: ...` 换成先 `safe_emit(EVENT_PRE_OPEN_AUCTION, round_idx=round_idx, prev_close=..., open_price=..., gap_pct=..., board_state=limit_board.state.value, sentiment=..., source=...)` 然后保留 log

#### 2.3 后端：新增 `EVENT_TERMINAL_RISK_CASCADE`

**文件**：`src/ssflow/event_bus.py` + `oasis_engine.py:1511`

1. 常量 `EVENT_TERMINAL_RISK_CASCADE`
2. 在现有 log `R%d TERMINAL_RISK cascade: ...` 之前 emit event with `{round_idx, forced_persona_count, fraction, board_state}`

#### 2.4 后端：`force_action_override` payload 加 `trigger_source`

**文件**：`oasis_engine.py` 在所有 emit 处，当前两处：
- 大约 `oasis_engine.py:1493-1507`（policy trade override）— `trigger_source = f"policy:{fire.policy.id}"` 或 `"policy:force_action"`
- 在 terminal cascade 生成的 `PendingOrder` 的 rationale 里已经有 "[强制] 终局风险"，要在对应的 `EVENT_FORCE_ACTION_OVERRIDE` emit（如果有；没有的话新增一次）中传 `trigger_source="terminal_risk_cascade"`

实际上级联注入的是 `PendingOrder`，不会自动产生 `EVENT_FORCE_ACTION_OVERRIDE`。解决办法：在 `oasis_engine.py:1498` 注入 forced_order 的循环内也 `safe_emit(EVENT_FORCE_ACTION_OVERRIDE, persona_id=_p.id, forced_side="sell", forced_quantity_pct=_cascade_frac, reason="终局风险 forced-seller cascade", trigger_source="terminal_risk_cascade", replaced_llm_order=(_p.id in _existing_ids))`

#### 2.5 前端：`TimelineEvent.vue` 新增 4 个分支

**文件**：`frontend/src/components/TimelineEvent.vue`

1. 给 `KIND_MAP` 加：
   ```js
   simulation_diagnosis: '事件诊断',
   pre_open_auction:     '开盘竞价',
   terminal_risk_cascade:'终局级联',
   ```
2. 给 `evClass` map 加对应 CSS class：`diagnosis / pre-open / cascade`
3. 给 `markerGlyph` 加：diagnosis → `◎`, pre_open → `⟡`, cascade → `▽`
4. 在模板里加 4 个 `v-else-if` 分支：

```vue
<div v-else-if="type === 'simulation_diagnosis'" class="t-text diag-line">
  <span class="diag-label">事件诊断</span>
  <span class="mono" :class="sentimentClass(payload.overnight_sentiment)">
    情绪 {{ (payload.overnight_sentiment >= 0 ? '+' : '') + payload.overnight_sentiment.toFixed(2) }}
  </span>
  <span class="mono">gap σ {{ (payload.gap_vol * 100).toFixed(0) }}%</span>
  <span v-if="payload.terminal_risk" class="terminal-chip">终局风险</span>
  <span class="diag-source">来源: {{ payload.source }}</span>
</div>

<div v-else-if="type === 'pre_open_auction'" class="t-text auction-line">
  <span class="auction-marker">开盘竞价</span>
  <span class="mono">{{ formatPrice(payload.prev_close) }} → {{ formatPrice(payload.open_price) }}</span>
  <strong :class="sentimentClass(payload.gap_pct)">
    ({{ payload.gap_pct >= 0 ? '+' : '' }}{{ (payload.gap_pct * 100).toFixed(2) }}%)
  </strong>
  <span class="board-chip" :class="'bs-' + payload.board_state">{{ boardLabel(payload.board_state) }}</span>
</div>

<div v-else-if="type === 'terminal_risk_cascade'" class="t-text cascade-line">
  <span class="cascade-marker">▽</span>
  <strong>终局风险级联</strong>
  <span>强制出清 {{ payload.forced_persona_count }} 家多头</span>
  <span class="mono">@ {{ Math.round(payload.fraction * 100) }}%</span>
  <span class="board-chip" :class="'bs-' + payload.board_state">{{ boardLabel(payload.board_state) }}</span>
</div>
```

加 helper computeds：
```js
function sentimentClass (v) {
  if (typeof v !== 'number') return ''
  if (v <= -0.3) return 'bad'
  if (v >= 0.3) return 'good'
  return ''
}
function boardLabel (state) {
  return {
    normal: '正常',
    limit_up: '涨停',
    limit_down: '跌停',
    one_word_up: '一字涨停',
    one_word_down: '一字跌停',
  }[state] || state
}
```

#### 2.6 前端：`SimulationRunView.vue` + `ReplayView.vue` 过滤按钮加 "诊断" 分类

**文件**：`frontend/src/views/SimulationRunView.vue:286-294` + `frontend/src/views/ReplayView.vue:226-232`

两处都加：
```js
{ label: '诊断', value: 'simulation_diagnosis,pre_open_auction,terminal_risk_cascade', on: true },
```

两处的 `reduce` 函数也要加对应 `case` 不报错（pass through；状态不用记，由 TimelineEvent 自己渲染）。

**验收（Batch 2 全套）**：起 api + vite，跑一个 000687 退市 scenario（或用 `scripts/run_one.py` 的 --event tmp/e2e-delist.txt 那个），等 events.json 落盘，playwright 开 /replay/:id 验证时间轴上能看到"事件诊断 情绪 -0.85 gap σ 25% 终局风险"、"开盘竞价 10.00 → 9.00 (-10.00%) 一字跌停"、"终局风险级联 强制出清 8 家多头 @ 30%" 三种新事件。

---

### Batch 3 — 结构化 K 线字段 + SetupView 增量（1 session）

让用户在设置页就能看到后端喂给 agents 的 30 日统计。

#### 3.1 后端：`Instrument.to_serializable` 加结构化 `compact_summary`

**文件**：`src/ssflow/instrument.py:100-118`

1. 重构：从 `compact_kline_summary()` 中抽出结构化计算（不要返回 text），新增 `def compact_summary_dict(self) -> dict | None`，返回：
   ```python
   {
     "cum_return_pct": 12.6,
     "vol_pct": 1.90,
     "high": 232.30,
     "low": 197.01,
     "ma20": 219.50,
     "mean_turnover_cny": 107.35e8,
     "recent_returns_pct": [-1.33, 2.24, -1.32, 2.22, -1.30],
     "days": 30,
   }
   ```
2. `compact_kline_summary()` 继续返回 text（保留 agent prompt 用法），但内部调用 `compact_summary_dict()` 然后格式化
3. `to_serializable()` 加：
   ```python
   summary = self.compact_summary_dict()
   if summary:
       d["compact_summary"] = summary
   if self.relationship in ("event_subject", "primary"):
       ohlcv = [
           {k: b.get(k) for k in ("date", "open", "high", "low", "close", "volume")}
           for b in self._valid_kline_bars()[-5:]
       ]
       if ohlcv:
           d["ohlcv_5d"] = ohlcv
   ```

#### 3.2 前端：`SetupView.vue` 扩展 instrument 展开面板

**文件**：`frontend/src/views/SetupView.vue:57-70`

**当前**：展开只显示 日均成交额 + 30日高低 + financials

**改成**：读 `inst.compact_summary` 和 `inst.ohlcv_5d`（如果存在）：
```vue
<div v-if="expandedTicker === inst.ticker" class="inst-detail">
  <div v-if="inst.compact_summary" class="detail-group">
    <div class="detail-row">
      <span>30日累计</span>
      <span class="mono" :class="inst.compact_summary.cum_return_pct >= 0 ? 'good' : 'bad'">
        {{ inst.compact_summary.cum_return_pct >= 0 ? '+' : '' }}{{ inst.compact_summary.cum_return_pct.toFixed(2) }}%
      </span>
    </div>
    <div class="detail-row">
      <span>日波动率</span>
      <span class="mono">{{ inst.compact_summary.vol_pct.toFixed(2) }}%/日</span>
    </div>
    <div class="detail-row">
      <span>20日均线</span>
      <span class="mono">{{ inst.compact_summary.ma20.toFixed(2) }}</span>
    </div>
    <div class="detail-row">
      <span>成交额均值</span>
      <span class="mono">{{ formatMoney(inst.compact_summary.mean_turnover_cny) }}</span>
    </div>
    <div class="detail-row">
      <span>近5日走势</span>
      <span class="mono">
        <span v-for="(r, i) in inst.compact_summary.recent_returns_pct" :key="i" :class="r >= 0 ? 'good' : 'bad'">
          {{ r >= 0 ? '+' : '' }}{{ r.toFixed(1) }}%{{ i < inst.compact_summary.recent_returns_pct.length - 1 ? ' / ' : '' }}
        </span>
      </span>
    </div>
  </div>
  <div v-if="inst.ohlcv_5d" class="detail-group">
    <div class="ohlcv-h">最近5日日线</div>
    <table class="ohlcv-table mono">
      <tr><th>日期</th><th>开</th><th>高</th><th>低</th><th>收</th><th>成交额</th></tr>
      <tr v-for="bar in inst.ohlcv_5d" :key="bar.date">
        <td>{{ (bar.date || '').slice(5) }}</td>
        <td>{{ bar.open.toFixed(2) }}</td>
        <td>{{ bar.high.toFixed(2) }}</td>
        <td>{{ bar.low.toFixed(2) }}</td>
        <td>{{ bar.close.toFixed(2) }}</td>
        <td>{{ formatMoney(bar.close * bar.volume) }}</td>
      </tr>
    </table>
  </div>
  <!-- existing detail-row for 日均成交额 / financials still ok -->
</div>
```

加 helper：
```js
function formatMoney(v) {
  if (!v) return '—'
  if (v >= 1e12) return (v / 1e12).toFixed(2) + '万亿'
  if (v >= 1e8) return (v / 1e8).toFixed(2) + '亿'
  if (v >= 1e4) return (v / 1e4).toFixed(1) + '万'
  return v.toFixed(0)
}
```

加 scoped CSS（文件内已有 `.inst-detail` 样式，补充）：
```css
.detail-group { margin-top: 8px; }
.ohlcv-h { font-size: 11px; color: var(--ss-fg-muted); margin-bottom: 4px; }
.ohlcv-table { width: 100%; font-size: 11px; border-collapse: collapse; }
.ohlcv-table th { font-weight: 400; color: var(--ss-fg-faint); text-align: right; padding: 2px 4px; }
.ohlcv-table td { text-align: right; padding: 2px 4px; }
.ohlcv-table tr + tr td { border-top: 1px solid var(--ss-line); }
```

**验收**：playwright 走 Home → 填示例 → 抽取 → 到 Setup → 点开一个 instrument 卡片 → 截图 → 断言截图里包含"30日累计"、"20日均线"、"近5日走势" 文本。

---

### Batch 4 — 会话叙事 + 诊断条 + distillation 状态（1 session）

#### 4.1 后端：`RoundDef.to_serializable` 加 `session_kind` + `trading_day_index` + `narrative`

**文件**：`src/ssflow/round_schedule.py:35-43`

```python
def to_serializable(self) -> dict[str, Any]:
    d = {
        "id": self.id,
        "label": self.label,
        "calendar_start": self.calendar_start,
        "calendar_end": self.calendar_end,
        "hours_since_event": self.hours_since_event,
        "active_agent_types": list(self.active_agent_types),
        "session_kind": self.session_kind,
        "trading_day_index": self.trading_day_index,
    }
    # narrative is derivable on the client from session_kind + day_idx
    # but pre-computing it here lets us keep the logic single-sourced.
    from .round_schedule import _session_narrative
    d["narrative"] = _session_narrative(
        self.session_kind, self.trading_day_index, is_first_round_of_day=True,
    )
    return d
```

注意 `_session_narrative` 是模块级函数（我 R7 写的时候放在那儿了），import 即可。

#### 4.2 前端：`SetupView.vue` round schedule chip 改造

**文件**：`frontend/src/views/SetupView.vue:107-113`

**改成**：每个 round chip 显示 `rd.label` 主文字 + 底下小字 `rd.session_kind`（中文化）。悬浮 tooltip 显示 `rd.narrative`。

```vue
<div
  v-for="(rd, i) in roundSchedule.rounds"
  :key="rd.id"
  class="round-chip-ex"
  :title="rd.narrative"
>
  <div class="chip-label mono">{{ rd.label }}</div>
  <div class="chip-sub">{{ sessionLabel(rd.session_kind) }}</div>
</div>
```

```js
function sessionLabel(kind) {
  return {
    pre_open: '开盘竞价',
    morning: '盘中',
    afternoon: '尾盘',
    whole_day: '全日',
  }[kind] || ''
}
```

#### 4.3 前端：新组件 `<EventDiagnosticBar>` 横贯 SimulationRun / Replay / Report 顶部

**新文件**：`frontend/src/components/EventDiagnosticBar.vue`

props：`{ eventType, overnightSentiment, gapVol, terminalRisk, source, executionSummary }`

显示一行横条：
```
事件类型: regulatory  情绪: -0.85  gap σ: 25%  [终局风险]  来源: severity_map  |  R3 跌停 · 封单 4.59 · 成交率 12%
```

`SimulationRunView.vue` 订阅新的 `simulation_diagnosis` 事件把字段存 `session.simulationDiagnosis`；`ReplayView.vue` 扫 `allEvents` 提取一次；`ReportView.vue` 从 `summary.execution_state` + `meta.event_type` 读。

三个 view 的顶部都插入 `<EventDiagnosticBar>`。

#### 4.4 前端：Home.vue distillation 失败显式 toast

**文件**：`frontend/src/views/Home.vue:378-380`

**当前**：`catch` 静默降级。

**改成**：catch 时设置 `session.distillationFallbackReason = err.message || 'unknown'`；SetupView 顶部加一个 dismissible 黄条，如果 `session.distillationFallbackReason` 非空则显示 "⚠ 未能构建多标的 universe，已回退单标的模式。原因: ..."。

**验收**：一次完整 Home → Setup 走通，Setup 顶部有诊断条；round chip 显示 session 类型；如果 distillation 失败（强行关掉 sina 网络或 mock 失败），Setup 顶部有黄条警告。

---

### Batch 5 — 结构化 execution state + per-instrument P&L（1-2 sessions）

#### 5.1 后端：`/report/:id` 加 `summary.execution_state` + `summary.terminal_risk_triggered` + `summary.cascade_summary`

**文件**：`api/app.py:939-950`（summary dict 构建处）

需要从 events.json 里提取（如果存在）：
```python
# Try to extract execution state from events.json if present
execution_state = None
terminal_risk_triggered = False
cascade_summary = None
events_path = settings.reports_dir / f"{simulation_id}.events.json"
if events_path.exists():
    try:
        data = json.loads(events_path.read_text(encoding="utf-8"))
        evs = data.get("events", [])
        # final round_complete carries the last board state
        for ev in reversed(evs):
            if ev.get("type") == "round_complete":
                execution_state = {
                    "board_state": ev.get("limit_board_state"),
                    "seal_strength": ev.get("seal_strength"),
                    "avg_fill_rate": ev.get("fill_rate"),
                    "total_unfilled": ev.get("unfilled_volume"),
                    "total_t1_blocked": ev.get("t1_blocked"),
                }
                break
        # terminal risk triggered anywhere?
        for ev in evs:
            if ev.get("type") == "simulation_diagnosis" and ev.get("terminal_risk"):
                terminal_risk_triggered = True
                break
        # cascade summary
        cascade_rounds = 0
        total_forced = 0
        for ev in evs:
            if ev.get("type") == "terminal_risk_cascade":
                cascade_rounds += 1
                total_forced += ev.get("forced_persona_count", 0)
        if cascade_rounds > 0:
            cascade_summary = {"rounds_fired": cascade_rounds, "total_forced_sells": total_forced}
    except Exception:
        pass

summary = {
    # ... existing ...
    "execution_state": execution_state,
    "terminal_risk_triggered": terminal_risk_triggered,
    "cascade_summary": cascade_summary,
}
```

#### 5.2 后端：`compute_class_pnl_by_ticker()` + API field

**文件**：`src/ssflow/oasis_engine.py:190-194`（`OasisSimResult.compute_class_pnl`）

新增方法：
```python
def compute_class_pnl_by_ticker(self) -> dict[str, dict[str, float]]:
    """Per-class P&L split by ticker (multi-instrument only).
    Single-instrument runs collapse to {class_id: {event_ticker: pnl}}."""
    out: dict[str, dict[str, float]] = {}
    for class_id, agents in self.final_agents_by_class.items():
        by_ticker: dict[str, float] = {}
        for a in agents:
            for ticker, shares in a.holdings.items():
                if shares != 0:
                    price = self.final_price  # single-instrument fallback
                    by_ticker[ticker] = by_ticker.get(ticker, 0.0) + shares * price
            # Plus cash delta attribution... (more complex, optional for v1)
        if by_ticker:
            out[class_id] = by_ticker
    return out
```

注意：这是 v1 的粗粒度实现——只按最终持仓价值拆分，不做进出场均价归因。完整的归因要等后续 P1 项。

**scorecard** 存一列新的 `class_pnl_by_ticker_json`（`scorecard.py`）：`ALTER TABLE simulations ADD COLUMN class_pnl_by_ticker_json TEXT` + `scorecard.py:insert_sandbox_simulation` 接受并写入。

**API `/report/:id`** 从 scorecard 读取，加进 `summary.per_class_pnl_by_ticker`（可选字段，老行为 null）。

#### 5.3 前端：`ReportView.vue` 左侧指标格扩展

**文件**：`frontend/src/views/ReportView.vue`

**改动**：
1. 左侧指标条的第三行加：
   - `summary.terminal_risk_triggered` → 显示红色 badge "终局风险"
   - `summary.execution_state.board_state` → 显示最终板状态
   - `summary.cascade_summary.rounds_fired` → "级联 N 轮"
2. Per-class P&L 区块加一个 "按标的拆" 可折叠 accordion（如果 `per_class_pnl_by_ticker` 存在）

**验收**：playwright 打开一个新跑的报告页，断言左侧指标条包含执行状态数据。

---

## 4. MCP 自测流程（必须跑完）

**规则**：每个 Batch 完成后必须跑一遍完整 E2E 自测，不通过不要进下一个 Batch。中间断了不需要人介入——直接看 console / playwright 报错自己诊断。

### 4.1 准备

```bash
# 1. 启动后端
cd /home/rufus/ssFlow
uv run python -m flask --app api.app run --host 127.0.0.1 --port 5001 > /tmp/ssflow_api.log 2>&1 &

# 2. 启动前端 dev server
cd /home/rufus/ssFlow/frontend && npm run dev > /tmp/ssflow_vite.log 2>&1 &

# 3. 等 2 秒让它们起来，curl 验证
curl -sS http://127.0.0.1:5001/healthz
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5173
```

password 从 `.env` 读：`SSFLOW_TOKEN` 或 `flask_password` 设置项，当前 dev 值是 `ssflow-dev-2026`（如果改了就用 `uv run python -c "from ssflow.config import settings; print(settings.flask_password.get_secret_value())"` 取）

### 4.2 Playwright session 初始化（每次自测第一步）

```
mcp__playwright__browser_navigate url=http://127.0.0.1:5173/
mcp__playwright__browser_evaluate function=() => { localStorage.setItem('ssflow.password', 'ssflow-dev-2026'); return 'ok'; }
mcp__playwright__browser_navigate url=http://127.0.0.1:5173/
```

### 4.3 基线对比

每个 Batch 的改动前，先对照 `docs/handoff-baselines/handoff-01-home.png` 到 `docs/handoff-baselines/handoff-05e-replay-leftcol.png` 确认视觉起点对齐。改动后再截图，diff 应该只在改动区域。

### 4.4 Batch 1 自测（纯前端，不需要跑新 sim）

用已有的 `oasis_0f3a0c853eec` sim（它同时有 scorecard row + .md + .events.json）：

```
1. mcp__playwright__browser_navigate url=http://127.0.0.1:5173/replay/oasis_0f3a0c853eec
2. mcp__playwright__browser_wait_for text="重看这次" time=5
3. mcp__playwright__browser_snapshot
4. 找 round_complete 事件的 article，抓 innerText
5. mcp__playwright__browser_evaluate function=() => {
     const cards = document.querySelectorAll('article.t-ev.round-end');
     return Array.from(cards).map(c => c.textContent.trim());
   }
6. 断言：至少一张卡 text 里应该看到 "封单" 或 "成交率" 字样（前提是那次 sim 的 round_complete 事件里有非 normal 的 board_state；老 sim 可能都是 normal，那这个 Batch 就换用 Batch 2 跑新 sim 后来验）
7. mcp__playwright__browser_take_screenshot filename=batch1-replay.png fullPage=true
```

**如果老 sim 没有非 normal round** → 跳到 Batch 2 跑 delisting scenario 后来回来验。

### 4.5 Batch 2 自测（需要新 sim）

后端改完后，**用 CLI 而不是 UI** 跑一次退市场景（快 + 不走 LLM 抽取）：

```bash
cd /home/rufus/ssFlow
# 需要先把 tmp/e2e-delist.txt 里的内容准备好（已存在，是 000687 退市场景）
LOG_LEVEL=INFO uv run python scripts/run_one.py \
  --event tmp/e2e-delist.txt \
  --ticker 000687 \
  --event-type regulatory \
  --event-date 2026-04-15 \
  --current-price 10.00 \
  --adv 850000000 \
  --personas personas/ashare.yaml \
  --schedule earnings-3d \
  --no-universe > /tmp/ssflow_delist.log 2>&1
# 成功后从日志取 simulation_id
SIM_ID=$(grep "simulation_id" /tmp/ssflow_delist.log | head -1 | awk '{print $NF}')
echo "SIM_ID=$SIM_ID"
# 检查 events.json 已生成
ls /home/rufus/ssFlow/reports/${SIM_ID}.events.json
```

注意：`scripts/run_one.py` 默认**不写** events.json（只有 API 路径写）。要么（a）改 `run_one.py` 也写 events.json，要么（b）通过 API 跑。最简单：改 `run_one.py` 在 sim 结束后自己 dump 一次：
```python
# 在 run_one.py 的 save_report 之后
events_path = settings.reports_dir / f"{result.simulation_id}.events.json"
events_path.write_text(
    json.dumps({
        "simulation_id": result.simulation_id,
        "n_rounds": result.n_rounds,
        "n_personas": result.n_personas,
        "events": event_log.events,  # ← event_log 需要从 run_simulation 返回
    }),
    encoding="utf-8",
)
```
但这需要 `run_simulation` 返回 event_log；不想动那个就通过 API 走。**推荐**：直接用 API 路径：

```bash
PW="ssflow-dev-2026"
# Step 1: init stream
STREAM_ID=$(curl -sS -X POST http://127.0.0.1:5001/simulate-stream/init \
  -H "X-Auth-Password: $PW" \
  -H "Content-Type: application/json" \
  -d '{"event":{"ticker":"000687","event_type":"regulatory","event_date":"2026-04-15","event_text":"*ST 华讯退市风险...","current_price":10.0,"adv_value":850000000},"personas_path":"personas/ashare.yaml","schedule_preset":"earnings-3d","n_rounds":4}' | jq -r '.stream_id')
echo "stream_id=$STREAM_ID"

# Step 2: open SSE and let it finish
curl -sS -N http://127.0.0.1:5001/simulate-stream/$STREAM_ID > /tmp/ssflow_sse.ndjson &
# Wait ~90 seconds for 4-round completion
# monitor /tmp/ssflow_sse.ndjson for "simulation_done"
```

这条路有点折腾，更靠谱是改 `run_one.py` 自己写 events.json：**直接改**（算在 Batch 2 的 scope 里）：
- `api/app.py:597` 的 `events_path.write_text(json.dumps({..., "events": event_log.events}))` 抽成 `_persist_event_log(sim_id, n_rounds, n_personas, events)` 共享函数放 `api/app.py` 或 `src/ssflow/`
- `run_simulation` signature 改为可选接受 `event_sink`；caller 可以传一个 `ListSink` 进来，跑完后 dump 自己
- `scripts/run_one.py` 传 `ListSink`，结束后写 events.json

Playwright 自测：
```
1. navigate /replay/${SIM_ID}
2. snapshot
3. 找事件轴第一张诊断卡
4. evaluate: return document.querySelector('article.t-ev.diagnosis')?.textContent
5. 断言文本包含 "情绪 -0.85" 和 "终局风险"
6. 找一张 pre-open 事件卡
7. 断言文本包含 "-10" 和 "一字跌停"
8. 找一张 cascade 事件卡
9. 断言文本包含 "强制出清" 和 "终局风险"
10. take_screenshot filename=batch2-replay-events.png fullPage=true
```

### 4.6 Batch 3 自测

```
1. 启动 api + vite（如果没在跑）
2. navigate /
3. 进入 playwright → localStorage auth
4. navigate /
5. click 第一个 example button → click 开始抽取
6. wait for text="标的宇宙" time=60 （distillation 要时间）
7. navigate /setup  (should already be there)
8. click 第一个 instrument 卡片（ref=事件主体那张）
9. evaluate: return document.querySelector('.inst-card.expanded .inst-detail')?.textContent
10. 断言文本包含 "30日累计" "20日均线" "近5日走势"
11. 断言包含 "最近5日日线" 和 "开" "高" "低" "收"
12. take_screenshot filename=batch3-setup-expanded.png fullPage=true
```

### 4.7 Batch 4 自测

```
1. 继续 Batch 3 的 Setup 页面
2. snapshot → 找 schedule-section
3. 检查 round chip 数量 = n_rounds
4. 检查每个 chip 下方有 "盘中" / "尾盘" / "全日" 等小字
5. hover 一个 chip，断言 title 属性包含 narrative 文本
6. 页面顶部应有 <EventDiagnosticBar>
7. take_screenshot filename=batch4-setup-narrative.png fullPage=true
```

### 4.8 Batch 5 自测

```
1. navigate /reports/${退市 SIM_ID from Batch 2}
2. wait for "推演报告"
3. evaluate 左侧指标条：return document.querySelector('.summary-stats')?.textContent
4. 断言包含 "终局风险" 和 "跌停" 字样
5. snapshot → 找 per-class-pnl 区块
6. evaluate: return document.querySelector('[data-section=per-class-pnl]')?.querySelectorAll('.pnl-row').length
7. click 按标的拆折叠触发器
8. take_screenshot filename=batch5-report.png fullPage=true
```

### 4.9 完整回归测试

每个 Batch 通过后跑一次 pytest 确认后端没回归：
```bash
uv run pytest tests/ --tb=line -q --ignore=tests/test_event_bus.py \
  --deselect tests/test_oasis_trading_tool.py::TestMakeSubmitOrderTool::test_tool_call_with_non_dict_falls_back_to_empty 2>&1 | tail -10
# 当前基线：888 passed
# Batch 2 会新增 event 常量，test_event_bus.py 的 frozenset 断言会需要更新
```

Batch 2 改 event_bus 后，`test_event_bus.py` 是 pre-existing 的"unrelated"失败（审核时已经发现的），修它会顺手修好，不要 deselect 它了。

### 4.10 停服

```bash
ps -ef | grep -E "flask|vite" | grep -v grep | awk '{print $2}' | xargs -r kill
```

---

## 5. Commit + Push 协议

**策略**：每个 Batch 一个 commit。每条提交消息遵守现有风格（参考 `git log --oneline -20`）：
- `feat(ui): ...` 纯前端
- `feat(api): ...` API 层
- `feat(engine): ...` 后端引擎
- `feat(events): ...` 新事件类型

**每个 Batch commit 之前必须**：
1. 跑完 4.4-4.8 对应的 playwright 自测 + 截图
2. 跑完 4.9 的 pytest 回归
3. `git status` 确认只有你改的文件
4. `git diff` 自己读一遍

**commit 消息模板**：
```
feat(ui): Batch N — <短标题>

What changed
- <bullet>
- <bullet>

Why
- 后端 R5-R8 新增的 X 机制之前在 UI 完全不可见 / 前端忽略了 Y 事件字段
- 这个 Batch 让 <具体用户可见变化>

Validation
- pytest: 888 passed (or updated count)
- playwright: batch{N}-<page>.png 截图里能看到 <具体断言>
- 浏览器 console 无新 error

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
```

**最后 push**：
```bash
git log --oneline origin/main..HEAD  # 看所有新 commit
git push origin main
```

**不要**：
- 不要 force push
- 不要改 git 配置
- 不要 amend 已 push 的 commit
- 不要把截图 commit 进 repo（它们是 gitignore 之外的，但提交时 `git add` 要明确列文件）
- 不要把 `/tmp/ssflow_*.log` commit

---

## 6. 风险、陷阱、已知坑

1. **test_event_bus.py 的 frozenset 断言**：当前已经 pre-existing 失败一个。Batch 2 加新 event 常量会让这个 test 按新常量更新（顺手修）。
2. **OASIS/CAMEL 系统提示烤死**：不要再尝试写 `user_info.profile` 来传递 per-round 上下文，一定要走 `set_round_context()` → 用户指令前置路径。
3. **events.json 的 simulation_diagnosis 事件**：因为它是 R0 前发射的一次性事件，老的 events.json 里没有。老 sim 报告回退到 `terminal_risk_triggered=False`。不要让 Batch 5 的代码 assume 字段一定存在。
4. **localStorage password**：playwright 自测每次新开页都要设一次，否则 401。
5. **run_one.py 不写 events.json**：要么改它写，要么走 API 路径。推荐改它。
6. **Vite proxy `/report/` 和 `/reports/`**：前者是 API，后者是 SPA route。已经是正确的了，不要动 `vite.config.js`。
7. **distillation 的 LLM 会选错 peer**：CATL 曾被选了贵州茅台当 peer。Batch 4 的诊断条不要让这个变成用户恐慌源——只显示 `relationship` 不做断言。
8. **短仓 P&L 符号**：`apply_action` 里 `pool=margin` 卖出返回 `return -amount`，所以 `class_flow.net_flow < 0` 代表净卖（正常）。前端显示"净流入 -X 万"时不要把它当成"资金流出"——加一个 `short_proceeds` 派生字段更清晰。这是 P2 polish。

---

## 7. 完成定义 (Definition of Done)

Batch 1-5 全部完成 = 以下所有断言成立：

- [ ] Replay 页的 round_complete 事件卡，当 board state 非 normal 时，显示"涨停/跌停/一字" badge + "封单 X.XX · 成交率 Y%"
- [ ] 时间轴有 `simulation_diagnosis` 事件，显示 "情绪 ±X.XX gap σ Y% [终局风险?] 来源: Z"
- [ ] 时间轴有 `pre_open_auction` 事件，显示 "prev → open (gap%) <board>"
- [ ] 时间轴有 `terminal_risk_cascade` 事件，显示 "强制出清 N 家 @ X%"
- [ ] `force_action_override` 事件带 `trigger_source` 显示对应中文标签
- [ ] Setup 页 instrument 卡片展开后显示 "30日累计 / 波动率 / 20日均线 / 成交额均值 / 近5日走势"
- [ ] Setup 页 event_subject instrument 展开后额外显示 "最近5日日线" 表格
- [ ] Setup 页 round chip 显示 session_kind 中文标签 + hover 显示 narrative
- [ ] `<EventDiagnosticBar>` 在 Simulation / Replay / Report 三个页面顶部都显示
- [ ] distillation 失败时 Setup 顶部有黄条提示
- [ ] Report 页左侧指标条显示 `execution_state` + `terminal_risk_triggered` + `cascade_summary`
- [ ] `Instrument.to_serializable` 包含 `compact_summary` + `ohlcv_5d`
- [ ] `RoundDef.to_serializable` 包含 `session_kind` + `trading_day_index` + `narrative`
- [ ] `GET /report/:id` 响应 summary 字段包含 `execution_state` / `terminal_risk_triggered` / `cascade_summary`
- [ ] pytest 888+ 个通过（event_bus 修了之后可能变 890+）
- [ ] playwright 所有 Batch 自测截图都已抓取
- [ ] 每个 Batch 一个 commit，都已 push 到 origin/main
- [ ] 浏览器 console 没有新 error

---

## 8. 如果卡住了怎么办

1. **找不到 DOM ref**：用 `mcp__playwright__browser_snapshot depth=4` 获取更深的 tree
2. **SSE 事件没到前端**：查 api log 确认 `safe_emit` 调用；查 `/simulate-stream/:id` 输出是否 streaming；确认 `event_log.events` 里有记录
3. **新事件 test 报 `unknown event type`**：`event_bus.py:ALL_EVENT_TYPES` 忘记更新
4. **Vue 模板报错**：检查 `v-else-if` 顺序；`v-if` 和 `v-else-if` 之间不能插入普通节点
5. **P&L 数字看起来不对**：参考 `tests/test_integration_smoke.py::TestPnLConservation` 的断言——那是权威基线
6. **playwright 超时**：distillation 要 40-90 秒；用 `wait_for time=120`
7. **测试跑飞**：先 `git stash`，再 `uv run pytest tests/test_integration_smoke.py -x` 确认 baseline 绿，再 stash pop

**紧急回滚**：任何单个 Batch 失败都可以 `git reset --hard HEAD~1` 回到上一个 Batch 的 commit。前几个 Batch 的 commit 是独立的，回滚安全。

---

## 9. 参考资料

- `AUTO_REVIEW.md` — R1-R8 完整评审记录
- `TODO.md` — 之前的改进清单（部分已实施）
- `tests/test_integration_smoke.py` — 后端 invariant 的权威定义
- `tests/test_event_severity.py` — severity resolver 的规则定义
- `src/ssflow/event_severity.py` — 解析器源码
- `src/ssflow/round_context.py` — per-round context stash helpers
- `src/ssflow/oasis_engine.py` — 主引擎（~2000 行，事件发射 + 编排）
- `api/app.py` — Flask API 层（~1300 行）
- `frontend/src/views/*.vue` — 5 个主要页面
- `frontend/src/components/TimelineEvent.vue` — 事件卡渲染（要扩的就是这个）

---

**最后一句话**：所有改动都是 additive（新字段、新事件、新分支），不会破坏现有行为。完成这份 handoff 后 ssFlow R5-R8 的全部新机制在前端都可见，用户可以 visual diff 每个机制的工作情况，而不是依赖读后端 log。
