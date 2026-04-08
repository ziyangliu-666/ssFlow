<template>
  <article
    class="p-card"
    :class="{ info: !isTrader, expanded }"
    tabindex="0"
    role="button"
    :aria-expanded="expanded"
    @click="onCardClick"
    @keydown.enter.prevent="toggle"
    @keydown.space.prevent="toggle"
  >
    <header class="p-h">
      <span class="glyph">{{ isTrader ? '◆' : '◇' }}</span>
      <div class="title">
        <input
          v-if="expanded"
          v-model="local.archetype"
          class="title-edit"
          @click.stop
        />
        <h3 v-else class="archetype">{{ local.archetype || '(unnamed)' }}</h3>
        <input
          v-if="expanded"
          v-model="local.display_name"
          class="subtitle-edit"
          @click.stop
        />
        <span v-else class="display-name">{{ local.display_name }}</span>
      </div>
      <span class="count-chip" v-if="isTrader">×{{ local.instance_count || 0 }}</span>
      <span class="count-chip" v-else>info</span>
      <button
        class="x"
        type="button"
        :aria-label="`Remove ${local.archetype || 'persona'}`"
        @click.stop="$emit('remove')"
      >×</button>
    </header>

    <div v-if="!expanded && local.voice_prompt" class="voice-preview">
      {{ local.voice_prompt }}
    </div>

    <div v-if="!expanded && isTrader" class="stats">
      <span><em>资金</em>{{ formatCny(local.capital_median_cny) }}</span>
      <span><em>持仓 p=</em>{{ (local.prob_holding || 0).toFixed(2) }}</span>
    </div>

    <div v-if="expanded" class="body" @click.stop>
      <div v-if="isTrader" class="row3">
        <label>
          <span>INSTANCE COUNT</span>
          <input v-model.number="local.instance_count" type="number" min="1" />
        </label>
        <label>
          <span>资金量 (CNY median)</span>
          <input v-model.number="local.capital_median_cny" type="number" min="0" />
        </label>
        <label>
          <span>初始持仓 prob (0–1)</span>
          <input v-model.number="local.prob_holding" type="number" min="0" max="1" step="0.05" />
        </label>
      </div>

      <label class="block">
        <span>VOICE PROMPT</span>
        <textarea v-model="local.voice_prompt" rows="6" />
      </label>
    </div>
  </article>
</template>

<script setup>
import { reactive, ref, computed, watch } from 'vue'

const props = defineProps({ persona: { type: Object, required: true } })
const emit = defineEmits(['update:persona', 'remove'])

const local = reactive({ ...props.persona })
const expanded = ref(false)

const isTrader = computed(() => local.entity_role === 'trader')

watch(() => props.persona, (v) => Object.assign(local, v), { deep: true })
watch(local, (v) => emit('update:persona', { ...v }), { deep: true })

function toggle () { expanded.value = !expanded.value }

function onCardClick (e) {
  // Clicks on inputs / buttons / textareas inside the expanded body must
  // NOT collapse the card. Only collapse when the click lands on the
  // card's whitespace or the header row.
  const t = e.target
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'BUTTON' || t.closest('input') || t.closest('textarea') || t.closest('.body'))) {
    return
  }
  toggle()
}

function formatCny (n) {
  if (typeof n !== 'number' || !n) return '—'
  if (n >= 1e8) return '¥' + (n / 1e8).toFixed(1) + '亿'
  if (n >= 1e4) return '¥' + (n / 1e4).toFixed(0) + '万'
  return '¥' + n
}
</script>

<style scoped>
.p-card {
  border: 1px solid var(--ss-line);
  border-left: 3px solid var(--ss-accent);
  background: #fff;
  border-radius: 8px;
  padding: 12px 14px;
  transition: box-shadow 0.15s ease, border-color 0.15s ease;
  cursor: pointer;
  outline: none;
}
.p-card:hover { box-shadow: 0 6px 20px -12px rgba(0,0,0,0.15); }
.p-card:focus-visible {
  box-shadow: 0 0 0 2px #fff, 0 0 0 4px var(--ss-accent);
}
.p-card.info { border-left-color: var(--ss-info); }
.p-card.expanded { box-shadow: 0 8px 24px -12px rgba(0,0,0,0.18); }

.p-h {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.glyph {
  font-size: 11px;
  color: var(--ss-accent);
  flex-shrink: 0;
}
.p-card.info .glyph { color: var(--ss-info); }

.title {
  flex: 1;
  min-width: 0;
}
.archetype {
  font-family: 'Noto Serif SC', serif;
  font-size: 15px;
  font-weight: 500;
  color: var(--ss-fg);
  word-break: break-all;
}
.display-name {
  display: block;
  font-size: 11px;
  color: var(--ss-fg-muted);
  margin-top: 2px;
}
.title-edit {
  width: 100%;
  font-family: 'Noto Serif SC', serif;
  font-size: 15px;
  font-weight: 500;
  border: 0;
  border-bottom: 1px dashed var(--ss-line-strong);
  padding: 2px 0;
  outline: none;
  background: transparent;
}
.title-edit:focus { border-bottom-color: var(--ss-accent); }
.subtitle-edit {
  width: 100%;
  font-size: 11px;
  color: var(--ss-fg-muted);
  border: 0;
  border-bottom: 1px dashed var(--ss-line);
  padding: 2px 0;
  margin-top: 4px;
  outline: none;
  background: transparent;
}

.count-chip {
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  color: var(--ss-fg-muted);
  background: var(--ss-bg-soft);
  padding: 2px 6px;
  border-radius: 3px;
  flex-shrink: 0;
}
.x {
  background: none;
  border: 0;
  font-size: 16px;
  line-height: 1;
  color: var(--ss-fg-faint);
  cursor: pointer;
  padding: 0 4px;
}
.x:hover { color: var(--ss-bad); }

.voice-preview {
  font-size: 12px;
  color: var(--ss-fg);
  line-height: 1.55;
  margin-top: 8px;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.stats {
  display: flex;
  gap: 14px;
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px dashed var(--ss-line);
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  color: var(--ss-fg-muted);
}
.stats em { font-style: normal; color: var(--ss-fg-faint); margin-right: 4px; }

.body {
  border-top: 1px solid var(--ss-line);
  margin-top: 10px;
  padding-top: 12px;
}
.row3 {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 10px;
  margin-bottom: 12px;
}
.row3 label, .block {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.row3 span, .block span {
  font-size: 10px;
  font-weight: 600;
  color: var(--ss-fg-muted);
  letter-spacing: 0.04em;
}
.row3 input, .block textarea {
  border: 0;
  border-bottom: 1px solid var(--ss-line-strong);
  background: transparent;
  padding: 6px 0;
  font: 500 13px 'Inter', sans-serif;
  color: var(--ss-fg);
  outline: none;
}
.row3 input:focus, .block textarea:focus { border-bottom-color: var(--ss-accent); }
.block textarea {
  resize: vertical;
  min-height: 96px;
  font-weight: 400;
  line-height: 1.55;
}
</style>
