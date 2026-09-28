import react from '@vitejs/plugin-react'
import { defineConfig, loadEnv } from 'vite'

export default defineConfig(({ mode }) => {
  // Brand layer switch: empty for the open-source build, `kith-climate` for the
  // hosted build (set in the hosted deployment's build config). Stamped onto <html
  // data-brand> so the brand CSS applies before first paint.
  const brand = loadEnv(mode, '.', 'VITE_').VITE_TRET_BRAND ?? ''

  return {
    plugins: [
      react(),
      {
        name: 'tret-brand',
        transformIndexHtml: (html: string) => html.replace('__TRET_BRAND__', brand),
      },
    ],
    server: {
      port: 5173,
      proxy: {
        '/api': {
          target: 'http://localhost:8000',
          changeOrigin: true,
        },
      },
    },
  }
})
