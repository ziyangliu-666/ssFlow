<template>
  <div class="pipeline" :class="{ visible: phase !== 'idle' }">
    <!-- Step 1: Upload -->
    <div class="step" :class="stepClass('upload')">
      <div class="rail">
        <div class="dot-wrap">
          <div class="dot" :class="dotClass('upload')"></div>
        </div>
        <div class="line"></div>
      </div>
      <div class="body">
        <div class="head">
          <span class="title">素材准备</span>
          <span v-if="stepDone('upload') && uploadItems.length" class="badge">{{ uploadItems.length }} 项</span>
          <span v-if="stepActive('upload')" class="active-hint">上传中<span class="dots-anim"></span></span>
        </div>
        <div v-if="stepDone('upload') && uploadItems.length" class="content">
          <div class="item-row" v-for="(item, i) in uploadItems" :key="i">
            <svg v-if="item.type === 'file'" class="item-ico" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6"/></svg>
            <svg v-else class="item-ico" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>
            <span class="item-name">{{ item.name }}</span>
            <span v-if="item.size" class="item-meta mono">{{ item.size }}</span>
            <span v-if="item.badge" class="item-badge">{{ item.badge }}</span>
          </div>
        </div>
        <div v-if="stepActive('upload')" class="content">
          <div class="skeleton-row"><div class="skeleton-bar w60"></div></div>
        </div>
        <div v-if="noFiles && stepDone('upload')" class="content">
          <span class="skip-note">无附件，直接分析文本</span>
        </div>
      </div>
    </div>

    <!-- Step 2: Extract -->
    <div class="step" :class="stepClass('extract')">
      <div class="rail">
        <div class="dot-wrap">
          <div class="dot" :class="dotClass('extract')"></div>
        </div>
        <div class="line"></div>
      </div>
      <div class="body">
        <div class="head">
          <span class="title">事件识别</span>
          <span v-if="stepDone('extract') && eventProposal" class="badge accent">{{ eventProposal.instrument || eventProposal.ticker }}</span>
          <span v-if="stepActive('extract')" class="active-hint">LLM 分析中<span class="dots-anim"></span></span>
        </div>

        <!-- Skeleton while extracting -->
        <div v-if="stepActive('extract')" class="content">
          <div class="skeleton-row"><div class="skeleton-bar w80"></div></div>
          <div class="skeleton-row"><div class="skeleton-bar w50"></div><div class="skeleton-bar w20"></div></div>
          <div class="skeleton-row"><div class="skeleton-bar w40"></div></div>
        </div>

        <!-- Results when done -->
        <div v-if="stepDone('extract') && eventProposal" class="content">
          <!-- Ingested docs -->
          <div v-if="ingestedDocs.length" class="doc-list">
            <div v-for="(doc, i) in ingestedDocs" :key="i" class="doc-item">
              <span class="doc-kind" :class="doc.kind">{{ docKindLabel(doc.kind) }}</span>
              <span class="doc-title">{{ doc.title || doc.source }}</span>
              <span class="doc-chars mono">{{ formatChars(doc.char_count) }}</span>
            </div>
          </div>

          <!-- Event proposal card -->
          <div class="event-card">
            <div class="event-text">{{ eventProposal.event_text }}</div>
            <div class="event-meta">
              <span class="tag accent">{{ eventProposal.ticker }}</span>
              <span class="tag">{{ marketLabel(eventProposal.market) }}</span>
              <span class="tag">{{ eventTypeLabel(eventProposal.event_type) }}</span>
              <span v-if="eventProposal.event_date" class="tag mono">{{ eventProposal.event_date }}</span>
            </div>
            <!-- Confidence bars -->
            <div v-if="eventProposal.confidence" class="conf-grid">
              <div v-for="(val, key) in eventProposal.confidence" :key="key" class="conf-row">
                <span class="conf-label">{{ confLabel(key) }}</span>
                <div class="conf-track">
                  <div class="conf-fill" :style="{ width: (val * 100) + '%' }" :class="confColor(val)"></div>
                </div>
                <span class="conf-val mono">{{ (val * 100).toFixed(0) }}%</span>
              </div>
            </div>
          </div>

          <!-- Personas preview -->
          <div v-if="personas.length" class="persona-preview">
            <div class="preview-h">
              <span class="preview-label">推荐角色</span>
              <span class="preview-count mono">{{ personas.length }} 个</span>
            </div>
            <div class="persona-chips">
              <span
                v-for="p in personas.slice(0, 8)"
                :key="p.id"
                class="persona-chip"
                :class="{ trader: p.entity_role === 'trader' }"
              >
                <span class="p-glyph">{{ p.entity_role === 'trader' ? '◆' : '◇' }}</span>
                {{ p.archetype }}
                <span v-if="p.instance_count" class="p-n mono">×{{ p.instance_count }}</span>
              </span>
              <span v-if="personas.length > 8" class="persona-chip more">+{{ personas.length - 8 }}</span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Step 3: Distill instruments + Sandbox -->
    <div class="step" :class="stepClass('distill')">
      <div class="rail">
        <div class="dot-wrap">
          <div class="dot" :class="dotClass('distill')"></div>
        </div>
        <div class="line"></div>
      </div>
      <div class="body">
        <div class="head">
          <span class="title">关联标的</span>
          <span v-if="stepDone('distill') && instrumentCount" class="badge">{{ instrumentCount }} 个标的</span>
          <span v-if="stepActive('distill')" class="active-hint">提炼中<span class="dots-anim"></span></span>
        </div>

        <div v-if="stepActive('distill')" class="content">
          <div class="skeleton-row"><div class="skeleton-bar w70"></div></div>
          <div class="skeleton-row"><div class="skeleton-bar w40"></div><div class="skeleton-bar w30"></div></div>
        </div>

        <div v-if="stepDone('distill') && instrumentUniverse" class="content">
          <!-- All instruments as equal cards -->
          <div class="inst-grid">
            <div
              v-for="inst in allInstruments"
              :key="inst.ticker"
              class="inst-card-mini"
            >
              <div class="inst-main">
                <span class="inst-name">{{ inst.name }}</span>
                <span class="inst-ticker mono">{{ inst.ticker }}</span>
              </div>
              <div class="inst-row">
                <span class="rel-tag">{{ relLabel(inst.relationship) }}</span>
                <span class="inst-price mono">{{ inst.price_currency || '¥' }}{{ inst.current_price?.toFixed(2) || '—' }}</span>
              </div>
              <div v-if="inst.kline_30d?.length" class="inst-spark">
                <svg :viewBox="'0 0 120 28'" preserveAspectRatio="none" class="spark-svg">
                  <polyline
                    :points="sparkline(inst.kline_30d)"
                    fill="none"
                    stroke="var(--ss-fg-muted)"
                    stroke-width="1.2"
                    vector-effect="non-scaling-stroke"
                  />
                </svg>
              </div>
            </div>
          </div>

          <!-- Entity graph summary -->
          <div v-if="entityGraph" class="entity-summary">
            <span class="preview-label">实体沙盘</span>
            <div class="entity-chips">
              <span
                v-for="(ent, eid) in entityGraph.entities"
                :key="eid"
                class="entity-chip"
              >{{ ent.display_name }}<span class="ent-type-tag">{{ entTypeLabel(ent.entity_type) }}</span></span>
            </div>
            <div v-if="entityGraph.flows?.length" class="flow-count mono">
              {{ entityGraph.flows.length }} 条资源流 · {{ (entityGraph.thresholds || []).length }} 个触发器
            </div>
          </div>

          <!-- Round schedule -->
          <div v-if="roundSchedule?.rounds?.length" class="schedule-bar">
            <span
              v-for="rd in roundSchedule.rounds"
              :key="rd.id"
              class="round-tag mono"
            >{{ rd.label }}</span>
          </div>
        </div>
      </div>
    </div>

    <!-- Step 4: Ready -->
    <div class="step last" :class="stepClass('ready')">
      <div class="rail">
        <div class="dot-wrap">
          <div class="dot" :class="dotClass('ready')"></div>
        </div>
      </div>
      <div class="body">
        <div class="head">
          <span class="title">准备就绪</span>
          <span v-if="stepActive('ready')" class="active-hint done-hint">全部完成</span>
        </div>
        <div v-if="stepActive('ready')" class="content">
          <div class="ready-summary">
            <span v-if="eventProposal">{{ eventProposal.instrument || eventProposal.ticker }}</span>
            <span v-if="personas.length" class="mono">· {{ personas.length }} 个角色</span>
            <span v-if="instrumentCount" class="mono">· {{ instrumentCount }} 个标的</span>
          </div>
          <button class="go-btn" type="button" @click="$emit('go-setup')">
            确认结果 →
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { session } from '../store/session'

