import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

const apiBaseUrl = process.env.API_BASE_URL || 'http://127.0.0.1:8000'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes('node_modules')) {
            return undefined
          }
          if (
            id.includes('/echarts/') ||
            id.includes('/echarts-for-react/')
          ) {
            return 'vendor-echarts'
          }
          if (
            id.includes('/@monaco-editor/react/') ||
            id.includes('/monaco-editor/')
          ) {
            return 'vendor-monaco'
          }
          if (id.includes('/@ant-design/pro-components/')) {
            return 'vendor-pro-components'
          }
          return undefined
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: apiBaseUrl,
        changeOrigin: true,
      },
    },
  },
})
