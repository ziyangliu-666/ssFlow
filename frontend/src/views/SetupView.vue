<template>
  <div class="setup">
    <WorkflowRail />

    <main class="main">
      <div v-if="!session.eventProposal" class="empty">
        <h1>还没有<span class="accent">抽取</span>结果。</h1>
        <p>先回到开始页上传文档并点「开始抽取」。</p>
        <button class="btn" type="button" @click="$router.push('/')">← 返回开始</button>
      </div>

      <div v-else class="wrap">
        <div class="page-h">
          <div>
            <h1>确认要<span class="accent">推演</span>的事件。</h1>
            <p class="sub">
              ssFlow 从你的材料里抽出了这些字段和角色。所有内容<em>可编辑</em>。
              不满意就改，或者添加新的角色。满意就点底部「开始推演」。
            </p>
          </div>
        </div>

        <!-- INSTRUMENT UNIVERSE (if distilled) -->
        <section v-if="instrumentUniverse" class="universe-section">
          <div class="section-h">
            <span class="t">标的宇宙</span>
            <span class="tag">{{ universeInstruments.length }} 个标的</span>
          </div>
          <div class="universe-grid">
            <div
              v-for="inst in universeInstruments"
              :key="inst.ticker"
              class="inst-card"
              :class="{ primary: inst.relationship === 'primary' }"
            >
              <div class="inst-h">
                <span class="inst-name">{{ inst.name }}</span>
                <span class="inst-ticker mono">{{ inst.ticker }}</span>
              </div>
              <div class="inst-meta">
                <span class="inst-rel">{{ relLabel(inst.relationship) }}</span>
                <span class="inst-price mono">{{ inst.price_currency || '¥' }}{{ inst.current_price?.toFixed(2) || '—' }}</span>
              </div>
              <div v-if="inst.kline_30d && inst.kline_30d.length" class="inst-kline">
                <svg :viewBox="`0 0 120 30`" preserveAspectRatio="none" class="mini-chart">
                  <polyline
                    :points="miniKline(inst.kline_30d)"
                    fill="none"
                    :stroke="inst.relationship === 'primary' ? 'var(--ss-accent)' : 'var(--ss-fg-muted)'"
                    stroke-width="1.2"
                    vector-effect="non-scaling-stroke"
                  />
                </svg>
              </div>
            </div>
          </div>
        </section>

        <!-- ENTITY GRAPH (if generated) -->
        <section v-if="entityGraph && entityGraph.entities" class="entity-section">
          <div class="section-h">
            <span class="t">实体沙盘</span>
            <span class="tag">{{ Object.keys(entityGraph.entities).length }} 个实体</span>
          </div>
          <div class="entity-grid">
            <div
              v-for="(ent, eid) in entityGraph.entities"
              :key="eid"
              class="ent-card"
            >
              <div class="ent-h">
                <span class="ent-name">{{ ent.display_name }}</span>
                <span class="ent-type">{{ entTypeLabel(ent.entity_type) }}</span>
              </div>
              <div class="ent-state">
                <div v-for="(val, key) in ent.state" :key="key" class="state-row">
                  <span class="var-label">{{ (ent.state_labels && ent.state_labels[key]) || key }}</span>
                  <span class="var-value mono">{{ typeof val === 'number' ? (val === Math.floor(val) ? val : val.toFixed(2)) : val }}</span>
                </div>
              </div>
            </div>
          </div>
        </section>

        <!-- ROUND SCHEDULE (if generated) -->
        <section v-if="roundSchedule && roundSchedule.rounds" class="schedule-section">
          <div class="section-h">
            <span class="t">时间轴</span>
            <span class="tag">{{ roundSchedule.rounds.length }} 轮</span>
          </div>
          <div class="schedule-row">
            <span
              v-for="(rd, i) in roundSchedule.rounds"
              :key="rd.id"
              class="round-chip mono"
            >{{ rd.label }}</span>
          </div>
        </section>

        <div class="cols">
          <!-- EVENT -->
          <section class="event-col">
            <div class="section-h">
              <span class="t">事件</span>
              <span class="tag">{{ eventTag }}</span>
            </div>
            <EventProposalForm v-model="event" />
          </section>

          <!-- PERSONAS -->
          <section class="persona-col">
            <div class="section-h">
              <span class="t">角色</span>
              <span class="tag">{{ personas.length }} 个</span>
              <button class="add-btn" type="button" @click="addPersona">+ 添加角色</button>
            </div>

            <div class="filter-row">
              <button
                v-for="f in filters"
                :key="f.id"
                type="button"
                class="pill"
                :class="{ active: activeFilter === f.id }"
                @click="activeFilter = f.id"
              >
                {{ f.label }}<span class="n">{{ f.count }}</span>
              </button>
            </div>

            <div v-if="!personas.length" class="persona-empty">
              暂无角色，请添加。
            </div>
            <div v-else class="persona-grid">
              <PersonaCard
                v-for="(p, i) in filteredPersonas"
                :key="p.id || i"
                :persona="p"
                @update:persona="(np) => updatePersona(p, np)"
                @remove="removePersona(p)"
              />
            </div>
          </section>
        </div>
      </div>
    </main>

    <footer v-if="session.eventProposal" class="bottom-bar">
      <div class="foot-meta">
        <span><em>轮数</em></span>
        <input v-model.number="nRounds" type="number" min="1" max="20" class="inp" />
        <span><em>随机种子</em></span>
        <input v-model.number="seed" type="number" class="inp" />
        <span class="base-pack">基础角色包：<em>{{ session.basePersonasPath }}</em></span>
      </div>
      <div class="spacer"></div>
      <span v-if="status" class="status">{{ status }}</span>
      <span v-if="session.lastError" class="status bad">{{ session.lastError }}</span>
      <button
        class="run-btn"
        type="button"
        :disabled="loading || personas.length === 0"
        @click="onStart"
      >
        <span v-if="!loading">开始推演 →</span>
        <span v-else>启动中…</span>
      </button>
    </footer>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { session } from '../store/session'
