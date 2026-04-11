<template>
  <div class="chart">
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
    </svg>

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
import { computed } from 'vue'

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
// A ticker that moved +1% no longer fills the full chart height just
// because it happens to be the only one on screen.
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
  // Pad the visual range a touch so a flat line isn't clamped to the top.
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
</script>

<style scoped>
.chart {
  width: 100%;
  height: 110px;
}
.svg {
  width: 100%;
  height: 100%;
  display: block;
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
