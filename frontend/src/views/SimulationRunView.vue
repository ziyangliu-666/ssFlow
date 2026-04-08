<template>
  <div class="run">
    <WorkflowRail />

    <main class="layout">
      <!-- LEFT: live state panel (no card stack) -->
      <section class="live">
        <div class="page-h">
          <h1>Watching it <em>ripple</em>.</h1>
          <div class="meta">
            {{ eventSummary }} ·
            <span class="mono">{{ totalRounds || '—' }} rounds</span> ·
            <span class="mono">stream {{ shorten(streamId, 12) }}</span>
          </div>
        </div>

        <!-- Price block -->
        <div class="price-block">
          <div class="price-row">
            <div>
              <div class="k">CURRENT</div>
              <div class="price-now mono" :class="cumulativeClass">{{ formatPrice(currentPrice) }}</div>
            </div>
            <div>
              <div class="k">Δ FROM OPEN</div>
              <div class="price-delta mono" :class="cumulativeClass">{{ formatPct(cumulativePct) }}</div>
            </div>
            <div style="margin-left: auto; text-align: right;">
              <div class="k">OPEN</div>
              <div class="mono" style="font-size: 13px;">{{ formatPrice(initialPrice) }}</div>
            </div>
          </div>

          <div class="chart-wrap">
            <PriceChart :prices="priceTrajectory" />
          </div>

          <!-- rounds as chart x-axis -->
          <div class="rnd-bars" v-if="totalRounds">
            <div
              v-for="i in totalRounds"
              :key="'b' + i"
              class="rnd-bar"
              :class="barClass(i - 1)"
            />
          </div>
          <div class="rnd-labels" v-if="totalRounds">
            <div
              v-for="i in totalRounds"
              :key="'l' + i"
              class="rnd-lbl mono"
              :class="barClass(i - 1)"
            >R{{ i - 1 }}</div>
          </div>
        </div>

        <!-- Per-class flow -->
        <div class="flows-h">
          <span class="t">Per-class flow</span>
          <span class="r mono">last round</span>
        </div>
        <div v-if="flowEntries.length" class="flow-list">
          <div v-for="f in flowEntries" :key="f.personaId" class="flow">
            <span class="name">{{ f.persona }}</span>
            <span class="bar" :class="f.cls">
              <div :style="{ width: f.w }"></div>
            </span>
            <span class="val mono" :class="f.cls">{{ f.display }}</span>
          </div>
        </div>
        <div v-else class="flow-empty">(no orders yet)</div>

        <!-- Tiny meta -->
        <div class="tiny-meta mono">
          <span><em>events</em>{{ events.length }}</span>
          <span><em>elapsed</em>{{ elapsedDisplay }}</span>
        </div>
      </section>

      <!-- RIGHT: timeline -->
      <section class="timeline-wrap">
        <div class="filter-bar">
          <span class="lbl">Show</span>
          <button
            v-for="t in filterTypes"
            :key="t.label"
            type="button"
            class="filter-chip"
            :class="{ on: t.on }"
            @click="t.on = !t.on"
          >{{ t.label }}</button>
          <div class="spacer"></div>
          <button
            v-if="phase === 'done'"
            type="button"
            class="view-report"
            @click="goReport"
          >VIEW REPORT →</button>
        </div>

        <div ref="feedEl" class="timeline">
          <transition-group name="ev">
            <TimelineEvent
              v-for="(e, idx) in visibleEvents"
              :key="e._key || idx"
              :type="e._type"
              :payload="e"
            />
          </transition-group>
          <div v-if="!events.length && phase !== 'error'" class="timeline-empty">
            <span class="dot"></span>
            connecting to <span class="mono">/simulate-stream</span> …
          </div>
        </div>
      </section>
    </main>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { session } from '../store/session'
import WorkflowRail from '../components/WorkflowRail.vue'
import PriceChart from '../components/PriceChart.vue'
import TimelineEvent from '../components/TimelineEvent.vue'
import { openSimulationStream } from '../api/simulate'

const props = defineProps({ streamId: { type: String, required: true } })
const router = useRouter()