import WorkflowRail from '../components/WorkflowRail.vue'
import EventProposalForm from '../components/EventProposalForm.vue'
import PersonaCard from '../components/PersonaCard.vue'
import { fetchPersonaTemplate } from '../api/extract'
import { initSimulationStream } from '../api/simulate'

const router = useRouter()

const event = ref({})
const personas = ref([])
const nRounds = ref(5)
const seed = ref(42)
const loading = ref(false)
const status = ref('')
const activeFilter = ref('all')

// Entity sandbox + multi-instrument data from session
const instrumentUniverse = computed(() => session.instrumentUniverse || null)
const entityGraph = computed(() => session.entityGraph || null)
const roundSchedule = computed(() => session.roundSchedule || null)

const universeInstruments = computed(() => {
  if (!instrumentUniverse.value) return []
  const u = instrumentUniverse.value
  const all = [u.primary, ...(u.related || [])]
  return all.filter(Boolean)
})

// If we have a round schedule, use its round count as default
onMounted(() => {
  if (session.roundSchedule && session.roundSchedule.rounds) {
    nRounds.value = session.roundSchedule.rounds.length
  }
})

const REL_LABELS = {
  primary: '主体', supplier: '供应商', competitor: '竞品', customer: '客户',
  sector_etf: '板块ETF', upstream: '上游', downstream: '下游', peer: '同行',
  opposing: '对立', index: '指数', other: '其他',
}
function relLabel (r) { return REL_LABELS[r] || r }

const ENT_TYPE_LABELS = {
  company: '公司', supplier: '供应商', dealer: '经销商', regulator: '监管',
  trader_class: '交易者', government: '政府', other: '市场环境', custom: '自定义',
}
function entTypeLabel (t) { return ENT_TYPE_LABELS[t] || t }

