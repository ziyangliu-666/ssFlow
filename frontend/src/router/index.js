import { createRouter, createWebHistory } from 'vue-router'
import Home from '../views/Home.vue'
import SetupView from '../views/SetupView.vue'
import SimulationRunView from '../views/SimulationRunView.vue'
import ReplayView from '../views/ReplayView.vue'
import ReportView from '../views/ReportView.vue'

const routes = [
  { path: '/', name: 'Home', component: Home },
  { path: '/setup', name: 'Setup', component: SetupView },
  { path: '/run/:streamId', name: 'Run', component: SimulationRunView, props: true },
  { path: '/replay/:simulationId', name: 'Replay', component: ReplayView, props: true },
  { path: '/reports/:simulationId', name: 'Report', component: ReportView, props: true },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

export default router
