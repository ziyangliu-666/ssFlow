import axios from 'axios'
import { session } from '../store/session'

// All API calls go through the Vite proxy in dev (and same-origin in prod
// once Flask serves frontend/dist), so the baseURL is just the current
// origin.
export const http = axios.create({
  baseURL: '',
  timeout: 120000,
})

http.interceptors.request.use((config) => {
  if (session.password) {
    config.headers = config.headers || {}
    config.headers['X-Auth-Password'] = session.password
  }
  return config
})

http.interceptors.response.use(
  (resp) => resp,
  (err) => {
    if (err.response) {
      session.lastError = err.response.data?.detail || err.response.data?.error || err.message
    } else {
      session.lastError = err.message || 'network error'
    }
    return Promise.reject(err)
  },
)

export async function ensureSession () {
  if (session.sessionId) return session.sessionId
  const r = await http.post('/session')
  session.sessionId = r.data.session_id
  return session.sessionId
}
