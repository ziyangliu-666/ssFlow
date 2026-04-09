// Lightweight reactive session store. Persists the auth password to
// localStorage so the user only enters it once. Holds the in-flight
// session_id, the EventProposal returned by /extract, and the persona
// partials the user is editing in SetupView.

import { reactive, watch } from 'vue'

const LS_PASSWORD_KEY = 'ssflow.password'
const LS_SESSION_KEY = 'ssflow.session'
const LS_LAST_SIM_KEY = 'ssflow.lastSim'

const initial = {
  password: localStorage.getItem(LS_PASSWORD_KEY) || '',
  sessionId: '',
  uploadedFiles: [],     // [{tmp_path, name, size, kind}]
  prompt: '',
  urls: [],              // string[]
  eventProposal: null,   // dict or null
  personasProposed: [],  // partial dicts
  basePersonasPath: 'personas/ashare.yaml',
  ingestedDocs: [],
  // IDs we remember so the workflow rail can navigate to the right
  // route when the user clicks a past step.
  activeStreamId: '',          // set when /simulate-stream/init succeeds
  lastSimulationId: localStorage.getItem(LS_LAST_SIM_KEY) || '',
  lastError: '',
}

// Restore prior session_id (so a refresh in the middle of editing
// doesn't lose the session). Everything else stays in memory.
try {
  const stored = JSON.parse(localStorage.getItem(LS_SESSION_KEY) || 'null')
  if (stored && stored.sessionId) {
    initial.sessionId = stored.sessionId
  }
} catch (_) { /* ignore */ }

export const session = reactive(initial)

watch(() => session.password, (val) => {
  if (val) localStorage.setItem(LS_PASSWORD_KEY, val)
  else localStorage.removeItem(LS_PASSWORD_KEY)
})

watch(() => session.sessionId, (val) => {
  if (val) {
    localStorage.setItem(LS_SESSION_KEY, JSON.stringify({ sessionId: val }))
  }
})

watch(() => session.lastSimulationId, (val) => {
  if (val) localStorage.setItem(LS_LAST_SIM_KEY, val)
  else localStorage.removeItem(LS_LAST_SIM_KEY)
})

export function resetSession () {
  session.sessionId = ''
  session.uploadedFiles = []
  session.prompt = ''
  session.urls = []
  session.eventProposal = null
  session.personasProposed = []
  session.basePersonasPath = 'personas/ashare.yaml'
  session.ingestedDocs = []
  session.activeStreamId = ''
  session.lastSimulationId = ''
  session.lastError = ''
}
