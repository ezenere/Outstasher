import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Em dev (npm run dev / python main.py dev) o Vite serve em :5173 e
// encaminha /api para o backend em :8008.
// Em WSL sobre disco do Windows (/mnt/...) nao existe inotify — usa polling.
const onWindowsMount = process.platform === 'linux' && process.cwd().startsWith('/mnt/')

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8008',
    },
    watch: onWindowsMount ? { usePolling: true, interval: 500 } : undefined,
  },
})
