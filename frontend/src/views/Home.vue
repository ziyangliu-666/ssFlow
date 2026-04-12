<template>
  <div class="home">
    <WorkflowRail />

    <main
      class="main"
      aria-label="输入推演素材"
      :class="{ 'drag-over': isDragOver }"
      @dragover.prevent="onDragOver"
      @dragleave.prevent="onDragLeave"
      @drop.prevent="onDrop"
    >
      <div class="wrap">
        <div class="eyebrow">市场事件 · 群体推演</div>
        <h1 aria-label="一条消息，如何在市场里发酵。">
          一条消息，如何在市场里<span class="verb-slot">
            <transition name="rip">
              <span
                class="accent-verb"
                :key="currentVerb"
                aria-live="polite"
              >{{ currentVerb }}</span>
            </transition>
          </span><span class="period">。</span>
        </h1>
        <p class="sub">
          拖入研报、新闻、财报，写下你想跟踪的走势。ssFlow 会抽出要研究的对象、
          推荐相关角色，逐轮推演群体行为如何把消息折价成一段行情。
        </p>

        <div class="composer" :class="{ focused: isFocused, 'drag-over': isDragOver }">
          <!-- File + URL chips -->
          <div v-if="files.length || serverFiles.length || detectedUrls.length" class="chips">
            <!-- Files already on the server from a prior extract — restored
                 when returning from /setup so the user keeps context. -->
            <span
              v-for="(sf, i) in serverFiles"
              :key="'sf' + i"
              class="chip restored"
              :title="'已上传 — ' + sf.name"
            >
              <svg class="chip-ico" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6"/></svg>
              <span class="chip-name">{{ sf.name }}</span>
              <span class="chip-size">{{ formatBytes(sf.size) }}</span>
              <span class="chip-badge">已上传</span>
              <button
                class="chip-x"
                type="button"
                :aria-label="'移除已上传文件 ' + sf.name"
                @click="removeServerFile(i)"
              >×</button>
            </span>
            <span
              v-for="(f, i) in files"
              :key="'f' + i"
              class="chip"
            >
              <svg class="chip-ico" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6"/></svg>
              <span class="chip-name">{{ f.name }}</span>
              <span class="chip-size">{{ formatBytes(f.size) }}</span>
              <button
                class="chip-x"
                type="button"
                :aria-label="'移除文件 ' + f.name"
                @click="removeFile(i)"
              >×</button>
            </span>
            <span
              v-for="(u, i) in detectedUrls"
              :key="'u' + i"
              class="chip url"
            >
              <svg class="chip-ico" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>
              <span class="chip-name">{{ shortenUrl(u) }}</span>
            </span>
          </div>

          <textarea
            ref="taEl"
            v-model="prompt"
            class="ta"
            :placeholder="placeholder"
            rows="4"
            @focus="isFocused = true"
            @blur="isFocused = false"
            @keydown="onKeydown"
          />

          <div class="composer-foot">
            <button class="tool" type="button" title="添加文件" @click="triggerInput">
              <svg width="17" height="17" viewBox="0 0 24 24" fill="none"
                   stroke="currentColor" stroke-width="2"
                   stroke-linecap="round" stroke-linejoin="round">
                <path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" />
              </svg>
            </button>
            <input
              ref="fileInput"
              type="file"
              multiple
              accept=".pdf,.md,.txt,.markdown,.csv,.html,.htm,.png,.jpg,.jpeg,.webp,.gif"
              style="display: none"
              @change="onFileSelect"
            />
            <span class="hint">
              拖文件到任意位置 · 粘 URL 自动识别 · <kbd>⌘</kbd><kbd>⏎</kbd>
            </span>
            <div class="spacer"></div>
            <span v-if="status && !errorMessage" class="status">{{ status }}</span>
            <button
              class="submit"
              type="button"
              :disabled="!canSubmit || loading"
              @click="onStart"
            >
              <span v-if="!loading">开始抽取 →</span>
              <span v-else>抽取中…</span>
            </button>
          </div>
        </div>

        <!-- Error banner — always visible when something failed, with a
             specific 401 branch that points the user at the settings
             gear. -->
        <div v-if="errorMessage" class="error-banner" role="alert">
          <div class="err-icon">!</div>
          <div class="err-body">
            <div class="err-title">{{ errorTitle }}</div>
            <div class="err-detail">{{ errorMessage }}</div>
            <div v-if="errorIs401" class="err-hint">
              去<strong>设置</strong>填一下<em>认证密码</em>（左侧边栏底部），再试一次。
            </div>
          </div>
          <button
            class="err-dismiss"
            type="button"
            aria-label="关闭错误提示"
            @click="dismissError"
          >×</button>
        </div>

        <!-- Pipeline visualization — replaces examples during extraction -->
        <ExtractPipeline
          :phase="pipelinePhase"
          :local-files="files"
          :detected-urls="detectedUrls"
          @go-setup="goSetup"
        />

        <div v-if="pipelinePhase === 'idle'" class="examples">
          <div class="examples-h">试试这些例子</div>
          <div class="examples-list">
            <button
              v-for="(ex, i) in examples"
              :key="i"
              type="button"
              class="example"
              @click="useExample(ex)"
            >
              <span class="arrow">→</span>{{ ex }}
            </button>
          </div>
        </div>
      </div>
    </main>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { useRouter } from 'vue-router'