const props = defineProps({
  phase: { type: String, default: 'idle' },
  localFiles: { type: Array, default: () => [] },
  detectedUrls: { type: Array, default: () => [] },
})

defineEmits(['go-setup'])

// Phase ordering for comparison
const PHASES = ['idle', 'uploading', 'extracting', 'distilling', 'done']
function phaseIdx (p) { return PHASES.indexOf(p) }

// Step-to-phase mapping
const STEP_PHASE = { upload: 'uploading', extract: 'extracting', distill: 'distilling', ready: 'done' }

function stepActive (step) { return props.phase === STEP_PHASE[step] }
function stepDone (step) { return phaseIdx(props.phase) > phaseIdx(STEP_PHASE[step]) }
function stepWaiting (step) { return phaseIdx(props.phase) < phaseIdx(STEP_PHASE[step]) }

function stepClass (step) {
  if (stepActive(step)) return 'active'
  if (stepDone(step)) return 'done'
  return 'waiting'
}
function dotClass (step) {
  if (stepActive(step)) return 'live'
  if (stepDone(step)) return 'done'
  return ''
}

// Data from session store
const eventProposal = computed(() => session.eventProposal)
const personas = computed(() => session.personasProposed || [])
const ingestedDocs = computed(() => session.ingestedDocs || [])
const instrumentUniverse = computed(() => session.instrumentUniverse)
const roundSchedule = computed(() => session.roundSchedule)
const entityGraph = computed(() => session.entityGraph)

