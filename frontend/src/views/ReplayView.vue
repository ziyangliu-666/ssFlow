<template>
  <div class="replay">
    <WorkflowRail />

    <main class="layout">
      <!-- LEFT: live state panel, same layout as the run view -->
      <section class="live">
        <div class="page-h">
          <h1>
            重看这次<span class="accent-verb">推演</span><span class="period">。</span>
          </h1>
          <div class="meta">
            {{ eventSummary }} ·
            <span class="mono">{{ totalRounds || '—' }} 轮</span> ·
            <span class="mono">sim {{ shorten(simulationId, 14) }}</span>
            <span v-if="source === 'reconstructed'" class="partial-tag">
              部分数据
            </span>
          </div>
        </div>

        <!-- Price block -->
        <div class="price-block" v-if="totalRounds">
          <div class="price-row">
            <div>
              <div class="k">现价</div>
              <div class="price-now mono" :class="cumulativeClass">
                {{ formatPrice(currentPrice) }}
              </div>
            </div>
            <div>
              <div class="k">较开盘</div>
              <div class="price-delta mono" :class="cumulativeClass">
                {{ formatPct(cumulativePct) }}
              </div>
            </div>
            <div style="margin-left: auto; text-align: right;">
              <div class="k">开盘</div>
              <div class="mono" style="font-size: 13px;">
                {{ formatPrice(initialPrice) }}
              </div>
            </div>
          </div>

          <div class="chart-wrap">
            <PriceChart :prices="fullTrajectory" />
          </div>

          <!-- round scrub bar: click any R pill to jump, or use play -->
          <div class="scrub-bars">
            <button
              v-for="i in totalRounds"
              :key="'sb' + (i - 1)"
              type="button"
              class="scrub-bar"
              :class="barClass(i - 1)"
              :title="'跳到 R' + (i - 1)"
              @click="seekToRound(i - 1)"
            />
          </div>
          <div class="scrub-labels">
            <button
              v-for="i in totalRounds"
              :key="'sl' + (i - 1)"
              type="button"
              class="scrub-lbl mono"
              :class="barClass(i - 1)"
              @click="seekToRound(i - 1)"
            >R{{ i - 1 }}</button>
          </div>
        </div>

        <!-- Per-class flow (whatever was in flight at the current cursor) -->
        <div class="flows-h">
          <span class="t">分组净流入</span>
          <span class="r mono">本轮</span>
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
        <div v-else class="flow-empty">（暂无流向）</div>

        <!-- Tiny meta -->
        <div class="tiny-meta mono">
          <span><em>事件</em>{{ visibleEvents.length }}</span>
          <span><em>当前</em>R{{ currentRound }}</span>
          <span><em>速度</em>{{ speed }}×</span>
        </div>
      </section>

      <!-- RIGHT: timeline with playback controls -->
      <section class="timeline-wrap">
        <div class="control-bar">
          <button
            type="button"
            class="ctl"
            :title="isPlaying ? '暂停' : '播放'"
            @click="togglePlay"
          >
            <span v-if="!isPlaying">▶</span>
            <span v-else>⏸</span>
          </button>
          <button
            type="button"
            class="ctl"
            title="回到开头"
            @click="seekToStart"
          >⏮</button>
          <button
            type="button"
            class="ctl"
            title="跳到结尾"
            @click="seekToEnd"
          >⏭</button>
          <div class="sep" />
          <span class="spd-lbl">速度</span>
          <button
            v-for="s in speeds"
            :key="s"
            type="button"
            class="spd"
            :class="{ on: speed === s }"
            @click="speed = s"
          >{{ s === 999 ? '瞬间' : s + '×' }}</button>
          <div class="spacer" />
          <button
            type="button"
            class="view-report-btn"
            @click="goReport"
          >查看报告 →</button>
        </div>

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
          <div v-if="loading" class="timeline-empty">
            <span class="dot"></span>
            正在加载时间线 …
          </div>
          <div v-if="errorMsg" class="timeline-error">
            <div class="te-title">无法加载这次推演。</div>
            <div class="te-body mono">{{ errorMsg }}</div>
            <button class="te-btn" type="button" @click="load">↻ 重试</button>
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
import { http } from '../api/client'
import WorkflowRail from '../components/WorkflowRail.vue'
import PriceChart from '../components/PriceChart.vue'
import TimelineEvent from '../components/TimelineEvent.vue'

const props = defineProps({ simulationId: { type: String, required: true } })
const router = useRouter()