import { session } from '../store/session'
import { ensureSession } from '../api/client'
import { uploadFiles } from '../api/upload'
import { runExtract } from '../api/extract'
import WorkflowRail from '../components/WorkflowRail.vue'
import ExtractPipeline from '../components/ExtractPipeline.vue'

const router = useRouter()

const files = ref([])
const serverFiles = ref([])  // files already uploaded from a prior extract
const prompt = ref(session.prompt || '')
const fileInput = ref(null)
const taEl = ref(null)
const isDragOver = ref(false)
const isFocused = ref(false)
const loading = ref(false)
const status = ref('')
const errorStatus = ref(0)  // HTTP status of the last failure, 0 if none
const pipelinePhase = ref('idle')  // idle | uploading | extracting | distilling | done

// Rotating accent verb — the signature gesture on Home. Cycles every
// 2.8s through a native editorial verb pool. Paused entirely
// when the user has prefers-reduced-motion set.
const verbs = ['发酵', '消化', '兑现', '搅动', '推演']
const verbIdx = ref(0)
const currentVerb = computed(() => verbs[verbIdx.value % verbs.length])
let verbTimer = null

const placeholder = '描述你想跟踪的市场走势，或拖入研报 / 新闻 / 财报 PDF…\n\n例：BYD Q1 2026 财报营收 +18% beat 但毛利率 -2.3pp miss，想看市场会怎么反应'

const examples = [
  '宁德时代海外订单超预期，能不能带动锂电板块？',
  '某白酒龙头季报 miss，历史上类似事件次日的盘口表现',
  '央行超预期降准，小盘成长股会怎么跑',
]

// Detect any URLs embedded in the prompt (whitespace- or newline-delimited).
const URL_RE = /https?:\/\/[^\s<>"')\]]+/g
const detectedUrls = computed(() => {
  const m = prompt.value.match(URL_RE)
  return m ? Array.from(new Set(m)) : []
})

const canSubmit = computed(() => {
  const hasText = prompt.value.trim().length > 0
  const hasFiles = files.value.length > 0 || serverFiles.value.length > 0
  return hasText || hasFiles
})

const errorMessage = computed(() => session.lastError || '')
const errorIs401 = computed(() => errorStatus.value === 401)
const errorTitle = computed(() => {
  if (errorIs401.value) return '认证失败'
  if (errorStatus.value === 429) return '请求过多'
  if (errorStatus.value >= 500) return '服务端错误'
  if (errorStatus.value === 0 && errorMessage.value) return '网络错误'
  if (errorMessage.value) return '请求失败'
  return ''
})

watch(prompt, (v) => { session.prompt = v })

onMounted(() => {
  session.lastError = ''
  // Restore files that are already uploaded on the server from a prior
  // extract — this fires when the user navigates back to Home from /setup
  // or refreshes the page mid-session.
  if (session.uploadedFiles && session.uploadedFiles.length) {
    serverFiles.value = session.uploadedFiles.map((f) => ({ ...f }))
  }
  // ⌘K / Ctrl+K from anywhere on the page focuses the composer.
  window.addEventListener('keydown', onGlobalKeydown)
  // Rotating verb — skip entirely if the user has reduced-motion on,
  // so we don't emit unnecessary DOM churn for motion-sensitive users.
  const mq = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)')
  if (!mq || !mq.matches) {
    verbTimer = setInterval(() => { verbIdx.value++ }, 2800)
  }
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onGlobalKeydown)
  if (verbTimer) clearInterval(verbTimer)
})

function onGlobalKeydown (e) {
  if ((e.metaKey || e.ctrlKey) && e.key?.toLowerCase() === 'k') {
    e.preventDefault()
    taEl.value && taEl.value.focus()
  }
}

