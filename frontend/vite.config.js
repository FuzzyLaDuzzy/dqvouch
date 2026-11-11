import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// The build goes inside the Python package, so `pip install` needs no Node; `npm run dev` proxies the API to a
// running `dqvouch ui` (or `python tests/serve_fake.py`).
export default defineConfig({
  plugins: [vue()],
  build: { outDir: '../src/dqvouch/static', emptyOutDir: true },
  server: { proxy: { '/api': { target: 'http://127.0.0.1:8765', changeOrigin: true } } },
})