const events = ref([])
const phase = ref('connecting') // connecting | running | done | error
const initialPrice = ref(0)
const currentPrice = ref(0)
const totalRounds = ref(0)
const currentRound = ref(0)
const priceTrajectory = ref([])
const elapsedSeconds = ref(0)
const startTime = ref(null)
const sse = ref(null)
const finalSimId = ref('')
const latestFlows = ref({})
const tickInterval = ref(null)

const filterTypes = ref([
  { label: 'Thoughts', value: 'persona_thought', on: true },
  { label: 'Trades', value: 'trade_submitted', on: true },
  { label: 'Flows', value: 'class_flow_computed', on: false },
  { label: 'Price', value: 'price_updated', on: true },
  { label: 'Rounds', value: 'round_start,round_complete', on: true },
])

const visibleEvents = computed(() => {
  const enabled = new Set()
  for (const t of filterTypes.value) {
    if (t.on) for (const v of t.value.split(',')) enabled.add(v.trim())
  }
  // Terminal + meta events always show.
  enabled.add('simulation_start')
  enabled.add('simulation_complete')
  enabled.add('simulation_done')
  enabled.add('error')
  return events.value.filter((e) => enabled.has(e._type))
})

const cumulativePct = computed(() => {
  if (!initialPrice.value || !currentPrice.value) return 0
  return currentPrice.value / initialPrice.value - 1
})
const cumulativeClass = computed(() => {
  if (cumulativePct.value > 0) return 'good'
  if (cumulativePct.value < 0) return 'bad'
  return ''
})

const elapsedDisplay = computed(() => elapsedSeconds.value.toFixed(1) + 's')

const eventSummary = computed(() => {
  const e = session.eventProposal || {}
  const parts = [e.instrument || e.ticker, e.ticker && e.market ? `${e.market}` : null].filter(Boolean)
  if (parts.length === 0) return 'Simulation'
  return parts.join(' · ')
})

const flowEntries = computed(() => {
  const entries = Object.entries(latestFlows.value || {})
  if (!entries.length) return []
  const max = Math.max(...entries.map(([, v]) => Math.abs(v.net_flow || 0))) || 1
  return entries.map(([personaId, v]) => {
    const net = v.net_flow || 0
    const cls = net > 0 ? 'good' : net < 0 ? 'bad' : ''
    const w = ((Math.abs(net) / max) * 100).toFixed(0) + '%'
    const label = v.archetype || personaId
    return {
      persona: shorten(label, 14),
      personaId,
      cls,
      w,
      display: formatFlow(net),
    }
  })
})

const feedEl = ref(null)

function pushEvent (type, payload) {
  payload._type = type
  payload._key = events.value.length + ':' + type + ':' + Math.random().toString(36).slice(2, 6)
  events.value.push(payload)
  nextTick(() => {
    if (feedEl.value) feedEl.value.scrollTop = feedEl.value.scrollHeight
  })
}

function handleEvent (type, payload) {
  pushEvent(type, payload)
  switch (type) {
    case 'simulation_start':
      phase.value = 'running'
      initialPrice.value = payload.initial_price || 0
      currentPrice.value = payload.initial_price || 0
      totalRounds.value = payload.n_rounds || 0
      priceTrajectory.value = [payload.initial_price || 0]
      startTime.value = Date.now()
      break
    case 'round_start':
      currentRound.value = payload.round_idx || 0
      break
    case 'price_updated':
      currentPrice.value = payload.price_after || currentPrice.value
      priceTrajectory.value.push(payload.price_after || 0)
      break
    case 'class_flow_computed':
      latestFlows.value = {
        ...latestFlows.value,
        [payload.persona_id]: {
          archetype: payload.archetype || payload.persona_id,
          net_flow: payload.net_flow,
          held: payload.held,
        },
      }
      break
    case 'round_complete':
      currentRound.value = (payload.round_idx || 0) + 1
      latestFlows.value = {}
      break
    case 'simulation_complete':
      currentPrice.value = payload.final_price || currentPrice.value
      if (payload.price_trajectory) priceTrajectory.value = payload.price_trajectory
      break
    case 'simulation_done':
      phase.value = 'done'
      finalSimId.value = payload.simulation_id || ''
      if (sse.value) sse.value.close()
      setTimeout(() => {
        if (finalSimId.value && phase.value === 'done') {
          router.push({ name: 'Report', params: { simulationId: finalSimId.value } })
        }
      }, 1200)
      break
    case 'error':
      phase.value = 'error'
      if (sse.value) sse.value.close()
      break
  }
}

