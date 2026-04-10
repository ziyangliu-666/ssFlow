import { http } from './client'
import { session } from '../store/session'

// POST the (event + personas) payload, get back a one-shot stream_id.
export async function initSimulationStream ({ event, personas, nRounds, seed, basePersonasPath, entityGraph, instrumentUniverse, roundSchedule }) {
  const payload = {
    session_id: session.sessionId,
    event,
    personas,
    n_rounds: nRounds || 5,
    seed: seed,
    base_personas_path: basePersonasPath || 'personas/ashare.yaml',
  }
  if (entityGraph) payload.entity_graph = entityGraph
  if (instrumentUniverse) payload.instrument_universe = instrumentUniverse
  if (roundSchedule) payload.round_schedule = roundSchedule
  const r = await http.post('/simulate-stream/init', payload)
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
  // Track whether we've received ANY event (including non-terminal).
  // If onerror fires before a single event arrived, the stream was
  // almost certainly already claimed or TTL'd on the backend — we
  // synthesize a richer error payload so the UI has something meaningful
  // to render instead of a bare em-dash.
  let anyEventSeen = false

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
    // Entity State Sandbox events
    'entity_state_updated',
    'resource_flow_executed',
    'threshold_fired',
    'force_action_override',
    'policy_fired',
    'agent_action',
    'policy_created',
  ]

  for (const t of knownTypes) {
    es.addEventListener(t, (ev) => {
      // GOTCHA: EventSource.addEventListener('error', …) fires on BOTH
      // server-emitted `event: error` frames AND native transport
      // failures (HTTP 404, socket close). The native variant has no
      // `data` payload, so we use that to distinguish them. A bodyless
      // 'error' frame is the transport error — let it fall through to
      // `es.onerror` below, which is where the synthetic
      // stream_expired payload is built. Only frames WITH data are
      // real server-sent events.
      if (t === 'error' && (ev.data === undefined || ev.data === null || ev.data === '')) {
        return
      }
      let payload
      try { payload = JSON.parse(ev.data) } catch (_) { payload = { raw: ev.data } }
      anyEventSeen = true
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
    // If the socket closed with ZERO events, the backend either 404'd
    // the stream (already claimed or TTL'd — streams are one-shot) or
    // the proxy cut us off before the first event landed. Either way,
    // the UI needs something to display. Synthesize a structured error
    // event so the timeline shows a real reason, not a blank em-dash.
    if (!anyEventSeen) {
      const synthetic = {
        code: 'stream_expired',
        detail:
          '这条推演 stream 已经被领取过或已超时。' +
          'simulate-stream 是一次性的，刷新或重新打开都会失效。',
      }
      if (typeof handlers.onEvent === 'function') {
        handlers.onEvent('error', synthetic)
      }
      if (typeof handlers.onError === 'function') handlers.onError(err)
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
