import maplibregl from 'maplibre-gl'
import { Protocol } from 'pmtiles'
import type { StreamedLook } from '@/lib/api'
import { publisherOf } from '@/features/map/basemapNames'

/**
 * The shared runtime a standalone basemap-drawing MapLibre map needs — hoisted out of
 * `AssetMap.tsx` for the ExtentPicker (decision-extent-picker.md §3.1), which builds its own
 * small map from the same recipe. Everything here is a straight move: the pmtiles protocol
 * cache (and its poison-eviction), the vendored glyph/sprite URLs, the USGS imagery source,
 * and the style-readiness helper.
 */

// Glyphs + sprites are VENDORED into the bundle (web/public/basemaps-assets — decision-basemap-
// provisioning.md F8): the four fontstacks the flavors + our label/sketch layers reference, and
// the v4 sprite sheets. Same-origin, so an air-gapped deployment renders labels and icons with
// zero external requests (previously the protomaps.github.io runtime dependency — the known gap).
// MapLibre wants absolute URLs in the style, so these resolve against our own origin at runtime
// — string concat, NOT new URL(): the {fontstack}/{range} template braces must reach MapLibre
// raw (the same rule as the tile URL templates).
export const GLYPHS = `${window.location.origin}/basemaps-assets/fonts/{fontstack}/{range}.pbf`
export const SPRITE = `${window.location.origin}/basemaps-assets/sprites/v4/light`

// USGS aerial imagery (Talus 18d) — public-domain NAIP via The National Map's cached tile service.
// One of the streamed (external) basemaps; offered only where the organization's settings list it, and a
// build can still hide it with VITE_IMAGERY_BASEMAP=off. The OSM roads/labels overlay drawn over it comes from our own pmtiles, so the
// labels add no new external dependency — only the imagery itself is external.
export const IMAGERY_TILES =
  'https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}'

/** The full raster-source spec, so the z16 cap and its reasoning live in exactly one place. */
export function imageryRasterSource(): maplibregl.RasterSourceSpecification {
  return {
    type: 'raster',
    tiles: [IMAGERY_TILES],
    tileSize: 256,
    // USGS national cache hard-caps at z16 (~2.4 m/px): z17+ return 404 (verified via tile probe;
    // the service's maxScale 1:9028 = z16). Do NOT raise this — a higher maxzoom requests tiles
    // that don't exist and leaves blank holes. Past z16 MapLibre overzooms (blurs) gracefully.
    // House/parcel-level detail (e.g. locating laterals) needs the customer's own county orthophotos
    // (bring-your-own imagery), out of scope here — see decision-18d §7.
    maxzoom: 16,
    attribution:
      'Imagery: <a href="https://www.usgs.gov/programs/national-geospatial-program/national-map">USGS The National Map</a>',
  }
}

// The two streamed STREET maps (V115 / decision-streamed-basemap.md §14:
// stream from the publisher, Talus hosts and relays nothing). Like the imagery above, each viewer's
// browser fetches these directly, so a portal draws one only when its organization OFFERS it (the
// `streamed` list on GET /basemap). These three addresses are the ONLY places a streamed basemap
// can point: a basemap is stored and exchanged as a NAME, and this file is where a name becomes an
// address. Do not add a way to pass one in.
//
// OpenStreetMap's standard tiles — conditions from the OSMF tile usage policy (read 2026-11-10):
// fetched by the browser itself (never relayed: referer-stripping is blocked), the credit kept
// visible while it draws, no bulk or offline fetching, and access can be withdrawn — the organization
// can stop offering it without an upgrade.
export const OSM_TILES = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png'
// USGS Topo — the same public-domain National Map host as the imagery, and the same z16 ceiling
// (z17 answers 404; probed 2026-11-10). Street names appear only at z16.
export const USGS_TOPO_TILES =
  'https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}'

/** The raster-source spec for a streamed basemap, by NAME. The one switch from name to address.
 *  It switches on the look's PUBLISHER: the three OpenStreetMap looks (V116) share one address, so
 *  adding a look adds no address. */
export function streamedRasterSource(look: StreamedLook): maplibregl.RasterSourceSpecification {
  const kind = publisherOf(look)
  if (kind === 'imagery') return imageryRasterSource()
  if (kind === 'usgstopo') {
    return {
      type: 'raster',
      tiles: [USGS_TOPO_TILES],
      tileSize: 256,
      maxzoom: 16, // the national cache's ceiling, as for the imagery — do not raise
      attribution:
        'Map: <a href="https://www.usgs.gov/programs/national-geospatial-program/national-map">USGS The National Map</a>',
    }
  }
  if (kind !== 'osm') {
    // Not one of the three publishers. No caller can pass such a name today (each checks it
    // against the closed list first), and if one ever does it must draw NOTHING: this function
    // used to fall through to OpenStreetMap, which made an unknown name a request to a publisher.
    return { type: 'raster', tiles: [], tileSize: 256 }
  }
  return {
    type: 'raster',
    tiles: [OSM_TILES],
    tileSize: 256,
    maxzoom: 19,
    // The copyright address is VISIBLE TEXT, not only a link target, on purpose: map exports take
    // their footer from a source's attribution with the tags stripped (mapExport.gatherAttribution),
    // and OSM's attribution guidelines ask that a printed map carry the address itself.
    attribution:
      '© OpenStreetMap contributors · <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">openstreetmap.org/copyright</a>',
  }
}

