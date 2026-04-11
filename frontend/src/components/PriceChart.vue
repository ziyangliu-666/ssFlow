<template>
  <div
    class="chart"
    ref="rootEl"
    @mousemove="onMove"
    @mouseleave="onLeave"
  >
    <svg :viewBox="`0 0 ${W} ${H}`" preserveAspectRatio="none" class="svg">
      <defs>
        <linearGradient
          v-for="ticker in drawOrder"
          :key="'grad-' + ticker"
          :id="gradientId + '-' + ticker"
          x1="0" y1="0" x2="0" y2="1"
        >
          <stop
            offset="0%"
            :stop-color="seriesColors[ticker]"
            :stop-opacity="ticker === primaryKey ? 0.08 : 0.03"
          />
          <stop offset="100%" :stop-color="seriesColors[ticker]" stop-opacity="0" />
        </linearGradient>
      </defs>

      <!-- Zero baseline (dashed) — only drawn if the range crosses 0. -->
      <line
        v-if="zeroY !== null"
        :x1="padX" :y1="zeroY"
        :x2="W - padX" :y2="zeroY"
        class="axis-zero"
      />

      <!-- Round gridlines (one per data point). Always uses the primary
           series length if possible, else the longest series we have. -->
      <line
        v-for="gx in gridXs"
        :key="'g' + gx"
        :x1="gx" :y1="padY"
        :x2="gx" :y2="H - padY"
        class="gridline"
      />

      <!-- Render each series. drawOrder puts neutrals first so the
           primary line draws on top. -->
      <template v-for="ticker in drawOrder" :key="ticker">
        <polygon
          v-if="allSeries[ticker] && allSeries[ticker].length > 1"
          :points="areaFor(allSeries[ticker])"
          :fill="`url(#${gradientId}-${ticker})`"
        />
        <polyline
          v-if="allSeries[ticker] && allSeries[ticker].length > 1"
          :points="lineFor(allSeries[ticker])"
          class="line"
          :class="{ 'line-primary': ticker === primaryKey }"
          :style="{ stroke: seriesColors[ticker] }"
        />
        <circle
          v-if="allSeries[ticker] && allSeries[ticker].length"
          :cx="allSeries[ticker][allSeries[ticker].length - 1].x"
          :cy="allSeries[ticker][allSeries[ticker].length - 1].y"
          :r="ticker === primaryKey ? 3 : 2"
          :fill="seriesColors[ticker]"
        />
      </template>

      <!-- Hover crosshair (vertical line) — only drawn when a hover
           index is locked onto a data point. -->
      <line
        v-if="hoverIdx !== null && gridXs.length"
        :x1="gridXs[hoverIdx]" :y1="padY"
        :x2="gridXs[hoverIdx]" :y2="H - padY"
        class="crosshair"
      />
      <!-- Hover dots: one per series at the hover index. -->
      <template v-if="hoverIdx !== null">
        <circle
          v-for="ticker in drawOrder"
          :key="'hd-' + ticker"
          v-show="allSeries[ticker] && allSeries[ticker][hoverIdx]"
          :cx="allSeries[ticker][hoverIdx]?.x"
          :cy="allSeries[ticker][hoverIdx]?.y"
          r="2.5"
          :fill="seriesColors[ticker]"
          class="hover-dot"
        />
      </template>
    </svg>

    <!-- Y-axis range labels — overlay HTML, stays crisp regardless of
         svg stretch. Top = sharedMax %, bottom = sharedMin %. -->
    <span class="y-label y-top mono">{{ formatPct(sharedMax) }}</span>
    <span class="y-label y-bot mono">{{ formatPct(sharedMin) }}</span>

    <!-- Hover tooltip — one row per series, primary highlighted. -->
    <div
      v-if="hoverIdx !== null && tooltipLines.length"
      class="tooltip"
      :style="tooltipStyle"
    >
      <div class="tt-head mono">R{{ hoverIdx }}</div>
      <div
        v-for="line in tooltipLines"
        :key="'tt-' + line.ticker"
        class="tt-row"
        :class="{ 'tt-primary': line.ticker === primaryKey }"
      >
        <span class="tt-dot" :style="{ background: seriesColors[line.ticker] }"></span>
        <span class="tt-ticker mono">{{ line.ticker }}</span>
        <span class="tt-val mono" :class="line.cls">{{ line.pct }}</span>
      </div>
    </div>

    <!-- Legend (only shown for multi-instrument) -->
    <div v-if="isMulti" class="legend">
      <span
        v-for="ticker in drawOrder"
        :key="'leg-' + ticker"
        class="legend-item"
        :class="{ 'legend-primary': ticker === primaryKey }"
      >
        <span class="dot" :style="{ background: seriesColors[ticker] }"></span>
        <span class="mono">{{ ticker }}</span>
      </span>
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'