function miniKline (bars) {
  if (!bars.length) return ''
  const closes = bars.map(b => b.close || 0)
  const min = Math.min(...closes)
  const max = Math.max(...closes)
  const span = (max - min) || 1
  return closes.map((c, i) => {
    const x = (i / Math.max(1, closes.length - 1)) * 120
    const y = 28 - ((c - min) / span) * 26
    return `${x.toFixed(1)},${y.toFixed(1)}`
  }).join(' ')
}

// One-way sync on mount: copy the session state into local editable refs.
onMounted(() => {
  if (session.eventProposal) {
    event.value = JSON.parse(JSON.stringify(session.eventProposal))
  }
  if (session.personasProposed && session.personasProposed.length) {
    personas.value = JSON.parse(JSON.stringify(session.personasProposed))
  }
})

// If the session state changes from OUTSIDE the view (e.g. a background
// re-extract while the user is still on this page), re-sync into local
// refs so the form reflects the new proposal. Guarded against echoing
// our own edits back by only syncing when the session payload is
// genuinely different from what we last emitted.
let syncing = false
watch(
  () => session.eventProposal,
  (v) => {
    if (syncing || !v) return
    if (JSON.stringify(v) !== JSON.stringify(event.value)) {
      event.value = JSON.parse(JSON.stringify(v))
    }
  },
  { deep: true },
)
watch(
  () => session.personasProposed,
  (v) => {
    if (syncing || !v) return
    if (JSON.stringify(v) !== JSON.stringify(personas.value)) {
      personas.value = JSON.parse(JSON.stringify(v))
    }
  },
  { deep: true },
)

// Forward sync: edits in the local refs write back to the session store.
watch(event, (v) => {
  syncing = true
  session.eventProposal = JSON.parse(JSON.stringify(v))
  syncing = false
}, { deep: true })
watch(personas, (v) => {
  syncing = true
  session.personasProposed = JSON.parse(JSON.stringify(v))
  syncing = false
}, { deep: true })

const traders = computed(() => personas.value.filter((p) => p.entity_role === 'trader'))
const infos = computed(() => personas.value.filter((p) => p.entity_role !== 'trader'))

const filters = computed(() => [
  { id: 'all', label: '全部', count: personas.value.length },
  { id: 'trader', label: '交易者', count: traders.value.length },
  { id: 'info', label: '信息源', count: infos.value.length },
])

const filteredPersonas = computed(() => {
  if (activeFilter.value === 'trader') return traders.value
  if (activeFilter.value === 'info') return infos.value
  return personas.value
})

const eventTag = computed(() => {
  const e = event.value || {}
  const parts = [e.ticker, e.market].filter(Boolean)
  return parts.join(' · ') || '—'
})

function updatePersona (oldP, newP) {
  const idx = personas.value.indexOf(oldP)
  if (idx >= 0) personas.value.splice(idx, 1, newP)
}
function removePersona (p) {
  const idx = personas.value.indexOf(p)
  if (idx >= 0) personas.value.splice(idx, 1)
}
async function addPersona () {
  try {
    const tmpl = await fetchPersonaTemplate('discretionary')
    tmpl.id = `custom_${Math.random().toString(36).slice(2, 7)}`
    tmpl.archetype = '自定义角色'
    tmpl.display_name = '自定义角色 (新建)'
    tmpl.voice_prompt = '你代表一个全新的市场参与者类型 …'
    personas.value.push(tmpl)
  } catch (err) {
    console.error(err)
    status.value = '添加角色失败：' + err.message
  }
}

