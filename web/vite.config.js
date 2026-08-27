import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: { proxy: { '/app': 'http://127.0.0.1:18000', '/agent': 'http://127.0.0.1:18000',
                     '/tools': 'http://127.0.0.1:18000', '/admin': 'http://127.0.0.1:18000' } },
  build: { outDir: 'dist' }
})
