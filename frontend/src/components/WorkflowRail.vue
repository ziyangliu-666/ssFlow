<template>
  <aside class="rail" aria-label="ssFlow sidebar">
    <router-link
      to="/"
      class="brand"
      aria-label="ssFlow home"
    >ss<em>Flow</em></router-link>

    <nav class="rail-nav" aria-label="推演流程">
      <div class="rail-title">流程</div>
      <ol class="rail-list">
        <li
          v-for="s in steps"
          :key="s.num"
          :class="[statusClass(s), s.to ? 'linked' : 'disabled']"
          :aria-current="s.active ? 'step' : undefined"
        >
          <router-link
            v-if="s.to"
            :to="s.to"
            class="step-link"
            :title="s.tooltip || ''"
          >
            <span class="dot" aria-hidden="true">{{ s.num }}</span>
            <div class="cell">
              <span class="label">{{ s.label }}</span>
              <span class="sub">{{ s.sub }}</span>
            </div>
          </router-link>
          <div v-else class="step-link" :title="s.tooltip || ''" aria-disabled="true">
            <span class="dot" aria-hidden="true">{{ s.num }}</span>
            <div class="cell">
              <span class="label">{{ s.label }}</span>
              <span class="sub">{{ s.sub }}</span>
            </div>
          </div>
        </li>
      </ol>
    </nav>

    <!-- Current event card — fills the space between the workflow
         rail and the footer with context about what the user is
         currently working on. Only visible once there's something
         to show (an extracted event or a remembered sim id). -->
    <div v-if="contextCard" class="ctx-card">
      <div class="ctx-title">{{ contextCard.title }}</div>
      <div class="ctx-body">
        <div class="ctx-row" v-if="contextCard.ticker">
          <span class="ctx-k">代码</span>
          <span class="ctx-v mono">{{ contextCard.ticker }}</span>
        </div>
        <div class="ctx-row" v-if="contextCard.instrument">
          <span class="ctx-v serif">{{ contextCard.instrument }}</span>
        </div>
        <div class="ctx-row" v-if="contextCard.market">
          <span class="ctx-k">市场</span>
          <span class="ctx-v mono">{{ contextCard.market }}</span>
        </div>
        <div class="ctx-row" v-if="contextCard.eventType">
          <span class="ctx-k">事件</span>
          <span class="ctx-v mono">{{ contextCard.eventType }}</span>
        </div>
        <div class="ctx-row" v-if="contextCard.price">
          <span class="ctx-k">价格</span>
          <span class="ctx-v mono">{{ contextCard.price }}</span>
        </div>
      </div>
      <div v-if="contextCard.footnote" class="ctx-foot">{{ contextCard.footnote }}</div>
    </div>

    <div class="rail-foot">
      <slot name="foot-extra" />

      <button
        class="gear"
        type="button"
        :title="authTitle"
        @click="showAuth = !showAuth"
      >
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" stroke-width="2"
             stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="12" r="3" />
          <path d="M12 1v6M12 17v6M4.22 4.22l4.24 4.24M15.54 15.54l4.24 4.24M1 12h6M17 12h6M4.22 19.78l4.24-4.24M15.54 8.46l4.24-4.24" />
        </svg>
        {{ showAuth ? '收起密码' : '设置' }}
      </button>
      <input
        v-if="showAuth"
        v-model="session.password"
        class="auth-input"
        type="password"
        placeholder="认证密码"
      />
      <span class="disclaimer">
        <em>研究工具</em> · 非投资建议
      </span>
    </div>
  </aside>
</template>

<script setup>
import { ref, computed } from 'vue'
import { useRoute } from 'vue-router'
import { session } from '../store/session'

const route = useRoute()
const showAuth = ref(false)