async function onStart () {
  loading.value = true
  status.value = ''
  session.lastError = ''
  try {
    const r = await initSimulationStream({
      event: event.value,
      personas: personas.value,
      nRounds: nRounds.value,
      seed: seed.value,
      basePersonasPath: session.basePersonasPath,
      entityGraph: session.entityGraph || undefined,
      instrumentUniverse: session.instrumentUniverse || undefined,
      roundSchedule: session.roundSchedule || undefined,
    })
    status.value = '正在连接 stream…'
    // Remember the stream id so the rail's Simulate step can navigate
    // back to it during the active run.
    session.activeStreamId = r.stream_id
    router.push({ name: 'Run', params: { streamId: r.stream_id } })
  } catch (err) {
    console.error(err)
    status.value = '失败：' + (err?.response?.data?.detail || err.message)
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.setup {
  min-height: 100vh;
  display: flex;
  align-items: stretch;
}
@media (max-width: 860px) {
  .setup { flex-direction: column; }
}

.main {
  flex: 1;
  padding: 40px 48px 120px;
  overflow-y: auto;
}
.wrap {
  width: 100%;
  max-width: 1120px;
  margin: 0 auto;
}

/* Empty state */
.empty {
  max-width: 540px;
  margin: 120px auto;
  text-align: left;
}
.empty h1 {
  font-family: 'Noto Serif SC', serif;
  font-size: 34px;
  font-weight: 500;
  margin-bottom: 12px;
}
.empty h1 .accent {
  font-family: 'Noto Serif SC', serif;
  font-weight: 600;
  color: var(--ss-accent);
  margin: 0 0.08em;
}
.empty p { color: var(--ss-fg-muted); margin-bottom: 24px; }
.btn {
  background: var(--ss-fg); color: #fff;
  border: 0; border-radius: 8px;
  padding: 10px 16px;
  font: 500 13px 'Inter', sans-serif;
  cursor: pointer;
}
.btn:hover { background: var(--ss-accent); }

/* Page header */
.page-h {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 32px;
}
.page-h h1 {
  font-family: 'Noto Serif SC', serif;
  font-size: 30px;
  font-weight: 500;
  letter-spacing: -0.01em;
  margin-bottom: 8px;
}
.page-h h1 .accent {
  font-family: 'Noto Serif SC', serif;
  font-weight: 600;
  color: var(--ss-accent);
  margin: 0 0.08em;
}
.page-h .sub {
  font-size: 13px;
  color: var(--ss-fg-muted);
  max-width: 640px;
  line-height: 1.7;
}
.page-h .sub em {
  font-family: 'Noto Serif SC', serif;
  font-style: normal;
  font-weight: 600;
  color: var(--ss-fg);
}

/* Two-column layout */
.cols {
  display: grid;
  grid-template-columns: 360px 1fr;
  gap: 32px;
}
@media (max-width: 1000px) { .cols { grid-template-columns: 1fr; } }

.section-h {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-bottom: 16px;
}
.section-h .t {
  font-family: 'Fraunces', serif;
  font-style: italic;
  font-size: 14px;
  color: var(--ss-fg);
}
.section-h .tag {
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
  color: var(--ss-fg-faint);
}
.section-h .add-btn {
  margin-left: auto;
  font-size: 11px;
  color: var(--ss-fg-muted);
  background: #fff;
  border: 1px solid var(--ss-line);
  padding: 5px 10px;
  border-radius: 6px;
  cursor: pointer;
  font-family: inherit;
}
.section-h .add-btn:hover {
  color: var(--ss-fg);
  border-color: var(--ss-fg);
}

/* Persona filter */
.filter-row {
  display: flex;
  gap: 6px;
  margin-bottom: 16px;
}
.pill {
  font-size: 11px;
  padding: 5px 12px;
  border-radius: 999px;
  border: 1px solid var(--ss-line);
  background: #fff;
  color: var(--ss-fg-muted);
  cursor: pointer;
  font-family: inherit;
}
.pill:hover { border-color: var(--ss-fg-muted); }
.pill.active {
  background: var(--ss-fg);
  color: #fff;
  border-color: var(--ss-fg);
}
.pill .n {
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  margin-left: 5px;
  opacity: 0.7;
}

.persona-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}
@media (max-width: 1200px) { .persona-grid { grid-template-columns: 1fr; } }

.persona-empty {
  padding: 40px 14px;
  text-align: center;
  color: var(--ss-fg-faint);
  font-size: 12px;
  border: 1px dashed var(--ss-line);
  border-radius: 8px;
}

/* Instrument Universe */
.universe-section, .entity-section, .schedule-section {
  margin-bottom: 24px;
}
.universe-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 10px;
}
.inst-card {
  border: 1px solid var(--ss-line);
  border-radius: 8px;
  padding: 10px 14px;
  background: #fff;
}
.inst-card.primary {
  border-left: 3px solid var(--ss-accent);
}
.inst-h {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 4px;
}
.inst-name {
  font-family: 'Noto Serif SC', serif;
  font-size: 13px;
  font-weight: 600;
}
.inst-ticker { font-size: 11px; color: var(--ss-fg-faint); }
.inst-meta {
  display: flex;
  justify-content: space-between;
  font-size: 11px;
  margin-bottom: 4px;
}
.inst-rel {
  color: var(--ss-fg-muted);
  font-size: 10px;
}
.inst-price { color: var(--ss-fg); }
.inst-kline { height: 30px; }
.mini-chart { width: 100%; height: 30px; display: block; }

