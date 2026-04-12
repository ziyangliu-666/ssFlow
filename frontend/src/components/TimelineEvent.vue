<template>
  <article class="t-ev" :class="evClass">
    <div class="marker">
      <span>{{ markerGlyph }}</span>
    </div>
    <div class="body">
      <div class="body-h">
        <span class="t-type">{{ kindLabel }}</span>
        <span v-if="who" class="t-who">{{ who }}</span>
        <span v-if="payload.round_idx !== undefined" class="r-tag">R{{ payload.round_idx }}</span>
        <span class="t-time">{{ time }}</span>
      </div>

      <!-- simulation_start -->
      <div v-if="type === 'simulation_start'" class="t-text">
        <em>{{ payload.n_personas }} 个角色</em> · {{ payload.n_rounds }} 轮 ·
        开盘价 <strong class="mono">{{ formatPrice(payload.initial_price) }}</strong>。
      </div>

      <!-- round_start -->
      <div v-else-if="type === 'round_start'" class="t-text">
        <template v-if="payload.round_label">
          <strong>{{ payload.round_label }}</strong>
        </template>
        <template v-else>
          第 <strong>{{ (payload.round_idx ?? 0) + 1 }}</strong> 轮
        </template>
        开盘 <strong class="mono">{{ formatPrice(payload.current_price) }}</strong>。
        <span v-if="payload.hours_since_event" class="mono subdued">
          (事件后 {{ payload.hours_since_event }}h)
        </span>
      </div>

      <!-- persona_thought -->
      <div v-else-if="type === 'persona_thought'" class="t-text">
        <span v-html="highlightThought(payload.text)"></span>
        <div v-if="payload.content_type && payload.content_type !== 'social_post'" class="meta-line">
          <span class="ctype">{{ payload.content_type }}</span>
        </div>
      </div>

      <!-- trade_submitted -->
      <div v-else-if="type === 'trade_submitted'">
        <!-- Structured freeform order (side/qty/pool) -->
        <div v-if="isFreeformOrder" class="t-text">
          <span class="order-side" :class="orderSideClass">{{ orderSideLabel }}</span>
          <span v-if="orderQtyPct > 0" class="mono order-qty">{{ (orderQtyPct * 100).toFixed(0) }}%</span>
          <span v-if="orderQtyPct > 0" class="order-pool">{{ orderPoolLabel }}</span>
          <span v-if="payload.instrument" class="mono instrument-tag">{{ payload.instrument }}</span>
        </div>
        <!-- Legacy distribution -->
        <div v-else>
          <div class="t-text">
            下单分布<span v-if="payload.instrument" class="mono instrument-tag"> · {{ payload.instrument }}</span>：
          </div>
          <ul class="dist">
            <li v-for="(v, k) in payload.distribution || {}" :key="k">
              <span class="action-name">{{ k }}</span>
              <span class="action-bar">
                <span class="bar-fill" :style="{ width: barWidth(v) }"></span>
              </span>
              <span class="action-pct mono">{{ (v * 100).toFixed(0) }}%</span>
            </li>
          </ul>
        </div>
        <div v-if="payload.rationale" class="rationale">
          <em>"{{ payload.rationale }}"</em>
        </div>
      </div>

      <!-- class_flow_computed -->
      <div v-else-if="type === 'class_flow_computed'" class="t-text flow-line">
        净流入
        <strong class="mono" :class="flowClass(payload.net_flow)">
          {{ formatFlow(payload.net_flow) }}
        </strong>
        · {{ payload.n_agents }} 人
        <span v-if="payload.held" class="held-chip">持仓不动</span>
      </div>

      <!-- price_updated -->
      <div v-else-if="type === 'price_updated'" class="t-text">
        <div class="price-line mono">
          <span class="before">{{ formatPrice(payload.price_before) }}</span>
          <span class="arrow">→</span>
          <strong :class="priceDeltaClass(payload.delta_pct)">{{ formatPrice(payload.price_after) }}</strong>
          <span :class="['delta', priceDeltaClass(payload.delta_pct)]">
            {{ payload.delta_pct >= 0 ? '+' : '' }}{{ (payload.delta_pct * 100).toFixed(2) }}%
          </span>
        </div>
        <div class="meta-line mono">总净流入 {{ formatFlow(payload.net_flow_total) }}</div>
      </div>

      <!-- round_complete -->
      <div v-else-if="type === 'round_complete'" class="t-text subdued">
        第 {{ (payload.round_idx ?? 0) + 1 }} 轮结束 · {{ payload.publications_count }} 篇发布 · {{ payload.orders_count }} 笔订单
      </div>

      <!-- simulation_complete / simulation_done — per-ticker distribution
           if the Run/Replay view attached one, else a neutral time-only line -->
      <div v-else-if="type === 'simulation_complete' || type === 'simulation_done'" class="t-text">
        <strong>推演结束。</strong>
        <span class="sim-done-meta mono">
          <span v-if="payload.elapsed_seconds">{{ payload.elapsed_seconds.toFixed(1) }}s</span>
        </span>
        <div
          v-if="Array.isArray(payload.final_prices_by_ticker) && payload.final_prices_by_ticker.length"
          class="per-ticker-dist"
        >
          <div
            v-for="row in payload.final_prices_by_ticker"
            :key="'fpt-' + row.ticker"
            class="fpt-row"
            :class="{ primary: row.isPrimary }"
          >
            <span class="fpt-name">{{ row.name }}</span>
            <span class="fpt-ticker mono">{{ row.ticker }}</span>
            <span
              class="fpt-pct mono"
              :class="row.pct > 0 ? 'good' : row.pct < 0 ? 'bad' : ''"
            >
              {{ (row.pct >= 0 ? '+' : '') + (row.pct * 100).toFixed(2) + '%' }}
            </span>
          </div>
        </div>
      </div>

      <!-- resource_flow_executed -->
      <div v-else-if="type === 'resource_flow_executed'" class="t-text subdued">
        {{ payload.source_name }} → {{ payload.target_name }}:
        <strong>{{ payload.resource_type }}</strong> × {{ typeof payload.amount === 'number' ? payload.amount.toFixed(1) : payload.amount }}
        <span v-if="payload.label" class="ctype">{{ payload.label }}</span>
      </div>

      <!-- threshold_fired -->
      <div v-else-if="type === 'threshold_fired'" class="t-text threshold-line">
        <span class="threshold-marker">⚡</span>
        <strong>{{ payload.entity_name }}</strong>:
        {{ payload.description }}
        <div v-if="payload.effect_event_text" class="meta-line">
          效果: {{ payload.effect_event_text }}
        </div>
      </div>

      <!-- force_action_override -->
      <div v-else-if="type === 'force_action_override'" class="t-text force-line">
        <span class="force-marker">&#x26A0;</span>
        <strong>{{ payload.persona_id }}</strong>
        <span class="force-badge">强制{{ payload.forced_side === 'sell' ? '卖出' : '买入' }} {{ Math.round((payload.forced_quantity_pct || 0) * 100) }}%</span>
        <span class="force-reason">{{ payload.reason }}</span>
        <span v-if="payload.replaced_llm_order" class="replaced-tag">(覆盖LLM决策)</span>
      </div>

      <!-- policy_fired -->
      <div v-else-if="type === 'policy_fired'" class="t-text force-line">
        <span class="force-marker">&#x26A1;</span>
        <strong>{{ payload.agent_name || payload.agent_id }}</strong>
        <span class="force-reason">{{ payload.description }}</span>
        <span v-if="payload.forced_side" class="force-badge">
          {{ payload.forced_side === 'sell' ? '强制卖出' : '强制买入' }} {{ Math.round((payload.forced_quantity_pct || 0) * 100) }}%
        </span>
        <span v-if="payload.replaced_llm_order" class="replaced-tag">(覆盖LLM)</span>
      </div>

      <!-- agent_action (non-trader domain actions) -->
      <div v-else-if="type === 'agent_action'" class="t-text">
        <strong>{{ payload.persona_id }}</strong>
        <span class="ctype">{{ payload.action_type }}</span>:
        {{ payload.text }}
      </div>

      <!-- entity_state_updated (usually filtered out, shown in panel) -->
      <div v-else-if="type === 'entity_state_updated'" class="t-text subdued">
        {{ payload.entity_name }} 状态更新
      </div>

      <!-- error -->
      <div v-else-if="type === 'error'" class="t-text bad">
        <strong v-if="payload.code">{{ payload.code }}</strong>
        <template v-if="payload.code && payload.detail"> · </template>
        <span v-if="payload.detail">{{ payload.detail }}</span>
        <span v-else-if="!payload.code">未知错误（无详情）</span>
      </div>

      <!-- Fallback -->
      <pre v-else class="t-text raw">{{ payload }}</pre>
    </div>
  </article>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  type: { type: String, required: true },
  payload: { type: Object, default: () => ({}) },
})