function barClass (i) {
  if (i < currentRound.value) return 'done'
  if (i === currentRound.value) return 'live'
  return 'pending'
}

function formatPrice (v) {
  if (typeof v !== 'number') return '—'
  return v.toFixed(2)
}
function formatPct (v) {
  if (typeof v !== 'number') return '—'
  return (v >= 0 ? '+' : '') + (v * 100).toFixed(2) + '%'
}
function formatFlow (v) {
  if (typeof v !== 'number') return '—'
  if (Math.abs(v) > 1e8) return (v >= 0 ? '+' : '') + (v / 1e8).toFixed(2) + '亿'
  if (Math.abs(v) > 1e4) return (v >= 0 ? '+' : '') + (v / 1e4).toFixed(2) + '万'
  return (v >= 0 ? '+' : '') + v.toFixed(0)
}
function shorten (s, n = 18) {
  if (!s) return ''
  return s.length > n ? s.slice(0, n - 1) + '…' : s
}
function goReport () {
  if (finalSimId.value) router.push({ name: 'Report', params: { simulationId: finalSimId.value } })
}

onMounted(() => {
  sse.value = openSimulationStream(props.streamId, {
    onEvent: handleEvent,
    onError: () => {
      if (phase.value !== 'done') phase.value = 'error'
    },
  })
  tickInterval.value = setInterval(() => {
    if (startTime.value && phase.value === 'running') {
      elapsedSeconds.value = (Date.now() - startTime.value) / 1000
    }
  }, 200)
})

onBeforeUnmount(() => {
  if (sse.value) sse.value.close()
  if (tickInterval.value) clearInterval(tickInterval.value)
})
</script>

<style scoped>
.run {
  height: 100vh;
  display: flex;
  align-items: stretch;
  overflow: hidden;
}

.layout {
  flex: 1;
  display: grid;
  grid-template-columns: 380px 1fr;
  overflow: hidden;
}
@media (max-width: 1100px) {
  .layout { grid-template-columns: 1fr; }
}

/* LIVE PANEL */
.live {
  padding: 36px 28px;
  border-right: 1px solid var(--ss-line);
  overflow-y: auto;
}

.page-h h1 {
  font-family: 'Noto Serif SC', serif;
  font-size: 22px;
  font-weight: 500;
  margin-bottom: 4px;
}
.page-h h1 em {
  font-family: 'Fraunces', serif;
  font-style: italic;
  color: var(--ss-accent);
}
.page-h .meta {
  font-size: 11px;
  color: var(--ss-fg-muted);
  margin-bottom: 28px;
}

/* Price block */
.price-block { margin-bottom: 28px; }
.price-row {
  display: flex;
  align-items: baseline;
  gap: 20px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--ss-line);
  margin-bottom: 14px;
}
.k {
  font-size: 10px;
  color: var(--ss-fg-faint);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  margin-bottom: 4px;
}
.price-now {
  font-size: 32px;
  font-weight: 500;
  color: var(--ss-fg);
}
.price-now.good { color: var(--ss-good); }
.price-now.bad  { color: var(--ss-bad); }
.price-delta {
  font-size: 14px;
  font-weight: 500;
}
.price-delta.good { color: var(--ss-good); }
.price-delta.bad  { color: var(--ss-bad); }

.chart-wrap { margin-bottom: 10px; }

.rnd-bars {
  display: flex;
  gap: 4px;
  margin-bottom: 4px;
}
.rnd-bar {
  flex: 1;
  height: 3px;
  background: var(--ss-line);
  border-radius: 1px;
}
.rnd-bar.done { background: var(--ss-fg); }
.rnd-bar.live {
  background: var(--ss-accent);
  animation: pulse-bar 1.4s infinite;
}
@keyframes pulse-bar {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.55; }
}
.rnd-labels {
  display: flex;
  gap: 4px;
}
.rnd-lbl {
  flex: 1;
  text-align: center;
  font-size: 9px;
  color: var(--ss-fg-faint);
}
.rnd-lbl.done { color: var(--ss-fg); }
.rnd-lbl.live { color: var(--ss-accent); font-weight: 600; }

