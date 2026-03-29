import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      '/dashboard/api': {
        target: 'http://localhost:8741',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/dashboard\/api/, ''),
      },
      '/dashboard/ws': {
        target: 'ws://localhost:8741',
        ws: true,
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/dashboard\/ws/, ''),
      },
    },
  },
})