const KIND_MAP = {
  simulation_start: '推演开始',
  round_start: '轮次开始',
  persona_thought: '想法',
  trade_submitted: '交易',
  class_flow_computed: '分组流向',
  price_updated: '价格更新',
  round_complete: '轮次结束',
  simulation_complete: '推演结束',
  simulation_done: '报告就绪',
  external_event_injected: '外部事件',
  resource_flow_executed: '资源流转',
  threshold_fired: '阈值触发',
  force_action_override: '强制约束',
  policy_fired: '规则触发',
  agent_action: '行动',
  entity_state_updated: '实体状态',
  error: '错误',
}

const kindLabel = computed(() => KIND_MAP[props.type] || props.type)

const evClass = computed(() => {
  const m = {
    persona_thought: 'thought',
    trade_submitted: 'trade',
    class_flow_computed: 'flow',
    price_updated: 'price',
    round_start: 'round',
    round_complete: 'round-end',
    simulation_start: 'sim-start',
    simulation_complete: 'sim-end',
    simulation_done: 'sim-end',
    external_event_injected: 'ext',
    resource_flow_executed: 'flow',
    threshold_fired: 'threshold',
    force_action_override: 'force',
    policy_fired: 'force',
    entity_state_updated: 'entity',
    error: 'error',
  }
  return m[props.type] || 'misc'
})

