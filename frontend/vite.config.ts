import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { VitePWA } from 'vite-plugin-pwa';

// The barcode decoder is a ~1 MB WebAssembly module loaded lazily, only when the
// scanner screen opens. Workbox does not see it through a static import graph
// entry, so `.wasm` is listed explicitly in globPatterns — see
// docs/technical-notes-scanning.md §2.3. Without this, scanning breaks offline
// and the failure only shows up on a plane.
export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: 'autoUpdate',
      injectRegister: 'auto',
      includeAssets: ['favicon.ico', 'apple-touch-icon.png'],
      workbox: {
        globPatterns: ['**/*.{js,css,html,ico,png,svg,woff2,wasm}'],
        // The reader WASM is ~1 MB raw; the default 2 MB ceiling would drop it.
        maximumFileSizeToCacheInBytes: 4 * 1024 * 1024,
        navigateFallback: 'index.html',
        cleanupOutdatedCaches: true,
      },
      manifest: {
        name: 'Chaudron',
        short_name: 'Chaudron',
        description: 'Inventaire de cuisine et suggestions de recettes.',
        lang: 'fr',
        dir: 'ltr',
        start_url: '/',
        scope: '/',
        display: 'standalone',
        orientation: 'portrait',
        background_color: '#22242A',
        theme_color: '#22242A',
        categories: ['food', 'lifestyle', 'utilities'],
        icons: [
          { src: 'icon-192.png', sizes: '192x192', type: 'image/png', purpose: 'any' },
          { src: 'icon-512.png', sizes: '512x512', type: 'image/png', purpose: 'any' },
          {
            src: 'icon-maskable-512.png',
            sizes: '512x512',
            type: 'image/png',
            purpose: 'maskable',
          },
        ],
      },
    }),
  ],
  build: {
    target: 'es2022',
    sourcemap: false,
  },
});
