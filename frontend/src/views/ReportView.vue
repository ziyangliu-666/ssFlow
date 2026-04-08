<template>
  <div class="report">
    <WorkflowRail>
      <template #foot-extra>
        <button class="aside-btn primary" type="button" @click="$router.push('/')">+ New sim</button>
        <button class="aside-btn" type="button" :disabled="!markdown" @click="downloadReport">↓ Download .md</button>
      </template>
    </WorkflowRail>

    <main class="main">
      <div v-if="loading" class="state">
        <p class="mono">loading report…</p>
      </div>

      <div v-else-if="errorMsg" class="state bad">
        <h1>Can't load this <span class="accent">report</span>.</h1>
        <p class="mono">{{ errorMsg }}</p>
        <p v-if="errorStatus === 401" class="auth-hint">
          Looks like your auth password isn't set or it's wrong.
          Click <em>Settings</em> in the sidebar, enter the password, and reload.
        </p>
        <div class="state-actions">
          <button class="back" type="button" @click="load">↻ Retry</button>
          <button class="back ghost" type="button" @click="$router.push('/')">← Back to Seed</button>
        </div>
      </div>

      <div v-else-if="markdown" class="wrap">
        <div class="kicker">
          Simulation report
          <code>{{ shortId }}</code>
          <span v-if="meta?.event_ticker"> · {{ meta.event_ticker }}</span>
          <span v-if="meta?.event_date"> · {{ meta.event_date }}</span>
        </div>

        <!-- Summary strip: 4 cells driven by the structured summary JSON -->
        <div v-if="summary" class="summary">
          <div class="cell">
            <div class="k">FINAL PRICE</div>
            <div class="v mono" :class="deltaClass">{{ formatPrice(summary.final_price) }}</div>
            <div class="sub">from {{ formatPrice(summary.initial_price) }} open</div>
          </div>
          <div class="cell">
            <div class="k">Δ %</div>
            <div class="v mono" :class="deltaClass">{{ formatPct(summary.delta_pct) }}</div>
            <div class="sub">{{ meta?.n_rounds || 0 }} rounds</div>
          </div>
          <div class="cell">
            <div class="k">NET P&amp;L</div>
            <div class="v mono" :class="netFlowClass">{{ formatMoney(summary.net_flow_total) }}</div>
            <div class="sub">across all classes</div>
          </div>
          <div class="cell">
            <div class="k">WINNING CLASS</div>
            <div
              class="v winning"
              v-if="summary.winning_class"
              :title="summary.winning_class.archetype"
            >
              {{ truncate(summary.winning_class.archetype, 14) }}
            </div>
            <div class="v" v-else>—</div>
            <div class="sub" v-if="summary.winning_class">
              {{ formatMoney(summary.winning_class.pnl) }} realized
            </div>
          </div>
        </div>

        <article class="report-body" v-html="rendered"></article>

        <div class="footnote">
          <em>Research tool only.</em> This report describes a simulation, not a forecast.
          Actual market behaviour may differ. ssFlow does not provide investment advice.
          <span v-if="meta?.cost_usd !== undefined" class="cost-tag">
            · cost ${{ meta.cost_usd?.toFixed(4) }}
          </span>
          <span v-if="meta?.elapsed_seconds !== undefined" class="cost-tag">
            · {{ meta.elapsed_seconds?.toFixed(1) }}s
          </span>
        </div>
      </div>
    </main>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { marked } from 'marked'
import { http } from '../api/client'
import WorkflowRail from '../components/WorkflowRail.vue'

const props = defineProps({ simulationId: { type: String, required: true } })

const markdown = ref('')
const summary = ref(null)
const meta = ref(null)
const loading = ref(true)
const errorMsg = ref('')
const errorStatus = ref(0)

const rendered = computed(() => {
  if (!markdown.value) return ''
  return marked.parse(markdown.value, { breaks: true })
})

const shortId = computed(() => {
  const id = props.simulationId || ''
  if (id.length > 14) return id.slice(0, 12) + '…'
  return id
})

