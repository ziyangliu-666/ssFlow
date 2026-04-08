<template>
  <div class="home">
    <WorkflowRail />

    <main
      class="main"
      :class="{ 'drag-over': isDragOver }"
      @dragover.prevent="onDragOver"
      @dragleave.prevent="onDragLeave"
      @drop.prevent="onDrop"
    >
      <div class="wrap">
        <div class="eyebrow">A-share sandbox · 群体仿真</div>
        <h1>
          看一条消息如何<span class="ripple">ripple</span>进一个价格<span class="period">。</span>
        </h1>
        <p class="sub">
          拖入研报 / 新闻 / 财报，写下你想跟踪的走势。ssFlow 会抽出要研究的对象、推荐相关角色，
          分轮推演群体行为如何把消息折叠进价格。
        </p>

        <div class="composer" :class="{ focused: isFocused, 'drag-over': isDragOver }">
          <!-- File + URL chips -->
          <div v-if="files.length || detectedUrls.length" class="chips">
            <span
              v-for="(f, i) in files"
              :key="'f' + i"
              class="chip"
            >
              <span class="chip-ico">📄</span>
              <span class="chip-name">{{ f.name }}</span>
              <span class="chip-size">{{ formatBytes(f.size) }}</span>
              <button class="chip-x" @click="removeFile(i)">×</button>
            </span>
            <span
              v-for="(u, i) in detectedUrls"
              :key="'u' + i"
              class="chip url"
            >
              <span class="chip-ico">🔗</span>
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
            <button class="tool" type="button" title="Attach file" @click="triggerInput">
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
            <span v-if="status" class="status">{{ status }}</span>
            <span v-if="session.lastError" class="status bad">{{ session.lastError }}</span>
            <button
              class="submit"
              type="button"
              :disabled="!canSubmit || loading"
              @click="onStart"
            >
              <span v-if="!loading">RUN EXTRACT →</span>
              <span v-else>EXTRACTING…</span>
            </button>
          </div>
        </div>

        <div class="examples">
          <div class="examples-h">Try an example</div>
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
import { ref, computed, watch, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { session } from '../store/session'
import { ensureSession } from '../api/client'
import { uploadFiles } from '../api/upload'
import { runExtract } from '../api/extract'
import WorkflowRail from '../components/WorkflowRail.vue'

const router = useRouter()

const files = ref([])
const prompt = ref(session.prompt || '')
const fileInput = ref(null)
const taEl = ref(null)
const isDragOver = ref(false)
const isFocused = ref(false)
const loading = ref(false)
const status = ref('')

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
  const hasFiles = files.value.length > 0
  return hasText || hasFiles
})

watch(prompt, (v) => { session.prompt = v })

onMounted(() => {
  session.lastError = ''
  // If the user came back from /setup, restore their files so they can
  // edit + resubmit without re-uploading.
  if (session.uploadedFiles && session.uploadedFiles.length && files.value.length === 0) {
    // We only have the server-side file refs, not the original File objects.
    // Leave the files array empty; uploaded state still shows in the rail.
  }
})

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
  try {
    // Wipe last extraction so a re-run replaces it cleanly.
    session.eventProposal = null
    session.personasProposed = []

    await ensureSession()
    if (files.value.length > 0) {
      status.value = 'uploading files…'
      await uploadFiles(files.value)
    }
    status.value = 'analyzing… (LLM classify + synthesize)'

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
    status.value = 'done. opening setup…'
    router.push({ name: 'Setup' })
  } catch (err) {
    console.error(err)
    status.value = 'failed: ' + (err?.response?.data?.detail || err.message)
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.home {
  min-height: 100vh;
  display: flex;
  align-items: stretch;
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
  letter-spacing: 0.06em;
  text-transform: uppercase;
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
h1 .ripple {
  font-family: 'Fraunces', Georgia, serif;
  font-style: italic;
  font-weight: 500;
  color: var(--ss-accent);
  font-size: 0.95em;
  margin: 0 0.08em;
}
h1 .period { color: var(--ss-fg-muted); }

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
.chip .chip-ico { font-size: 11px; color: var(--ss-fg-muted); }
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
  width: 32px;
  height: 32px;
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
  background: var(--ss-fg);
  color: #fff;
  border: 0;
  border-radius: 8px;
  font: 500 13px/1 'Inter', sans-serif;
  cursor: pointer;
  transition: background 0.15s ease;
}
.submit:hover:not(:disabled) { background: var(--ss-accent); }
.submit:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

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
  padding: 8px 10px;
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
  h1 { font-size: 32px; }
}
</style>