const steps = computed(() => {
  const hasEvent = !!session.eventProposal
  const hasPersonas = (session.personasProposed || []).length > 0
  const onSetup = route.name === 'Setup'
  const onRun = route.name === 'Run'
  const onReport = route.name === 'Report'
  const onHome = route.name === 'Home'

  // Seed is always navigable — it's the entry point, takes you back
  // to Home where the composer lives. The existing session is
  // preserved so the user can tweak the prompt + resubmit without
  // losing uploaded files.
  const seedTo = '/'

  // Confirm is navigable when an extraction has produced an
  // event_proposal. Clicking it takes the user back to /setup where
  // they can edit the proposal or the persona list before running
  // (or re-running) a simulation.
  const confirmTo = hasEvent && hasPersonas ? '/setup' : ''

  // Simulate is navigable while a stream is actively running
  // (session.activeStreamId is set) OR when a prior simulation has
  // completed (session.lastSimulationId is set). In the completed
  // case it points at /replay/:id, which re-plays the persisted
  // event log — the original stream id is one-shot and expired.
  const simulateTo = session.activeStreamId
    ? `/run/${session.activeStreamId}`
    : session.lastSimulationId
      ? `/replay/${session.lastSimulationId}`
      : ''
  const simulateTooltip = !session.activeStreamId && session.lastSimulationId
    ? '推演已完成，可重播'
    : ''

  // Report is navigable once a simulation has completed at least
  // once in this session.
  const reportTo = session.lastSimulationId
    ? `/reports/${session.lastSimulationId}`
    : ''

  return [
    {
      num: '01',
      label: '开始',
      sub: session.uploadedFiles.length
        ? `${session.uploadedFiles.length} 个文件`
        : '文件 + 描述',
      done: !onHome && (hasEvent || session.uploadedFiles.length > 0),
      active: onHome,
      to: seedTo,
    },
    {
      num: '02',
      label: '确认',
      sub: hasEvent
        ? `${session.personasProposed.length} 个角色`
        : '抽取结果',
      done: onRun || onReport,
      active: onSetup,
      to: confirmTo,
      tooltip: hasEvent ? '' : '先抽取一次',
    },
    {
      num: '03',
      label: '推演',
      sub: onRun
        ? '推演中…'
        : session.lastSimulationId
          ? '可重播'
          : '群体推演',
      done: onReport || (!!session.lastSimulationId && !onRun),
      active: onRun,
      to: simulateTo,
      tooltip: simulateTooltip,
    },
    {
      num: '04',
      label: '报告',
      sub: onReport
        ? '当前'
        : session.lastSimulationId
          ? '查看上次'
          : '价格 · 盈亏',
      done: false,
      active: onReport,
      to: reportTo,
      tooltip: reportTo ? '' : '还没有完成的推演',
    },
  ]
})

const contextCard = computed(() => {
  const ep = session.eventProposal
  if (!ep && !session.lastSimulationId) return null

  const onReport = route.name === 'Report'
  const onRun = route.name === 'Run'

  let title = '当前事件'
  let footnote = ''

  if (onReport) {
    title = '上次推演'
    footnote = session.lastSimulationId
      ? `${session.lastSimulationId.slice(0, 14)}…`
      : ''
  } else if (onRun) {
    title = '推演中'
  } else if (ep) {
    title = '当前事件'
  }

  const price = ep && ep.current_price
    ? (currencySymbol(ep.price_currency || 'CNY') + Number(ep.current_price).toFixed(2))
    : ''

  return {
    title,
    ticker: ep?.ticker || '',
    instrument: ep?.instrument || '',
    market: ep?.market || '',
    eventType: ep?.event_type || '',
    price,
    footnote,
  }
})

function currencySymbol (cur) {
  return { CNY: '¥', USD: '$', EUR: '€', JPY: '¥', HKD: 'HK$', BTC: '₿' }[cur] || '$'
}

const authTitle = computed(() => (session.password ? '密码已保存' : '填入认证密码'))

function statusClass (s) {
  if (s.active) return 'active'
  if (s.done) return 'done'
  return ''
}
</script>

<style scoped>
.rail {
  width: var(--ss-sidebar-w);
  flex-shrink: 0;
  border-right: 1px solid var(--ss-line);
  padding: 22px 20px;
  display: flex;
  flex-direction: column;
  background: var(--ss-bg-soft);
  min-height: 100vh;
}

/* ── Narrow viewport: sidebar collapses to a horizontal top strip ──
   At < 860px, the 220px sidebar eats half the screen. Collapse the
   whole <aside> into a 56px-tall strip with brand + horizontal rail
   + gear button. The workflow steps become a compact dot row with
   only the current step's label showing. */
@media (max-width: 860px) {
  .rail {
    width: 100%;
    min-height: auto;
    border-right: 0;
    border-bottom: 1px solid var(--ss-line);
    padding: 10px 16px;
    flex-direction: row;
    align-items: center;
    gap: 18px;
    position: sticky;
    top: 0;
    z-index: 10;
    background: #fff;
  }
}

.brand {
  display: block;
  font-family: 'Fraunces', serif;
  font-size: 19px;
  font-weight: 500;
  letter-spacing: -0.01em;
  color: var(--ss-fg);
  margin-bottom: 42px;
  outline: none;
  border-radius: 4px;
}
.brand:focus-visible {
  box-shadow: 0 0 0 2px var(--ss-accent);
  text-decoration: underline;
  text-decoration-color: var(--ss-accent);
  text-decoration-thickness: 2px;
  text-underline-offset: 4px;
}
.brand em {
  font-style: italic;
  color: var(--ss-accent);
  font-weight: 500;
}

@media (max-width: 860px) {
  .brand {
    margin-bottom: 0;
    font-size: 17px;
    flex-shrink: 0;
  }
}

.rail-title {
  font-size: 10px;
  font-weight: 600;
  color: var(--ss-fg-muted);
  letter-spacing: 0.08em;
  margin-bottom: 14px;
}

.rail-nav { display: contents; }

