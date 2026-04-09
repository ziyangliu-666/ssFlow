<template>
  <div class="chart">
    <svg :viewBox="`0 0 ${W} ${H}`" preserveAspectRatio="none" class="svg">
      <defs>
        <linearGradient
          v-for="(color, ticker) in seriesColors"
          :key="'grad-' + ticker"
          :id="gradientId + '-' + ticker"
          x1="0" y1="0" x2="0" y2="1"
        >
          <stop offset="0%" :stop-color="color" stop-opacity="0.08" />
          <stop offset="100%" :stop-color="color" stop-opacity="0" />
        </linearGradient>
      </defs>

      <!-- Render each series -->
      <template v-for="(pts, ticker) in allSeries" :key="ticker">
        <polygon
          v-if="pts.length > 1"
          :points="areaFor(pts)"
          :fill="`url(#${gradientId}-${ticker})`"
        />
        <polyline
          v-if="pts.length > 1"
          :points="lineFor(pts)"
          class="line"
          :style="{ stroke: seriesColors[ticker] || 'var(--ss-accent)' }"
        />
        <circle
          v-if="pts.length"
          :cx="pts[pts.length - 1].x"
          :cy="pts[pts.length - 1].y"
          r="3"
          :fill="seriesColors[ticker] || 'var(--ss-accent)'"
        />
      </template>
    </svg>

    <!-- Legend (only shown for multi-instrument) -->
    <div v-if="isMulti" class="legend">
      <span
        v-for="(color, ticker) in seriesColors"
        :key="'leg-' + ticker"
        class="legend-item"
      >
        <span class="dot" :style="{ background: color }"></span>
        <span class="mono">{{ ticker }}</span>
      </span>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  // Accept either:
  //   Array<number> — single instrument (backward compat)
  //   Object { ticker: Array<number> } — multi-instrument
  prices: { type: [Array, Object], default: () => [] },
})

const W = 380
const H = 110
const padX = 4
const padY = 8

const gradientId = 'pc-grad-' + Math.random().toString(36).slice(2, 8)

// Palette for up to 6 instruments
const PALETTE = [
  'var(--ss-accent)',  // orange — primary
  '#2563eb',           // blue
  '#059669',           // green
  '#7c3aed',           // purple
  '#dc2626',           // red
  '#ca8a04',           // yellow
]

const isMulti = computed(() => !Array.isArray(props.prices) && typeof props.prices === 'object')

const pricesByTicker = computed(() => {
  if (Array.isArray(props.prices)) {
    return { primary: props.prices }
  }
  if (typeof props.prices === 'object' && props.prices !== null) {
    return props.prices
  }
  return { primary: [] }
})

const seriesColors = computed(() => {
  const tickers = Object.keys(pricesByTicker.value)
  const colors = {}
  tickers.forEach((t, i) => {
    colors[t] = PALETTE[i % PALETTE.length]
  })
  return colors
})

// Compute global min/max across ALL series for consistent Y axis
const globalMin = computed(() => {
  let min = Infinity
  for (const ps of Object.values(pricesByTicker.value)) {
    for (const p of ps) {
      if (p < min) min = p
    }
  }
  return min === Infinity ? 0 : min
})

const globalMax = computed(() => {
  let max = -Infinity
  for (const ps of Object.values(pricesByTicker.value)) {
    for (const p of ps) {
      if (p > max) max = p
    }
  }
  return max === -Infinity ? 1 : max
})

// For multi-instrument, normalize prices to % change from initial for fair comparison
const allSeries = computed(() => {
  const result = {}
  const spanY = (globalMax.value - globalMin.value) || 1

  for (const [ticker, ps] of Object.entries(pricesByTicker.value)) {
    if (!ps.length) { result[ticker] = []; continue }

    // For multi-instrument, use percentage change normalization
    const useNorm = isMulti.value && ps.length > 0
    const basePrice = useNorm ? ps[0] : null
    const normPs = useNorm
      ? ps.map(p => basePrice > 0 ? ((p / basePrice - 1) * 100) : 0)
      : ps

    // Recalculate span for normalized values
    const localMin = Math.min(...normPs)
    const localMax = Math.max(...normPs)
    const localSpan = (useNorm ? (localMax - localMin) : spanY) || 1
    const localBase = useNorm ? localMin : globalMin.value

    result[ticker] = normPs.map((p, i) => ({
      x: padX + ((W - 2 * padX) * (i / Math.max(1, normPs.length - 1))),
      y: padY + ((H - 2 * padY) * (1 - (p - localBase) / localSpan)),
    }))
  }
  return result
})

function lineFor (pts) {
  return pts.map(p => `${p.x},${p.y}`).join(' ')
}
function areaFor (pts) {
  if (!pts.length) return ''
  const head = pts.map(p => `${p.x},${p.y}`).join(' ')
  return `${head} ${pts[pts.length - 1].x},${H} ${pts[0].x},${H}`
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
  stroke-width: 1.5;
  vector-effect: non-scaling-stroke;
}
.legend {
  display: flex;
  gap: 12px;
  margin-top: 4px;
  padding: 0 4px;
}
.legend-item {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 10px;
  color: var(--ss-fg-muted);
}
.legend-item .dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
}
</style>