const markerGlyph = computed(() => {
  if (props.type === 'round_start' || props.type === 'round_complete') return 'R' + (props.payload.round_idx ?? '')
  if (props.type === 'persona_thought') return '◆'
  if (props.type === 'trade_submitted') return '◆'
  if (props.type === 'class_flow_computed') return '·'
  if (props.type === 'price_updated') return '$'
  if (props.type === 'threshold_fired') return '⚡'
  if (props.type === 'force_action_override') return '⚠'
  if (props.type === 'policy_fired') return '⚡'
  if (props.type === 'resource_flow_executed') return '→'
  if (props.type === 'entity_state_updated') return '◇'
  if (props.type === 'error') return '!'
  if (props.type === 'simulation_start' || props.type === 'simulation_complete' || props.type === 'simulation_done') return '★'
  return '·'
})

const who = computed(() => {
  if (props.type === 'persona_thought' || props.type === 'trade_submitted' || props.type === 'class_flow_computed') {
    return props.payload.archetype || props.payload.persona_id || ''
  }
  if (props.type === 'threshold_fired' || props.type === 'entity_state_updated') {
    return props.payload.entity_name || ''
  }
  if (props.type === 'force_action_override') {
    return props.payload.persona_id || ''
  }
  if (props.type === 'policy_fired') {
    return props.payload.agent_name || props.payload.agent_id || ''
  }
  if (props.type === 'resource_flow_executed') {
    return props.payload.source_name || ''
  }
  return ''
})

// Freeform order detection + display
const isFreeformOrder = computed(() => {
  const d = props.payload?.distribution
  return d && 'side' in d && 'quantity_pct' in d
})
const orderSide = computed(() => props.payload?.distribution?.side || 'hold')
const orderQtyPct = computed(() => props.payload?.distribution?.quantity_pct || 0)
const orderSideLabel = computed(() => {
  const m = { buy: '买入', sell: '卖出', hold: '观望' }
  return m[orderSide.value] || orderSide.value
})
const orderSideClass = computed(() => {
  const m = { buy: 'good', sell: 'bad', hold: '' }
  return m[orderSide.value] || ''
})
const orderPoolLabel = computed(() => {
  const pool = props.payload?.distribution?.pool || ''
  if (pool === 'cash') return '可用资金'
  if (pool === 'holdings_in_target') return '持仓'
  return pool
})

const time = computed(() => {
  const ts = props.payload?.ts
  if (!ts) return ''
  const d = new Date(ts * 1000)
  return d.toTimeString().slice(0, 8)
})

// Highlight key action verbs inline inside persona thoughts with the signature
// Fraunces italic orange accent. Keep the list small and load-bearing.
const HIGHLIGHT_RE = /(割肉止损|加仓|减仓|做空|做多|止盈|追涨|抄底|空仓|清仓|持有不动|离场)/g
function highlightThought (txt) {
  if (!txt) return ''
  const escaped = txt.replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]))
  return escaped.replace(HIGHLIGHT_RE, '<span class="hl">$1</span>')
}

function formatPrice (v) {
  if (typeof v !== 'number') return v
  return v.toFixed(2)
}
function formatFlow (v) {
  if (typeof v !== 'number') return v
  if (Math.abs(v) > 1e8) return (v / 1e8).toFixed(2) + '亿'
  if (Math.abs(v) > 1e4) return (v / 1e4).toFixed(2) + '万'
  return v.toFixed(0)
}
function barWidth (v) {
  return Math.max(0, Math.min(100, v * 100)) + '%'
}
function flowClass (v) {
  if (typeof v !== 'number') return ''
  return v > 0 ? 'good' : v < 0 ? 'bad' : ''
}
function priceDeltaClass (v) {
  if (typeof v !== 'number') return ''
  return v > 0 ? 'good' : v < 0 ? 'bad' : ''
}
</script>

