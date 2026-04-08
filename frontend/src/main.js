import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import { session } from './store/session'
import './assets/global.css'

const app = createApp(App)
app.use(router)
app.mount('#app')

// Expose the reactive session store on window for QA / debugging.
// The store has no secrets beyond the auth password (which the user
// already typed in), so this is safe to ship.
if (typeof window !== 'undefined') {
  window.__ssflow = { session, router }
}
