import { defineConfig } from 'tsup'

export default defineConfig({
  entry: ['src/index.ts'],
  format: ['esm', 'cjs'],
  dts: true,
  sourcemap: true,
  clean: true,
  target: 'es2022',
  platform: 'neutral',
  // Zero runtime dependencies: everything the SDK needs (fetch, streams,
  // FormData, Blob, TextDecoder, AbortController) is a web-platform global in
  // Node >= 18, browsers and Electron.
})
