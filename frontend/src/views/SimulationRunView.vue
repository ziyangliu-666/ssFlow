<template>
  <div class="run">
    <WorkflowRail />

    <main class="layout">
      <!-- LEFT: live state panel (no card stack) -->
      <section class="live">
        <div class="page-h">
          <h1 v-if="phase !== 'error'" aria-label="看它发酵。">
            看它<span class="accent-verb">发酵</span><span class="period">。</span>
          </h1>
          <h1 v-else aria-label="这条推演 stream 已经没了。">
            这条推演 <span class="accent-verb">stream</span> 已经没了<span class="period">。</span>
          </h1>
          <div class="meta">
            {{ eventSummary }} ·
            <span v-if="phase !== 'error'">
              <span class="mono">{{ totalRounds || '—' }} 轮</span> ·
              <span class="mono">stream {{ shorten(streamId, 12) }}</span>
            </span>
            <span v-else class="mono">stream {{ shorten(streamId, 12) }} · 已过期</span>
          </div>
        </div>

        <!-- Error recovery panel — shown instead of the price/flow
             panels when the stream is dead. Gives the user three
             concrete actions instead of a 0.00 price and a blank feed. -->
        <div v-if="phase === 'error'" class="err-panel">
          <p class="err-body">
            simulate-stream 是一次性的。你打开的链接已经被领取过或已经超时
            （TTL 60 秒）。没法在原地重播，但可以：
          </p>
          <div class="err-actions">
            <router-link
              v-if="session.lastSimulationId"
              :to="`/replay/${session.lastSimulationId}`"
              class="err-btn primary"
            >▶ 重播上次推演</router-link>
            <router-link
              v-if="session.lastSimulationId"
              :to="`/reports/${session.lastSimulationId}`"
              class="err-btn"
            >查看上次报告 →</router-link>
            <router-link to="/" class="err-btn">← 返回开始</router-link>
          </div>
        </div>

        <!-- Price block (hidden in error state — see .err-panel) -->
        <div v-if="phase !== 'error'" class="price-block">
          <!-- Per-ticker price grid — all instruments are peers. The
               event_subject gets a subtle orange accent but no size
               privilege. There is no "the price" in multi-instrument
               mode; every ticker stands on its own. -->
          <div v-if="tickerRows.length" class="ticker-grid">
            <div
              v-for="row in tickerRows"
              :key="row.ticker"
              class="ticker-row"
              :class="{ primary: row.isPrimary }"
            >
              <div class="tr-meta">
                <span class="tr-name">{{ row.name }}</span>
                <span class="tr-ticker mono">{{ row.ticker }}</span>
              </div>
              <div class="tr-vals">
                <span class="tr-price mono">{{ formatPrice(row.currentPrice) }}</span>
                <span class="tr-pct mono" :class="row.pctClass">{{ formatPct(row.pct) }}</span>
              </div>
            </div>
          </div>

          <div class="chart-wrap">
            <PriceChart
              :prices="chartPrices"
              :primary-ticker="primaryTicker"
            />
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

        <!-- Per-class flow (hidden in error state) -->
        <template v-if="phase !== 'error'">
          <div class="flows-h">
            <span class="t">分组净流入</span>
            <span class="r mono">上一轮</span>
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
          <div v-else class="flow-empty">（暂无订单）</div>

          <!-- Entity State Panel (Entity Sandbox mode) -->
          <EntityStatePanel />

          <!-- Tiny meta -->
          <div class="tiny-meta mono">
            <span><em>事件</em>{{ events.length }}</span>
            <span><em>耗时</em>{{ elapsedDisplay }}</span>
          </div>
        </template>
      </section>

      <!-- RIGHT: timeline -->
      <section class="timeline-wrap">
        <div class="filter-bar">
          <span class="lbl">显示</span>
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
            type="button"
            class="filter-chip auto-chip"
            :class="{ on: autoProceed }"
            @click="autoProceed = !autoProceed; if (autoProceed && pausedAtRound) flushBuffer()"
          >{{ autoProceed ? '自动' : '手动' }}</button>
        </div>

        <!-- Round pause banner -->
        <div v-if="pausedAtRound && phase === 'running'" class="round-pause-banner">
          <span>第 <strong>{{ currentRound }}</strong> 轮结束</span>
          <span class="spacer"></span>
          <span class="mono buffered" v-if="eventBuffer.length">{{ eventBuffer.length }} 条待显示</span>
          <button type="button" class="proceed-btn" @click="flushBuffer">继续下一轮 →</button>
        </div>

        <!-- Simulation-complete CTA banner. Replaces the old 1.2s
             auto-redirect: the user decides when to move on, and the
             full event feed stays scrollable above it. -->
        <div v-if="phase === 'done'" class="done-banner">
          <div class="db-body">
            <div class="db-title">
              推演完成 · <span class="mono">{{ totalRounds }}</span> 轮
              · <span class="mono">{{ elapsedDisplay }}</span>
            </div>
            <div class="db-sub">{{ eventSummary }} · {{ tickerRows.length }} 标的各自涨跌见左侧</div>
          </div>
          <button
            type="button"
            class="db-primary"
            @click="goReport"
          >查看报告 →</button>
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
            正在连接 <span class="mono">/simulate-stream</span> …
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
import EntityStatePanel from '../components/EntityStatePanel.vue'
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

