<template>
  <aside class="rail">
    <router-link to="/" class="brand">ss<em>Flow</em></router-link>

    <div class="rail-title">WORKFLOW</div>
    <ul class="rail-list">
      <li
        v-for="s in steps"
        :key="s.num"
        :class="statusClass(s)"
      >
        <span class="dot">{{ s.num }}</span>
        <div class="cell">
          <span class="label">{{ s.label }}</span>
          <span class="sub">{{ s.sub }}</span>
        </div>
      </li>
    </ul>

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
        {{ showAuth ? 'Hide auth' : 'Settings' }}
      </button>
      <input
        v-if="showAuth"
        v-model="session.password"
        class="auth-input"
        type="password"
        placeholder="SSFLOW_PASSWORD"
      />
      <span class="disclaimer">
        <em>Research tool</em> · not investment advice
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
  const onSetup = route.name === 'Setup'
  const onRun = route.name === 'Run'
  const onReport = route.name === 'Report'
  const onHome = route.name === 'Home'

  return [
    {
      num: '01',
      label: 'Seed',
      sub: session.uploadedFiles.length
        ? `${session.uploadedFiles.length} file${session.uploadedFiles.length === 1 ? '' : 's'}`
        : '文件 + 描述',
      done: !onHome && (hasEvent || session.uploadedFiles.length > 0),
      active: onHome,
    },
    {
      num: '02',
      label: 'Confirm',
      sub: hasEvent
        ? `${session.personasProposed.length} personas`
        : '抽取结果',
      done: onRun || onReport,
      active: onSetup,
    },
    {
      num: '03',
      label: 'Simulate',
      sub: onRun ? 'running…' : '群体推演',
      done: onReport,
      active: onRun,
    },
    {
      num: '04',
      label: 'Report',
      sub: onReport ? 'done' : '价格 · P&L',
      done: false,
      active: onReport,
    },
  ]
})

const authTitle = computed(() => (session.password ? 'Auth saved' : 'Enter auth password'))

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

.brand {
  display: block;
  font-family: 'Fraunces', serif;
  font-size: 19px;
  font-weight: 500;
  letter-spacing: -0.01em;
  color: var(--ss-fg);
  margin-bottom: 42px;
}
.brand em {
  font-style: italic;
  color: var(--ss-accent);
  font-weight: 500;
}

.rail-title {
  font-size: 10px;
  font-weight: 600;
  color: var(--ss-fg-muted);
  letter-spacing: 0.08em;
  margin-bottom: 14px;
}

.rail-list {
  list-style: none;
  display: flex;
  flex-direction: column;
  position: relative;
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
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 0;
  position: relative;
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

.rail-foot {
  margin-top: auto;
  display: flex;
  flex-direction: column;
  gap: 8px;
  font-size: 11px;
  color: var(--ss-fg-faint);
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
  font-family: 'Fraunces', serif;
  font-style: italic;
  color: var(--ss-fg-muted);
}
</style>
