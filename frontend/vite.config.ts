import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'
import ui from '@nuxt/ui/vite'

export default defineConfig({
  plugins: [
    vue(),
    ui({
      router: false,
      colorMode: false,
      icon: { clientBundle: { scan: true } },
      components: { dts: 'src/components.d.ts' },
      autoImport: { dts: 'src/auto-imports.d.ts' },
      ui: { colors: { primary: 'emerald', neutral: 'neutral' } },
    }),
  ],
  server: {
    port: 5173,
    strictPort: true,
    proxy: { '/health': 'http://127.0.0.1:8000', '/v1': 'http://127.0.0.1:8000' },
  },
  test: {
    include: ['src/**/*.test.ts'],
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
  },
})