// Multi-instrument price trajectories: {ticker: [prices]}
const multiPriceTrajectories = ref(null)

// The ticker we mark with the brand accent (event_subject). Derived from
// session.instrumentUniverse when available; otherwise left blank and the
// chart falls back to "first series = primary".
const primaryTicker = computed(() => {
  const iu = session.instrumentUniverse
  if (!iu || !iu.instruments) return ''
  const subject = iu.instruments.find(
    inst => inst.relationship === 'event_subject' || inst.relationship === 'primary'
  )
  return subject ? subject.ticker : (iu.instruments[0]?.ticker || '')
})

// Build a map of ticker → instrument metadata for name lookup.
const instrumentMeta = computed(() => {
  const iu = session.instrumentUniverse
  if (!iu || !iu.instruments) return {}
  const m = {}
  for (const inst of iu.instruments) {
    m[inst.ticker] = inst
  }
  return m
})

// Per-ticker display rows — one per instrument, in document order with
// primary first. Each row stands on its own: there is no "the price"
// scalar anymore.
const tickerRows = computed(() => {
  const mpt = multiPriceTrajectories.value
  const meta = instrumentMeta.value
  const rows = []

  // If we have live multi-ticker trajectories, use them.
  if (mpt && Object.keys(mpt).length > 0) {
    const tickers = Object.keys(mpt)
    // Primary first if present
    tickers.sort((a, b) => {
      if (a === primaryTicker.value) return -1
      if (b === primaryTicker.value) return 1
      return 0
    })
    for (const ticker of tickers) {
      const traj = mpt[ticker] || []
      if (!traj.length) continue
      const initial = traj[0]
      const latest = traj[traj.length - 1]
      const pct = initial > 0 ? latest / initial - 1 : 0
      rows.push({
        ticker,
        name: meta[ticker]?.name || ticker,
        currentPrice: latest,
        pct,
        pctClass: pct > 0 ? 'good' : pct < 0 ? 'bad' : '',
        isPrimary: ticker === primaryTicker.value,
      })
    }
    return rows
  }

  // No live data yet — seed from the universe's opening prices so the
  // grid is populated immediately at mount time instead of appearing
  // after the first price_updated.
  const iu = session.instrumentUniverse
  if (iu && iu.instruments) {
    for (const inst of iu.instruments) {
      if (!inst || !inst.current_price) continue
      rows.push({
        ticker: inst.ticker,
        name: inst.name || inst.ticker,
        currentPrice: inst.current_price,
        pct: 0,
        pctClass: '',
        isPrimary: inst.ticker === primaryTicker.value,
      })
    }
    rows.sort((a, b) => (a.isPrimary ? -1 : b.isPrimary ? 1 : 0))
    return rows
  }

  // Absolute fallback: single-instrument trajectory (used by
  // legacy tests / direct curl smoke tests without a universe).
  if (priceTrajectory.value.length) {
    const traj = priceTrajectory.value
    const init = traj[0] || initialPrice.value
    const latest = traj[traj.length - 1] || currentPrice.value
    const pct = init > 0 ? latest / init - 1 : 0
    rows.push({
      ticker: session.eventProposal?.ticker || 'primary',
      name: session.eventProposal?.instrument || session.eventProposal?.ticker || '推演',
      currentPrice: latest,
      pct,
      pctClass: pct > 0 ? 'good' : pct < 0 ? 'bad' : '',
      isPrimary: true,
    })
  }
  return rows
})