// Full timeline as returned by /simulation/:id/timeline — all events,
// in order. The replay cursor walks through this list at the selected
// speed, pushing events one at a time into the visibleEvents feed.
const allEvents = ref([])
const cursor = ref(0) // index of the NEXT event to emit
const source = ref('full') // "full" or "reconstructed"

const events = ref([]) // events emitted so far (mirror of cursor)
const phase = ref('idle') // idle | playing | paused | done
const isPlaying = ref(false)
const speed = ref(2) // 1, 2, 4, 999 (instant)
const speeds = [1, 2, 4, 999]

const initialPrice = ref(0)
const currentPrice = ref(0)
const totalRounds = ref(0)
const currentRound = ref(0)
const priceTrajectory = ref([])
// Full price trajectory built once at load time from allEvents — fed
// to PriceChart so the chart x-axis stays stable across playback and
// scrubs. The progressive priceTrajectory above is no longer used by
// the chart in replay (it would rescale every tick and look wrong).
const fullTrajectory = ref([])
// Metadata extracted from the loaded timeline's simulation_start event,
// used as a fallback for the meta line when there's no live
// session.eventProposal (e.g. direct-link visits, refreshes).
const simMeta = ref(null)
const latestFlows = ref({})

// Per-round bucketing: built once when the timeline loads, lets the
// round-scrub pills jump to the exact event index that starts each
// round (round_start event for round R is the anchor).
const roundAnchors = ref([]) // array of event indices keyed by round_idx

const loading = ref(true)
const errorMsg = ref('')

const filterTypes = ref([
  { label: '想法', value: 'persona_thought', on: true },
  { label: '交易', value: 'trade_submitted', on: true },
  { label: '流向', value: 'class_flow_computed', on: false },
  { label: '价格', value: 'price_updated', on: true },
  { label: '轮次', value: 'round_start,round_complete', on: true },
])