/* Flow list */
.flows-h {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-bottom: 10px;
}
.flows-h .t {
  font-family: 'Fraunces', serif;
  font-style: italic;
  font-size: 13px;
  color: var(--ss-fg);
}
.flows-h .r {
  font-size: 10px;
  color: var(--ss-fg-faint);
  margin-left: auto;
}
.flow-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.flow {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 12px;
}
.flow .name {
  flex: 1;
  font-family: 'Inter', sans-serif;
  color: var(--ss-fg);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.flow .bar {
  flex: 0 0 60px;
  height: 4px;
  background: var(--ss-line);
  border-radius: 2px;
  overflow: hidden;
  position: relative;
}
.flow .bar > div {
  position: absolute;
  top: 0;
  bottom: 0;
  background: var(--ss-fg-muted);
}
.flow .bar.good > div { background: var(--ss-good); left: 50%; }
.flow .bar.bad > div  { background: var(--ss-bad); right: 50%; }
.flow .val {
  font-size: 11px;
  font-weight: 600;
  min-width: 56px;
  text-align: right;
}
.flow .val.good { color: var(--ss-good); }
.flow .val.bad  { color: var(--ss-bad); }
.flow-empty {
  font-size: 11px;
  color: var(--ss-fg-faint);
  padding: 6px 0;
}

/* Tiny meta */
.tiny-meta {
  margin-top: 24px;
  padding-top: 14px;
  border-top: 1px solid var(--ss-line);
  display: flex;
  flex-wrap: wrap;
  gap: 14px 20px;
  font-size: 10px;
  color: var(--ss-fg-faint);
}
.tiny-meta em {
  font-family: 'Inter', sans-serif;
  font-style: normal;
  color: var(--ss-fg-muted);
  margin-right: 4px;
}

.mono {
  font-family: 'JetBrains Mono', monospace;
  font-variant-numeric: tabular-nums;
}

/* TIMELINE */
.timeline-wrap {
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.filter-bar {
  padding: 14px 28px;
  border-bottom: 1px solid var(--ss-line);
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  background: #fff;
}
.filter-bar .lbl {
  font-family: 'Fraunces', serif;
  font-style: italic;
  font-size: 12px;
  color: var(--ss-fg-muted);
  margin-right: 4px;
}
.filter-chip {
  font-size: 11px;
  padding: 4px 10px;
  border-radius: 999px;
  border: 1px solid var(--ss-line);
  background: #fff;
  color: var(--ss-fg-muted);
  cursor: pointer;
  font-family: inherit;
}
.filter-chip.on {
  background: var(--ss-fg);
  color: #fff;
  border-color: var(--ss-fg);
}
.spacer { flex: 1; }
.view-report {
  font-size: 11px;
  padding: 6px 12px;
  border: 1px solid var(--ss-fg);
  background: #fff;
  color: var(--ss-fg);
  border-radius: 6px;
  cursor: pointer;
  font-family: inherit;
}
.view-report:hover {
  background: var(--ss-fg);
  color: #fff;
}

.timeline {
  flex: 1;
  overflow-y: auto;
  padding: 20px 28px 40px;
  position: relative;
  background: #fff;
}
.timeline::before {
  content: '';
  position: absolute;
  left: 41px;
  top: 20px;
  bottom: 20px;
  width: 1px;
  background: var(--ss-line);
}

.timeline-empty {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 20px 4px;
  color: var(--ss-fg-muted);
  font-size: 12px;
}
.timeline-empty .dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--ss-accent);
  animation: pulse-dot 1.4s infinite;
}
@keyframes pulse-dot {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.35; }
}

/* enter/leave transitions */
.ev-enter-from { opacity: 0; transform: translateY(6px); }
.ev-enter-active { transition: opacity 0.25s ease, transform 0.25s ease; }
.ev-leave-active { transition: opacity 0.15s ease; }
.ev-leave-to { opacity: 0; }
</style>