/** The raster PAINT for a streamed look (V116 / decision-streamed-basemap.md §16).
 *  OpenStreetMap publishes one look. Gray and Dark are that same
 *  look recoloured as MapLibre draws it — paint properties on the basemap's OWN raster layer, so
 *  the user's data layers keep their colours (a filter on the canvas would recolour those too). No
 *  tile is altered, stored or re-served; the request is identical to the standard look's.
 *
 *  Gray: all colour removed, contrast eased a little so roads do not shout.
 *  Dark: brightness-min ABOVE brightness-max inverts the image (light land becomes dark, dark
 *  lettering becomes light), with the colour removed so the inverted hues do not go lurid. The
 *  ends are pulled in from 1 and 0 so the result is charcoal with soft lettering, not black
 *  with white. It is a recolouring, not a designed dark map: parks, water and road classes all
 *  become grays.
 *
 *  Every other look returns no paint at all. */
export function streamedRasterPaint(
  look: StreamedLook,
): NonNullable<maplibregl.RasterLayerSpecification['paint']> {
  if (look === 'osm-gray') return { 'raster-saturation': -1, 'raster-contrast': -0.12 }
  if (look === 'osm-dark') {
    return {
      'raster-saturation': -1,
      'raster-brightness-min': 0.9,
      'raster-brightness-max': 0.12,
      'raster-contrast': 0.05,
    }
  }
  return {}
}

/**
 * Run `fn` once the style can accept layer/source mutations, and return an unsubscribe.
 *
 * ★ Why this exists: the call sites used to fall back to `map.once('load', fn)` when
 * `isStyleLoaded()` was false. MapLibre's `load` fires EXACTLY ONCE, at initial map load — so a
 * fallback registered any time after that waits for an event that will never fire again, and the
 * work is dropped **silently and permanently**. `isStyleLoaded()` goes false transiently whenever
 * the style is mutating, so this hit at random: measured on the VM 2026-08-13, two of four imports
 * never registered their layer (import→first-tile 125 s and 27 s, i.e. only when the user reloaded)
 * while the other two registered in 1.7 s and 2.6 s. `idle`/`styledata` recur, so the work always lands.
 */
export const whenStyleReady = (map: maplibregl.Map, fn: () => void): (() => void) => {
  if (map.isStyleLoaded()) {
    fn()
    return () => {}
  }
  const onReady = (): void => {
    if (!map.isStyleLoaded()) return // still mutating — both events fire again
    map.off('idle', onReady)
    map.off('styledata', onReady)
    fn()
  }
  map.on('idle', onReady)
  map.on('styledata', onReady)
  return () => {
    map.off('idle', onReady)
    map.off('styledata', onReady)
  }
}

// Held, not discarded: we need `.tiles` to evict a poisoned entry — see evictPmtilesArchive.
let pmtilesProtocol: Protocol | null = null
export function ensurePmtilesProtocol(): void {
  if (pmtilesProtocol) return
  pmtilesProtocol = new Protocol()
  maplibregl.addProtocol('pmtiles', pmtilesProtocol.tile)
}

/**
 * Drop one archive URL's cached PMTiles instance.
 *
 * ★ Why this must exist (found live; decision-basemap-cold-start.md §10.5).
 * The pmtiles Protocol keeps `tiles: Map<url, PMTiles>` for the LIFE OF THE JS CONTEXT, keyed by
 * the URL with `pmtiles://` stripped — "references will resolve to the same instance if the URLs
 * match". Worse, each PMTiles holds a SharedPromiseCache whose getHeader() stores the *promise*
 * before it settles and NEVER deletes it on rejection. So one 404 leaves a permanently-rejected
 * header promise cached under that URL.
 *
 * We walk straight into that: while unprovisioned the probe deliberately points a source at
 * basemapArchiveUrl() knowing it 404s (so the map still builds for data layers). That poisons the
 * instance, and every LATER map in the same context — an in-app route change to New Map, or
 * AssetMap's F3 rebuild — resolves to it and stays blank. Only a full page reload used to recover, which is
 * exactly the "install succeeded but the map is still gray" report. Removing/re-adding the MapLibre
 * source is NOT enough; the poison is one layer deeper, in the protocol's own cache.
 *
 * Evicting forces `new PMTiles(url)` with a fresh cache. Cost is one ~16 KB header read.
 */
export function evictPmtilesArchive(archiveUrl: string): void {
  pmtilesProtocol?.tiles.delete(archiveUrl)
}

/** The shared Protocol instance (after ensurePmtilesProtocol). Talus Field's packs register an
 *  OPFS-backed archive on it (`protocol.add(new PMTiles(new FileSource(file)))`) so `pmtiles://<key>`
 *  resolves with no server — one protocol, one cache, the same eviction rule above. */
export function pmtilesProtocolInstance(): Protocol {
  ensurePmtilesProtocol()
  return pmtilesProtocol as Protocol
}