function onDragOver () { isDragOver.value = true }
function onDragLeave () { isDragOver.value = false }
function onDrop (e) {
  isDragOver.value = false
  const dropped = Array.from(e.dataTransfer?.files || [])
  if (dropped.length) files.value = files.value.concat(dropped)
}
function triggerInput () { fileInput.value && fileInput.value.click() }
function onFileSelect (e) {
  const picked = Array.from(e.target.files || [])
  files.value = files.value.concat(picked)
  e.target.value = ''
}
function removeFile (i) { files.value.splice(i, 1) }
function removeServerFile (i) {
  serverFiles.value.splice(i, 1)
  // Also drop it from the session store so the next /extract doesn't
  // still see the stale file.
  if (session.uploadedFiles && session.uploadedFiles.length > i) {
    session.uploadedFiles.splice(i, 1)
  }
}
function dismissError () {
  session.lastError = ''
  errorStatus.value = 0
}

function onKeydown (e) {
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
    e.preventDefault()
    if (canSubmit.value && !loading.value) onStart()
  }
}

function useExample (text) {
  prompt.value = text
  taEl.value && taEl.value.focus()
}

function goSetup () {
  router.push({ name: 'Setup' })
}

function formatBytes (n) {
  if (n < 1024) return n + 'B'
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + 'KB'
  return (n / 1024 / 1024).toFixed(1) + 'MB'
}
function shortenUrl (u) {
  try {
    const p = new URL(u)
    const host = p.hostname.replace(/^www\./, '')
    const path = p.pathname === '/' ? '' : p.pathname
    const display = host + path
    return display.length > 44 ? display.slice(0, 43) + '…' : display
  } catch (_) {
    return u.length > 44 ? u.slice(0, 43) + '…' : u
  }
}