<style scoped>
.t-ev {
  display: flex;
  gap: 16px;
  margin-bottom: 18px;
  position: relative;
}

.marker {
  width: 26px;
  height: 26px;
  border-radius: 50%;
  background: #fff;
  border: 1px solid var(--ss-line-strong);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-family: 'JetBrains Mono', monospace;
  font-size: 9px;
  color: var(--ss-fg-muted);
  flex-shrink: 0;
  z-index: 1;
}
.t-ev.trade .marker {
  background: var(--ss-accent);
  border-color: var(--ss-accent);
  color: #fff;
}
.t-ev.thought .marker {
  background: var(--ss-bg-soft);
  border-color: var(--ss-fg-muted);
  color: var(--ss-fg);
}
.t-ev.price .marker {
  background: var(--ss-fg);
  border-color: var(--ss-fg);
  color: #fff;
}
.t-ev.round .marker, .t-ev.round-end .marker {
  background: var(--ss-bg-soft);
  border-color: var(--ss-line-strong);
  font-weight: 600;
}

/* Round-start events are section headers, not regular rows. They get a
   horizontal rule across the full feed width so visually the reader
   sees "new round starts here" at a glance. */
.t-ev.round {
  margin-top: 22px;
  padding-top: 12px;
  border-top: 1px dashed var(--ss-line-strong);
}
.t-ev.round :deep(.body-h) {
  font-size: 13px;
  font-weight: 600;
  color: var(--ss-fg);
}
.t-ev.round :deep(.t-type) {
  font-family: 'Fraunces', serif;
  font-style: italic;
}
.t-ev.round :deep(.t-text) {
  font-size: 12px;
  color: var(--ss-fg);
}
/* First round in the feed shouldn't have a rule above it (nothing to
   separate from). */
.t-ev.round:first-child,
.t-ev.sim-start + .t-ev.round {
  margin-top: 6px;
  border-top: 0;
  padding-top: 0;
}

/* Engine breadcrumb events (flow / entity / force / threshold) are
   secondary signals — downweight them visually so they don't compete
   with persona thoughts and trades even when their filter chip is on. */
.t-ev.flow,
.t-ev.entity {
  opacity: 0.7;
}
.t-ev.flow :deep(.t-text),
.t-ev.entity :deep(.t-text) {
  font-size: 11px;
}
.t-ev.sim-start .marker, .t-ev.sim-end .marker {
  background: #fff;
  border-color: var(--ss-accent);
  color: var(--ss-accent);
}
.t-ev.error .marker {
  background: var(--ss-bad);
  border-color: var(--ss-bad);
  color: #fff;
}
.t-ev.flow .marker {
  background: #fff;
  color: var(--ss-fg-faint);
}
.t-ev.threshold .marker {
  background: var(--ss-accent);
  border-color: var(--ss-accent);
  color: #fff;
}
.t-ev.entity .marker {
  background: #fff;
  color: var(--ss-fg-faint);
}
.threshold-marker {
  color: var(--ss-accent);
  margin-right: 4px;
}
.threshold-line {
  border-left: 2px solid var(--ss-line-strong);
  padding-left: 10px;
}
.force-marker {
  color: var(--ss-bad);
  margin-right: 4px;
}
.force-line {
  border-left: 3px solid var(--ss-bad);
  padding-left: 10px;
  background: rgba(196, 60, 60, 0.04);
}
.force-badge {
  display: inline-block;
  background: var(--ss-bad);
  color: #fff;
  font-size: 0.78em;
  font-weight: 600;
  padding: 1px 6px;
  border-radius: 3px;
  margin: 0 6px;
}
.force-reason {
  color: var(--ss-fg-faint);
  font-size: 0.88em;
}
.replaced-tag {
  color: var(--ss-bad);
  font-size: 0.78em;
  font-style: italic;
}

.body { flex: 1; min-width: 0; }

.body-h {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-bottom: 4px;
  flex-wrap: wrap;
}
.t-type {
  font-family: 'Fraunces', serif;
  font-style: italic;
  font-size: 13px;
  color: var(--ss-fg);
}
.t-who {
  font-size: 12px;
  color: var(--ss-fg-muted);
}
.r-tag {
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  color: var(--ss-fg-faint);
  background: var(--ss-bg-soft);
  padding: 1px 6px;
  border-radius: 3px;
}
.t-time {
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  color: var(--ss-fg-faint);
  margin-left: auto;
}

