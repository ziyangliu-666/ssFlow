import { http, ensureSession } from './client'
import { session } from '../store/session'

export async function uploadFiles (files) {
  if (!files || files.length === 0) return { files: [] }
  await ensureSession()

  const fd = new FormData()
  fd.append('session_id', session.sessionId)
  for (const f of files) fd.append('files', f, f.name)

  const r = await http.post('/upload', fd, {
    headers: { 'Content-Type': 'multipart/form-data' },
  })
  // Merge into the session store
  session.sessionId = r.data.session_id
  session.uploadedFiles = r.data.files
  return r.data
}
