import { existsSync, readFileSync } from 'node:fs'
import { fileURLToPath, URL } from 'node:url'

import { defineConfig, type Plugin } from 'vite'
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
//
// Talus+ files. A Talus+ build compiles the
// tree behind the '@paid' alias into this one dist. The rules below keep that code apart from the app
// everyone downloads, and none of them knows what any paid module is:
//   * every lazily loaded file that holds a module from the paid tree is written to assets/plus/,
//     and so is the CSS such a file imports;
//   * assets/plus/ is left out of the precache, so a browser fetches one of those files only when a
//     page asks for it (and keeps it in a runtime cache afterwards);
//   * each of those files starts with the notice the paid tree supplies, if it supplies one;
//   * the paid code the app must load at start (the '@paid' entry and what it statically imports:
//     route table, menu entries) stays in the app's own entry file, and the build FAILS if that
//     grows past PAID_EAGER_CAP — so a paid page can never drift into what every visitor downloads.
// In a Community build there is no paid tree: PAID_ENTRY does not exist and every rule matches nothing.
const PAID_ROOT = fileURLToPath(new URL('../paid/web/', import.meta.url))
const PAID_ENTRY = fileURLToPath(new URL('../paid/web/src/index.tsx', import.meta.url))
const HAS_PAID = existsSync(PAID_ENTRY)
const PAID_NOTICE_FILE = `${PAID_ROOT}file-notice.txt`
const PAID_NOTICE = HAS_PAID && existsSync(PAID_NOTICE_FILE) ? readFileSync(PAID_NOTICE_FILE, 'utf8').trim() : ''
const PLUS_DIR = 'assets/plus/'
const PAID_EAGER_CAP = 24 * 1024 // bytes of rendered paid code allowed outside assets/plus/

function fromPaid(id: string): boolean {
  return HAS_PAID && id.startsWith(PAID_ROOT) && !id.includes('node_modules')
}

/** Where a JS file is written: assets/plus/ if it holds a paid module, else Vite's default. */
function jsFileName(chunk: { moduleIds: string[] }): string {
  return chunk.moduleIds.some(fromPaid) ? `${PLUS_DIR}[name]-[hash].js` : 'assets/[name]-[hash].js'
}

/** Put the paid tree's notice at the top of every JS and CSS file in assets/plus/. Done here, after
 *  minification, and not with output.postBanner: a banner function that returns '' for the other
 *  files still adds a blank line to each of them, and a Community build must come out unchanged. */
function addPaidNotice(bundle: Record<string, { type: string; fileName: string; code?: string; source?: unknown }>): void {
  if (!PAID_NOTICE) return
  for (const file of Object.values(bundle)) {
    if (!file.fileName.startsWith(PLUS_DIR)) continue
    if (file.type === 'chunk') file.code = `${PAID_NOTICE}\n${file.code}`
    else if (file.fileName.endsWith('.css')) file.source = `${PAID_NOTICE}\n${String(file.source)}`
  }
}

/** The same notice for a web worker, which Vite bundles on its own (see `worker` below). */
function paidWorkerNotice(): Plugin {
  return {
    name: 'talus-paid-worker-notice',
    enforce: 'post',
    generateBundle(_options, bundle) {
      addPaidNotice(bundle)
    },
  }
}

// Names of the lazily loaded chunks that hold paid code, noted as their file names are chosen. Vite
// names a chunk's CSS file after the chunk, so this is how assetFileNames sends that CSS after it.
const paidChunkNames = new Set<string>()

function paidFilesStayApart(): Plugin {
  return {
    name: 'talus-paid-files-stay-apart',
    apply: 'build',
    enforce: 'post',
    generateBundle(_options, bundle) {
      let eagerPaid = 0
      for (const file of Object.values(bundle)) {
        if (file.type !== 'chunk') continue
        const inPlus = file.fileName.startsWith(PLUS_DIR)
        if (inPlus) {
          // The CSS a paid file imports must have followed it (the name rule above held).
          const css = (file as { viteMetadata?: { importedCss?: Set<string> } }).viteMetadata?.importedCss ?? []
          for (const name of css) {
            if (!name.startsWith(PLUS_DIR)) this.error(`${name} is the CSS of a Talus+ file but is outside ${PLUS_DIR}`)
          }
          continue
        }
        for (const [id, mod] of Object.entries(file.modules)) {
          if (fromPaid(id)) eagerPaid += mod.renderedLength
        }
      }
      if (eagerPaid > PAID_EAGER_CAP) {
        this.error(
          `${eagerPaid} bytes of Talus+ code sit outside ${PLUS_DIR} (cap ${PAID_EAGER_CAP}): paid code is ` +
            'statically imported from the paid entry and would reach every visitor. Load it with lazy() ' +
            'instead.',
        )
      }
      if (HAS_PAID) console.log(`[talus-plus] ${eagerPaid} bytes of paid start-up code in the app entry (cap ${PAID_EAGER_CAP})`)
      addPaidNotice(bundle)
    },
  }
}

export default defineConfig({
  plugins: [
    react(),
    paidFilesStayApart(),
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
        // Ditto the Talus+ files (see PLUS_DIR above): fetched when a page asks for one, never ahead.
        // Both Talus+ entries are added only when the paid tree is present, so a Community build's
        // service worker is the same file it was before they existed.
        globIgnores: ['**/*.pmtiles', 'basemaps-assets/**', ...(HAS_PAID ? [`${PLUS_DIR}**`] : [])],
        runtimeCaching: [
          ...(HAS_PAID
            ? [
                {
                  // Talus+ files, once a page has asked for one, so a paid page already opened
                  // survives a blip. Served from the cache and refreshed behind it, NOT cache-first:
                  // the edge answers a file that is not there yet with the portal page and 200, and
                  // cache-first would keep that wrong answer for the whole expiry; this way the next
                  // request puts it right. ★ The path is a LITERAL on purpose: workbox copies this
                  // function into sw.js as text, where a constant from this file (PLUS_DIR) does
                  // not exist.
                  urlPattern: ({ url }: { url: URL }) => url.pathname.startsWith('/assets/plus/'),
                  handler: 'StaleWhileRevalidate' as const,
                  options: {
                    cacheName: 'talus-plus-files',
                    expiration: { maxEntries: 200, maxAgeSeconds: 30 * 24 * 3600 },
                    cacheableResponse: { statuses: [200] },
                  },
                },
              ]
            : []),
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
        // Talus+ files go to their own folder, with their notice (see PLUS_DIR above). A Community
        // build takes the second branch every time, which is Vite's default naming.
        chunkFileNames: (chunk) => {
          if (chunk.moduleIds.some(fromPaid)) paidChunkNames.add(chunk.name)
          return jsFileName(chunk)
        },
        assetFileNames: (asset) =>
          asset.names.some((n) => n.endsWith('.css') && paidChunkNames.has(n.slice(0, -4)))
            ? `${PLUS_DIR}[name]-[hash][extname]`
            : 'assets/[name]-[hash][extname]',
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
  // A web worker is bundled on its own, outside the rules above, so it gets the same two: a worker
  // built from the paid tree is written to assets/plus/ with the notice.
  worker: {
    plugins: () => [paidWorkerNotice()],
    rollupOptions: {
      output: { entryFileNames: jsFileName, chunkFileNames: jsFileName },
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
