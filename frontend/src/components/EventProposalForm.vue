<template>
  <div class="form">
    <div class="field">
      <label>标的</label>
      <input v-model="local.instrument" />
    </div>

    <div class="two">
      <div class="field">
        <label>代码</label>
        <input v-model="local.ticker" />
      </div>
      <div class="field">
        <label>市场</label>
        <input v-model="local.market" />
      </div>
    </div>

    <div class="two">
      <div class="field">
        <label>事件类型</label>
        <input v-model="local.event_type" />
      </div>
      <div class="field">
        <label>日期 (YYYY-MM-DD)</label>
        <input v-model="local.event_date" />
      </div>
    </div>

    <div class="two">
      <div class="field">
        <label>现价</label>
        <input v-model.number="local.current_price" type="number" step="0.01" />
      </div>
      <div class="field">
        <label>币种</label>
        <input v-model="local.price_currency" />
      </div>
    </div>

    <div class="field">
      <label>日均成交额 (同币种)</label>
      <input v-model.number="local.adv_value" type="number" />
    </div>

    <div class="field">
      <label>事件描述</label>
      <textarea v-model="local.event_text" rows="3" />
    </div>

    <button
      type="button"
      class="more-toggle"
      @click="moreOpen = !moreOpen"
    >
      {{ moreOpen ? '− 收起上下文' : '+ 补充上下文（市场共识 / 近期走势 / 板块）' }}
    </button>

    <div v-if="moreOpen" class="more-block">
      <div class="field">
        <label>市场共识</label>
        <textarea v-model="local.prior_consensus" rows="2" />
      </div>
      <div class="field">
        <label>近期走势</label>
        <textarea v-model="local.recent_price_action" rows="2" />
      </div>
      <div class="field">
        <label>板块背景</label>
        <textarea v-model="local.sector_context" rows="2" />
      </div>
    </div>

    <div v-if="confidenceItems.length" class="confidence">
      <span v-for="ci in confidenceItems" :key="ci.key" class="conf-pill" :class="ci.cls">
        <em>{{ ci.key }}</em>{{ ci.label }}
      </span>
    </div>
  </div>
</template>

<script setup>
import { reactive, ref, watch, computed } from 'vue'

const props = defineProps({ modelValue: { type: Object, default: () => ({}) } })
const emit = defineEmits(['update:modelValue'])

const local = reactive({ ...props.modelValue })
const moreOpen = ref(false)

watch(() => props.modelValue, (v) => Object.assign(local, v), { deep: true })
watch(local, (v) => emit('update:modelValue', { ...v }), { deep: true })

const confidenceItems = computed(() => {
  const c = props.modelValue?.confidence || {}
  return Object.keys(c).map((k) => {
    const v = Number(c[k]) || 0
    let cls = 'low'
    if (v >= 0.8) cls = 'high'
    else if (v >= 0.5) cls = 'mid'
    return { key: k, label: ` ${(v * 100).toFixed(0)}%`, cls }
  })
})
</script>

<style scoped>
.form {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.field label {
  font-size: 11px;
  color: var(--ss-fg-muted);
  letter-spacing: 0.02em;
}
.field input,
.field textarea {
  width: 100%;
  border: 0;
  border-bottom: 1px solid var(--ss-line-strong);
  background: transparent;
  padding: 6px 0;
  font: 500 14px 'Inter', sans-serif;
  color: var(--ss-fg);
  outline: none;
}
.field input:focus,
.field textarea:focus { border-bottom-color: var(--ss-accent); }
.field textarea {
  resize: vertical;
  min-height: 52px;
  font-weight: 400;
  line-height: 1.55;
  font-family: 'Inter', sans-serif;
}

.two {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
}

.more-toggle {
  margin-top: 4px;
  font-family: 'Fraunces', serif;
  font-style: italic;
  font-size: 12px;
  color: var(--ss-fg-muted);
  background: none;
  border: 0;
  padding: 4px 0;
  cursor: pointer;
  text-align: left;
}
.more-toggle:hover { color: var(--ss-accent); }

.more-block {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding-top: 4px;
}

.confidence {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px dashed var(--ss-line);
}
.conf-pill {
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  padding: 3px 8px;
  border-radius: 4px;
  background: var(--ss-bg-soft);
  color: var(--ss-fg-muted);
}
.conf-pill em {
  font-style: normal;
  color: var(--ss-fg-faint);
  margin-right: 4px;
}
.conf-pill.high { background: #e7f5ec; color: var(--ss-good); }
.conf-pill.mid  { background: var(--ss-accent-soft); color: var(--ss-accent); }
.conf-pill.low  { background: #fbecec; color: var(--ss-bad); }
</style>
