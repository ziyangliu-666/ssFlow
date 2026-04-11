<template>
  <div class="persona-panel" v-if="hasPersonas">
    <div class="panel-h">
      <span class="t">角色内心戏</span>
      <span class="tag">{{ personaCount }} 个</span>
    </div>
    <div class="persona-list">
      <article
        v-for="(p, id) in sortedPersonas"
        :key="id"
        class="persona-card"
      >
        <div class="card-h">
          <div class="name-block">
            <span class="name">{{ p.displayName || p.archetype || id }}</span>
            <span class="arch mono" v-if="p.archetype && p.displayName !== p.archetype">
              {{ p.archetype }}
            </span>
          </div>
          <div class="util-block">
            <span class="util-val mono" :class="utilityClass(p.utility)">
              {{ formatUtility(p.utility) }}
            </span>
            <span
              v-if="typeof p.utilityDelta === 'number' && p.utilityDelta !== 0"
              class="util-delta mono"
              :class="utilityDeltaClass(p.utilityDelta)"
            >
              {{ formatDelta(p.utilityDelta) }}
            </span>
          </div>
        </div>
        <div class="state-grid" v-if="p.state">
          <div
            v-for="key in visibleStateKeys(p)"
            :key="key"
            class="state-row"
          >
            <span class="var-label">{{ (p.labels && p.labels[key]) || key }}</span>
            <span class="var-value mono">{{ formatVal(p.state[key]) }}</span>
          </div>
        </div>
        <div
          v-if="p.breakdown && Object.keys(p.breakdown).length"
          class="breakdown"
        >
          <span
            v-for="[comp, val] in topBreakdown(p.breakdown)"
            :key="comp"
            class="breakdown-chip mono"
            :class="val >= 0 ? 'good' : 'bad'"
          >
            {{ comp }} {{ val >= 0 ? '+' : '' }}{{ val.toFixed(1) }}
          </span>
        </div>
        <div v-if="p.round !== undefined" class="round-tag mono">R{{ p.round }}</div>
      </article>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { session } from '../store/session'

const personas = computed(() => session.personaStates || {})
const hasPersonas = computed(() => Object.keys(personas.value).length > 0)
const personaCount = computed(() => Object.keys(personas.value).length)

// Sort by utility descending so "winners" bubble to the top — this is
// the exact same sort the peer_watchlist uses inside the prompts, so
// the UI mirrors what the agents themselves see.
const sortedPersonas = computed(() => {
  const entries = Object.entries(personas.value)
  entries.sort((a, b) => {
    const ua = typeof a[1].utility === 'number' ? a[1].utility : 0
    const ub = typeof b[1].utility === 'number' ? b[1].utility : 0
    return ub - ua
  })
  return Object.fromEntries(entries)
})

// State keys visible on the card — we filter out internal anchors
// (initial_nav / peak_nav) and keys whose label_zh is empty. The
// backend already applies observable_by_self filtering before
// emitting, so this is belt + suspenders.
const HIDDEN_KEYS = new Set(['initial_nav', 'peak_nav'])

function visibleStateKeys (p) {
  if (!p.state) return []
  const keys = Object.keys(p.state).filter(k => !HIDDEN_KEYS.has(k))
  // Cap at 6 rows so tall cards don't dominate the panel
  return keys.slice(0, 6)
}

function topBreakdown (breakdown) {
  const entries = Object.entries(breakdown)
  entries.sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]))
  return entries.slice(0, 3)
}

function formatUtility (v) {
  if (typeof v !== 'number') return '—'
  const sign = v >= 0 ? '+' : ''
  return `${sign}${v.toFixed(2)}`
}

function formatDelta (v) {
  if (typeof v !== 'number' || v === 0) return ''
  const sign = v >= 0 ? '+' : ''
  const arrow = v >= 0 ? '↑' : '↓'
  return `${arrow} ${sign}${v.toFixed(2)}`
}

function formatVal (v) {
  if (v === null || v === undefined) return '—'
  if (typeof v !== 'number') return String(v)
  const abs = Math.abs(v)
  if (abs >= 1e8) return (v / 1e8).toFixed(2) + '亿'
  if (abs >= 1e4) return (v / 1e4).toFixed(1) + '万'
  if (abs !== 0 && abs < 10) return v.toFixed(2)
  return v.toFixed(0)
}

function utilityClass (v) {
  if (typeof v !== 'number') return ''
  if (v >= 2) return 'good'
  if (v <= -2) return 'bad'
  return ''
}

function utilityDeltaClass (v) {
  if (typeof v !== 'number') return ''
  if (v > 0) return 'good'
  if (v < 0) return 'bad'
  return ''
}
</script>

<style scoped>
.persona-panel {
  padding: 16px 0;
}
.panel-h {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-bottom: 12px;
}
.panel-h .t {
  font-family: 'Fraunces', serif;
  font-style: italic;
  font-size: 14px;
  color: var(--ss-fg);
}
.panel-h .tag {
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
  color: var(--ss-fg-faint);
}

.persona-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.persona-card {
  background: #fff;
  border: 1px solid var(--ss-line);
  border-radius: 8px;
  padding: 10px 14px;
  position: relative;
}

.card-h {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 10px;
  margin-bottom: 6px;
}
.name-block {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.name {
  font-family: 'Noto Serif SC', serif;
  font-size: 13px;
  font-weight: 600;
  color: var(--ss-fg);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.arch {
  font-size: 10px;
  color: var(--ss-fg-faint);
  margin-top: 2px;
}

.util-block {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 2px;
  flex-shrink: 0;
}
.util-val {
  font-size: 13px;
  font-weight: 600;
  color: var(--ss-fg);
}
.util-val.good { color: var(--ss-good); }
.util-val.bad { color: var(--ss-bad); }
.util-delta {
  font-size: 10px;
  color: var(--ss-fg-faint);
}
.util-delta.good { color: var(--ss-good); }
.util-delta.bad { color: var(--ss-bad); }

.state-grid {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 2px 12px;
  margin-top: 6px;
}
.state-row {
  display: contents;
}
.var-label {
  font-size: 11px;
  color: var(--ss-fg-muted);
}
.var-value {
  font-size: 11px;
  text-align: right;
  color: var(--ss-fg);
}

.breakdown {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 6px;
  padding-top: 6px;
  border-top: 1px dashed var(--ss-line);
}
.breakdown-chip {
  font-size: 9px;
  padding: 1px 6px;
  border-radius: 3px;
  background: var(--ss-bg-soft, #f7f7f7);
}
.breakdown-chip.good { color: var(--ss-good); }
.breakdown-chip.bad { color: var(--ss-bad); }

.round-tag {
  position: absolute;
  top: 8px;
  right: 10px;
  font-size: 9px;
  color: var(--ss-fg-faint);
  display: none; /* utility block already has round context; keep hidden */
}
</style>