async function onStart () {
  loading.value = true
  status.value = ''
  session.lastError = ''
  errorStatus.value = 0
  pipelinePhase.value = 'uploading'
  try {
    // Wipe last extraction so a re-run replaces it cleanly.
    session.eventProposal = null
    session.personasProposed = []
    session.instrumentUniverse = null
    session.roundSchedule = null
    session.entityGraph = null
    session.ingestedDocs = []

    await ensureSession()
    if (files.value.length > 0) {
      status.value = '上传文件中…'
      await uploadFiles(files.value)
    }

    // Phase 2: Extract
    pipelinePhase.value = 'extracting'
    status.value = '分析中…（LLM 分类 + 合成）'

    // Strip the detected URLs out of the prompt text we send to the backend
    // (they go in the `urls` field instead). Keep the remaining narrative.
    const urls = detectedUrls.value
    const strippedPrompt = urls.length
      ? prompt.value.replace(URL_RE, '').replace(/\n{3,}/g, '\n\n').trim()
      : prompt.value

    await runExtract({
      prompt: strippedPrompt,
      urls,
    })

    // Phase 3: Distill + Sandbox (parallel). Distillation hits real
    // market data per ticker — that's where price/ADV come from now,
    // not from the EventProposal.
    pipelinePhase.value = 'distilling'
    const ep = session.eventProposal || {}
    const topic = ep.event_text || strippedPrompt
    const ticker = ep.ticker || ''
    const market = ep.market || 'ashare'
    const eventDate = ep.event_date || ''

    status.value = '提炼关联标的…'
    try {
      const { runDistill, generateSandbox } = await import('../api/extract')
      await Promise.all([
        runDistill({ topic, market, eventTicker: ticker, eventDate }),
        generateSandbox({ topic, market }),
      ])
    } catch (distillErr) {
      // Non-fatal: if distill/sandbox fails, proceed without them
      console.warn('Distill/sandbox failed (non-fatal):', distillErr)
    }

    // Phase 4: Done — show results briefly, then auto-navigate
    pipelinePhase.value = 'done'
    status.value = ''
    setTimeout(() => router.push({ name: 'Setup' }), 1500)
  } catch (err) {
    console.error(err)
    status.value = ''
    pipelinePhase.value = 'idle'
    errorStatus.value = err?.response?.status || 0
    if (!session.lastError) {
      session.lastError =
        err?.response?.data?.detail ||
        err?.response?.data?.error ||
        err?.message ||
        '请求失败'
    }
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.home {
  min-height: 100svh;
  display: flex;
  align-items: stretch;
}
@media (max-width: 860px) {
  .home { flex-direction: column; }
}

.main {
  flex: 1;
  padding: 72px 56px 60px;
  display: flex;
  flex-direction: column;
  align-items: center;
  position: relative;
  transition: background 0.15s ease;
}
.main.drag-over { background: var(--ss-accent-soft); }

.wrap {
  width: 100%;
  max-width: 760px;
}

.eyebrow {
  font-size: 11px;
  color: var(--ss-fg-muted);
  margin-bottom: 18px;
}

h1 {
  font-family: 'Noto Serif SC', 'Songti SC', 'SimSun', serif;
  font-size: 44px;
  line-height: 1.18;
  font-weight: 500;
  letter-spacing: -0.01em;
  margin-bottom: 14px;
}
/* Signature gesture — one Noto Serif SC headline with one orange
   verb inline. We use weight 600 + color for emphasis (no italic —
   synthesized italic on CJK is ugly). The verb rotates through a
   native vocabulary pool; see the component script. */

/* The verb sits inside a fixed-size "slot" so the slot-machine
   transition (incoming verb slides up from below, outgoing slides
   up and out the top) doesn't shift the rest of the headline. All
   verbs in the pool are exactly 2 CJK characters wide, so a 2-char
   slot is enough. overflow:hidden clips the verbs as they slide. */
h1 .verb-slot {
  display: inline-block;
  position: relative;
  width: 2.16em;          /* 2 CJK chars at the headline font-size */
  height: 1.18em;          /* matches h1 line-height */
  vertical-align: baseline;
  margin: 0 0.08em;
  overflow: hidden;
}
h1 .accent-verb {
  font-family: 'Noto Serif SC', serif;
  font-weight: 600;
  color: var(--ss-accent);
  position: absolute;
  inset: 0;
  text-align: center;
  white-space: nowrap;
  /* keep the baseline aligned with the surrounding headline */
  line-height: 1.18;
}
h1 .period { color: var(--ss-fg-muted); }

/* Slot-machine slide: outgoing verb translates up & out the top,
   incoming verb translates up from the bottom into the slot. Both
   are absolutely positioned inside .verb-slot so they overlap during
   the transition (no out-in mode — both elements are present). */
.rip-enter-active,
.rip-leave-active {
  transition: transform 0.5s cubic-bezier(0.32, 0.72, 0, 1), opacity 0.5s ease;
}
.rip-enter-from { transform: translateY(100%); opacity: 0; }
.rip-leave-to   { transform: translateY(-100%); opacity: 0; }
@media (prefers-reduced-motion: reduce) {
  .rip-enter-active,
  .rip-leave-active {
    transition: none;
  }
  .rip-enter-from { transform: none; opacity: 1; }
  .rip-leave-to   { transform: none; opacity: 1; }
}

.sub {
  font-size: 14px;
  color: var(--ss-fg-muted);
  margin-bottom: 34px;
  max-width: 560px;
  line-height: 1.7;
}

/* Composer */
.composer {
  border: 1px solid var(--ss-line-strong);
  border-radius: 12px;
  background: #fff;
  box-shadow: 0 1px 0 rgba(0,0,0,0.02), 0 14px 44px -26px rgba(0,0,0,0.2);
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}
.composer.focused {
  border-color: var(--ss-fg);
  box-shadow: 0 1px 0 rgba(0,0,0,0.04), 0 16px 48px -22px rgba(0,0,0,0.22);
}
.composer.drag-over {
  border-color: var(--ss-accent);
  background: var(--ss-accent-soft);
}

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 14px 16px 0;
}
.chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 6px 5px 8px;
  background: var(--ss-bg-soft);
  border: 1px solid var(--ss-line);
  border-radius: 6px;
  font-size: 12px;
  color: var(--ss-fg);
}
.chip .chip-ico { color: var(--ss-fg-muted); flex-shrink: 0; }
.chip .chip-name {
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.chip .chip-size {
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  color: var(--ss-fg-faint);
}
.chip.url {
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
}
.chip.restored {
  background: var(--ss-accent-soft);
  border-color: var(--ss-accent);
}
.chip.restored .chip-ico { color: var(--ss-accent); }
.chip-badge {
  font-family: 'Fraunces', serif;
  font-style: italic;
  font-size: 10px;
  color: var(--ss-accent);
  margin-left: 2px;
}
.chip-x {
  background: none;
  border: 0;
  color: var(--ss-fg-faint);
  cursor: pointer;
  font-size: 14px;
  line-height: 1;
  padding: 0 2px;
}
.chip-x:hover { color: var(--ss-bad); }

.ta {
  width: 100%;
  border: 0;
  outline: none;
  resize: none;
  font: inherit;
  color: var(--ss-fg);
  padding: 14px 16px 6px;
  min-height: 96px;
  background: transparent;
}
/* The .composer:focus-within rule already darkens the container border
   on textarea focus (visible feedback). For strict a11y tooling that
   inspects the textarea itself, add a subtle inset box-shadow on direct
   keyboard focus so automated auditors don't report a missing
   indicator. */
.ta:focus-visible {
  box-shadow: inset 0 0 0 1px var(--ss-accent);
  border-radius: 8px;
}
.ta::placeholder {
  color: var(--ss-fg-faint);
  white-space: pre-line;
}

.composer-foot {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px 12px 12px;
}
.tool {
  width: 44px;
  height: 44px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid transparent;
  border-radius: 8px;
  color: var(--ss-fg-muted);
  cursor: pointer;
  background: none;
}
.tool:hover {
  background: var(--ss-bg-soft);
  border-color: var(--ss-line);
}

.hint {
  font-size: 11px;
  color: var(--ss-fg-faint);
  margin-left: 4px;
}
.hint kbd {
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  border: 1px solid var(--ss-line);
  border-radius: 3px;
  padding: 0 4px;
  margin: 0 1px;
  background: var(--ss-bg-soft);
  color: var(--ss-fg-muted);
}

.spacer { flex: 1; }
.status { font-size: 11px; color: var(--ss-fg-muted); }
.status.bad { color: var(--ss-bad); }

.submit {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 9px 14px 9px 16px;
  min-height: 44px;
  background: var(--ss-fg);
  color: #fff;
  border: 0;
  border-radius: 8px;
  font: 500 13px/1 'Inter', sans-serif;
  cursor: pointer;
  transition: background 0.15s ease, box-shadow 0.15s ease;
}
.submit:hover:not(:disabled) { background: var(--ss-accent); }
.submit:focus-visible {
  outline: none;
  box-shadow: 0 0 0 2px #fff, 0 0 0 4px var(--ss-accent);
}
.submit:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

/* Error banner — shown when onStart or its subcalls fail */
.error-banner {
  margin-top: 16px;
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 12px 14px;
  border: 1px solid var(--ss-bad);
  border-color: var(--ss-bad);
  background: #fbecec;
  border-radius: 8px;
}
.err-icon {
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: var(--ss-bad);
  color: #fff;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-family: 'Fraunces', serif;
  font-style: italic;
  font-weight: 600;
  flex-shrink: 0;
  margin-top: 2px;
}
.err-body { flex: 1; min-width: 0; }
.err-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--ss-bad);
  margin-bottom: 2px;
}
.err-detail {
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
  color: var(--ss-fg);
  word-break: break-word;
}
.err-hint {
  margin-top: 8px;
  font-size: 12px;
  color: var(--ss-fg);
  line-height: 1.5;
}
.err-hint em {
  font-family: 'Noto Serif SC', serif;
  font-style: normal;
  font-weight: 600;
  color: var(--ss-accent);
}
.err-hint strong { font-weight: 600; }
.err-dismiss {
  background: none;
  border: 0;
  font-size: 18px;
  line-height: 1;
  color: var(--ss-bad);
  cursor: pointer;
  padding: 0 4px;
  margin-left: 4px;
  flex-shrink: 0;
}
.err-dismiss:hover { color: var(--ss-fg); }

