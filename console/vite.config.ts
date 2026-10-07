import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  base: '/',
  plugins: [vue()],
  build: { outDir: '../community/static', emptyOutDir: true, sourcemap: false, assetsInlineLimit: 0 },
  server: {
    port: 5175, strictPort: true,
    proxy: { '/catalog': 'http://127.0.0.1:18965' },
  },
})