const noFiles = computed(() => !props.localFiles.length && !(session.uploadedFiles?.length))

const uploadItems = computed(() => {
  const items = []
  // Server files (already uploaded)
  if (session.uploadedFiles?.length) {
    for (const f of session.uploadedFiles) {
      items.push({ type: 'file', name: f.name, size: formatBytes(f.size), badge: '' })
    }
  }
  // Local files just uploaded
  for (const f of props.localFiles) {
    const exists = items.some(x => x.name === f.name)
    if (!exists) items.push({ type: 'file', name: f.name, size: formatBytes(f.size), badge: '' })
  }
  // URLs
  for (const u of props.detectedUrls) {
    items.push({ type: 'url', name: shortenUrl(u), size: '', badge: '链接' })
  }
  return items
})

const allInstruments = computed(() => {
  if (!instrumentUniverse.value) return []
  const u = instrumentUniverse.value
  // New flat format: u.instruments; legacy: [u.primary, ...u.related]
  return (u.instruments || [u.primary, ...(u.related || [])]).filter(Boolean)
})

const instrumentCount = computed(() => allInstruments.value.length)

// Helpers
function formatBytes (n) {
  if (!n) return ''
  if (n < 1024) return n + 'B'
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + 'KB'
  return (n / 1024 / 1024).toFixed(1) + 'MB'
}
function formatChars (n) {
  if (!n) return ''
  if (n >= 10000) return (n / 10000).toFixed(1) + '万字'
  return n + '字'
}
function shortenUrl (u) {
  try {
    const p = new URL(u)
    const host = p.hostname.replace(/^www\./, '')
    const path = p.pathname === '/' ? '' : p.pathname
    const display = host + path
    return display.length > 36 ? display.slice(0, 35) + '…' : display
  } catch (_) {
    return u.length > 36 ? u.slice(0, 35) + '…' : u
  }
}

const MARKET_LABELS = {
  ashare: 'A股', 'us-equity': '美股', 'hk-equity': '港股',
  'jp-equity': '日股', 'crude-oil-wti': '原油', 'btc-spot': 'BTC',
  'gold-spot': '黄金', other: '其他',
}
function marketLabel (m) { return MARKET_LABELS[m] || m || '' }

