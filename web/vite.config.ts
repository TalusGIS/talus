import { existsSync } from 'node:fs'
import { fileURLToPath, URL } from 'node:url'

import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { VitePWA } from 'vite-plugin-pwa'

// https://vite.dev/config/
//
// Talus 35: the SPA is a PWA so the portal survives a *connectivity blip* for VIEWING — the app
// opens with no network and you can still pan an already-cached area. Editing is **online-only**
// (re-scoped 2026-06-22, decision-disconnected-editing.md §11): offline writes are blocked with a
// "reconnect to edit" message, NOT queued. The offline edit-queue was removed — its failure surface
// (token expiry offline, navigator.onLine unreliability, per-panel offline reads) was unbounded for a
// case ESRI itself doesn't offer; real field offline-editing belongs to the **QField + QFieldCloud**
// native track (see field-app-decision.md), the right host for it.
// vite-plugin-pwa generates the service worker + a build-injected precache manifest (hashed asset
// filenames change every build, so the manifest MUST be build-injected — exactly the plugin's job).
//   * precache  -> the app shell (JS/CSS/index.html); the SPA opens with no network.
//   * runtime   -> GETs the viewer depends on, so a previously-viewed area stays readable across a blip:
//                  API feature tiles + JSON reads (SWR / NetworkFirst), raster basemap tiles (SWR).
// Bounded limit (by design, not a bug): the vector PMTiles basemap (range requests) and any
// never-visited area are NOT cached — offline they show blank. The app degrades gracefully (no
// scary "Map error" banner; an honest "Offline — viewing only" notice).
export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: 'autoUpdate',
      // Offline is verified against the built app (`npm run build && npm run preview`); the SW stays
      // off during `vite dev` to avoid caching churn while developing.
      devOptions: { enabled: false },
      manifest: {
        name: 'Talus GIS Portal',
        short_name: 'Talus GIS',
        description: 'Talus GIS portal — field-capable, offline-aware editing.',
        theme_color: '#1f2933',
        background_color: '#ffffff',
        display: 'standalone',
        start_url: '/',
        scope: '/',
      },
      workbox: {
        // SPA: serve the precached shell for any client-side route when offline.
        navigateFallback: '/index.html',
        // Talus Field (decision-talus-field-f1.md §2): the Talus+ field app is a SECOND build
        // target at /field/ with its OWN service worker and scope. Without this denylist, a
        // browser that installed THIS worker from a portal visit would answer a /field/ navigation
        // from the portal precache and hand the crew the portal shell — before the field worker
        // has ever registered. Harmless in a Community build: /field/ simply falls through to the
        // network (and 404s there). The regex is the whole scope, so /field and /field/x both pass.
        // The open-source licenses file (scripts/third-party-licenses.mjs) is opened in a tab — a
        // navigation — so without this the worker would answer it with the portal shell and the
        // "Open-source licenses" link would show the app instead of the licenses.
        navigateFallbackDenylist: [/^\/field(\/|$)/, /^\/third-party-licenses\.txt$/],
        // Precache cap = TRIPWIRE, not a treadmill. Before code-splitting (2026-07-13) this was one
        // ~2.2 MB blob that crossed workbox's 2 MiB default and we raised the cap to 4 MiB as a
        // stopgap. Now the app is split into chunks (largest ~1.25 MiB, the maplibre engine), so a
        // 2.5 MiB cap sits ~2x above the biggest chunk: normal builds precache fine, but a chunk
        // that balloons past 2.5 MiB trips the build/precache warning and forces a re-split instead
        // of silently precaching a huge file. Offline viewing (T35's kept half) needs the shell
        // precached, so this must stay comfortably above real chunk sizes.
        maximumFileSizeToCacheInBytes: 2.5 * 1024 * 1024,
        // Don't precache the (large) bundled basemap PMTiles; it is range-requested at runtime.
        // Ditto the vendored basemap glyphs/sprites (decision-basemap-provisioning.md F8):
        // hundreds of small font-range .pbf files MapLibre fetches on demand — precaching them
        // all would bloat every install for ranges most maps never render.
        globIgnores: ['**/*.pmtiles', 'basemaps-assets/**'],
        runtimeCaching: [
          {
            // API vector feature tiles: /tiles/{layerId}/{z}/{x}/{y}.pbf (cross-origin API).
            urlPattern: ({ url }) => url.pathname.startsWith('/tiles/'),
            handler: 'StaleWhileRevalidate',
            options: {
              cacheName: 'talus-feature-tiles',
              expiration: { maxEntries: 2000, maxAgeSeconds: 7 * 24 * 3600 },
              cacheableResponse: { statuses: [0, 200] },
            },
          },
          {
            // Raster imagery basemap (USGS) + protomaps sprite/glyph assets.
            urlPattern: ({ url }) =>
              url.hostname.endsWith('nationalmap.gov') || url.hostname.endsWith('protomaps.github.io'),
            handler: 'StaleWhileRevalidate',
            options: {
              cacheName: 'talus-basemap',
              expiration: { maxEntries: 3000, maxAgeSeconds: 30 * 24 * 3600 },
              cacheableResponse: { statuses: [0, 200] },
            },
          },
          {
            // API JSON reads the offline editor depends on (feature reads, domains, capabilities).
            urlPattern: ({ url, request }) =>
              request.method === 'GET' &&
              /\/(layers|domains|me)\b/.test(url.pathname),
            handler: 'NetworkFirst',
            options: {
              cacheName: 'talus-api',
              networkTimeoutSeconds: 5,
              expiration: { maxEntries: 1000, maxAgeSeconds: 24 * 3600 },
              cacheableResponse: { statuses: [0, 200] },
            },
          },
        ],
      },
    }),
  ],
  build: {
    // maplibre-gl is inherently ~1.25 MiB and is now its own deliberate chunk — Vite's default
    // 500 kB advisory would warn on it every build for no action. Raise the advisory to 1500 kB so
    // it stays a real signal: it fires only if a chunk grows past the intended map-engine size.
    chunkSizeWarningLimit: 1500,
    rollupOptions: {
      output: {
        // Code-splitting (2026-07-13): the app used to compile to ONE ~2.2 MB bundle that only
        // grew — it crossed workbox's 2 MiB precache default in July (build FAIL), and the 4 MiB
        // stopgap below just moved the treadmill. Split the eager bundle along its natural seams so
        // no single chunk is huge and the precache cap stops being a moving target.
        //   * maplibre -> the map engine (maplibre-gl + tiling/draw deps); by far the biggest mass.
        //   * vendor   -> framework core (react ecosystem + auth); changes rarely, caches well.
        //   * (default) -> app code, now much smaller.
        // Only the two named groups are routed; EVERYTHING else in node_modules is left to the
        // bundler on purpose — jsPDF/html2canvas are reached solely via the lazy import in
        // mapExportPdf, so leaving them unrouted keeps them in their own async chunk (not eager).
        manualChunks(id) {
          if (!id.includes('node_modules')) return
          if (/[\\/]node_modules[\\/](maplibre-gl|pmtiles|terra-draw|terra-draw-maplibre-gl-adapter|@protomaps[\\/]basemaps)[\\/]/.test(id))
            return 'maplibre'
          if (/[\\/]node_modules[\\/](react|react-dom|react-router|react-oidc-context|oidc-client-ts|scheduler)[\\/]/.test(id))
            return 'vendor'
          // ECharts + its zrender engine (Talus+ dashboards, d5): one lazy chunk shared by the
          // dashboard views instead of being duplicated into each async chunk that imports it.
          if (/[\\/]node_modules[\\/](echarts|zrender)[\\/]/.test(id)) return 'echarts'
          return
        },
      },
    },
  },
  resolve: {
    // "@/..." -> "src/..." (keep in sync with tsconfig.app.json paths)
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
      // '@paid' (decision-story-maps.md §4, the ruled mounting seam): the Talus+ web entry when
      // paid/web exists (the dev repo), else the inert stub (every public-repo build — paid/
      // never crosses the release MANIFEST). Keep in sync with tsconfig.app.json paths.
      // Bare deps in paid files (react etc.) resolve through the committed
      // paid/web/node_modules -> ../../web/node_modules symlink — one shared dependency tree,
      // one React instance, no per-package aliasing.
      '@paid': existsSync(fileURLToPath(new URL('../paid/web/src/index.tsx', import.meta.url)))
        ? fileURLToPath(new URL('../paid/web/src/index.tsx', import.meta.url))
        : fileURLToPath(new URL('./src/paid-stub.tsx', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    // Pin the dev server to IPv4 loopback. Vite 8 on current Node otherwise binds ONLY ::1
    // (verbatim DNS order), and a client that tries 127.0.0.1 first gets ERR_CONNECTION_REFUSED
    // (bit Chrome on 2026-09-03). Every localhost client falls back to IPv4 cleanly; still
    // loopback-only — nothing is exposed on the LAN.
    host: '127.0.0.1',
    // The '@paid' alias points outside this root; allow Vite's dev server to serve it (build is
    // unaffected). The web root stays allowed alongside it.
    fs: {
      allow: [
        fileURLToPath(new URL('.', import.meta.url)),
        fileURLToPath(new URL('../paid/web', import.meta.url)),
      ],
    },
  },
})
