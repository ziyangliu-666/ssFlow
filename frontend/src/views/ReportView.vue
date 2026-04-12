<template>
  <div class="report">
    <WorkflowRail>
      <template #foot-extra>
        <button
          class="aside-btn primary"
          type="button"
          @click="$router.push('/')"
        >+ 新推演</button>
        <button
          class="aside-btn"
          type="button"
          :disabled="!markdown"
          @click="openReplay"
        >▶ 重播推演</button>
        <button
          class="aside-btn"
          type="button"
          :disabled="!markdown"
          @click="downloadReport"
        >↓ 下载 .md</button>
      </template>
    </WorkflowRail>

    <main class="main" aria-label="推演报告">
      <div v-if="loading" class="state">
        <p class="mono">加载中…</p>
      </div>

      <div v-else-if="errorMsg" class="state bad">
        <h1>无法加载这份<span class="accent">报告</span>。</h1>
        <p class="mono">{{ errorMsg }}</p>
        <p v-if="errorStatus === 401" class="auth-hint">
          认证密码没设或填错了。
          点左侧边栏的<em>设置</em>填一下密码，然后刷新。
        </p>
        <div class="state-actions">
          <button class="back" type="button" @click="load">↻ 重试</button>
          <button class="back ghost" type="button" @click="$router.push('/')">← 返回开始</button>
        </div>
      </div>

      <div v-else-if="markdown" class="wrap">
        <div class="kicker">
          推演报告
          <code>{{ shortId }}</code>
          <span v-if="meta?.event_ticker"> · {{ meta.event_ticker }}</span>
          <span v-if="meta?.event_date"> · {{ meta.event_date }}</span>
        </div>

        <!-- Multi-instrument summary: per-ticker peer grid. When the
             sim involved multiple instruments, the scalar initial/final
             scalars are misleading (they only track event_subject), so
             we show every instrument's move side by side instead. The
             total P&L + winning class cells stay the same since they're
             portfolio-level. -->
        <div
          v-if="summary && summary.per_ticker_finals && summary.per_ticker_finals.length"
          class="summary-multi"
        >
          <div class="sm-h">
            <span class="t">标的涨跌</span>
            <span class="tag mono">{{ summary.per_ticker_finals.length }} 个标的 · {{ meta?.n_rounds || 0 }} 轮</span>
          </div>
          <div class="sm-grid">
            <div
              v-for="row in summary.per_ticker_finals"
              :key="'pt-' + row.ticker"
              class="sm-row"
              :class="{ primary: row.is_primary }"
            >
              <div class="sm-left">
                <span class="sm-ticker mono">{{ row.ticker }}</span>
                <span v-if="row.is_primary" class="sm-badge">事件主体</span>
              </div>
              <div class="sm-mid mono">
                {{ formatPrice(row.initial) }} → {{ formatPrice(row.final) }}
              </div>
              <span
                class="sm-pct mono"
                :class="row.pct > 0 ? 'good' : row.pct < 0 ? 'bad' : ''"
              >
                {{ (row.pct >= 0 ? '+' : '') + (row.pct * 100).toFixed(2) + '%' }}
              </span>
            </div>
          </div>
          <div class="sm-extras">
            <span class="sm-extra">
              <em>总盈亏</em>
              <span class="mono" :class="netFlowClass">{{ formatMoney(summary.net_flow_total) }}</span>
            </span>
            <span class="sm-extra" v-if="summary.winning_class">
              <em>领先分组</em>
              <span class="mono">{{ truncate(summary.winning_class.archetype, 16) }}</span>
              <span class="mono subdued">({{ formatMoney(summary.winning_class.pnl) }})</span>
            </span>
          </div>
        </div>

        <!-- Legacy single-instrument summary strip. Falls through when
             per_ticker_finals isn't populated (pre-peer-mode sims or
             single-ticker runs). -->
        <div v-else-if="summary" class="summary">
          <div class="cell">
            <div class="k">收盘价</div>
            <div class="v mono" :class="deltaClass">{{ formatPrice(summary.final_price) }}</div>
            <div class="sub">开盘 {{ formatPrice(summary.initial_price) }}</div>
          </div>
          <div class="cell">
            <div class="k">涨跌幅</div>
            <div class="v mono" :class="deltaClass">{{ formatPct(summary.delta_pct) }}</div>
            <div class="sub">共 {{ meta?.n_rounds || 0 }} 轮</div>
          </div>
          <div class="cell">
            <div class="k">总盈亏</div>
            <div class="v mono" :class="netFlowClass">{{ formatMoney(summary.net_flow_total) }}</div>
            <div class="sub">跨全部分组</div>
          </div>
          <div class="cell">
            <div class="k">领先分组</div>
            <div
              class="v winning"
              v-if="summary.winning_class"
              :title="summary.winning_class.archetype"
            >
              {{ truncate(summary.winning_class.archetype, 14) }}
            </div>
            <div class="v" v-else>—</div>
            <div class="sub" v-if="summary.winning_class">
              实现 {{ formatMoney(summary.winning_class.pnl) }}
            </div>
          </div>
        </div>

        <!-- Round-by-round inspector — the "I want to see what
             happened in round 2 without playing anything back" view.
             Consumes the same /simulation/:id/timeline endpoint as
             the replay view. Lazy-loaded on mount; shows a compact
             pill bar + the selected round's publications / trades /
             price delta / class flows. -->
        <section v-if="rounds.length" class="rounds">
          <header class="rounds-h">
            <span class="rh-title">逐轮回看</span>
            <span v-if="timelineSource === 'reconstructed'" class="rh-tag">
              部分数据
            </span>
            <div class="rh-spacer" />
            <button
              type="button"
              class="rh-replay"
              @click="openReplay"
            >▶ 完整重播</button>
          </header>
          <div class="rnd-pills">
            <button
              v-for="r in rounds"
              :key="r.idx"
              type="button"
              class="rnd-pill"
              :class="{ on: selectedRound === r.idx }"
              @click="selectedRound = r.idx"
            >
              <span class="rp-idx mono">R{{ r.idx }}</span>
              <span
                class="rp-delta mono"
                :class="r.deltaClass"
              >{{ r.deltaLabel }}</span>
            </button>
          </div>

          <div v-if="currentRoundDetail" class="rnd-detail">
            <div class="rd-head">
              <span class="rd-price mono">
                {{ formatPrice(currentRoundDetail.priceBefore) }}
                →
                <strong :class="currentRoundDetail.deltaClass">{{
                  formatPrice(currentRoundDetail.priceAfter)
                }}</strong>
              </span>
              <span
                class="rd-delta mono"
                :class="currentRoundDetail.deltaClass"
              >{{ currentRoundDetail.deltaLabel }}</span>
              <span class="rd-counts mono">
                {{ currentRoundDetail.publications.length }} 篇
                · {{ currentRoundDetail.trades.length }} 单
              </span>
            </div>

            <div class="rd-cols">
              <!-- Publications column -->
              <div class="rd-col">
                <div class="rd-col-h">
                  <em>想法 / 发布</em>
                  <span class="mono">{{
                    currentRoundDetail.publications.length
                  }}</span>
                </div>
                <div
                  v-if="!currentRoundDetail.publications.length"
                  class="rd-empty"
                >（本轮无发布）</div>
                <ul v-else class="rd-list">
                  <li
                    v-for="(p, i) in currentRoundDetail.publications"
                    :key="'p' + i"
                    class="rd-pub"
                  >
                    <span class="pub-type mono">{{
                      p.content_type || 'social_post'
                    }}</span>
                    <span class="pub-who">{{
                      p.archetype || p.persona_id
                    }}</span>
                    <div v-if="p.text" class="pub-text">
                      {{ truncateText(p.text, 200) }}
                    </div>
                  </li>
                </ul>
              </div>

              <!-- Trades column -->
              <div class="rd-col">
                <div class="rd-col-h">
                  <em>交易</em>
                  <span class="mono">{{
                    currentRoundDetail.trades.length
                  }}</span>
                </div>
                <div
                  v-if="!currentRoundDetail.trades.length"
                  class="rd-empty"
                >（本轮无交易记录）</div>
                <ul v-else class="rd-list">
                  <li
                    v-for="(t, i) in currentRoundDetail.trades"
                    :key="'t' + i"
                    class="rd-trade"
                  >
                    <div class="trade-h">
                      <span class="trade-who">{{
                        t.archetype || t.persona_id
                      }}</span>
                    </div>
                    <div class="trade-dist">
                      <span
                        v-for="(v, k) in t.distribution || {}"
                        :key="k"
                        class="td-item"
                      >
                        <span class="td-k">{{ k }}</span>
                        <span class="td-v mono">{{
                          (v * 100).toFixed(0)
                        }}%</span>
                      </span>
                    </div>
                    <div v-if="t.rationale" class="trade-rationale">
                      “{{ truncateText(t.rationale, 140) }}”
                    </div>
                  </li>
                </ul>
              </div>

              <!-- Class flows column -->
              <div class="rd-col">
                <div class="rd-col-h">
                  <em>分组流向</em>
                  <span class="mono">{{
                    currentRoundDetail.flows.length
                  }}</span>
                </div>
                <div
                  v-if="!currentRoundDetail.flows.length"
                  class="rd-empty"
                >（本轮无分组流向）</div>
                <ul v-else class="rd-list">
                  <li
                    v-for="(f, i) in currentRoundDetail.flows"
                    :key="'f' + i"
                    class="rd-flow"
                  >
                    <span class="flow-who">{{
                      f.archetype || f.persona_id
                    }}</span>
                    <span
                      class="flow-val mono"
                      :class="f.cls"
                    >{{ f.label }}</span>
                  </li>
                </ul>
              </div>
            </div>
          </div>
        </section>

        <article class="report-body" v-html="rendered"></article>

        <div class="footnote">
          <em>研究工具。</em>这是一份推演记录，不是对行情的预测。
          实际市场走势可能有出入。ssFlow 不构成投资建议。
          <span v-if="meta?.cost_usd !== undefined" class="cost-tag">
            · 成本 ${{ meta.cost_usd?.toFixed(4) }}
          </span>
          <span v-if="meta?.elapsed_seconds !== undefined" class="cost-tag">
            · 耗时 {{ meta.elapsed_seconds?.toFixed(1) }}s
          </span>
        </div>
      </div>
    </main>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { marked } from 'marked'