/* Examples */
.examples { margin-top: 28px; }
.examples-h {
  font-family: 'Fraunces', serif;
  font-style: italic;
  font-size: 12px;
  color: var(--ss-fg-faint);
  margin-bottom: 10px;
}
.examples-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.example {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 10px;
  min-height: 44px;
  border: 0;
  background: none;
  border-radius: 6px;
  font: 13px 'Inter', sans-serif;
  color: var(--ss-fg-muted);
  cursor: pointer;
  text-align: left;
  width: 100%;
}
.example:hover {
  background: var(--ss-bg-soft);
  color: var(--ss-fg);
}
.example .arrow {
  color: var(--ss-fg-faint);
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
}
.example:hover .arrow { color: var(--ss-accent); }

@media (max-width: 860px) {
  .main { padding: 40px 24px; }
  h1 { font-size: 30px; line-height: 1.2; }
  .wrap { max-width: 100%; }
  .composer-foot { flex-wrap: wrap; }
  .hint { order: 3; flex-basis: 100%; margin-left: 0; margin-top: 4px; }
  .examples { margin-top: 20px; }
}
@media (max-width: 560px) {
  .main { padding: 28px 16px; }
  h1 { font-size: 24px; }
  .submit { padding: 9px 12px; font-size: 12px; }
  .hint { font-size: 10px; }
}
</style>