const visibleEvents = computed(() => {
  const enabled = new Set()
  for (const t of filterTypes.value) {
    if (t.on) for (const v of t.value.split(',')) enabled.add(v.trim())
  }
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

const eventSummary = computed(() => {
  // Prefer the live session state from the just-extracted event; if
  // empty (direct-link visit, page refresh), fall back to whatever
  // metadata the loaded timeline carries in simulation_start.
  const live = session.eventProposal && Object.keys(session.eventProposal).length
    ? session.eventProposal
    : null
  const e = live || simMeta.value || {}
  const parts = [e.instrument || e.ticker, e.market].filter(Boolean)
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

function applyEvent (e) {
  // Mirror of SimulationRunView.handleEvent, minus the done-redirect.
  const type = e.type
  const payload = { ...e }
  payload._type = type
  payload._key = events.value.length + ':' + type + ':' + Math.random().toString(36).slice(2, 6)
  events.value.push(payload)
  nextTick(() => {
    if (feedEl.value) feedEl.value.scrollTop = feedEl.value.scrollHeight
  })
  switch (type) {
    case 'simulation_start':
      initialPrice.value = payload.initial_price || 0
      currentPrice.value = payload.initial_price || 0
      totalRounds.value = payload.n_rounds || 0
      priceTrajectory.value = [payload.initial_price || 0]
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
      break
  }
}

function resetPlaybackState () {
  events.value = []
  cursor.value = 0
  initialPrice.value = 0
  currentPrice.value = 0
  totalRounds.value = 0
  currentRound.value = 0
  priceTrajectory.value = []
  latestFlows.value = {}
  phase.value = 'idle'
}

function buildRoundAnchors () {
  const anchors = []
  allEvents.value.forEach((e, i) => {
    if (e.type === 'round_start') {
      const r = e.round_idx || 0
      anchors[r] = i
    }
  })
  roundAnchors.value = anchors
}

// Walk allEvents once to extract every published price (initial + each
// round close). The chart consumes this fixed array so its x-axis is
// stable regardless of where playback is paused or scrubbed to.
function buildFullTrajectory () {
  const traj = []
  for (const e of allEvents.value) {
    if (e.type === 'simulation_start' && typeof e.initial_price === 'number') {
      traj.push(e.initial_price)
    } else if (e.type === 'price_updated' && typeof e.price_after === 'number') {
      traj.push(e.price_after)
    } else if (e.type === 'simulation_complete' && Array.isArray(e.price_trajectory) && e.price_trajectory.length > traj.length) {
      // Prefer the explicit complete-event trajectory if it's richer
      // than what we accumulated (some reconstructed timelines only
      // emit it via simulation_complete).
      return e.price_trajectory.slice()
    }
  }
  return traj
}

// ── Playback control ──────────────────────────────────────────────
let playTimer = null

function stepOne () {
  if (cursor.value >= allEvents.value.length) {
    stopPlayback()
    phase.value = 'done'
    return false
  }
  applyEvent(allEvents.value[cursor.value])
  cursor.value += 1
  return true
}

function tickInterval () {
  // Each tick emits 1 event. Speed controls the delay between ticks.
  // 1× ≈ 500ms, 2× ≈ 250ms, 4× ≈ 120ms, 999 = instant (drain all).
  if (speed.value === 999) return 0
  return 500 / speed.value
}

function startPlayback () {
  if (isPlaying.value) return
  if (cursor.value >= allEvents.value.length) {
    // Already at end — rewind first so "play" means "play from start"
    seekToStart()
  }
  isPlaying.value = true
  phase.value = 'playing'
  const tick = () => {
    if (!isPlaying.value) return
    if (speed.value === 999) {
      // Instant: drain everything remaining.
      while (stepOne()) { /* no-op */ }
      stopPlayback()
      return
    }
    const ok = stepOne()
    if (!ok) return
    playTimer = setTimeout(tick, tickInterval())
  }
  tick()
}

function stopPlayback () {
  isPlaying.value = false
  if (playTimer) {
    clearTimeout(playTimer)
    playTimer = null
  }
  if (cursor.value >= allEvents.value.length) phase.value = 'done'
  else phase.value = 'paused'
}

function togglePlay () {
  if (isPlaying.value) stopPlayback()
  else startPlayback()
}

function seekToStart () {
  stopPlayback()
  resetPlaybackState()
}

function seekToEnd () {
  stopPlayback()
  // Drain whatever is left synchronously.
  while (cursor.value < allEvents.value.length) {
    applyEvent(allEvents.value[cursor.value])
    cursor.value += 1
  }
  phase.value = 'done'
}

function seekToRound (roundIdx) {
  stopPlayback()
  // Rewind and re-play up to (but not including) the target round's
  // round_start event, so the panel shows the state AT the start of
  // that round. If the target is past the end, jump to end.
  const anchors = roundAnchors.value
  const target = anchors[roundIdx]
  if (target === undefined) {
    seekToEnd()
    return
  }
  resetPlaybackState()
  while (cursor.value < target) {
    applyEvent(allEvents.value[cursor.value])
    cursor.value += 1
  }
  // Also play the round_start for the target round so the user
  // immediately sees the round header in the feed.
  if (cursor.value < allEvents.value.length) {
    applyEvent(allEvents.value[cursor.value])
    cursor.value += 1
  }
  phase.value = 'paused'
}

// ── Data fetch ────────────────────────────────────────────────────
async function load () {
  loading.value = true
  errorMsg.value = ''
  resetPlaybackState()
  try {
    const r = await http.get(`/simulation/${encodeURIComponent(props.simulationId)}/timeline`)
    const data = r.data || {}
    allEvents.value = Array.isArray(data.events) ? data.events : []
    source.value = data.source || 'full'
    totalRounds.value = Number(data.n_rounds || 0)
    buildRoundAnchors()
    fullTrajectory.value = buildFullTrajectory()
    // Cache simulation_start metadata for the meta line so direct-link
    // visits show the actual ticker/instrument instead of just "推演".
    const startEvt = allEvents.value.find((e) => e && e.type === 'simulation_start')
    if (startEvt) {
      simMeta.value = {
        instrument: startEvt.instrument,
        ticker: startEvt.ticker,
        market: startEvt.market,
      }
    }
    // Auto-start on load so the user sees something move immediately.
    // Speed defaults to 2× which is a nice "re-watch" pace.
    nextTick(() => startPlayback())
  } catch (err) {
    errorMsg.value = err?.response?.data?.error || err.message || '加载失败'
  } finally {
    loading.value = false
  }
}

// ── Helpers ───────────────────────────────────────────────────────
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
  router.push({ name: 'Report', params: { simulationId: props.simulationId } })
}

onMounted(() => {
  load()
})

onBeforeUnmount(() => {
  stopPlayback()
})
</script>

<style scoped>
.replay {
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
  .replay { height: auto; min-height: 100vh; flex-direction: column; overflow: visible; }
  .layout { overflow: visible; }
  .live { padding: 20px 16px; max-height: none; }
  .page-h h1 { font-size: 19px; }
  .price-now { font-size: 26px; }
  .filter-bar, .control-bar { padding: 10px 16px; }
  .timeline { padding: 16px 16px 40px; }
}

/* LIVE PANEL — identical vocabulary to SimulationRunView */
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
.partial-tag {
  margin-left: 6px;
  padding: 1px 6px;
  font-size: 9px;
  background: var(--ss-accent-soft);
  color: var(--ss-accent);
  border-radius: 3px;
  font-family: 'JetBrains Mono', monospace;
}

/* Price block — same styles as the run view */
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

/* Scrub bar — clickable version of the run view's rnd-bars */
.scrub-bars {
  display: flex;
  gap: 4px;
  margin-bottom: 4px;
}
.scrub-bar {
  flex: 1;
  height: 6px;
  background: var(--ss-line);
  border: 0;
  border-radius: 2px;
  padding: 0;
  cursor: pointer;
  transition: background 0.15s ease;
}
.scrub-bar:hover { background: var(--ss-fg-muted); }
.scrub-bar.done { background: var(--ss-fg); }
.scrub-bar.live {
  background: var(--ss-accent);
  animation: scrub-pulse 1.4s infinite;
}
@keyframes scrub-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.6; }
}
.scrub-labels {
  display: flex;
  gap: 4px;
}
.scrub-lbl {
  flex: 1;
  text-align: center;
  font-size: 9px;
  color: var(--ss-fg-faint);
  background: none;
  border: 0;
  cursor: pointer;
  font-family: 'JetBrains Mono', monospace;
  padding: 2px 0;
}
.scrub-lbl:hover { color: var(--ss-fg); }
.scrub-lbl.done { color: var(--ss-fg); }
.scrub-lbl.live { color: var(--ss-accent); font-weight: 600; }

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

/* TIMELINE PANEL */
.timeline-wrap {
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.control-bar {
  padding: 12px 28px;
  border-bottom: 1px solid var(--ss-line);
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  background: #fff;
}
.control-bar .ctl {
  min-width: 44px;
  min-height: 44px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--ss-line-strong);
  background: #fff;
  color: var(--ss-fg);
  border-radius: 8px;
  cursor: pointer;
  font-size: 13px;
  font-family: inherit;
  padding: 0;
}
.control-bar .ctl:hover {
  border-color: var(--ss-accent);
  color: var(--ss-accent);
}
.control-bar .sep {
  width: 1px;
  height: 20px;
  background: var(--ss-line);
  margin: 0 4px;
}
.control-bar .spd-lbl {
  font-family: 'Fraunces', serif;
  font-style: italic;
  font-size: 12px;
  color: var(--ss-fg-muted);
  margin-right: 4px;
}
.control-bar .spd {
  font-size: 11px;
  padding: 4px 10px;
  border-radius: 999px;
  border: 1px solid var(--ss-line);
  background: #fff;
  color: var(--ss-fg-muted);
  cursor: pointer;
  font-family: inherit;
  font-variant-numeric: tabular-nums;
}
.control-bar .spd.on {
  background: var(--ss-fg);
  color: #fff;
  border-color: var(--ss-fg);
}
.control-bar .spacer { flex: 1; }
.view-report-btn {
  font-size: 11px;
  padding: 6px 12px;
  border: 1px solid var(--ss-fg);
  background: #fff;
  color: var(--ss-fg);
  border-radius: 6px;
  cursor: pointer;
  font-family: inherit;
}
.view-report-btn:hover {
  background: var(--ss-fg);
  color: #fff;
}

.filter-bar {
  padding: 10px 28px;
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

.timeline-error {
  padding: 24px 4px;
  max-width: 420px;
}
.timeline-error .te-title {
  font-family: 'Noto Serif SC', serif;
  font-size: 16px;
  font-weight: 500;
  color: var(--ss-fg);
  margin-bottom: 8px;
}
.timeline-error .te-body {
  font-size: 11px;
  color: var(--ss-bad);
  margin-bottom: 14px;
  word-break: break-word;
}
.timeline-error .te-btn {
  padding: 8px 14px;
  background: var(--ss-fg);
  color: #fff;
  border: 0;
  border-radius: 6px;
  font: 500 12px 'Inter', sans-serif;
  cursor: pointer;
}
.timeline-error .te-btn:hover { background: var(--ss-accent); }

/* enter/leave transitions */
.ev-enter-from { opacity: 0; transform: translateY(6px); }
.ev-enter-active { transition: opacity 0.25s ease, transform 0.25s ease; }
.ev-leave-active { transition: opacity 0.15s ease; }
.ev-leave-to { opacity: 0; }
</style>