import { http } from '../api/client'
import WorkflowRail from '../components/WorkflowRail.vue'

const props = defineProps({ simulationId: { type: String, required: true } })
const router = useRouter()

const markdown = ref('')
const summary = ref(null)
const meta = ref(null)
const loading = ref(true)
const errorMsg = ref('')
const errorStatus = ref(0)

// Round-by-round inspector state. Populated by fetching the same
// /simulation/:id/timeline endpoint the replay view uses. Fetch is
// non-blocking — the main report renders from the /report response,
// and the inspector fades in once the timeline fetch completes.
const timelineEvents = ref([])
const timelineSource = ref('full')
const selectedRound = ref(0)

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

function truncateText (s, n) {
  if (!s) return ''
  const trimmed = s.replace(/\s+/g, ' ').trim()
  return trimmed.length > n ? trimmed.slice(0, n - 1) + '…' : trimmed
}

// Group the flat timeline into per-round buckets. Each round carries
// its publications (persona_thought events), trades, flows, and the
// price_updated delta. round_start tells us price_before; price_updated
// tells us price_after and delta_pct.
const rounds = computed(() => {
  if (!timelineEvents.value.length) return []
  const buckets = new Map() // round_idx → {publications, trades, flows, priceBefore, priceAfter, delta}
  for (const e of timelineEvents.value) {
    const r = e.round_idx
    if (typeof r !== 'number') continue
    if (!buckets.has(r)) {
      buckets.set(r, {
        idx: r,
        publications: [],
        trades: [],
        flows: [],
        priceBefore: null,
        priceAfter: null,
        delta: 0,
      })
    }
    const b = buckets.get(r)
    switch (e.type) {
      case 'round_start':
        b.priceBefore = e.current_price
        break
      case 'persona_thought':
        b.publications.push(e)
        break
      case 'trade_submitted':
        b.trades.push(e)
        break
      case 'class_flow_computed':
        b.flows.push(e)
        break
      case 'price_updated':
        if (b.priceBefore == null) b.priceBefore = e.price_before
        b.priceAfter = e.price_after
        b.delta = e.delta_pct || 0
        break
    }
  }
  const out = Array.from(buckets.values()).sort((a, b) => a.idx - b.idx)
  // Back-fill missing priceBefore from the prior round's priceAfter.
  for (let i = 1; i < out.length; i++) {
    if (out[i].priceBefore == null && out[i - 1].priceAfter != null) {
      out[i].priceBefore = out[i - 1].priceAfter
    }
  }
  // Decorate for the pill row
  return out.map((b) => {
    const cls = b.delta > 0 ? 'good' : b.delta < 0 ? 'bad' : ''
    return {
      ...b,
      deltaClass: cls,
      deltaLabel: formatPct(b.delta),
    }
  })
})