/* Entity sandbox */
.entity-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 10px;
}
.ent-card {
  border: 1px solid var(--ss-line);
  border-radius: 8px;
  padding: 10px 14px;
  background: #fff;
}
.ent-h {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 6px;
}
.ent-name {
  font-family: 'Noto Serif SC', serif;
  font-size: 13px;
  font-weight: 600;
}
.ent-type { font-size: 10px; color: var(--ss-fg-faint); text-transform: uppercase; }
.ent-state {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 2px 10px;
}
.state-row { display: contents; }
.var-label { font-size: 11px; color: var(--ss-fg-muted); }
.var-value { font-size: 11px; text-align: right; color: var(--ss-fg); }

/* Round schedule */
.schedule-row {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.round-chip {
  padding: 4px 10px;
  border: 1px solid var(--ss-line);
  border-radius: 6px;
  font-size: 11px;
  color: var(--ss-fg-muted);
  background: #fff;
}

/* Bottom bar */
.bottom-bar {
  position: fixed;
  bottom: 0;
  left: var(--ss-sidebar-w);
  right: 0;
  background: #fff;
  border-top: 1px solid var(--ss-line);
  padding: 14px 48px;
  display: flex;
  align-items: center;
  gap: 16px;
  z-index: 10;
}
@media (max-width: 860px) {
  .bottom-bar {
    left: 0;
    padding: 12px 16px;
    flex-wrap: wrap;
  }
  .bottom-bar .foot-meta { flex-wrap: wrap; gap: 10px; font-size: 11px; }
  .bottom-bar .base-pack { display: none; }
  .main { padding: 24px 16px 140px; }
  .page-h h1 { font-size: 24px; }
  .cols { gap: 20px; }
}
.foot-meta {
  display: flex;
  align-items: center;
  gap: 14px;
  font-size: 12px;
  color: var(--ss-fg-muted);
}
.foot-meta em {
  font-family: 'Fraunces', serif;
  font-style: italic;
  color: var(--ss-fg);
  margin-right: 4px;
}
.foot-meta .inp {
  border: 1px solid var(--ss-line);
  padding: 4px 8px;
  border-radius: 4px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
  width: 56px;
  outline: none;
  color: var(--ss-fg);
  background: #fff;
}
.foot-meta .inp:focus { border-color: var(--ss-accent); }
.base-pack {
  margin-left: 14px;
  font-size: 11px;
}
.base-pack em {
  font-family: 'JetBrains Mono', monospace;
  font-style: normal;
  color: var(--ss-fg-muted);
}

.spacer { flex: 1; }
.status {
  font-size: 11px;
  color: var(--ss-fg-muted);
}
.status.bad { color: var(--ss-bad); }

.run-btn {
  background: var(--ss-fg);
  color: #fff;
  border: 0;
  border-radius: 8px;
  padding: 10px 18px;
  font: 500 13px 'Inter', sans-serif;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 8px;
}
.run-btn:hover:not(:disabled) { background: var(--ss-accent); }
.run-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
</style>