// Chart input: always the multi-series dict when we have one, else the
// single scalar trajectory. The chart itself handles both shapes.
const chartPrices = computed(() => {
  const mpt = multiPriceTrajectories.value
  if (mpt && Object.keys(mpt).length > 0) return mpt
  // Seed from universe openings so the chart has at least one data
  // point at mount time even before any price_updated fires.
  const iu = session.instrumentUniverse
  if (iu && iu.instruments && iu.instruments.length) {
    const seed = {}
    for (const inst of iu.instruments) {
      if (inst && inst.current_price > 0) {
        seed[inst.ticker] = [inst.current_price]
      }
    }
    if (Object.keys(seed).length) return seed
  }
  return priceTrajectory.value
})

// Round-by-round pause: buffer events when paused at a round boundary.
// Default to auto-advance — the SSE events are arriving in real time
// anyway, and "watch it happen live" is the entire point of this view.
// Users who want to pause can flip the 自动/手动 toggle in the filter bar.
const autoProceed = ref(true)
const pausedAtRound = ref(false)      // true when waiting for user click
const eventBuffer = ref([])           // buffered events while paused

// Primary-content filters (想法/交易/价格/轮次) are on by default so the
// user sees what they care about; everything else is engine-detail noise
// and defaults off. Users who want to see the mechanics can flip chips on.
const filterTypes = ref([
  { label: '想法', value: 'persona_thought', on: true },
  { label: '交易', value: 'trade_submitted', on: true },
  { label: '价格', value: 'price_updated', on: true },
  { label: '轮次', value: 'round_start,round_complete', on: true },
  { label: '流向', value: 'class_flow_computed', on: false },
  { label: '阈值', value: 'threshold_fired', on: false },
  { label: '强制', value: 'force_action_override', on: false },
  { label: '规则', value: 'policy_fired', on: false },
  { label: '资源', value: 'resource_flow_executed', on: false },
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
  return events.value.filter((e) => {
    if (!enabled.has(e._type)) return false
    // Hide empty persona thoughts (no text content)
    if (e._type === 'persona_thought' && (!e.text || !e.text.trim())) return false
    // Hide entity_state_updated from timeline (shown in EntityStatePanel)
    if (e._type === 'entity_state_updated') return false
    return true
  })
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
  if (parts.length === 0) return '推演'
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

function flushBuffer () {
  for (const { type, payload } of eventBuffer.value) {
    _processEvent(type, payload)
  }
  eventBuffer.value = []
  pausedAtRound.value = false
}

function handleEvent (type, payload) {
  // If paused at round boundary, buffer all events until user clicks "继续"
  if (pausedAtRound.value) {
    eventBuffer.value.push({ type, payload })
    // Always process terminal events immediately even when paused
    if (type === 'simulation_done' || type === 'simulation_complete' || type === 'error') {
      flushBuffer()
    }
    return
  }
  _processEvent(type, payload)
}

function _processEvent (type, payload) {
  // Before storing terminal events, augment them with a per-ticker
  // breakdown computed from the trajectories we've been building.
  // TimelineEvent reads this instead of the scalar initial/final_price.
  if (type === 'simulation_complete' || type === 'simulation_done') {
    const mpt = multiPriceTrajectories.value
    if (mpt && Object.keys(mpt).length > 0) {
      const byTicker = []
      const meta = instrumentMeta.value
      const pk = primaryTicker.value
      for (const [ticker, traj] of Object.entries(mpt)) {
        if (!traj || !traj.length) continue
        const first = traj[0]
        const last = traj[traj.length - 1]
        const pct = first > 0 ? last / first - 1 : 0
        byTicker.push({
          ticker,
          name: meta[ticker]?.name || ticker,
          initial: first,
          final: last,
          pct,
          isPrimary: ticker === pk,
        })
      }
      // Primary first, rest sorted by |pct| descending.
      byTicker.sort((a, b) => {
        if (a.isPrimary) return -1
        if (b.isPrimary) return 1
        return Math.abs(b.pct) - Math.abs(a.pct)
      })
      payload.final_prices_by_ticker = byTicker
    }
  }
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
      // Multi-instrument: update per-ticker trajectories
      if (payload.prices && typeof payload.prices === 'object') {
        if (!multiPriceTrajectories.value) multiPriceTrajectories.value = {}
        for (const [ticker, price] of Object.entries(payload.prices)) {
          if (!multiPriceTrajectories.value[ticker]) multiPriceTrajectories.value[ticker] = []
          multiPriceTrajectories.value[ticker].push(price)
        }
      }
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
      // Pause here if auto-proceed is off (let user review the round)
      if (!autoProceed.value) {
        pausedAtRound.value = true
      }
      break
    case 'simulation_complete':
      currentPrice.value = payload.final_price || currentPrice.value
      if (payload.price_trajectory) priceTrajectory.value = payload.price_trajectory
      break
    case 'simulation_done':
      phase.value = 'done'
      finalSimId.value = payload.simulation_id || ''
      // Remember the sim id so the rail Report + Simulate steps can
      // navigate to it from any page. The SSE stream is one-shot and
      // can't be re-opened, but the new /replay/:id route backed by
      // reports/<sim_id>.events.json can replay the whole run at any
      // time — the rail now points Simulate at /replay instead of
      // the expired stream.
      if (finalSimId.value) session.lastSimulationId = finalSimId.value
      session.activeStreamId = ''
      if (sse.value) sse.value.close()
      // NOTE: we intentionally do NOT auto-navigate to the report.
      // The user stays on this page with a prominent "查看报告"
      // banner so they can scroll back through the live feed, read
      // the rationales, and click through on their own timing.
      break
    case 'error':
      phase.value = 'error'
      // Dead stream — clear activeStreamId so the rail doesn't keep
      // saying "running…". If we know the last successful simulation
      // id, the user almost certainly wants to see that replay rather
      // than land on a full-page "your link expired" error card, so
      // redirect immediately. Otherwise the in-page recovery panel
      // takes over.
      session.activeStreamId = ''
      if (sse.value) sse.value.close()
      if (session.lastSimulationId) {
        router.replace({ name: 'Replay', params: { simulationId: session.lastSimulationId } })
      }
      break

    // ── Entity State Sandbox events ──
    case 'entity_state_updated':
      session.entityStates = {
        ...session.entityStates,
        [payload.entity_id]: {
          name: payload.entity_name,
          type: payload.entity_type,
          state: payload.state,
          labels: payload.state_labels,
          round: payload.round_idx,
        },
      }
      break
    case 'resource_flow_executed':
      // Rendered by TimelineEvent — no extra state to track
      break
    case 'threshold_fired':
      // Rendered by TimelineEvent — no extra state to track
      break
    case 'force_action_override':
      // Rendered by TimelineEvent — the forced trade shows in the timeline
      break
    case 'policy_fired':
      // Unified policy event — rendered by TimelineEvent
      break
    case 'agent_action':
      // Non-trader domain action — rendered by TimelineEvent
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
      if (phase.value !== 'done') {
        phase.value = 'error'
        session.activeStreamId = ''
        // Same auto-redirect rule as the explicit 'error' event
        // above — if we have a replayable target, skip the
        // error-card view and jump the user straight there.
        if (session.lastSimulationId) {
          router.replace({ name: 'Replay', params: { simulationId: session.lastSimulationId } })
        }
      }
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
  .live {
    border-right: 0;
    border-bottom: 1px solid var(--ss-line);
    max-height: 40vh;
  }
}
@media (max-width: 860px) {
  .run { height: auto; min-height: 100vh; flex-direction: column; overflow: visible; }
  .layout { overflow: visible; }
  .live { padding: 20px 16px; max-height: none; }
  .page-h h1 { font-size: 19px; }
  .price-now { font-size: 26px; }
  .filter-bar { padding: 10px 16px; }
  .timeline { padding: 16px 16px 40px; }
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
.page-h h1 .accent-verb {
  font-family: 'Noto Serif SC', serif;
  font-weight: 600;
  color: var(--ss-accent);
  display: inline-block;
  margin: 0 0.08em;
}
.page-h h1 .period { color: var(--ss-fg-muted); }
.page-h .meta {
  font-size: 11px;
  color: var(--ss-fg-muted);
  margin-bottom: 28px;
}

/* Error recovery panel (replaces the price/flow panels when
   phase === 'error'). Dignified state with concrete actions. */
.err-panel {
  border: 1px solid var(--ss-bad);
  background: #fff;
  padding: 18px 18px 16px;
  border-radius: 8px;
  margin-top: 6px;
}
.err-panel .err-body {
  font-family: 'Noto Serif SC', serif;
  font-size: 13px;
  line-height: 1.7;
  color: var(--ss-fg);
  margin: 0 0 16px;
}
.err-panel .err-actions {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.err-panel .err-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 9px 14px;
  font: 500 12px/1 'Inter', sans-serif;
  border: 1px solid var(--ss-line-strong);
  background: #fff;
  color: var(--ss-fg);
  border-radius: 8px;
  cursor: pointer;
  text-decoration: none;
  justify-content: center;
}
.err-panel .err-btn:hover {
  border-color: var(--ss-accent);
  color: var(--ss-accent);
}
.err-panel .err-btn.primary {
  background: var(--ss-fg);
  color: #fff;
  border-color: var(--ss-fg);
}
.err-panel .err-btn.primary:hover {
  background: var(--ss-accent);
  border-color: var(--ss-accent);
  color: #fff;
}

/* Price block */
.price-block { margin-bottom: 28px; }

/* Per-ticker peer grid — every instrument is an equal citizen. The
   event_subject gets a subtle orange accent (left border + bold name)
   but the same card height/font sizes as everyone else. No scalar
   "THE price" headline. */
.ticker-grid {
  display: flex;
  flex-direction: column;
  gap: 0;
  border-top: 1px solid var(--ss-line);
  margin-bottom: 14px;
}
.ticker-row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 0 10px 10px;
  border-bottom: 1px dashed var(--ss-line);
  border-left: 2px solid transparent;
  transition: background 0.1s;
}
.ticker-row:last-child { border-bottom: 1px solid var(--ss-line); }
.ticker-row.primary {
  border-left-color: var(--ss-accent);
  background: var(--ss-accent-soft);
}
.ticker-row.primary .tr-name {
  font-weight: 600;
  color: var(--ss-fg);
}
.tr-meta {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.tr-name {
  font-family: 'Noto Serif SC', serif;
  font-size: 12px;
  color: var(--ss-fg);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tr-ticker {
  font-size: 9px;
  color: var(--ss-fg-faint);
  letter-spacing: 0.03em;
}
.tr-vals {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 2px;
}
.tr-price {
  font-size: 14px;
  font-weight: 500;
  color: var(--ss-fg);
}
.tr-pct {
  font-size: 11px;
  color: var(--ss-fg-muted);
}
.tr-pct.good { color: var(--ss-good); }
.tr-pct.bad  { color: var(--ss-bad); }

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

.auto-chip {
  margin-left: auto;
}
.auto-chip.on {
  background: var(--ss-fg-muted);
  color: #fff;
  border-color: var(--ss-fg-muted);
}

/* Round pause banner */
.round-pause-banner {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 12px 28px;
  background: #fffbf5;
  border-bottom: 1px solid var(--ss-accent);
  font-size: 13px;
}
.round-pause-banner .buffered {
  font-size: 11px;
  color: var(--ss-fg-faint);
}
.proceed-btn {
  background: var(--ss-fg);
  color: #fff;
  border: 0;
  border-radius: 6px;
  padding: 6px 14px;
  font: 500 12px 'Inter', sans-serif;
  cursor: pointer;
}
.proceed-btn:hover { background: var(--ss-accent); }

/* Done banner — shown once the sim finishes, inside the timeline
   column above the event feed. Replaces the old auto-redirect. */
.done-banner {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 14px 28px;
  background: var(--ss-accent-soft);
  border-bottom: 1px solid var(--ss-accent);
  border-top: 1px solid var(--ss-line);
}
.done-banner .db-body { flex: 1; min-width: 0; }
.done-banner .db-title {
  font-family: 'Noto Serif SC', serif;
  font-size: 14px;
  font-weight: 500;
  color: var(--ss-fg);
  margin-bottom: 2px;
}
.done-banner .db-title .mono {
  font-family: 'JetBrains Mono', monospace;
  font-size: 13px;
  font-weight: 600;
}
.done-banner .db-title .good { color: var(--ss-good); }
.done-banner .db-title .bad { color: var(--ss-bad); }
.done-banner .db-sub {
  font-size: 11px;
  color: var(--ss-fg-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.done-banner .db-primary {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 9px 16px;
  background: var(--ss-fg);
  color: #fff;
  border: 0;
  border-radius: 8px;
  font: 500 13px/1 'Inter', sans-serif;
  cursor: pointer;
  flex-shrink: 0;
}
.done-banner .db-primary:hover { background: var(--ss-accent); }

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