const currentRoundDetail = computed(() => {
  const r = rounds.value.find((x) => x.idx === selectedRound.value)
  if (!r) return null
  // Decorate flows with the good/bad class and a signed label so the
  // template stays dumb.
  const flows = (r.flows || []).map((f) => {
    const net = f.net_flow || 0
    const cls = net > 0 ? 'good' : net < 0 ? 'bad' : ''
    return {
      ...f,
      cls,
      label: formatFlow(net),
    }
  })
  return {
    ...r,
    flows,
  }
})

async function loadTimeline () {
  try {
    const r = await http.get(
      `/simulation/${encodeURIComponent(props.simulationId)}/timeline`,
    )
    timelineEvents.value = r.data?.events || []
    timelineSource.value = r.data?.source || 'full'
  } catch (err) {
    // Timeline fetch is best-effort — if it fails the main report
    // still renders, we just hide the round inspector.
    timelineEvents.value = []
  }
}

function formatFlow (v) {
  if (typeof v !== 'number') return '—'
  const sym = currencySymbol(meta.value?.price_currency || 'CNY')
  const sign = v >= 0 ? '+' : '−'
  const absV = Math.abs(v)
  if (absV >= 1e8) return sign + sym + (absV / 1e8).toFixed(2) + '亿'
  if (absV >= 1e4) return sign + sym + (absV / 1e4).toFixed(2) + '万'
  return sign + sym + absV.toFixed(0)
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

function openReplay () {
  router.push({ name: 'Replay', params: { simulationId: props.simulationId } })
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

onMounted(() => {
  load()
  loadTimeline()
})
</script>

<style scoped>
.report {
  min-height: 100svh;
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
  font-family: 'Noto Serif SC', serif;
  font-weight: 600;
  color: var(--ss-accent);
  margin: 0 0.08em;
}
.state .mono {
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
  color: var(--ss-fg-muted);
}
.state .auth-hint {
  margin-top: 14px;
  padding: 12px 14px;
  background: var(--ss-accent-soft);
  border: 1px solid var(--ss-accent);
  border-radius: 6px;
  font-size: 13px;
  line-height: 1.6;
  color: var(--ss-fg);
}
.state .auth-hint em {
  font-family: 'Noto Serif SC', serif;
  font-style: normal;
  font-weight: 600;
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
  min-height: 44px;
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

/* Multi-instrument summary — per-ticker peer grid. Used when the sim
   involved >1 instrument; otherwise .summary (below) takes over. */
.summary-multi {
  margin: 28px 0 44px;
  border-top: 1px solid var(--ss-line-strong);
  border-bottom: 1px solid var(--ss-line-strong);
  padding: 20px 0;
}
.summary-multi .sm-h {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-bottom: 14px;
}
.summary-multi .sm-h .t {
  font-family: 'Fraunces', serif;
  font-style: italic;
  font-size: 14px;
  color: var(--ss-fg);
}
.summary-multi .sm-h .tag {
  font-size: 11px;
  color: var(--ss-fg-faint);
}
.summary-multi .sm-grid {
  display: flex;
  flex-direction: column;
  gap: 0;
}
.summary-multi .sm-row {
  display: grid;
  grid-template-columns: minmax(140px, 1fr) minmax(140px, auto) 80px;
  gap: 16px;
  align-items: baseline;
  padding: 10px 0 10px 10px;
  border-bottom: 1px dashed var(--ss-line);
  border-left: 2px solid transparent;
}
.summary-multi .sm-row:last-child { border-bottom: 0; }
.summary-multi .sm-row.primary {
  border-left-color: var(--ss-accent);
  background: var(--ss-accent-soft);
}
.summary-multi .sm-left {
  display: flex;
  align-items: baseline;
  gap: 10px;
}
.summary-multi .sm-ticker {
  font-size: 13px;
  color: var(--ss-fg);
  font-weight: 500;
}
.summary-multi .sm-row.primary .sm-ticker { color: var(--ss-accent); font-weight: 600; }
.summary-multi .sm-badge {
  font-family: 'Fraunces', serif;
  font-style: italic;
  font-size: 10px;
  color: var(--ss-accent);
}
.summary-multi .sm-mid {
  font-size: 12px;
  color: var(--ss-fg-muted);
}
.summary-multi .sm-pct {
  font-size: 14px;
  font-weight: 600;
  text-align: right;
  color: var(--ss-fg-muted);
}
.summary-multi .sm-pct.good { color: var(--ss-good); }
.summary-multi .sm-pct.bad  { color: var(--ss-bad); }
.summary-multi .sm-extras {
  display: flex;
  flex-wrap: wrap;
  gap: 18px 28px;
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px dashed var(--ss-line);
  font-size: 12px;
}
.summary-multi .sm-extra em {
  font-family: 'Inter', sans-serif;
  font-style: normal;
  color: var(--ss-fg-muted);
  margin-right: 6px;
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}
.summary-multi .sm-extra .mono.good { color: var(--ss-good); }
.summary-multi .sm-extra .mono.bad  { color: var(--ss-bad); }
.summary-multi .sm-extra .mono.subdued { color: var(--ss-fg-faint); }

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
  padding: 9px 10px;
  min-height: 38px;
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

/* Round-by-round inspector — sits between the summary strip and the
   report body. Click R0/R1/… to inspect a round without playing back. */
.rounds {
  margin: 0 0 36px;
  padding-top: 12px;
  border-top: 1px dashed var(--ss-line);
}
.rounds-h {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 14px;
}
.rh-title {
  font-family: 'Fraunces', serif;
  font-style: italic;
  font-size: 15px;
  color: var(--ss-fg);
}
.rh-tag {
  padding: 1px 6px;
  font-size: 9px;
  background: var(--ss-accent-soft);
  color: var(--ss-accent);
  border-radius: 3px;
  font-family: 'JetBrains Mono', monospace;
}
.rh-spacer { flex: 1; }
.rh-replay {
  font-size: 11px;
  padding: 6px 12px;
  border: 1px solid var(--ss-fg);
  background: #fff;
  color: var(--ss-fg);
  border-radius: 6px;
  cursor: pointer;
  font-family: inherit;
}
.rh-replay:hover { background: var(--ss-fg); color: #fff; }

.rnd-pills {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 14px;
}
.rnd-pill {
  display: inline-flex;
  align-items: baseline;
  gap: 8px;
  padding: 6px 12px;
  border: 1px solid var(--ss-line-strong);
  background: #fff;
  border-radius: 999px;
  cursor: pointer;
  font-family: inherit;
}
.rnd-pill:hover { border-color: var(--ss-accent); }
.rnd-pill.on {
  background: var(--ss-fg);
  border-color: var(--ss-fg);
  color: #fff;
}
.rnd-pill .rp-idx {
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
  font-weight: 600;
}
.rnd-pill .rp-delta {
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  font-variant-numeric: tabular-nums;
}
.rnd-pill .rp-delta.good { color: var(--ss-good); }
.rnd-pill .rp-delta.bad  { color: var(--ss-bad); }
.rnd-pill.on .rp-delta.good,
.rnd-pill.on .rp-delta.bad { color: #fff; opacity: 0.9; }

.rnd-detail {
  border: 1px solid var(--ss-accent);
  border-radius: 8px;
  background: #fff;
  padding: 16px 18px;
}
.rd-head {
  display: flex;
  align-items: baseline;
  gap: 14px;
  padding-bottom: 12px;
  border-bottom: 1px dashed var(--ss-line);
  margin-bottom: 14px;
  flex-wrap: wrap;
}
.rd-price {
  font-family: 'JetBrains Mono', monospace;
  font-size: 14px;
  color: var(--ss-fg);
  font-variant-numeric: tabular-nums;
}
.rd-price strong.good { color: var(--ss-good); }
.rd-price strong.bad  { color: var(--ss-bad); }
.rd-delta {
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 3px;
}
.rd-delta.good { background: #e7f5ec; color: var(--ss-good); }
.rd-delta.bad  { background: #fbecec; color: var(--ss-bad); }
.rd-counts {
  font-size: 10px;
  color: var(--ss-fg-faint);
  margin-left: auto;
}

.rd-cols {
  display: grid;
  grid-template-columns: 2fr 1.4fr 1fr;
  gap: 18px;
}
@media (max-width: 860px) {
  .rd-cols { grid-template-columns: 1fr; gap: 14px; }
}
.rd-col {
  min-width: 0;
}
.rd-col-h {
  display: flex;
  align-items: baseline;
  gap: 6px;
  margin-bottom: 10px;
  padding-bottom: 4px;
  border-bottom: 1px dashed var(--ss-line);
}
.rd-col-h em {
  font-family: 'Fraunces', serif;
  font-style: italic;
  font-size: 12px;
  color: var(--ss-fg);
}
.rd-col-h .mono {
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  color: var(--ss-fg-faint);
  margin-left: auto;
}
.rd-empty {
  font-size: 11px;
  color: var(--ss-fg-faint);
  padding: 4px 0;
}
.rd-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-height: 340px;
  overflow-y: auto;
}
.rd-pub {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding-bottom: 8px;
  border-bottom: 1px dashed var(--ss-line);
}
.rd-pub:last-child { border-bottom: 0; }
.rd-pub .pub-type {
  display: inline-block;
  font-size: 9px;
  padding: 1px 6px;
  background: var(--ss-bg-soft);
  border: 1px solid var(--ss-line);
  border-radius: 3px;
  color: var(--ss-fg-muted);
  font-family: 'JetBrains Mono', monospace;
  margin-right: 6px;
  align-self: flex-start;
}
.rd-pub .pub-who {
  font-size: 11px;
  color: var(--ss-fg-muted);
}
.rd-pub .pub-text {
  font-size: 12px;
  line-height: 1.55;
  color: var(--ss-fg);
}

.rd-trade {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding-bottom: 8px;
  border-bottom: 1px dashed var(--ss-line);
}
.rd-trade:last-child { border-bottom: 0; }
.trade-who {
  font-size: 11px;
  color: var(--ss-fg);
  font-weight: 500;
}
.trade-dist {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 10px;
  font-size: 10px;
}
.td-item {
  display: inline-flex;
  align-items: baseline;
  gap: 3px;
}
.td-k { color: var(--ss-fg-muted); }
.td-v {
  font-family: 'JetBrains Mono', monospace;
  color: var(--ss-fg);
  font-weight: 600;
}
.trade-rationale {
  font-family: 'Noto Serif SC', serif;
  font-size: 11px;
  color: var(--ss-fg-muted);
  border-left: 2px solid var(--ss-line-strong);
  padding-left: 8px;
  line-height: 1.5;
}

.rd-flow {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 4px 0;
  border-bottom: 1px dashed var(--ss-line);
  font-size: 11px;
}
.rd-flow:last-child { border-bottom: 0; }
.flow-who {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--ss-fg);
}
.rd-flow .flow-val {
  font-family: 'JetBrains Mono', monospace;
  font-variant-numeric: tabular-nums;
  font-weight: 600;
}
.rd-flow .flow-val.good { color: var(--ss-good); }
.rd-flow .flow-val.bad  { color: var(--ss-bad); }

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
  border-left: 2px solid var(--ss-line-strong);
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
  font-family: 'Noto Serif SC', serif;
  font-style: normal;
  font-weight: 500;
  color: var(--ss-fg-muted);
}
</style>
