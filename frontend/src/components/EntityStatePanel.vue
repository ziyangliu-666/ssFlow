<template>
  <div class="entity-panel" v-if="hasEntities">
    <div class="panel-h">
      <span class="t">实体状态</span>
      <span class="tag">{{ entityCount }} 个</span>
    </div>
    <div class="entity-list">
      <div
        v-for="(entity, id) in entities"
        :key="id"
        class="entity-card"
        :class="entityTypeClass(entity.type)"
      >
        <div class="entity-h">
          <span class="entity-name">{{ entity.name }}</span>
          <span class="entity-type">{{ typeLabel(entity.type) }}</span>
        </div>
        <div class="state-vars">
          <div v-for="(val, key) in entity.state" :key="key" class="state-row">
            <span class="var-label">{{ (entity.labels && entity.labels[key]) || key }}</span>
            <span class="var-value mono">{{ formatVal(val) }}</span>
          </div>
        </div>
        <div v-if="entity.round !== undefined" class="round-tag">R{{ entity.round }}</div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { session } from '../store/session'

const entities = computed(() => session.entityStates || {})
const hasEntities = computed(() => Object.keys(entities.value).length > 0)
const entityCount = computed(() => Object.keys(entities.value).length)

const TYPE_LABELS = {
  company: '公司',
  supplier: '供应商',
  dealer: '经销商',
  regulator: '监管',
  trader_class: '交易者',
  government: '政府',
  other: '市场环境',
  custom: '自定义',
}

function typeLabel (t) { return TYPE_LABELS[t] || t }

function entityTypeClass (t) {
  if (t === 'company') return 'type-company'
  if (t === 'trader_class') return 'type-trader'
  if (t === 'other') return 'type-other'
  return ''
}

function formatVal (v) {
  if (typeof v !== 'number') return v
  if (v === Math.floor(v)) return v.toString()
  return v.toFixed(2)
}
</script>

<style scoped>
.entity-panel {
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

.entity-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.entity-card {
  background: #fff;
  border: 1px solid var(--ss-line);
  border-radius: 8px;
  padding: 10px 14px;
  position: relative;
}
.entity-card.type-company { border-color: var(--ss-accent); }
.entity-card.type-trader { border-color: var(--ss-fg-muted); }
.entity-card.type-other { border-color: var(--ss-line-strong); }

.entity-h {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 6px;
}
.entity-name {
  font-family: 'Noto Serif SC', serif;
  font-size: 13px;
  font-weight: 600;
  color: var(--ss-fg);
}
.entity-type {
  font-size: 10px;
  color: var(--ss-fg-faint);
  text-transform: uppercase;
}

.state-vars {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 2px 12px;
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

.round-tag {
  position: absolute;
  top: 8px;
  right: 10px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 9px;
  color: var(--ss-fg-faint);
}
</style>
