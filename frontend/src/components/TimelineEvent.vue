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
        <em>{{ payload.n_personas }} personas</em>, {{ payload.n_rounds }} rounds,
        initial price <strong class="mono">{{ formatPrice(payload.initial_price) }}</strong>.
      </div>

      <!-- round_start -->
      <div v-else-if="type === 'round_start'" class="t-text">
        Round <strong>{{ payload.round_idx }}</strong> open at
        <strong class="mono">{{ formatPrice(payload.current_price) }}</strong>.
      </div>

      <!-- persona_thought -->
      <div v-else-if="type === 'persona_thought'" class="t-text">
        <span v-html="highlightThought(payload.text)"></span>
        <div class="meta-line">
          <span v-if="payload.content_type" class="ctype">{{ payload.content_type }}</span>
          <span class="mono">❤ {{ payload.likes || 0 }} · ↻ {{ payload.reposts || 0 }}</span>
        </div>
      </div>

      <!-- trade_submitted -->
      <div v-else-if="type === 'trade_submitted'">
        <div class="t-text">Submits orders across actions:</div>
        <ul class="dist">
          <li v-for="(v, k) in payload.distribution || {}" :key="k">
            <span class="action-name">{{ k }}</span>
            <span class="action-bar">
              <span class="bar-fill" :style="{ width: barWidth(v) }"></span>
            </span>
            <span class="action-pct mono">{{ (v * 100).toFixed(0) }}%</span>
          </li>
        </ul>
        <div v-if="payload.rationale" class="rationale">
          <em>“{{ payload.rationale }}”</em>
        </div>
      </div>

      <!-- class_flow_computed -->
      <div v-else-if="type === 'class_flow_computed'" class="t-text flow-line">
        net flow
        <strong class="mono" :class="flowClass(payload.net_flow)">
          {{ formatFlow(payload.net_flow) }}
        </strong>
        across {{ payload.n_agents }} agents
        <span v-if="payload.held" class="held-chip">HELD</span>
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
        <div class="meta-line mono">net flow {{ formatFlow(payload.net_flow_total) }}</div>
      </div>

      <!-- round_complete -->
      <div v-else-if="type === 'round_complete'" class="t-text subdued">
        Round {{ payload.round_idx }} done · {{ payload.publications_count }} pubs · {{ payload.orders_count }} orders
      </div>

      <!-- simulation_complete / simulation_done -->
      <div v-else-if="type === 'simulation_complete' || type === 'simulation_done'" class="t-text">
        <strong>Simulation complete.</strong>
        <div class="meta-line mono">
          {{ formatPrice(payload.initial_price) }} → {{ formatPrice(payload.final_price) }}
          ({{ payload.cumulative_delta_pct !== undefined ? (payload.cumulative_delta_pct * 100).toFixed(2) : '?' }}%)
          · {{ payload.elapsed_seconds ? payload.elapsed_seconds.toFixed(1) + 's' : '' }}
        </div>
      </div>

      <!-- error -->
      <div v-else-if="type === 'error'" class="t-text bad">
        <strong>{{ payload.code }}</strong> — {{ payload.detail }}
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
  simulation_start: 'Simulation start',
  round_start: 'Round start',
  persona_thought: 'Thought',
  trade_submitted: 'Trade',
  class_flow_computed: 'Class flow',
  price_updated: 'Price updated',
  round_complete: 'Round complete',
  simulation_complete: 'Complete',
  simulation_done: 'Report ready',
  external_event_injected: 'External event',
  error: 'Error',
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
  if (props.type === 'error') return '!'
  if (props.type === 'simulation_start' || props.type === 'simulation_complete' || props.type === 'simulation_done') return '★'
  return '·'
})

const who = computed(() => {
  if (props.type === 'persona_thought' || props.type === 'trade_submitted' || props.type === 'class_flow_computed') {
    return props.payload.archetype || props.payload.persona_id || ''
  }
  return ''
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
.ctype {
  display: inline-block;
  padding: 1px 6px;
  border: 1px solid var(--ss-line);
  border-radius: 3px;
  font-size: 9px;
  margin-right: 6px;
  color: var(--ss-fg-muted);
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
