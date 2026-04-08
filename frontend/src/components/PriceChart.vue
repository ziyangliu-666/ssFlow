<template>
  <div class="chart">
    <svg :viewBox="`0 0 ${W} ${H}`" preserveAspectRatio="none" class="svg">
      <defs>
        <linearGradient :id="gradientId" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="var(--ss-accent)" stop-opacity="0.22" />
          <stop offset="100%" stop-color="var(--ss-accent)" stop-opacity="0" />
        </linearGradient>
      </defs>

      <polygon
        v-if="points.length > 1"
        :points="areaPoints"
        :fill="`url(#${gradientId})`"
      />
      <polyline
        v-if="points.length > 1"
        :points="polyPoints"
        class="line"
      />
      <circle
        v-if="points.length"
        :cx="lastPoint.x"
        :cy="lastPoint.y"
        r="3"
        class="last"
      />
    </svg>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  prices: { type: Array, default: () => [] },
})

const W = 380
const H = 110
const padX = 4
const padY = 8

// Make the gradient id unique per instance (in case multiple charts end up
// on the same page).
const gradientId = 'pc-grad-' + Math.random().toString(36).slice(2, 8)

const minPrice = computed(() => (props.prices.length ? Math.min(...props.prices) : 0))
const maxPrice = computed(() => (props.prices.length ? Math.max(...props.prices) : 1))

const points = computed(() => {
  const ps = props.prices
  if (!ps.length) return []
  const spanY = (maxPrice.value - minPrice.value) || 1
  return ps.map((p, i) => {
    const x = padX + ((W - 2 * padX) * (i / Math.max(1, ps.length - 1)))
    const y = padY + ((H - 2 * padY) * (1 - (p - minPrice.value) / spanY))
    return { x, y }
  })
})
const polyPoints = computed(() => points.value.map((p) => `${p.x},${p.y}`).join(' '))
const areaPoints = computed(() => {
  if (!points.value.length) return ''
  const head = points.value.map((p) => `${p.x},${p.y}`).join(' ')
  const last = points.value[points.value.length - 1]
  const first = points.value[0]
  return `${head} ${last.x},${H} ${first.x},${H}`
})
const lastPoint = computed(() => points.value[points.value.length - 1] || { x: 0, y: 0 })
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
  stroke: var(--ss-accent);
  stroke-width: 1.5;
  vector-effect: non-scaling-stroke;
}
.last {
  fill: var(--ss-accent);
}
</style>
