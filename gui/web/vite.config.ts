import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev server proxies API calls to the engine daemon (make paper) so the GUI
// is always a pure client — it holds no trading logic and no credentials.
const engine = 'http://127.0.0.1:8765'
const apiRoutes = [
  '/status', '/account', '/positions', '/orders', '/risk', '/logs',
  '/backtests', '/strategy', '/halt', '/killswitch', '/keys',
  '/equity', '/gate2', '/config', '/alerts', '/simulate', '/thinking',
  '/processes', '/system', '/reconcile', '/notes',
]

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: Object.fromEntries(apiRoutes.map((p) => [p, engine])),
  },
})