const props = defineProps({
  // Accept either:
  //   Array<number> — single instrument (legacy / fallback)
  //   Object { ticker: Array<number> } — multi-instrument (normal)
  prices: { type: [Array, Object], default: () => [] },
  // Optional ticker id that should receive the brand accent color.
  // Every other series renders in neutral gray so the composition
  // never drifts into the "rainbow SaaS chart" look.
  primaryTicker: { type: String, default: '' },
})

const W = 380
const H = 110
const padX = 4
const padY = 8

const gradientId = 'pc-grad-' + Math.random().toString(36).slice(2, 8)

// Brand accent for the primary/event-subject series; muted neutrals for the
// rest. No purple, no red, no rainbow — one loud color, everything else
// calibrated to let it dominate. See DESIGN.md §"Color — one accent, used
// sparingly".
const PRIMARY_COLOR = 'var(--ss-accent)'
const NEUTRAL_PALETTE = [
  '#9ca3af',  // gray-400
  '#6b7280',  // gray-500
  '#4b5563',  // gray-600
  '#94a3b8',  // slate-400
  '#64748b',  // slate-500
]

const pricesByTicker = computed(() => {
  if (Array.isArray(props.prices)) {
    return { primary: props.prices }
  }
  if (typeof props.prices === 'object' && props.prices !== null) {
    return props.prices
  }
  return { primary: [] }
})

const isMulti = computed(() => Object.keys(pricesByTicker.value).length > 1)

// Primary ticker is whichever the caller named; default to the first key
// (Array input falls into `primary`).
const primaryKey = computed(() => {
  const keys = Object.keys(pricesByTicker.value)
  if (props.primaryTicker && keys.includes(props.primaryTicker)) {
    return props.primaryTicker
  }
  return keys[0] || ''
})

const seriesColors = computed(() => {
  const keys = Object.keys(pricesByTicker.value)
  const colors = {}
  let neutralIdx = 0
  for (const k of keys) {
    if (k === primaryKey.value) {
      colors[k] = PRIMARY_COLOR
    } else {
      colors[k] = NEUTRAL_PALETTE[neutralIdx % NEUTRAL_PALETTE.length]
      neutralIdx += 1
    }
  }
  return colors
})

// All series share one normalization space so relative moves are visually
// honest. Every price becomes its % change from that series' own opening,
// then we compute min/max across ALL those normalized values together.
const normalizedSeries = computed(() => {
  const result = {}
  for (const [ticker, ps] of Object.entries(pricesByTicker.value)) {
    if (!ps.length) { result[ticker] = []; continue }
    const base = ps[0]
    if (!base || base <= 0) { result[ticker] = ps.map(() => 0); continue }
    result[ticker] = ps.map(p => (p / base - 1) * 100)
  }
  return result
})

const sharedMin = computed(() => {
  let min = Infinity
  for (const ns of Object.values(normalizedSeries.value)) {
    for (const v of ns) {
      if (v < min) min = v
    }
  }
  if (min === Infinity) return -1
  return min < 0 ? min : -1
})

const sharedMax = computed(() => {
  let max = -Infinity
  for (const ns of Object.values(normalizedSeries.value)) {
    for (const v of ns) {
      if (v > max) max = v
    }
  }
  if (max === -Infinity) return 1
  return max > 0 ? max : 1
})

const allSeries = computed(() => {
  const result = {}
  const span = (sharedMax.value - sharedMin.value) || 1
  const base = sharedMin.value
  for (const [ticker, ns] of Object.entries(normalizedSeries.value)) {
    if (!ns.length) { result[ticker] = []; continue }
    result[ticker] = ns.map((v, i) => ({
      x: padX + ((W - 2 * padX) * (i / Math.max(1, ns.length - 1))),
      y: padY + ((H - 2 * padY) * (1 - (v - base) / span)),
    }))
  }
  return result
})

// Render order: all non-primary series first, primary last. SVG paints
// in document order, so primary ends up on top of the neutral stack.
const drawOrder = computed(() => {
  const keys = Object.keys(pricesByTicker.value)
  const pk = primaryKey.value
  return [...keys.filter(k => k !== pk), ...(keys.includes(pk) ? [pk] : [])]
})

// X positions for the round gridlines. Uses the longest series so gridlines
// always span every data point available.
const maxLen = computed(() => {
  let max = 0
  for (const ps of Object.values(pricesByTicker.value)) {
    if (ps.length > max) max = ps.length
  }
  return max
})

const gridXs = computed(() => {
  const n = maxLen.value
  if (n <= 1) return []
  const xs = []
  for (let i = 0; i < n; i++) {
    xs.push(padX + ((W - 2 * padX) * (i / (n - 1))))
  }
  return xs
})

// Y position of the zero-change baseline, if the visible range crosses it.
const zeroY = computed(() => {
  const span = sharedMax.value - sharedMin.value
  if (span <= 0) return null
  if (sharedMin.value >= 0 || sharedMax.value <= 0) return null
  return padY + ((H - 2 * padY) * (1 - (0 - sharedMin.value) / span))
})