.t-text {
  font-size: 13px;
  color: var(--ss-fg);
  line-height: 1.65;
}
.t-text.subdued { color: var(--ss-fg-muted); font-size: 12px; }
.t-text.bad { color: var(--ss-bad); }
.t-text.raw { white-space: pre-wrap; font-family: monospace; font-size: 11px; color: var(--ss-fg-muted); }

.t-text :deep(.hl),
.t-text .hl {
  font-family: 'Fraunces', serif;
  font-style: italic;
  color: var(--ss-accent);
}

.meta-line {
  margin-top: 4px;
  font-size: 10px;
  color: var(--ss-fg-faint);
}

/* Simulation-complete per-ticker distribution */
.sim-done-meta {
  font-size: 10px;
  color: var(--ss-fg-faint);
  margin-left: 6px;
}
.per-ticker-dist {
  margin-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 8px 10px;
  background: var(--ss-bg-soft, #f7f7f7);
  border-radius: 6px;
  border-left: 2px solid var(--ss-line-strong);
}
.fpt-row {
  display: grid;
  grid-template-columns: 1fr auto 68px;
  gap: 10px;
  align-items: baseline;
  font-size: 11px;
}
.fpt-row.primary {
  font-weight: 600;
}
.fpt-row.primary .fpt-name {
  color: var(--ss-accent);
}
.fpt-name {
  font-family: 'Noto Serif SC', serif;
  color: var(--ss-fg);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.fpt-ticker {
  font-size: 9px;
  color: var(--ss-fg-faint);
}
.fpt-pct {
  text-align: right;
  font-size: 11px;
  color: var(--ss-fg-muted);
}
.fpt-pct.good { color: var(--ss-good); }
.fpt-pct.bad  { color: var(--ss-bad); }
.ctype {
  display: inline-block;
  padding: 1px 6px;
  border: 1px solid var(--ss-line);
  border-radius: 3px;
  font-size: 9px;
  margin-right: 6px;
  color: var(--ss-fg-muted);
}
.instrument-tag {
  font-size: 11px;
  color: var(--ss-accent);
}

/* Trade distribution */
.dist {
  list-style: none;
  margin: 8px 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.dist li {
  display: grid;
  grid-template-columns: 96px 1fr 44px;
  gap: 8px;
  align-items: center;
  font-size: 11px;
}
.action-name { color: var(--ss-fg); font-family: 'Inter', sans-serif; }
.action-bar {
  height: 4px;
  background: var(--ss-line);
  border-radius: 2px;
  overflow: hidden;
}
.bar-fill { display: block; height: 100%; background: var(--ss-accent); }
.action-pct {
  text-align: right;
  font-variant-numeric: tabular-nums;
  color: var(--ss-fg-muted);
}
/* Structured freeform order display */
.order-side {
  font-weight: 600;
  font-size: 13px;
  margin-right: 4px;
}
.order-side.good { color: var(--ss-good); }
.order-side.bad  { color: var(--ss-bad); }
.order-qty {
  font-size: 13px;
  font-weight: 500;
  color: var(--ss-fg);
  margin-right: 2px;
}
.order-pool {
  font-size: 12px;
  color: var(--ss-fg-muted);
  margin-right: 6px;
}

.rationale {
  margin-top: 6px;
  font-size: 12px;
  color: var(--ss-fg-muted);
  border-left: 2px solid var(--ss-line-strong);
  padding-left: 10px;
}
.rationale em {
  font-family: 'Noto Serif SC', serif;
  font-style: normal;
}

/* Price line */
.price-line {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 13px;
}
.price-line .before { color: var(--ss-fg-muted); }
.price-line .arrow { color: var(--ss-fg-faint); }
.price-line strong.good { color: var(--ss-good); }
.price-line strong.bad  { color: var(--ss-bad); }
.delta {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 3px;
  font-weight: 600;
}
.delta.good { background: #e7f5ec; color: var(--ss-good); }
.delta.bad  { background: #fbecec; color: var(--ss-bad); }

.flow-line .good { color: var(--ss-good); }
.flow-line .bad  { color: var(--ss-bad); }
.held-chip {
  margin-left: 6px;
  padding: 1px 6px;
  font-size: 9px;
  background: var(--ss-bg-soft);
  color: var(--ss-fg-muted);
  border-radius: 3px;
  font-family: 'JetBrains Mono', monospace;
}

.mono {
  font-family: 'JetBrains Mono', monospace;
  font-variant-numeric: tabular-nums;
}

.good { color: var(--ss-good); }
.bad  { color: var(--ss-bad); }
</style>
