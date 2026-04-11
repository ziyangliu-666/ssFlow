import { http, ensureSession } from './client'
import { session } from '../store/session'

export async function runExtract ({ prompt, urls }) {
  await ensureSession()
  const r = await http.post('/extract', {
    session_id: session.sessionId,
    prompt: prompt || '',
    urls: urls || [],
  })
  session.sessionId = r.data.session_id
  session.eventProposal = r.data.event_proposal
  session.personasProposed = r.data.personas_proposed
  session.basePersonasPath = r.data.base_personas_path
  session.ingestedDocs = r.data.ingested_docs || []
  return r.data
}

export async function fetchPersonaTemplate (decisionMode = 'discretionary') {
  const r = await http.post('/personas/template', { decision_mode: decisionMode })
  return r.data
}

/**
 * Distill: from a topic string, identify related instruments + build round schedule.
 * Real market prices/ADVs come from the distill endpoint per ticker.
 * Returns { instrument_universe, round_schedule }.
 */
export async function runDistill ({ topic, market, eventTicker, eventDate }) {
  const r = await http.post('/distill', {
    topic: topic || '',
    market: market || 'ashare',
    event_ticker: eventTicker || undefined,
    event_date: eventDate || '',
  })
  session.instrumentUniverse = r.data.instrument_universe
  session.roundSchedule = r.data.round_schedule
  return r.data
}

/**
 * Generate sandbox: from a topic, build an EntityGraph (template-only or LLM-tuned).
 * Returns the serialized EntityGraph.
 */
export async function generateSandbox ({ topic, market, useLlm, entityOverrides }) {
  const r = await http.post('/sandbox/generate', {
    topic: topic || '',
    market: market || 'ashare',
    use_llm: useLlm || false,
    entity_overrides: entityOverrides || undefined,
  })
  session.entityGraph = r.data
  return r.data
}
