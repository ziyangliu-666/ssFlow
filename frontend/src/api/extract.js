import { http, ensureSession } from './client'
import { session } from '../store/session'

export async function runExtract ({ prompt, extraText, urls }) {
  await ensureSession()
  const r = await http.post('/extract', {
    session_id: session.sessionId,
    prompt: prompt || '',
    extra_text: extraText || '',
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