const EVENT_TYPE_LABELS = {
  earnings: '财报', ipo: 'IPO', dividend: '分红', shareholder_action: '股东行为',
  m_a: '并购', management_change: '管理层变动', supply_disruption: '供应中断',
  demand_shock: '需求冲击', opec_meeting: 'OPEC', inventory_release: '库存数据',
  weather: '天气', geopolitical: '地缘', halving: '减半', exchange_event: '交易所事件',
  protocol_upgrade: '协议升级', policy: '政策', regulatory: '监管', lawsuit: '诉讼',
  macro: '宏观', other: '其他',
}
function eventTypeLabel (t) { return EVENT_TYPE_LABELS[t] || t || '' }

const CONF_LABELS = {
  market: '市场', instrument: '标的', ticker: '代码', event_type: '类型', event_date: '日期',
}
function confLabel (k) { return CONF_LABELS[k] || k }
function confColor (v) {
  if (v >= 0.8) return 'high'
  if (v >= 0.5) return 'mid'
  return 'low'
}

const REL_LABELS = {
  event_subject: '事件相关', primary: '事件相关',
  supplier: '供应商', competitor: '竞争对手', customer: '客户',
  sector_etf: '板块ETF', upstream: '上游', downstream: '下游', peer: '同行',
  opposing: '对立', index: '指数', other: '其他',
}
function relLabel (r) { return REL_LABELS[r] || r || '' }

const ENT_TYPE_LABELS = {
  company: '公司', supplier: '供应商', dealer: '经销商', regulator: '监管',
  trader_class: '交易者', government: '政府', other: '市场环境', custom: '自定义',
}
function entTypeLabel (t) { return ENT_TYPE_LABELS[t] || t || '' }

function docKindLabel (k) {
  const m = { text: '文本', pdf: 'PDF', html: '网页', image: '图片', url: '链接' }
  return m[k] || k || ''
}

function sparkline (bars) {
  if (!bars.length) return ''
  const closes = bars.map(b => b.close)
  const min = Math.min(...closes)
  const max = Math.max(...closes)
  const range = max - min || 1
  const w = 120
  const h = 28
  const pad = 2
  return closes.map((c, i) => {
    const x = (i / (closes.length - 1)) * w
    const y = pad + (1 - (c - min) / range) * (h - 2 * pad)
    return `${x.toFixed(1)},${y.toFixed(1)}`
  }).join(' ')
}
</script>

<style scoped>
.pipeline {
  margin-top: 24px;
  opacity: 0;
  transform: translateY(12px);
  transition: opacity 0.4s ease, transform 0.4s ease;
  pointer-events: none;
}
.pipeline.visible {
  opacity: 1;
  transform: translateY(0);
  pointer-events: auto;
}

/* Step layout */
.step {
  display: flex;
  gap: 14px;
  padding-bottom: 4px;
}
.step.last { padding-bottom: 0; }

/* Rail (timeline) */
.rail {
  display: flex;
  flex-direction: column;
  align-items: center;
  width: 20px;
  flex-shrink: 0;
}
.dot-wrap {
  width: 20px;
  height: 20px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--ss-line-strong);
  transition: background 0.3s ease, box-shadow 0.3s ease;
}
.dot.live {
  background: var(--ss-accent);
  box-shadow: 0 0 0 3px var(--ss-accent-soft);
  animation: pulse 1.4s infinite;
}
.dot.done {
  background: var(--ss-accent);
}
.line {
  width: 2px;
  flex: 1;
  min-height: 12px;
  background: var(--ss-line);
  transition: background 0.3s ease;
}
.step.done .line { background: var(--ss-accent); }
.step.active .line { background: linear-gradient(to bottom, var(--ss-accent) 40%, var(--ss-line) 100%); }

/* Body */
.body {
  flex: 1;
  min-width: 0;
  padding-bottom: 16px;
}
.step.last .body { padding-bottom: 0; }

.head {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 20px;
}
.title {
  font-size: 13px;
  font-weight: 600;
  color: var(--ss-fg);
}
.step.waiting .title { color: var(--ss-fg-faint); }

.badge {
  font-size: 11px;
  color: var(--ss-fg-muted);
  background: var(--ss-bg-soft);
  padding: 1px 7px;
  border-radius: 3px;
}
.badge.accent {
  background: var(--ss-accent-soft);
  color: var(--ss-accent);
  font-weight: 500;
}

.active-hint {
  font-size: 11px;
  color: var(--ss-fg-muted);
}
.done-hint { color: var(--ss-accent); font-weight: 500; }

