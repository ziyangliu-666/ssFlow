import { http } from './client'
import { session } from '../store/session'

// POST the (event + personas) payload, get back a one-shot stream_id.
export async function initSimulationStream ({ event, personas, nRounds, seed, basePersonasPath }) {
  const r = await http.post('/simulate-stream/init', {
    session_id: session.sessionId,
    event,
    personas,
    n_rounds: nRounds || 5,
    seed: seed,
    base_personas_path: basePersonasPath || 'personas/ashare.yaml',
  })
  return r.data
}

// Open the SSE stream and route each event to the supplied handler.
// Returns a `close()` function the caller can use to abort.
export function openSimulationStream (streamId, handlers) {
  const url = `/simulate-stream/${encodeURIComponent(streamId)}`
  const es = new EventSource(url)

  // The server emits a `simulation_done` (or `error`) event right before
  // closing the stream. EventSource will fire `onerror` immediately after
  // the close — that is normal end-of-stream behavior, NOT a real error.
  // Track whether we've seen the terminal event so onError can ignore it.
  let terminalSeen = false

  const knownTypes = [
    'simulation_start',
    'round_start',
    'external_event_injected',
    'persona_thought',
    'trade_submitted',
    'class_flow_computed',
    'price_updated',
    'round_complete',
    'simulation_complete',
    'simulation_done',
    'error',
  ]

  for (const t of knownTypes) {
    es.addEventListener(t, (ev) => {
      let payload
      try { payload = JSON.parse(ev.data) } catch (_) { payload = { raw: ev.data } }
      if (t === 'simulation_done' || t === 'error') {
        terminalSeen = true
      }
      if (typeof handlers.onEvent === 'function') {
        handlers.onEvent(t, payload)
      }
      const fn = handlers[`on${t.replace(/(^|_)(\w)/g, (_m, _u, c) => c.toUpperCase())}`]
      if (typeof fn === 'function') fn(payload)
    })
  }

  es.onerror = (err) => {
    // Suppress the inevitable end-of-stream "error" that fires once the
    // server closes the connection cleanly after a terminal event.
    if (terminalSeen) {
      es.close()
      return
    }
    if (typeof handlers.onError === 'function') handlers.onError(err)
    es.close()
  }

  return {
    close: () => es.close(),
    es,
  }
}