const deltaClass = computed(() => {
  const d = summary.value?.delta_pct
  if (typeof d !== 'number') return ''
  if (d > 0) return 'good'
  if (d < 0) return 'bad'
  return ''
})

const netFlowClass = computed(() => {
  const n = summary.value?.net_flow_total
  if (typeof n !== 'number') return ''
  if (n > 0) return 'good'
  if (n < 0) return 'bad'
  return ''
})

function formatPrice (v) {
  if (typeof v !== 'number') return '—'
  const sym = currencySymbol(meta.value?.price_currency || 'CNY')
  return sym + v.toFixed(2)
}

function formatPct (v) {
  if (typeof v !== 'number') return '—'
  return (v >= 0 ? '+' : '') + (v * 100).toFixed(2) + '%'
}

function formatMoney (v) {
  if (typeof v !== 'number') return '—'
  const sym = currencySymbol(meta.value?.price_currency || 'CNY')
  const sign = v >= 0 ? '+' : '−'
  const absV = Math.abs(v)
  if (absV >= 1e8) return sign + sym + (absV / 1e8).toFixed(2) + '亿'
  if (absV >= 1e4) return sign + sym + (absV / 1e4).toFixed(2) + '万'
  return sign + sym + absV.toFixed(0)
}

function currencySymbol (cur) {
  return { CNY: '¥', USD: '$', EUR: '€', JPY: '¥', HKD: 'HK$', BTC: '₿' }[cur] || '$'
}

function truncate (s, n) {
  if (!s) return ''
  return s.length > n ? s.slice(0, n - 1) + '…' : s
}

async function load () {
  loading.value = true
  errorMsg.value = ''
  errorStatus.value = 0
  try {
    const r = await http.get(`/report/${encodeURIComponent(props.simulationId)}`)
    // The endpoint returns JSON {markdown, summary, meta, simulation_id}.
    // For backwards-compat, if the server sends raw markdown (e.g. a
    // legacy deployment), fall back to the string branch.
    if (typeof r.data === 'string') {
      markdown.value = r.data
      summary.value = null
      meta.value = null
    } else if (r.data && typeof r.data === 'object') {
      markdown.value = r.data.markdown || ''
      summary.value = r.data.summary || null
      meta.value = r.data.meta || null
    } else {
      markdown.value = JSON.stringify(r.data)
    }
  } catch (err) {
    errorMsg.value = err?.response?.data?.error || err.message
    errorStatus.value = err?.response?.status || 0
  } finally {
    loading.value = false
  }
}

