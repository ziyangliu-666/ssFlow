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
        <button class="back" type="button" @click="$router.push('/')">← Back to Seed</button>
      </div>

      <div v-else-if="markdown" class="wrap">
        <div class="kicker">
          Simulation report
          <code>{{ shortId }}</code>
          · {{ loadedAtLabel }}
        </div>

        <article class="report-body" v-html="rendered"></article>

        <div class="footnote">
          <em>Research tool only.</em> This report describes a simulation, not a forecast.
          Actual market behaviour may differ. ssFlow does not provide investment advice.
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
const loading = ref(true)
const errorMsg = ref('')
const loadedAt = ref(null)

const rendered = computed(() => {
  if (!markdown.value) return ''
  return marked.parse(markdown.value, { breaks: true })
})

const shortId = computed(() => {
  const id = props.simulationId || ''
  if (id.length > 14) return id.slice(0, 12) + '…'
  return id
})

const loadedAtLabel = computed(() => {
  if (!loadedAt.value) return ''
  const d = loadedAt.value
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
})

async function load () {
  loading.value = true
  errorMsg.value = ''
  try {
    const r = await http.get(`/report/${encodeURIComponent(props.simulationId)}`)
    markdown.value = typeof r.data === 'string' ? r.data : JSON.stringify(r.data)
    loadedAt.value = new Date()
  } catch (err) {
    errorMsg.value = err?.response?.data?.error || err.message
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

.main {
  flex: 1;
  padding: 56px 48px 80px;
  overflow-y: auto;
}
.wrap {
  max-width: 820px;
  margin: 0 auto;
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
.state .back {
  margin-top: 18px;
  background: var(--ss-fg);
  color: #fff;
  border: 0;
  padding: 10px 16px;
  border-radius: 8px;
  font: 500 13px 'Inter', sans-serif;
  cursor: pointer;
}
.state .back:hover { background: var(--ss-accent); }

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
