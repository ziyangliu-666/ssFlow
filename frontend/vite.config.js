import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import path from 'path'

// Vite dev server runs on :5173 by default. Flask runs on :5001.
// All API calls (and the SSE stream) are proxied so the browser sees a
// single origin and we don't have to deal with CORS or cookie scopes.
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, 'src'),
    },
  },
  server: {
    port: 5173,
    host: true,
    proxy: {
      '/healthz':            { target: 'http://localhost:5001', changeOrigin: true },
      '/session':            { target: 'http://localhost:5001', changeOrigin: true },
      '/upload':             { target: 'http://localhost:5001', changeOrigin: true },
      '/extract':            { target: 'http://localhost:5001', changeOrigin: true },
      '/extract-event':      { target: 'http://localhost:5001', changeOrigin: true },
      '/personas':           { target: 'http://localhost:5001', changeOrigin: true },
      '/simulate':           { target: 'http://localhost:5001', changeOrigin: true },
      '/simulate-stream':    {
        target: 'http://localhost:5001',
        changeOrigin: true,
        ws: false,
        // SSE: keep the connection open and don't buffer.
        configure: (proxy) => {
          proxy.on('proxyReq', (proxyReq) => {
            proxyReq.setHeader('X-Accel-Buffering', 'no')
          })
        },
      },
      // Trailing slash is load-bearing: it scopes the proxy to the API
      // endpoint `/report/<id>` and prevents it from eating the frontend
      // router path `/reports/<id>`, which otherwise got 500'd by the
      // proxy before the SPA could even boot.
      '/report/':            { target: 'http://localhost:5001', changeOrigin: true },
      // Same trailing-slash trick: the backend timeline endpoint lives
      // at `/simulation/<id>/timeline`. The SPA route is `/replay/<id>`,
      // so there's no collision, but the trailing slash keeps the proxy
      // strictly scoped to API calls.
      '/simulation/':        { target: 'http://localhost:5001', changeOrigin: true },
      '/cost':               { target: 'http://localhost:5001', changeOrigin: true },
      '/distill':            { target: 'http://localhost:5001', changeOrigin: true },
      '/sandbox':            { target: 'http://localhost:5001', changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