function downloadReport () {
  if (!markdown.value) return
  const blob = new Blob([markdown.value], { type: 'text/markdown' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${props.simulationId}.md`
  a.click()
  URL.revokeObjectURL(url)
}

onMounted(load)
</script>

<style scoped>
.report {
  min-height: 100vh;
  display: flex;
  align-items: stretch;
}
@media (max-width: 860px) {
  .report { flex-direction: column; }
}

.main {
  flex: 1;
  padding: 56px 48px 80px;
  overflow-y: auto;
}
.wrap {
  max-width: 820px;
  margin: 0 auto;
}
@media (max-width: 860px) {
  .main { padding: 28px 16px 40px; }
  .report-body :deep(h1) { font-size: 26px; }
  .report-body :deep(h2) { font-size: 18px; }
}

.state {
  max-width: 540px;
  margin: 120px auto;
  text-align: left;
}
.state.bad h1 {
  font-family: 'Noto Serif SC', serif;
  font-size: 30px;
  font-weight: 500;
  margin-bottom: 12px;
}
.state.bad h1 .accent {
  font-family: 'Fraunces', serif;
  font-style: italic;
  color: var(--ss-accent);
  font-weight: 500;
}
.state .mono {
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
  color: var(--ss-fg-muted);
}
.state .auth-hint {
  margin-top: 14px;
  padding: 12px 14px;
  border-left: 3px solid var(--ss-accent);
  background: var(--ss-accent-soft);
  border-radius: 0 6px 6px 0;
  font-size: 13px;
  line-height: 1.6;
  color: var(--ss-fg);
}
.state .auth-hint em {
  font-family: 'Fraunces', serif;
  font-style: italic;
  color: var(--ss-accent);
}
.state-actions {
  margin-top: 18px;
  display: flex;
  gap: 10px;
}
.state .back {
  background: var(--ss-fg);
  color: #fff;
  border: 0;
  padding: 10px 16px;
  border-radius: 8px;
  font: 500 13px 'Inter', sans-serif;
  cursor: pointer;
}
.state .back.ghost {
  background: #fff;
  color: var(--ss-fg);
  border: 1px solid var(--ss-line-strong);
}
.state .back:hover { background: var(--ss-accent); color: #fff; border-color: var(--ss-accent); }

/* Kicker */
.kicker {
  font-family: 'Fraunces', serif;
  font-style: italic;
  font-size: 12px;
  color: var(--ss-fg-muted);
  margin-bottom: 20px;
}
.kicker code {
  font-family: 'JetBrains Mono', monospace;
  font-style: normal;
  font-size: 10px;
  background: var(--ss-bg-soft);
  padding: 2px 7px;
  border-radius: 3px;
  margin-left: 6px;
  color: var(--ss-fg);
}

/* Summary strip — 4 cells above the report body */
.summary {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  border-top: 1px solid var(--ss-line-strong);
  border-bottom: 1px solid var(--ss-line-strong);
  margin: 28px 0 44px;
}
.summary .cell {
  padding: 18px 20px 18px 0;
  border-right: 1px dashed var(--ss-line);
}
.summary .cell:last-child { border-right: 0; }
.summary .cell:first-child { padding-left: 0; }

@media (max-width: 860px) {
  .summary {
    grid-template-columns: repeat(2, 1fr);
    gap: 0;
  }
  .summary .cell {
    padding: 14px 14px;
    border-right: 1px dashed var(--ss-line);
    border-bottom: 1px dashed var(--ss-line);
  }
  .summary .cell:nth-child(2n),
  .summary .cell:last-child { border-right: 0; }
  .summary .cell:nth-last-child(-n+2) { border-bottom: 0; }
  .summary .cell:first-child,
  .summary .cell:nth-child(3) { padding-left: 0; }
  .summary .v { font-size: 20px; }
  .summary .v.winning { font-size: 16px; }
}
@media (max-width: 500px) {
  .summary { grid-template-columns: 1fr; }
  .summary .cell {
    border-right: 0;
    border-bottom: 1px dashed var(--ss-line);
    padding: 12px 0;
  }
  .summary .cell:last-child { border-bottom: 0; }
}
.summary .k {
  font-size: 10px;
  color: var(--ss-fg-faint);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  margin-bottom: 6px;
}
.summary .v {
  font-size: 22px;
  font-weight: 500;
  color: var(--ss-fg);
}
.summary .v.good { color: var(--ss-good); }
.summary .v.bad  { color: var(--ss-bad); }
.summary .v.winning {
  font-family: 'Noto Serif SC', serif;
  font-size: 18px;
  color: var(--ss-good);
}
.summary .v.mono {
  font-variant-numeric: tabular-nums;
}
.summary .sub {
  font-size: 11px;
  color: var(--ss-fg-muted);
  margin-top: 4px;
}

.cost-tag {
  color: var(--ss-fg-faint);
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  margin-left: 4px;
}

/* Sidebar slot buttons — these render inside WorkflowRail's slot,
   but scoped styles still reach them because Vue compiles slot content
   with the parent's scope id. */
.aside-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 7px 10px;
  font-size: 11px;
  color: var(--ss-fg);
  border: 1px solid var(--ss-line);
  border-radius: 6px;
  background: #fff;
  cursor: pointer;
  font-family: inherit;
  width: 100%;
  justify-content: flex-start;
}
.aside-btn:hover:not(:disabled) { border-color: var(--ss-fg); }
.aside-btn:disabled { opacity: 0.4; cursor: not-allowed; }
.aside-btn.primary {
  background: var(--ss-fg);
  color: #fff;
  border-color: var(--ss-fg);
}
.aside-btn.primary:hover:not(:disabled) { background: var(--ss-accent); border-color: var(--ss-accent); }

/* Report body — restyled markdown render */
.report-body {
  font-family: 'Inter', sans-serif;
  font-size: 15px;
  line-height: 1.75;
  color: var(--ss-fg);
}

.report-body :deep(h1) {
  font-family: 'Noto Serif SC', serif;
  font-size: 36px;
  font-weight: 500;
  line-height: 1.2;
  letter-spacing: -0.01em;
  margin: 0 0 16px;
}
.report-body :deep(h1 em),
.report-body :deep(h1 strong) {
  font-family: 'Fraunces', serif;
  font-style: italic;
  color: var(--ss-accent);
  font-weight: 500;
}

.report-body :deep(h2) {
  font-family: 'Noto Serif SC', serif;
  font-size: 22px;
  font-weight: 500;
  letter-spacing: -0.005em;
  margin: 40px 0 14px;
  padding: 0;
  border: 0;
}
.report-body :deep(h2)::before {
  content: '';
  display: inline-block;
  width: 24px;
  height: 1px;
  background: var(--ss-fg);
  vertical-align: middle;
  margin-right: 10px;
}
.report-body :deep(h2 em),
.report-body :deep(h2 strong) {
  font-family: 'Fraunces', serif;
  font-style: italic;
  color: var(--ss-accent);
  font-weight: 500;
}

.report-body :deep(h3) {
  font-family: 'Inter', sans-serif;
  font-size: 14px;
  font-weight: 600;
  margin: 22px 0 8px;
  color: var(--ss-fg);
  text-transform: none;
}

.report-body :deep(p) {
  margin: 0 0 14px;
  font-size: 15px;
  line-height: 1.75;
}
.report-body :deep(p em) {
  font-family: 'Fraunces', serif;
  font-style: italic;
  color: var(--ss-accent);
}
.report-body :deep(p strong) {
  font-weight: 600;
  color: var(--ss-fg);
}

.report-body :deep(ul), .report-body :deep(ol) {
  padding-left: 22px;
  margin: 0 0 16px;
}
.report-body :deep(li) {
  margin: 4px 0;
}

.report-body :deep(blockquote) {
  border-left: 2px solid var(--ss-accent);
  padding: 4px 0 4px 18px;
  margin: 18px 0;
  font-family: 'Noto Serif SC', serif;
  font-size: 15px;
  color: var(--ss-fg);
  font-style: normal;
}

.report-body :deep(table) {
  width: 100%;
  border-collapse: collapse;
  margin: 16px 0;
  font-size: 13px;
}
.report-body :deep(th) {
  text-align: left;
  padding: 8px 10px 8px 0;
  border-bottom: 1px solid var(--ss-fg);
  font-family: 'Inter', sans-serif;
  font-weight: 600;
  font-size: 11px;
  color: var(--ss-fg-muted);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  background: transparent;
}
.report-body :deep(td) {
  padding: 10px 10px 10px 0;
  border-bottom: 1px dashed var(--ss-line);
  font-family: 'Inter', sans-serif;
}
.report-body :deep(table tr:last-child td) { border-bottom: 1px solid var(--ss-line-strong); }

.report-body :deep(code) {
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.88em;
  background: var(--ss-bg-soft);
  padding: 1px 6px;
  border-radius: 3px;
  color: var(--ss-fg);
}
.report-body :deep(pre) {
  background: var(--ss-bg-soft);
  padding: 12px 14px;
  border-radius: 6px;
  overflow-x: auto;
  font-size: 12px;
  margin: 12px 0;
}
.report-body :deep(pre code) {
  background: transparent;
  padding: 0;
}

.report-body :deep(hr) {
  border: 0;
  border-top: 1px solid var(--ss-line);
  margin: 32px 0;
}

.footnote {
  margin-top: 48px;
  padding-top: 14px;
  border-top: 1px solid var(--ss-line);
  font-size: 11px;
  color: var(--ss-fg-faint);
}
.footnote em {
  font-family: 'Fraunces', serif;
  font-style: italic;
  color: var(--ss-fg-muted);
}
</style>