/* Animated dots */
.dots-anim::after {
  content: '';
  animation: dots 1.5s steps(4, end) infinite;
}
@keyframes dots {
  0%  { content: ''; }
  25% { content: '.'; }
  50% { content: '..'; }
  75% { content: '...'; }
}

/* Content area */
.content {
  margin-top: 10px;
  animation: fadeSlideIn 0.4s ease both;
}
@keyframes fadeSlideIn {
  from { opacity: 0; transform: translateY(8px); }
  to   { opacity: 1; transform: translateY(0); }
}

/* Skeleton loading */
.skeleton-row {
  display: flex;
  gap: 8px;
  margin-bottom: 8px;
}
.skeleton-bar {
  height: 12px;
  background: linear-gradient(90deg, var(--ss-bg-soft) 25%, #eee 50%, var(--ss-bg-soft) 75%);
  background-size: 200% 100%;
  animation: shimmer 1.8s ease infinite;
  border-radius: 4px;
}
.w20 { width: 20%; }
.w30 { width: 30%; }
.w40 { width: 40%; }
.w50 { width: 50%; }
.w60 { width: 60%; }
.w70 { width: 70%; }
.w80 { width: 80%; }
@keyframes shimmer {
  0%   { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}
@media (prefers-reduced-motion: reduce) {
  .skel { animation: none; }
  .status-dot.pulse { animation: none; }
  .status-label::after { animation: none; }
}

/* Skip note */
.skip-note {
  font-size: 11px;
  color: var(--ss-fg-faint);
  font-style: italic;
}

/* Upload items */
.item-row {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 0;
  font-size: 12px;
  color: var(--ss-fg);
}
.item-ico { color: var(--ss-fg-muted); flex-shrink: 0; }
.item-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.item-meta { font-size: 10px; color: var(--ss-fg-faint); flex-shrink: 0; }
.item-badge {
  font-size: 10px;
  color: var(--ss-info);
  background: rgba(44, 102, 212, 0.08);
  padding: 1px 5px;
  border-radius: 2px;
  flex-shrink: 0;
}

/* Ingested docs */
.doc-list {
  margin-bottom: 12px;
}
.doc-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 0;
  font-size: 12px;
}
.doc-kind {
  font-size: 10px;
  padding: 1px 5px;
  border-radius: 2px;
  background: var(--ss-bg-soft);
  color: var(--ss-fg-muted);
  flex-shrink: 0;
  font-weight: 500;
}
.doc-kind.pdf { background: rgba(196, 60, 60, 0.08); color: var(--ss-bad); }
.doc-kind.html, .doc-kind.url { background: rgba(44, 102, 212, 0.08); color: var(--ss-info); }
.doc-kind.image { background: rgba(107, 107, 107, 0.08); color: var(--ss-fg-muted); }
.doc-title {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--ss-fg);
}
.doc-chars { font-size: 10px; color: var(--ss-fg-faint); flex-shrink: 0; }

/* Event card */
.event-card {
  background: var(--ss-bg-soft);
  border: 1px solid var(--ss-line);
  border-radius: 8px;
  padding: 12px 14px;
  margin-bottom: 12px;
}
.event-text {
  font-size: 13px;
  line-height: 1.6;
  color: var(--ss-fg);
  margin-bottom: 10px;
}
.event-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  margin-bottom: 10px;
}
.tag {
  display: inline-block;
  padding: 1px 7px;
  border: 1px solid var(--ss-line);
  border-radius: 2px;
  font-size: 10px;
  color: var(--ss-fg-muted);
  background: #fff;
}
.tag.accent {
  background: var(--ss-accent-soft);
  color: var(--ss-accent);
  border-color: var(--ss-accent);
  font-weight: 500;
}
.tag.mono, .mono {
  font-family: 'JetBrains Mono', monospace;
  font-variant-numeric: tabular-nums;
}

/* Confidence bars */
.conf-grid {
  display: grid;
  grid-template-columns: 36px 1fr 32px;
  gap: 4px 8px;
  align-items: center;
}
.conf-label { font-size: 10px; color: var(--ss-fg-faint); }
.conf-track {
  height: 4px;
  background: var(--ss-line);
  border-radius: 2px;
  overflow: hidden;
}
.conf-fill {
  height: 100%;
  border-radius: 2px;
  transition: width 0.6s ease;
}
.conf-fill.high { background: var(--ss-accent); }
.conf-fill.mid  { background: var(--ss-fg-muted); }
.conf-fill.low  { background: var(--ss-fg-faint); }
.conf-val { font-size: 10px; color: var(--ss-fg-faint); text-align: right; }