function lineFor (pts) {
  return pts.map(p => `${p.x},${p.y}`).join(' ')
}
function areaFor (pts) {
  if (!pts.length) return ''
  // Clamp the polygon bottom to the usable drawing region so the gradient
  // doesn't leak past the line baseline.
  const bottom = H - padY
  const head = pts.map(p => `${p.x},${p.y}`).join(' ')
  return `${head} ${pts[pts.length - 1].x},${bottom} ${pts[0].x},${bottom}`
}

function formatPct (v) {
  const sign = v >= 0 ? '+' : ''
  return `${sign}${v.toFixed(1)}%`
}

// ── Hover crosshair + tooltip ─────────────────────────────────────────

const rootEl = ref(null)
const hoverIdx = ref(null)
const hoverX = ref(0)
const hoverY = ref(0)

function onMove (ev) {
  if (!rootEl.value || maxLen.value <= 1) return
  const rect = rootEl.value.getBoundingClientRect()
  const t = (ev.clientX - rect.left) / rect.width
  if (t < 0 || t > 1) { hoverIdx.value = null; return }
  const idx = Math.round(t * (maxLen.value - 1))
  hoverIdx.value = Math.max(0, Math.min(maxLen.value - 1, idx))
  hoverX.value = ev.clientX - rect.left
  hoverY.value = ev.clientY - rect.top
}

function onLeave () {
  hoverIdx.value = null
}

const tooltipLines = computed(() => {
  if (hoverIdx.value === null) return []
  const lines = []
  for (const ticker of drawOrder.value) {
    const ns = normalizedSeries.value[ticker]
    if (!ns || ns.length === 0) continue
    const idx = Math.min(hoverIdx.value, ns.length - 1)
    const v = ns[idx]
    lines.push({
      ticker,
      pct: formatPct(v),
      cls: v > 0 ? 'good' : v < 0 ? 'bad' : '',
    })
  }
  // Primary last in drawOrder; reverse so primary shows first in tooltip.
  return lines.reverse()
})

const tooltipStyle = computed(() => {
  if (!rootEl.value) return {}
  const rectW = rootEl.value.clientWidth || 300
  // Pin the tooltip near the crosshair but keep it inside the chart width.
  const raw = hoverX.value + 12
  const tooltipW = 140
  const clampedLeft = Math.max(4, Math.min(raw, rectW - tooltipW - 4))
  return {
    left: `${clampedLeft}px`,
    top: `${Math.max(2, hoverY.value - 8)}px`,
  }
})
</script>

<style scoped>
.chart {
  position: relative;
  width: 100%;
  height: 110px;
}
.svg {
  width: 100%;
  height: 100%;
  display: block;
  cursor: crosshair;
}
.gridline {
  stroke: var(--ss-line);
  stroke-width: 1;
  stroke-opacity: 0.35;
  vector-effect: non-scaling-stroke;
}
.axis-zero {
  stroke: var(--ss-fg-faint);
  stroke-width: 1;
  stroke-dasharray: 2 3;
  stroke-opacity: 0.55;
  vector-effect: non-scaling-stroke;
}
.line {
  fill: none;
  stroke-width: 1;
  stroke-opacity: 0.7;
  vector-effect: non-scaling-stroke;
}
.line-primary {
  stroke-width: 1.75;
  stroke-opacity: 1;
}
.crosshair {
  stroke: var(--ss-fg);
  stroke-width: 1;
  stroke-opacity: 0.5;
  stroke-dasharray: 1 2;
  vector-effect: non-scaling-stroke;
  pointer-events: none;
}
.hover-dot {
  pointer-events: none;
}

/* Y-axis labels — absolute HTML overlay so text stays crisp no matter
   how the svg stretches. */
.y-label {
  position: absolute;
  left: 2px;
  font-size: 9px;
  color: var(--ss-fg-faint);
  pointer-events: none;
  background: rgba(255, 255, 255, 0.7);
  padding: 0 2px;
}
.y-top { top: 2px; }
.y-bot { bottom: 2px; }

/* Tooltip */
.tooltip {
  position: absolute;
  min-width: 120px;
  max-width: 160px;
  padding: 6px 8px;
  background: rgba(10, 10, 10, 0.92);
  color: #fff;
  border-radius: 6px;
  font-size: 10px;
  pointer-events: none;
  z-index: 10;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.18);
}
.tt-head {
  font-size: 9px;
  color: #cbd5e1;
  margin-bottom: 4px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.15);
  padding-bottom: 3px;
}
.tt-row {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 2px 0;
  opacity: 0.75;
}
.tt-row.tt-primary {
  opacity: 1;
  font-weight: 600;
}
.tt-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex-shrink: 0;
}
.tt-ticker { flex: 1; color: #e5e7eb; }
.tt-val { color: #fff; }
.tt-val.good { color: #34d399; }
.tt-val.bad  { color: #f87171; }

.legend {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 14px;
  margin-top: 6px;
  padding: 0 4px;
}
.legend-item {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 10px;
  color: var(--ss-fg-faint);
}
.legend-item.legend-primary {
  color: var(--ss-fg);
  font-weight: 600;
}
.legend-item .dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
}
</style>