.rail-list {
  list-style: none;
  display: flex;
  flex-direction: column;
  position: relative;
  padding: 0;
  margin: 0;
}
.rail-list::before {
  content: '';
  position: absolute;
  left: 9px;
  top: 14px;
  bottom: 14px;
  width: 1px;
  background: var(--ss-line-strong);
}
.rail-list li {
  position: relative;
  padding: 0;
  list-style: none;
}
.step-link {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 0;
  color: inherit;
  text-decoration: none;
  border-radius: 6px;
  transition: background 0.15s ease;
  outline: none;
}
.rail-list li.linked .step-link {
  cursor: pointer;
}
.rail-list li.linked .step-link:hover {
  background: rgba(255, 106, 0, 0.06);
}
.rail-list li.linked .step-link:focus-visible {
  box-shadow: inset 0 0 0 2px var(--ss-accent);
}
.rail-list li.disabled .step-link {
  cursor: not-allowed;
  opacity: 0.55;
}

@media (max-width: 860px) {
  .rail-title { display: none; }
  .rail-list {
    flex-direction: row;
    gap: 6px;
    flex: 1;
  }
  .rail-list::before {
    left: 14px;
    right: 14px;
    top: 50%;
    bottom: auto;
    height: 1px;
    width: auto;
  }
  .rail-list li {
    padding: 0;
    gap: 6px;
  }
  /* Hide step text/sub on narrow; show only dots + active step label */
  .rail-list li .cell { display: none; }
  .rail-list li.active .cell { display: flex; }
  .rail-list li.active .sub { display: none; }
}
.dot {
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: #fff;
  border: 1px solid var(--ss-line-strong);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-family: 'JetBrains Mono', monospace;
  font-size: 9px;
  font-weight: 600;
  color: var(--ss-fg-muted);
  z-index: 1;
  flex-shrink: 0;
}
.cell {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.label {
  font-family: 'Fraunces', serif;
  font-size: 12px;
  color: var(--ss-fg-muted);
}
.sub {
  font-size: 10px;
  color: var(--ss-fg-faint);
  margin-top: 2px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.active .dot {
  background: var(--ss-accent);
  border-color: var(--ss-accent);
  color: #fff;
}
.active .label {
  color: var(--ss-fg);
  font-weight: 600;
  font-style: italic;
}
.done .dot {
  background: var(--ss-fg);
  border-color: var(--ss-fg);
  color: #fff;
}
.done .label { color: var(--ss-fg); }

/* Context card — fills the space between workflow rail and footer */
.ctx-card {
  margin-top: 32px;
  padding: 14px 12px;
  border: 1px solid var(--ss-line);
  background: #fff;
  border-radius: 8px;
  border-top: 2px solid var(--ss-accent);
}
.ctx-title {
  font-family: 'Noto Serif SC', serif;
  font-size: 11px;
  font-weight: 600;
  color: var(--ss-fg-muted);
  margin-bottom: 8px;
  letter-spacing: 0.02em;
}
.ctx-body {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.ctx-row {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 11px;
  line-height: 1.4;
}
.ctx-k {
  font-size: 9px;
  color: var(--ss-fg-faint);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  min-width: 40px;
}
.ctx-v { color: var(--ss-fg); }
.ctx-v.mono {
  font-family: 'JetBrains Mono', monospace;
  font-variant-numeric: tabular-nums;
}
.ctx-v.serif {
  font-family: 'Noto Serif SC', serif;
  font-size: 12px;
  font-weight: 500;
}
.ctx-foot {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px dashed var(--ss-line);
  font-family: 'JetBrains Mono', monospace;
  font-size: 9px;
  color: var(--ss-fg-faint);
}

@media (max-width: 860px) {
  /* On narrow, the rail is a horizontal strip — hide the context
     card so it doesn't overflow the strip. */
  .ctx-card { display: none; }
}

.rail-foot {
  margin-top: auto;
  display: flex;
  flex-direction: column;
  gap: 8px;
  font-size: 11px;
  color: var(--ss-fg-faint);
}
@media (max-width: 860px) {
  .rail-foot {
    margin-top: 0;
    margin-left: auto;
    flex-direction: row;
    align-items: center;
    gap: 10px;
  }
  .rail-foot .disclaimer { display: none; }
  .rail-foot .auth-input { width: 140px; }
}
.gear {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--ss-fg-muted);
  background: none;
  border: 0;
  padding: 4px 0;
  cursor: pointer;
  font-size: 11px;
  font-family: inherit;
}
.gear:hover { color: var(--ss-fg); }

.auth-input {
  border: 1px solid var(--ss-line-strong);
  background: #fff;
  padding: 6px 8px;
  border-radius: 4px;
  font-size: 11px;
  font-family: 'JetBrains Mono', monospace;
  outline: none;
}
.auth-input:focus { border-color: var(--ss-accent); }

.disclaimer em {
  font-family: 'Noto Serif SC', serif;
  font-style: normal;
  font-weight: 500;
  color: var(--ss-fg-muted);
}
</style>