/* Persona preview */
.persona-preview {
  margin-top: 0;
}
.preview-h {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 8px;
}
.preview-label {
  font-family: 'Fraunces', serif;
  font-style: italic;
  font-size: 11px;
  color: var(--ss-fg-faint);
}
.preview-count { font-size: 10px; color: var(--ss-fg-faint); }

.persona-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}
.persona-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 8px;
  background: #fff;
  border: 1px solid var(--ss-line);
  border-radius: 4px;
  font-size: 11px;
  color: var(--ss-fg);
}
.persona-chip.trader { font-weight: 500; }
.persona-chip.more {
  color: var(--ss-fg-faint);
  font-style: italic;
}
.p-glyph { font-size: 9px; color: var(--ss-accent); }
.persona-chip:not(.trader) .p-glyph { color: var(--ss-info); }
.p-n { font-size: 9px; color: var(--ss-fg-faint); }

/* Instrument grid — all instruments equal */
.inst-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  gap: 8px;
  margin-bottom: 10px;
}
.inst-card-mini {
  border: 1px solid var(--ss-line);
  border-radius: 6px;
  padding: 8px 10px;
  background: #fff;
}
.inst-main { min-width: 0; margin-bottom: 4px; }
.inst-name {
  font-family: 'Noto Serif SC', serif;
  font-size: 13px;
  font-weight: 500;
  color: var(--ss-fg);
}
.inst-ticker {
  font-size: 10px;
  color: var(--ss-fg-muted);
  margin-left: 5px;
}
.inst-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 4px;
}
.inst-price {
  font-size: 12px;
  font-weight: 500;
  color: var(--ss-fg);
}
.inst-spark { }
.spark-svg {
  width: 100%;
  height: 28px;
}
.rel-name { flex: 1; color: var(--ss-fg); min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rel-ticker { font-size: 11px; color: var(--ss-fg-muted); flex-shrink: 0; }
.rel-tag {
  font-size: 10px;
  padding: 1px 5px;
  border-radius: 2px;
  background: var(--ss-bg-soft);
  color: var(--ss-fg-muted);
  flex-shrink: 0;
}
.rel-price { font-size: 11px; color: var(--ss-fg); flex-shrink: 0; }

/* Entity summary */
.entity-summary {
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px dashed var(--ss-line);
}
.entity-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  margin-top: 6px;
}
.entity-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 8px;
  background: #fff;
  border: 1px solid var(--ss-line);
  border-radius: 4px;
  font-size: 11px;
  color: var(--ss-fg);
}
.ent-type-tag {
  font-size: 9px;
  color: var(--ss-fg-faint);
  background: var(--ss-bg-soft);
  padding: 0 4px;
  border-radius: 2px;
}
.flow-count {
  font-size: 10px;
  color: var(--ss-fg-faint);
  margin-top: 6px;
}

/* Schedule bar */
.schedule-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px dashed var(--ss-line);
}
.round-tag {
  font-size: 10px;
  padding: 2px 6px;
  background: var(--ss-bg-soft);
  border: 1px solid var(--ss-line);
  border-radius: 3px;
  color: var(--ss-fg-muted);
}

/* Ready summary */
.ready-summary {
  font-size: 12px;
  color: var(--ss-fg-muted);
  margin-bottom: 10px;
}

/* Go button */
.go-btn {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  margin-top: 4px;
  padding: 10px 20px;
  background: var(--ss-accent);
  color: #fff;
  border: 0;
  border-radius: 8px;
  font: 500 13px/1 'Inter', sans-serif;
  cursor: pointer;
  transition: background 0.15s ease;
}
.go-btn:hover { background: #d65800; }
.go-btn:focus-visible {
  outline: none;
  box-shadow: 0 0 0 2px #fff, 0 0 0 4px var(--ss-accent);
}

@media (max-width: 560px) {
  .step { gap: 10px; }
  .event-meta { gap: 4px; }
  .inst-grid { grid-template-columns: 1fr; }
  .conf-grid { grid-template-columns: 30px 1fr 28px; }
}
</style>
