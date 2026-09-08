import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

export default defineConfig({
  plugins: [react()],
  root: '../web/frontend',
  resolve: {
    alias: {
      '@': path.resolve(__dirname, '../web/frontend/src'),
    },
  },
  server: {
    port: 5173,
    strictPort: true,
  },
  build: {
    outDir: '../web/frontend/dist',
    emptyOutDir: true,
  },
})
