import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
    strictPort: true,
    // File-change events don't reach containers through Windows bind mounts.
    watch: { usePolling: process.env.VITE_USE_POLLING === 'true' },
  },
})
