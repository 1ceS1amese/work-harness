import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

export default defineConfig(({ mode }) => {
  return {
    plugins:
      mode === 'test'
        ? [vue()]
        : [
            vue(),
            AutoImport({
              dts: 'src/auto-imports.d.ts',
              resolvers: [ElementPlusResolver()],
            }),
            Components({
              dts: 'src/components.d.ts',
              resolvers: [ElementPlusResolver()],
            }),
          ],
    server: {
      port: 5173,
      strictPort: true,
      proxy: {
        '/health': 'http://127.0.0.1:8000',
        '/v1': 'http://127.0.0.1:8000',
      },
    },
    test: {
      environment: 'jsdom',
    },
  }
})
