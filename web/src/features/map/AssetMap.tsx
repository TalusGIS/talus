import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useBlocker, useNavigate, useParams, useSearchParams } from 'react-router'

import { moveFreshBaseline } from './freshBaseline'
import { LIGHT, DARK, GRAYSCALE, layers as basemapLayers } from '@protomaps/basemaps'
import maplibregl, { type PositionAnchor } from 'maplibre-gl'
import { PMTiles } from 'pmtiles'
import { createRoot, type Root } from 'react-dom/client'
import { useAuth } from 'react-oidc-context'
import 'maplibre-gl/dist/maplibre-gl.css'

import {
  apiBaseAbsolute,
  ApiError,
  basemapArchiveUrl,
  ConflictError,
  createExternalRef,
  createWebMap,
  fetchBasemapStatus,
  fetchExternalRef,
  fetchHasAttachments,
  fetchLayerColumns,
  fetchPublicSlotFeature,
  fetchLayerExtent,
  fetchMyExternalRefs,
  fetchMyLayers,
  fetchMyRasters,
  fetchRasterInfo,
  fetchWebMap,
  getCustomDomain,
  identifyRasterPoint,
  importFeatures,
  listCustomFields,
  queryLayerFeatures,
  transformPoint,
  updateWebMap,
  uploadFeatureAttachment,
  editOpsOf,
} from '@/lib/api'
import type { ExternalRef, ExternalRefSummary, FeatureSearchResult, FilterCondition, GeoCandidate, GeometryType, LayerColumn, MapUnits, MyLayer, RasterSummary, Renderer, SketchFeature, StreamedBasemap, WebMapBookmark, WebMapDefinition, WebMapLabelConfig, WebMapPopupConfig,
  EditOps,
} from '@/lib/api'
import { useIsPublisher } from '@/lib/auth/roles'
import {
  ensurePmtilesProtocol,
  evictPmtilesArchive,
  GLYPHS,
  SPRITE,
  streamedRasterPaint,
  streamedRasterSource,
  whenStyleReady,
} from '@/features/map/basemapRuntime'
import FeaturePopup, { type FeaturePage } from '@/features/map/FeaturePopup'
import WhatsHerePopup from '@/features/map/WhatsHerePopup'
import RecordDetailLoader from '@/features/record-detail/RecordDetailLoader'
import AddLayersPanel from '@/features/map/AddLayersPanel'
import CreateLayerWizard from '@/features/map/CreateLayerWizard'
import { buildIngestPayloads, prepareFile, seedFromPrepared, UseImportDataError } from '@/features/map/addFromFile'
import type { PreparedFile, SeedIngestContext } from '@/features/map/addFromFile'
import {
  EXT_PALETTE,
  externalExtent4326,
  externalRasterTemplate,
  fetchExternalLayerColumns,
  fetchExternalViewport,
  geojsonBbox,
  newExternalId,
  searchExternalLayers,
} from '@/features/map/addFromUrl'
import type { ExternalLayerSpec } from '@/features/map/addFromUrl'
import type { AddableReference } from '@/features/map/AddLayersPanel'
import ExternalTableView from '@/features/map/ExternalTableView'
import {
  SKETCH_LAYERS,
  ensureSketchLayers,
  geometryToSketch,
  newSketchId,
  setSketchData,
  setSketchVisibility,
  sketchBounds,
  sketchGeometry,
} from '@/features/map/sketch'
import { SketchFeatureCard, SketchToolbar, type SketchTool } from '@/features/map/SketchToolbar'
import BookmarksPanel from '@/features/map/BookmarksPanel'
import MapExportPanel from '@/features/map/MapExportPanel'
import { buildLegendEntries, captureMapPng, defaultExportName, exportScreenshotBlob, downloadBlob, isPitched, type LegendEntry } from '@/features/map/mapExport'
import GeocodeSearch from '@/features/map/GeocodeSearch'
import LocatePanel, { type LocateLayer } from '@/features/map/LocatePanel'
import LayerInspector, { InspectorDock, type InspectorTab } from '@/features/map/LayerInspector'
import FormPanel from '@/features/map/FormPanel'
import {
  Bookmark,
  Crosshair,
  Layers2,
  LocateFixed,
  Maximize2,
  Minimize2,
  Minus,
  Pencil,
  Plus,
  Square,
  SquareDashedMousePointer,
} from 'lucide-react'
import CloseButton from '@/components/CloseButton'
import useConfirm from '@/components/useConfirm'
import LabelsPanel, { LABEL_DEFAULTS } from '@/features/map/LabelsPanel'
import PopupsPanel from '@/features/map/PopupsPanel'
import { popupRowsFor, popupTitleFor, type SlotFieldDefs } from '@/features/map/popupConfig'
import { fieldDefsFromColumns } from '@/lib/api/publicFeature'
import {
  BASEMAP_LABELS,
  downloadedListed,
  isKnownBasemap,
  isStreamedBasemap,
  showNoBasemapNote,
  offeredStreamed,
  publisherOf,
  resolveOffered,
  resolveSaved,
  type Basemap,
} from '@/features/map/basemapNames'
import BasemapSwitcher from '@/features/map/BasemapSwitcher'
import MapHeaderBar from '@/features/map/MapHeaderBar'
import { FieldPacksAction, PopupSheetAction } from '@paid'
import LayerPane from '@/features/map/LayerPane'
import LayerContextMenu from '@/features/map/LayerContextMenu'
import { type TocItem } from '@/features/map/LayerToggle'
import { GeometryIcon, TableIcon } from '@/features/catalog/ItemTypeIcons'
import StylesPanel from '@/features/map/StylesPanel'
import FilterPanel from '@/features/map/FilterPanel'
import TableView from '@/features/catalog/TableView'
import ExportRasterDialog from '@/features/map/ExportRasterDialog'
import { startBoxDraw, type DrawBbox } from '@/features/map/drawBox'
import { useFeatureDraw, type DrawnGeometry, SNAP_INDICATOR_STYLES } from '@/features/map/useFeatureDraw'
import EditToolbar, { type EditGesture, type EditToolbarLayer } from '@/features/map/EditToolbar'
import { useFeatureEdit } from '@/features/map/useFeatureEdit'
import { SnapEngine } from '@/features/map/snapEngine'
import { buildExternalLayerDef, buildSlotLayerDef, buildViewLayerDef, isSlotId, isViewId } from '@/features/map/layers'
import {
  resolveColor,
  lineWidthOf,
  lineDashOf,
  fillOpacityOf,
  outlineColorOf,
  outlineWidthOf,
  pointRadiusOf,
} from '@/features/map/symbology'
import {
  iconSizeFor,
  pointSymbolsLoaded,
  preloadPointSymbols,
  rendererUsesIcon,
  resolveIconImage,
} from '@/features/map/pointIcons'
import type { LayerConfig, LayerDef } from '@/features/map/types'
import { useSync } from '@/lib/offline/SyncProvider'

// The LAST-RESORT view — only shown when nothing more specific exists (a saved map's
// initialView wins; the V094 org default extent wins; a provisioned basemap's coverage
// fit wins — decision-org-default-extent.md fork H). A neutral whole-world frame, deliberately not any one country: the
// pre-provisioning first-run is the only state that ever lands here, and deployments are
// not US-only (was CONUS [-98.5, 39.5] z3.5).

/** V109: does the layer allow anything the pencil can do? All four off = no edit tools at all. */
function anyEditOp(ops: EditOps): boolean {
  return ops.add || ops.attributes || ops.geometry || ops.delete
}

const DEFAULT_CENTER: [number, number] = [0, 20]
const DEFAULT_ZOOM = 1.5
const EDIT_SOURCE = '__edit'
const EDIT_LAYER = '__edit-point'
// Snap hover indicator for the point-place surface (decision-web-editor-snapping.md surface 3 —
// the terra-draw modes render their own built-in snappingPoint indicator, place mode has none).
const SNAP_SOURCE = '__snap'
const SNAP_LAYER = '__snap-point'
const EMPTY_FC: GeoJSON.FeatureCollection = { type: 'FeatureCollection', features: [] }
// The DEV FALLBACK archive (a repo clone's gitignored extract) — production deployments get their
// basemap from the portal-provisioned archive instead, discovered at RUNTIME via GET /basemap
// (decision-basemap-provisioning.md F2). This build-time path only matters when nothing is
// provisioned, so `scripts/basemap/extract-basemap-pmtiles.sh` dev clones keep working unchanged.
// ★ The local extract is a DEV FIXTURE and the DEV gate is load-bearing: web/public ships into
// every production dist, so an ungated fallback silently fake-provisions clean customer installs
// with an irrelevant jurisdiction AND suppresses the unprovisioned notice built for exactly that
// state (found on a clean-box tour, 2026-08-19). Production resolves basemaps ONLY via GET /basemap.
const BASEMAP_FALLBACK_URL: string | null = import.meta.env.DEV
  ? (import.meta.env.VITE_BASEMAP_PMTILES_URL ?? '/basemap.pmtiles')
  : (import.meta.env.VITE_BASEMAP_PMTILES_URL ?? null)
// Self-hosted Protomaps vector flavors offered by the basemap switcher. Sprite + glyphs are
// unchanged across flavors (self-hosting + per-flavor sprites is a separate TCO hardening item).
const VECTOR_FLAVORS: Record<'light' | 'dark' | 'grayscale', typeof LIGHT> = {
  light: LIGHT,
  dark: DARK,
  grayscale: GRAYSCALE,
}

/** Legacy-basemap guard (E11, ruled 2026-08-25): White left the switcher, so a map saved on it —
 * or any unknown future value — falls back to Light at load instead of crashing the flavor
 * lookup. The next Save then writes the sanitized value. */
function sanitizeBasemap(v: string): Basemap {
  return isKnownBasemap(v) ? v : 'light'
}
// Whether THIS portal may draw a named basemap is a second question, answered against the
// organization's offered list by `resolveOffered` (BasemapSwitcher.tsx, V115). It replaces fork C's
// `resolveOrgDefault` (decision-default-basemap.md), which asked it of imagery alone: a stored
// default, or a saved map, may name a streamed map the organization has since withdrawn, and F5's
// rule — never select a basemap that provably must not or cannot draw — now covers all three.

// The streamed-raster source/layer ids. ONE slot, shared by whichever streamed basemap is drawn
// (OpenStreetMap, USGS Topo, USGS imagery): only one basemap is ever on the canvas, and every
// teardown already removes exactly this pair. The ids keep their Talus 18d names because they were
// imagery's alone until V115. Tile addresses + source specs live in basemapRuntime.ts.
const IMAGERY_SOURCE = '__imagery'
const IMAGERY_LAYER = '__imagery-raster'

const srcId = (layerId: string) => `src-${layerId}`
const lyrId = (layerId: string) => `lyr-${layerId}`

// An icon layer with no explicit pointSize renders at this diameter — the size the circle ramp
// reaches at its top zoom, so a layer keeps its visual weight when it gains an icon.
// How long the saved-map canvas hold may last before it releases regardless
// (decision-saved-map-basemap-flash.md §6.5). Generous next to a local fetch, and bounded.
const RESTORE_HOLD_MS = 4000

const DEFAULT_ICON_DIAMETER = 14

/**
 * ★★ Icons draw at z14 and above; below it a point layer shows its circle.
 *
 * NOT a style preference — a hard rendering ceiling. MapLibre's vertex arrays are 16-bit indexed
 * (`MAX_VERTEX_ARRAY_LENGTH = 2^16 - 1`) and an icon is a 4-vertex quad, so a tile can carry at most
 * ~16,383 symbols before the rest are DROPPED (MapLibre's own "Too many glyphs being rendered in a
 * tile" warning). Circles have no such limit: they never go through symbol placement.
 *
 * Measured on the 318,761-feature AddressPoint layer, max features in a single tile:
 *   z11 126,182 · z12 50,120 · z13 15,661 · z14 9,653 · z15 4,263
 * z13 sits at 95% of the ceiling — which is exactly what was seen: icons rendering across half
 * the viewport and stopping at a hard tile edge. z14 leaves 1.7x headroom. Placement is also the
 * per-frame cost that made zoomed-out panning heavy, so the gate fixes both.
 *
 * Per-layer override: the renderer's `minZoom` field (WIRED 2026-09-03 — a 14k-point
 * layer can afford icons far earlier than the 318k layer this default was measured on).
 */
const ICON_MIN_ZOOM = 14

const MAX_ZOOM = 24

/** The icon/circle handover zoom for a point layer: the renderer's own minZoom when sane
 *  (0..MAX_ZOOM), else the measured ICON_MIN_ZOOM default. */
function iconHandoverZoom(renderer: Renderer | null | undefined): number {
  const v = renderer?.minZoom
  return typeof v === 'number' && Number.isFinite(v) && v >= 0 && v <= MAX_ZOOM ? v : ICON_MIN_ZOOM
}

/** A point layer's icon `symbol` layer — a SIBLING of its circle, not a replacement. Both exist for
 *  the life of the layer, split by zoom, so choosing or clearing an icon is a layout change rather
 *  than a teardown (which forced a re-tile and a placement pass over every feature). */
const iconLyrId = (layerId: string) => `${lyrId(layerId)}-icon`

/** The circle half of a point layer. Unchanged from what points have always drawn; when an icon is
 *  set it simply stops at ICON_MIN_ZOOM and the symbol sibling takes over. */
function pointCirclePaint(
  renderer: Renderer | null,
  paintColor: string | maplibregl.ExpressionSpecification,
  strokeColor: string,
): maplibregl.CircleLayerSpecification['paint'] {
  return {
    'circle-radius': pointRadiusOf(renderer),
    'circle-color': paintColor,
    'circle-stroke-color': strokeColor,
    'circle-stroke-width': ['interpolate', ['linear'], ['zoom'], 10, 0.3, 16, 1],
    'circle-opacity': ['interpolate', ['linear'], ['zoom'], 10, 0.6, 14, 0.9],
  }
}

/** The symbol half. Created for EVERY point layer, even one with no icon — an empty `icon-image`
 *  draws nothing and costs nothing, and having the layer already there is what lets an icon change
 *  be a layout update instead of a teardown.
 *
 *  ★★ `icon-allow-overlap` + `icon-ignore-placement` are NOT cosmetic. MapLibre symbol layers do
 *  collision detection and HIDE colliding icons, where circles always draw — and
 *  `queryRenderedFeatures` does not return what was never drawn, so without these a dense point
 *  layer would silently lose features AND their popups (decision-point-icons.md §3.3). */
function pointIconLayout(
  map: maplibregl.Map,
  renderer: Renderer | null,
  fallbackColor: string,
): NonNullable<maplibregl.SymbolLayerSpecification['layout']> {
  const image = resolveIconImage(map, renderer, fallbackColor)
  return {
    ...(image ? { 'icon-image': image } : {}),
    'icon-size': iconSizeFor(renderer?.pointSize ?? DEFAULT_ICON_DIAMETER),
    'icon-allow-overlap': true,
    'icon-ignore-placement': true,
  }
}

/**
 * Point the circle/symbol pair at the current renderer, in place — no teardown.
 *
 * The zoom split is the whole mechanism: with an icon the circle stops at ICON_MIN_ZOOM and the
 * symbol starts there; without one the circle spans every zoom and the symbol draws nothing. Both
 * ranges are set through `setLayerZoomRange`, so choosing an icon never removes a source, never
 * re-tiles, and never triggers a placement pass over a whole low-zoom tile.
 */
function applyPointIconLayout(
  map: maplibregl.Map,
  layerId: string,
  renderer: Renderer | null,
  fallbackColor: string,
): void {
  const hasIcon = rendererUsesIcon(renderer)
  const circleId = lyrId(layerId)
  const symbolId = iconLyrId(layerId)
  if (map.getLayer(circleId)) {
    map.setLayerZoomRange(circleId, 0, hasIcon ? iconHandoverZoom(renderer) : MAX_ZOOM)
  }
  if (!map.getLayer(symbolId)) return
  const layout = pointIconLayout(map, renderer, fallbackColor)
  // Clearing an icon must REMOVE icon-image, not set it to undefined-ish: a symbol layer keeps its
  // last image otherwise, and the circle would draw underneath it.
  map.setLayoutProperty(symbolId, 'icon-image', layout['icon-image'] ?? null)
  map.setLayoutProperty(symbolId, 'icon-size', layout['icon-size'])
  map.setLayerZoomRange(symbolId, iconHandoverZoom(renderer), MAX_ZOOM)
}

const tileUrl = (layerId: string, v: number) =>
  `${apiBaseAbsolute()}/tiles/${layerId}/{z}/{x}/{y}.pbf?v=${v}`
// Dynamic MVT (decision-dynamic-mvt.md): a filtered / labeled / view layer stays on TILES — the api
// composes ST_AsMVT per request, applying the same validated filter grammar as /features and
// including only the requested attribute fields. The {z}/{x}/{y} template must stay raw in the path
// (MapLibre substitutes the literal braces); only the query string is URL-encoded.
const dynTileUrl = (layerId: string, filters: string[], fields: string[], v: number): string => {
  const q = new URLSearchParams()
  for (const f of filters) q.append('filter', f)
  if (fields.length) q.set('fields', fields.join(','))
  q.set('v', String(v))
  return `${apiBaseAbsolute()}/tiles-dyn/${layerId}/{z}/{x}/{y}.pbf?${q.toString()}`
}

// The table-selection highlight trio (decision-zoom-to-selected.md F3) — defined beside
// subLayerIds below, its only consumer, so the id inventory reads top to bottom.
const selLayerIds = (layerId: string): string[] => [
  `${lyrId(layerId)}-sel-fill`,
  `${lyrId(layerId)}-sel-line`,
  `${lyrId(layerId)}-sel-circle`,
]

// Every render layer id a layer may have on the map: the circle (points), the -line (lines +
// polygon outlines), and the -fill (polygons). Point/polygon layers only have a subset; view layers
// (Talus 38) carry all three on one GeoJSON source. All call sites guard with getLayer, so listing
// ids a given layer lacks is a harmless no-op — this centralizes visibility/restack/teardown.
const subLayerIds = (layerId: string): string[] => [
  lyrId(layerId),
  `${lyrId(layerId)}-icon`, // the point icon symbol layer, sibling of the circle (point-icons)
  `${lyrId(layerId)}-line`,
  `${lyrId(layerId)}-fill`,
  `${lyrId(layerId)}-hit`, // transparent wide click target for thin line layers (c6a QA)
  `${lyrId(layerId)}-label`, // per-layer labels symbol layer (decision-map-labels.md)
  ...selLayerIds(layerId), // table-selection highlight trio (decision-zoom-to-selected.md F3)
]

/** Add a layer's label symbol layer (decision-map-labels.md): ONE label class, text = an attribute field.
 * Works on either source type — a vector source (a built-in's baked V022 columns, or the dynamic-MVT
 * endpoint that flattens a slot's owner-defined fields to TOP-LEVEL tile attributes named by field_key —
 * decision-dynamic-mvt.md #3, which is why a flat get serves every layer kind) or an external layer's
 * GeoJSON source (remote features are flat by nature); coded fields label with their raw CODE in v1.
 * Placement is AUTO by geometry family: along the line for lines; point placement for polygons (MapLibre
 * anchors within the polygon); points offset the text just below the circle marker so both stay readable.
 * Collision handling is the renderer's. NOT interactive — clicks pass through to the feature layers. */
function addLabelLayer(map: maplibregl.Map, def: LayerDef, cfg: WebMapLabelConfig): void {
  const src = map.getSource(srcId(def.id))
  if (!src || map.getLayer(`${lyrId(def.id)}-label`)) return
  map.addLayer({
    id: `${lyrId(def.id)}-label`,
    type: 'symbol',
    source: srcId(def.id),
    ...(src.type === 'vector' ? { 'source-layer': def.sourceLayer } : {}),
    ...(cfg.minZoom != null ? { minzoom: cfg.minZoom } : {}),
    layout: {
      'text-field': ['get', cfg.field],
      'text-size': cfg.size ?? LABEL_DEFAULTS.size,
      // Bold = the Medium fontstack (both vendored in /basemaps-assets, where the style's `glyphs`
      // already points) — a two-font toggle, deliberately NOT a font picker (§0 fence).
      'text-font': [cfg.bold ? 'Noto Sans Medium' : 'Noto Sans Regular'],
      ...(def.geometryType === 'line'
        ? { 'symbol-placement': 'line' as const }
        : def.geometryType === 'point'
          ? { 'text-offset': [0, 0.9] as [number, number], 'text-anchor': 'top' as const }
          : {}),
    },
    paint: {
      'text-color': cfg.color ?? LABEL_DEFAULTS.color,
      'text-halo-color': cfg.haloColor ?? LABEL_DEFAULTS.haloColor,
      'text-halo-width': 1.3, // fixed — only the halo COLOR is a knob (§3.3)
    },
  })
}

// The definition pieces that come from React state — everything a Save writes except the live viewport.
// ONE builder shared by currentDefinition (live state, for saves) and the unsaved-work BASELINE captures
// (restore/reset locals), so the two signatures compare like with like and can never drift apart when the
// definition grows a new field.
interface DefinitionContent {
  basemap: string
  order: string[] // unified front=top draw order (vectors + rasters + externals)
  visible: Record<string, boolean>
  rasterMeta: Record<string, { title: string; opacity: number; colormap?: string; externalTiles?: string }>
  externals: Record<string, ExternalLayerSpec> // remote-service references (decision-add-from-url.md)
  layerFilters: Record<string, FilterCondition[]>
  layerLabels: Record<string, WebMapLabelConfig>
  layerPopups: Record<string, WebMapPopupConfig>
  layerTitles: Record<string, string> // map-only display-name overrides (decision-map-rename.md)
  mapTables: string[]
  bookmarks: WebMapBookmark[]
  sketch: SketchFeature[] // map notes (decision-sketch-layer.md) — sketch edits count as unsaved work
  fieldReady: boolean // Talus Field "Field-ready" switch (decision-talus-field-f2.md §4.4); persisted as definition.field
}

function buildDefinition(c: DefinitionContent, view: WebMapDefinition['initialView']): WebMapDefinition {
  return {
    version: 1,
    basemap: c.basemap,
    // Persist the unified order verbatim per type so reopening restores the exact order. Raster ops
    // carry their opacity/colormap; vector ops their filters/labels (omitted when off so pre-feature
    // maps stay byte-identical).
    operationalLayers: c.order.map((id) =>
      // Externals FIRST — a raster-kind external mirrors into rasterMeta for rendering, but it
      // persists as kind:'external' (the spec is the source of truth), never as a Talus raster.
      c.externals[id]
        ? {
            kind: 'external' as const,
            externalId: id,
            externalKind: c.externals[id].externalKind,
            url: c.externals[id].url,
            // Map-only rename applies to externals too — layerTitles wins so a rename actually saves
            // (the display already overrides via selectedDefs; the spec title is the pasted-in default).
            title: c.layerTitles[id] ?? c.externals[id].title,
            visible: c.visible[id] ?? true,
            opacity: c.rasterMeta[id]?.opacity ?? 1,
            ...(c.externals[id].geometryType ? { geometryType: c.externals[id].geometryType } : {}),
            ...(c.externals[id].color ? { color: c.externals[id].color } : {}),
            ...(c.externals[id].where ? { where: c.externals[id].where } : {}),
            // Catalog-item link (decision-external-catalog-items.md): the id rides with the spec
            // SNAPSHOT above. Restore resolved the item at open, so saving here IS the silent
            // snapshot refresh — a healthy item's central fixes get re-baked into the fallback.
            ...(c.externals[id].externalItemId ? { externalItemId: c.externals[id].externalItemId } : {}),
            // Rail parity: externals persist their panel filter/labels/popup too —
            // the server model never kind-gated these, so pre-parity maps stay valid.
            ...(c.layerFilters[id]?.length ? { filters: c.layerFilters[id] } : {}),
            ...(c.layerLabels[id] ? { labels: c.layerLabels[id] } : {}),
            ...(c.layerPopups[id] ? { popup: c.layerPopups[id] } : {}),
          }
        : c.rasterMeta[id]
          ? {
              kind: 'raster' as const,
              rasterId: id,
              visible: c.visible[id] ?? true,
              opacity: c.rasterMeta[id].opacity,
              colormap: c.rasterMeta[id].colormap,
            }
          : {
              layerId: id,
              visible: c.visible[id] ?? true,
              ...(c.layerFilters[id]?.length ? { filters: c.layerFilters[id] } : {}),
              ...(c.layerLabels[id] ? { labels: c.layerLabels[id] } : {}),
              ...(c.layerPopups[id] ? { popup: c.layerPopups[id] } : {}),
              ...(c.layerTitles[id] ? { title: c.layerTitles[id] } : {}),
            },
    ),
    initialView: view,
    ...(c.mapTables.length ? { tables: c.mapTables.map((id) => ({ layerId: id })) } : {}),
    // Map notes (decision-sketch-layer.md): omitted when empty so pre-sketch definitions stay
    // byte-identical (same posture as tables).
    ...(c.sketch.length ? { sketch: c.sketch } : {}),
    // Field-ready (f2): opt-in, so the key is written only when ON — pre-f2 definitions stay byte-identical.
    ...(c.fieldReady ? { field: { enabled: true } } : {}),
    bookmarks: c.bookmarks,
  }
}

// Unsaved-work signature (the AGO-style leave warning): the definition MINUS the viewport — content
// changes count as unsaved work, a pan/zoom alone never does. Both sides of
// every compare come from buildDefinition, so key order is deterministic and string equality is exact.
function defSignature(def: WebMapDefinition): string {
  const content: Record<string, unknown> = { ...def }
  delete content.initialView
  return JSON.stringify(content)
}

/** The signature of an untouched New Map on `basemap` — what a Save would write for it. The
 *  unsaved-work baseline of a new map, and what `moveFreshBaseline` compares against (C1). */
function freshMapSignature(basemap: Basemap): string {
  return defSignature(
    buildDefinition(
      {
        basemap,
          order: [],
          visible: {},
          rasterMeta: {},
          externals: {},
          layerFilters: {},
          layerLabels: {},
          layerPopups: {},
          layerTitles: {},
          mapTables: [],
          bookmarks: [],
          sketch: [],
          fieldReady: false,
      },
      { center: DEFAULT_CENTER, zoom: DEFAULT_ZOOM },
    ),
  )
}

// Draft autosave: the saveable definition of a SAVED map the caller can
// manage, snapshotted to this browser's localStorage a moment after changes settle, and offered back on the
// next open of the same map (Restore / Discard). It is a per-device convenience, not a server state: a crash,
// a tab closed past the leave warning, or an expired session no longer loses the edits. v1 = saved maps only —
// a never-saved New Map has no identity to reopen. Every access is try/catch'd: storage can be blocked, full,
// or private-window-empty, and the map must render identically without it.
const DRAFT_MAX_BYTES = 2 * 1024 * 1024 // a sketch-heavy map past this is just not drafted (quota is ~5 MB)
const draftKey = (mapId: string): string => `talus:mapDraft:${mapId}`
function readDraft(mapId: string): { savedAt: string; definition: WebMapDefinition } | null {
  try {
    const raw = localStorage.getItem(draftKey(mapId))
    if (!raw) return null
    const d = JSON.parse(raw) as { savedAt?: unknown; definition?: unknown }
    if (typeof d.savedAt !== 'string' || !d.definition || typeof d.definition !== 'object') return null
    return { savedAt: d.savedAt, definition: d.definition as WebMapDefinition }
  } catch {
    return null
  }
}
function writeDraft(mapId: string, definition: WebMapDefinition): void {
  try {
    const raw = JSON.stringify({ savedAt: new Date().toISOString(), definition })
    if (raw.length > DRAFT_MAX_BYTES) return
    localStorage.setItem(draftKey(mapId), raw)
  } catch {
    // storage blocked or full — the leave warning still stands; nothing else changes
  }
}
function clearDraft(mapId: string): void {
  try {
    localStorage.removeItem(draftKey(mapId))
  } catch {
    // nothing to clear
  }
}

// One identified feature: its logical LayerDef id (NOT the render-layer id), feature id, and the
// tile/GeoJSON properties carried in the click. `collectFeatureHits` gathers these across ALL interactive
// layers at one point — the ESRI multi-layer identify — deduped and topmost-first. Today the click handler
// opens only the top hit; the paged popup (c3) walks the whole list (decision-multi-layer-popup.md).
export interface FeatureHit {
  defId: string
  featureId: string
  props: Record<string, unknown>
  geometry: GeoJSON.Geometry // in lng/lat (queryRenderedFeatures) — drives the active-feature highlight
}

// Gather every feature at `point` from the given render layers, map each hit back to its logical layer
// (renderLayerToDef), and dedupe by (layer, feature id). Dedup is required: one logical feature hits
// multiple render layers (polygon fill+outline, line stroke+hit, view fill+line+circle) and repeats across
// tile splits. queryRenderedFeatures returns topmost-first, so the first occurrence wins and the result
// order = draw order (top layer first), matching the ESRI pager. A small pixel box (not a bare point) makes
// thin points/lines easy to hit; layers absent from the map are filtered out so the query never throws.
function collectFeatureHits(
  map: maplibregl.Map,
  point: { x: number; y: number },
  renderLayerToDef: Record<string, string>,
): FeatureHit[] {
  const ids = Object.keys(renderLayerToDef).filter((lid) => map.getLayer(lid))
  if (ids.length === 0) return []
  const tol = 5
  const box: [[number, number], [number, number]] = [
    [point.x - tol, point.y - tol],
    [point.x + tol, point.y + tol],
  ]
  const seen = new Set<string>()
  const hits: FeatureHit[] = []
  for (const f of map.queryRenderedFeatures(box, { layers: ids })) {
    const defId = renderLayerToDef[f.layer.id]
    const featureId = f.properties?.id
    if (!defId || typeof featureId !== 'string') continue
    const key = `${defId}:${featureId}`
    if (seen.has(key)) continue
    seen.add(key)
    hits.push({
      defId,
      featureId,
      props: (f.properties ?? {}) as Record<string, unknown>,
      geometry: f.geometry,
    })
  }
  return hits
}

// Active-feature highlight (c4, decision-multi-layer-popup.md): a bright halo over the currently-paged
// feature. One geometry-agnostic GeoJSON source feeds fill + line + circle so points, lines, and polygons
// all highlight; it's added lazily on the first highlight and kept above the feature layers.
const HL_SRC = 'identify-highlight'
const HL_LAYERS = ['lyr-identify-hl-fill', 'lyr-identify-hl-line', 'lyr-identify-hl-circle'] as const
function ensureHighlightLayers(map: maplibregl.Map): void {
  if (map.getSource(HL_SRC)) return
  map.addSource(HL_SRC, { type: 'geojson', data: { type: 'FeatureCollection', features: [] } })
  map.addLayer({
    id: HL_LAYERS[0], type: 'fill', source: HL_SRC,
    paint: { 'fill-color': '#00b3ff', 'fill-opacity': 0.18 },
  })
  map.addLayer({
    id: HL_LAYERS[1], type: 'line', source: HL_SRC,
    paint: { 'line-color': '#00b3ff', 'line-width': 3 },
  })
  map.addLayer({
    id: HL_LAYERS[2], type: 'circle', source: HL_SRC,
    // Point features ONLY: unfiltered, the circle bucket dotted EVERY
    // VERTEX of a highlighted line/polygon (county boundary identify) — same lesson as the
    // agnostic trio's circle rung. fill+line alone carry the polygon/line treatment.
    filter: ['==', ['geometry-type'], 'Point'],
    paint: {
      'circle-radius': ['interpolate', ['linear'], ['zoom'], 10, 5, 17, 11],
      'circle-color': 'rgba(0, 179, 255, 0.25)',
      'circle-stroke-color': '#00b3ff',
      'circle-stroke-width': 2,
    },
  })
}

// Table-selection highlight (decision-zoom-to-selected.md F3): filter-based layers on the layer's
// OWN vector source — every tile family carries id::text (V011/V014/V017/V022 built-ins, V061
// slot_mvt, tiles_dyn), so no geometry fetch. Selection yellow, deliberately
// distinct from the identify halo's cyan.
const SEL_COLOR = '#ffcc00'
// Stable empty set for the controlled TableView prop — a layer with no entry still gets a
// controlled (not internal) selection, so its first check lifts straight into the map's state.
const EMPTY_SELECTION = new Set<string>()
// One entry in the multi-layer identify set. A vector feature carries its FeatureHit (props + geometry);
// a raster carries the id + title, its value sampled lazily from GET /rasters/{id}/point when paged to.
export type IdentifyItem =
  | { kind: 'feature'; hit: FeatureHit }
  | { kind: 'raster'; rasterId: string; title: string }

// Which on-map rasters cover the click: visible, and whose (cached) extent contains the point. Rasters
// aren't queryable via queryRenderedFeatures (they're raster sources), so this is the parallel path to
// collectFeatureHits. `selected` order (front=top) gives stable paging; a raster with no cached bounds yet
// is skipped (offered once its /info bounds load), so we never page to a value we can't place.
function collectRasterHits(
  lngLat: [number, number],
  selected: string[],
  rasterMeta: Record<string, { title: string }>,
  visible: Record<string, boolean>,
  bounds: Record<string, [number, number, number, number]>,
  availRasters: RasterSummary[],
): IdentifyItem[] {
  const [lng, lat] = lngLat
  const out: IdentifyItem[] = []
  for (const id of selected) {
    if (!rasterMeta[id] || !(visible[id] ?? true)) continue
    const b = bounds[id]
    if (!b) continue
    if (lng < b[0] || lng > b[2] || lat < b[1] || lat > b[3]) continue
    // A saved map stores no raster title, so rasterMeta[id].title can be the id on restore — prefer the
    // catalog title (availRasters), matching the TOC's rasterTitle().
    const title = availRasters.find((r) => r.id === id)?.title ?? rasterMeta[id].title
    out.push({ kind: 'raster', rasterId: id, title })
  }
  return out
}

// Bbox feature cap for an EXTERNAL layer's GeoJSON source (the one non-tiled vector kind left —
// decision-dynamic-mvt.md). Beyond this the user is nudged to zoom in ("zoom in to load more").
const VIEW_FEATURE_CAP = 2000

function pointFC(coords: [number, number]): GeoJSON.FeatureCollection {
  return {
    type: 'FeatureCollection',
    features: [{ type: 'Feature', geometry: { type: 'Point', coordinates: coords }, properties: {} }],
  }
}

type FormState =
  | { kind: 'create'; layerId: string; lon: number; lat: number }
  | { kind: 'edit'; layerId: string; id: string; props: unknown }
  // Line/polygon slice c6a: a feature drawn with terra-draw — the GeoJSON geometry is already captured (WGS-84),
  // so the form collects attributes only (no coordinate entry) and the create attaches `geometry`.
  | { kind: 'createGeom'; layerId: string; geometry: DrawnGeometry }

// A 409 from a guarded write (Talus 34): the row changed under the user. `current` is the
// server's latest row (carries a fresh `version`); we offer overwrite (re-apply with that
// version) or discard (reload the latest) — never silently lose the user's work.
type ConflictState =
  | { kind: 'edit'; layerId: string; id: string; attrs: unknown; current: Record<string, unknown> }
  | { kind: 'move'; layerId: string; id: string; coords: [number, number]; current: Record<string, unknown> }
  | { kind: 'delete'; layerId: string; id: string; current: Record<string, unknown> }

/** The opaque optimistic-concurrency token off a fetched feature's props (Talus 34). */
function versionOf(props: unknown): string {
  const v = (props as { version?: unknown } | null)?.version
  return typeof v === 'string' ? v : ''
}

/** The integer objectid off a fetched feature (F11): a flat row carries it at the top level, a GeoJSON-shaped
 * one under `properties`. Null when absent (a page without a per-feature record) — the sheet icon then hides. */
function objectIdOf(props: unknown): number | null {
  const p = props as { objectid?: unknown; properties?: { objectid?: unknown } } | null
  const v = p?.objectid ?? p?.properties?.objectid
  return typeof v === 'number' && Number.isInteger(v) ? v : null
}

/** Unwrap a slot feature's stored geometry to the single-part seed terra-draw edits (c6b). Slot geometry is
 * stored Multi* (MultiLineString/MultiPolygon); a 1-part Multi unwraps to its LineString/Polygon, which the
 * backend re-wraps with ST_Multi on save. A genuinely multi-part feature returns 'multipart' — vertex
 * editing it single-part would silently drop the other parts, so the caller refuses it (deferred). An
 * unexpected shape returns null. */
function singlePartSeed(geometry: unknown, geomType: 'line' | 'polygon'): DrawnGeometry | 'multipart' | null {
  const g = geometry as { type?: string; coordinates?: unknown } | null
  if (!g || typeof g.type !== 'string') return null
  if (geomType === 'line') {
    if (g.type === 'LineString') return { type: 'LineString', coordinates: g.coordinates as number[][] }
    if (g.type === 'MultiLineString') {
      const parts = g.coordinates as number[][][]
      return parts.length === 1 ? { type: 'LineString', coordinates: parts[0] } : 'multipart'
    }
  } else {
    if (g.type === 'Polygon') return { type: 'Polygon', coordinates: g.coordinates as number[][][] }
    if (g.type === 'MultiPolygon') {
      const parts = g.coordinates as number[][][][]
      return parts.length === 1 ? { type: 'Polygon', coordinates: parts[0] } : 'multipart'
    }
  }
  return null
}

type MoveListeners = {
  down: (e: maplibregl.MapLayerMouseEvent) => void
  enter: () => void
  leave: () => void
}

export interface AssetMapView {
  center: [number, number]
  zoom: number
  bearing: number
  pitch: number
}
export interface AssetMapFlyOptions {
  bearing?: number
  pitch?: number
  animate?: boolean
}

/** Imperative handle for composition surfaces (decision-story-maps.md s3). Callbacks are stable
 *  (they read refs, not captured state), so a consumer may store them once from onMapApi. */
export interface AssetMapApi {
  /** Fly the camera. `bearing`/`pitch` are optional (decision-story-maps.md §9: a story step may
   *  tilt/rotate); omitted => unchanged. `animate: false` jumps. Deliberately NOT `essential`, so
   *  MapLibre's own prefers-reduced-motion fallback (flyTo => jumpTo) stays in force. */
  flyTo: (center: [number, number], zoom: number, opts?: AssetMapFlyOptions) => void
  /** Show exactly `ids` (TOC ids of this map's operational layers) and hide the rest; null
   *  restores the visibility the map had before the first override. Unknown ids are ignored. */
  setVisibleLayers: (ids: string[] | null) => void
  /** The map's current viewpoint — the story editor's "use current view" capture (the
   *  currentDefinition() pattern). Null until the map instance exists. */
  getView: () => AssetMapView | null
  /** Subscribe to camera moves (every `move` frame, with the view). Returns the unsubscribe.
   *  Composition surfaces that mirror one map onto another (the story swipe step, §9) sync
   *  through this; it exposes nothing getView does not. No-op unsubscribe until the map exists. */
  onMove: (cb: (view: AssetMapView) => void) => () => void
  /** Apply an EPHEMERAL attribute filter to one of this map's layers (the dashboards
   *  cross-filter riding the SAME validated grammar + render path as the map's own filter
   *  panel). [] or null clears. A layerId not on this map is ignored. View-only composition
   *  surfaces (embed/inApp) have no Save, so the override is never persisted. */
  setLayerFilters: (layerId: string, conds: FilterCondition[] | null) => void
}

export interface AssetMapProps {
  /** Embed mode (decision-map-embed.md F2): the chrome-reduced iframe viewer — map + zoom +
   *  scale + attribution + popups + a one-line title bar with a collapsed legend. Read-only by
   *  construction (anon, or at most a viewer); all authoring/rails/panels are suppressed. */
  embed?: boolean
  /** In-app composition mode (decision-story-maps.md s3): embed's reduced chrome, but rendered
   *  INSIDE the SPA (e.g. the Talus+ story viewer), so it KEEPS the viewer's session. The
   *  embed-is-anon review ruling below is about the any-origin-frameable /embed document; the
   *  main app ships X-Frame-Options DENY (proxy/Caddyfile), so an in-app surface cannot be
   *  framed. Never set this from the /embed mini-app, and never add an in-app route to the
   *  proxy's frame-relaxed path list. */
  inApp?: boolean
  /** Open THIS web map instead of the /map/:webMapId route param — for composition surfaces
   *  whose own route isn't /map (e.g. /story/:storyId). */
  webMapId?: string
  /** Receive the imperative handle once on mount. */
  onMapApi?: (api: AssetMapApi) => void
  /** Composition surfaces (decision-story-maps.md §9): called once the SAVED map's basemap and
   *  viewport have been restored onto the canvas (the document load's applyView). The handle
   *  arrives at mount, before that restore — a camera move issued earlier is overwritten by it,
   *  so a surface that drives the camera waits for this. Not called on new-map routes. */
  onViewRestored?: () => void
  /** Live legend feed for composition surfaces (the dashboards Legend widget): called with
   *  the flattened legend entries — the SAME builder the PDF/PNG export legend uses — every
   *  time the TOC or visibility changes, so a consumer's legend always matches what this map
   *  actually shows. */
  onLegend?: (entries: LegendEntry[]) => void
  /** Composition-surface selection tap (decision-dashboards.md, the fixed map↔list link):
   *  fired on every identify-path click with the TOPMOST vector hit, or null when the click
   *  found no feature (a consumer clears its selection). Purely additive — the identify popup
   *  behaves exactly as before; consumers get the same FeatureHit identify uses. */
  onFeatureClick?: (hit: FeatureHit | null) => void
}

export default function AssetMap({
  embed = false,
  inApp = false,
  onViewRestored,
  webMapId: webMapIdProp,
  onMapApi,
  onFeatureClick,
  onLegend,
}: AssetMapProps) {
  const auth = useAuth()
  // A LAPSED session must read as anonymous here (decision-map-embed.md F0): oidc-client keeps the
  // expired user in storage, so `auth.user?.access_token` alone hands every fetch a stale token and
  // a public-map visitor gets 401s instead of the anon path. (T35 degraded-offline is unaffected in
  // practice: offline, every fetch fails the same with or without the stale header.)
  // EMBED is hardwired anonymous (review finding): a framed page must never ride the viewer's live
  // session — that would render their privileged view (even private maps) plus edit affordances
  // inside an any-origin-frameable document, the exact clickjacking surface the F1 split closes.
  // An embed always shows the PUBLIC view, by construction. The one exception is inApp
  // (composition inside the DENY-framed SPA — see AssetMapProps), which keeps the session so an
  // authed viewer's shared-but-private content renders.
  const accessToken =
    embed && !inApp ? undefined : auth.isAuthenticated ? auth.user?.access_token : undefined
  const isPublisher = useIsPublisher()
  // Talus 20: /map is a fresh New Map; /map/:webMapId opens a saved web map (restored below).
  // A composition surface may hand the id in as a prop instead (its own route isn't /map).
  const { webMapId: webMapIdParam } = useParams()
  const webMapId = webMapIdProp ?? webMapIdParam
  // V045+: /map?raster=<id> opens a fresh New Map with that raster pre-added (the catalog's "open
  // raster" action). Honored once per id, after the caller's rasters resolve (for the title).
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()

  // Talus 35 — editing is online-only; `submitWrite` returns 'offline' when disconnected (the caller
  // shows "reconnect to edit"). `online` drives the offline map notice. Online 409s still use the
  // inline `conflict` panel below (T34, unchanged).
  const { online, submitWrite } = useSync()

  // 7b: per-layer capabilities come from GET /me/layers (the three-key model — the
  // api is the authority; this just mirrors it for affordances). null = not yet
  // loaded; an empty object = loaded with no viewable layers.
  const [caps, setCaps] = useState<Record<string, MyLayer> | null>(null)
  const [capsError, setCapsError] = useState<string | null>(null)
  // Bumped after add-from-file creates a layer, so /me/layers refetches and the new layer becomes
  // viewable/addable without a reload (decision-add-from-file.md).
  const [capsVersion, setCapsVersion] = useState(0)

  useEffect(() => {
    let cancelled = false
    if (!accessToken) {
      // ANON (decision-map-embed.md F0): /me/layers is authed; an anonymous opener of a public
      // saved map gets its layer metadata from the MAP DOC itself (layersMeta, the server-computed
      // public subset). Loaded HERE — not in the restore effect — so caps lands ONCE, fully
      // populated, exactly like the authed flow: the map-init 'load' handler and the one-shot
      // registration effect both key on the single capsReady flip and would miss a second
      // caps-content change. A fresh anon New Map (no webMapId) has no layers: empty caps.
      if (!webMapId) {
        setCaps({})
        return
      }
      fetchWebMap(webMapId)
        .then((wm) => {
          if (!cancelled) setCaps(Object.fromEntries((wm.layersMeta ?? []).map((m) => [m.id, m])))
        })
        .catch(() => {
          if (!cancelled) setCaps({}) // private/unknown → restore's own fetch shows the notice
        })
      return () => {
        cancelled = true
      }
    }
    fetchMyLayers(accessToken)
      .then((res) => {
        if (!cancelled) setCaps(Object.fromEntries(res.layers.map((l) => [l.id, l])))
      })
      .catch((err) => {
        if (!cancelled) setCapsError(err instanceof ApiError ? `HTTP ${err.status}` : 'request failed')
      })
    return () => {
      cancelled = true
    }
  }, [accessToken, capsVersion, webMapId])

  // The caller's rasters (owned + shared-with-me) for the raster panel's add picker + title lookup.
  // Best-effort: a failure just leaves the picker empty (the map still renders restored raster ops).
  // (State declared ABOVE its first textual use — the no-use-before-define discipline; a memo/effect
  // reading state declared below it is the const-TDZ blank-route class.)
  const [availRasters, setAvailRasters] = useState<RasterSummary[]>([])
  const mapRef = useRef<maplibregl.Map | null>(null)
  // Seeded from the VALIDITY-AWARE accessToken (never raw auth.user — an expired session must not
  // hand tile/attachment requests a stale bearer; see the F0 note above). Kept fresh below.
  const tokenRef = useRef<string | undefined>(undefined)
  const [visible, setVisible] = useState<Record<string, boolean>>({})
  // Mirror visibility into a ref so the post-load registration effect can read the
  // CURRENT visibility for a freshly-registered layer without taking `visible` as a dep.
  const visibleRef = useRef(visible)
  visibleRef.current = visible
  useEffect(() => {
    if (!accessToken) return
    let cancelled = false
    fetchMyRasters(accessToken)
      .then((res) => {
        if (!cancelled) setAvailRasters([...res.owned, ...res.shared])
      })
      .catch(() => {
        /* non-fatal — leave availRasters empty */
      })
    return () => {
      cancelled = true
    }
  }, [accessToken])

  // The caller's shared reference items (decision-external-catalog-items.md) for the picker's
  // "References" group. Best-effort like rasters: a failure just leaves the group empty (restored
  // referenced ops still render — they carry their own snapshot).
  const [availRefs, setAvailRefs] = useState<ExternalRefSummary[]>([])
  useEffect(() => {
    if (!accessToken) return
    let cancelled = false
    fetchMyExternalRefs(accessToken)
      .then((refs) => {
        if (!cancelled) setAvailRefs(refs)
      })
      .catch(() => {
        /* non-fatal — leave availRefs empty */
      })
    return () => {
      cancelled = true
    }
  }, [accessToken])

  // Only layers the user can view become configs/sources — so the map never requests
  // a tile the api would 403. canEdit/editable ride along for the edit affordances.
  //
  // Every layer comes from `caps` (GET /me/layers): we manufacture a LayerDef for each viewable
  // slot via buildSlotLayerDef. The static LAYER_DEFS list this used to merge with held the three
  // pilot built-ins and is gone (decision-retire-builtins.md phase 2).
  // (`as unknown as LayerDef` erases the per-layer P/C/U, as the static list used to.)
  const viewable = useMemo(() => {
    if (!caps) return []
    const slotDefs = Object.values(caps)
      // Exclude standalone non-spatial tables (geomType 'none') — they're not map layers (decision-nonspatial-tables.md).
      .filter((c) => c.canView && isSlotId(c.id) && c.geomType !== 'none')
      .map((c) => buildSlotLayerDef(c) as unknown as LayerDef)
    // Talus 38 — view layers come from /me/layers with renderMode 'features'; manufacture a GeoJSON
    // LayerDef (isView:true) so the map renders them off /layers/{id}/features, not MVT tiles.
    const viewDefs = Object.values(caps)
      .filter((c) => c.canView && c.renderMode === 'features')
      .map((c) => buildViewLayerDef(c))
    return [...slotDefs, ...viewDefs]
  }, [caps])

  // Talus 18b: the New Map renders a user-controlled, ORDERED selection of the viewable universe
  // — it opens EMPTY and grows as the user adds layers via "Add layers". `selected` holds the
  // on-map layer ids in draw order; `selectedDefs` resolves them to LayerDefs.
  const [selected, setSelected] = useState<string[]>([])
  // Raster operational layers (V045+) live in the SAME ordered `selected` list + TOC as vectors, so a
  // user can reorder a raster above or below a vector. `rasterMeta` records WHICH selected ids are
  // rasters (membership) + their title/opacity; visibility rides the shared `visible` map; draw order
  // rides `selected`. (Rendered by the effect below; ordered by `restack`, like vector layers.)
  const [rasterMeta, setRasterMeta] = useState<
    // externalTiles (decision-add-from-url.md): set for a RASTER-KIND external (xyz / arcgis-image) —
    // the render effect uses this tile template instead of a Talus tilejson; colormap/export never apply.
    Record<string, { title: string; opacity: number; colormap?: string; externalTiles?: string }>
  >({})
  // External layers (decision-add-from-url.md): remote-service references, keyed by ext_<8hex>. The
  // single source of truth for SERIALIZING external ops (raster-kind externals also mirror into
  // rasterMeta for rendering/TOC). Ref-mirrored for the stable refreshViewLayer callback; the geojson
  // cache holds a fetched-once document per geojson-kind external (re-fetched per map load, never stored).
  const [externals, setExternals] = useState<Record<string, ExternalLayerSpec>>({})
  const externalsRef = useRef(externals)
  externalsRef.current = externals
  const extGeojsonCacheRef = useRef<Record<string, GeoJSON.FeatureCollection>>({})
  // External column schemas for the rail panels (Filter/Labels/Pop-ups) + the grid — loaded lazily
  // the first time a panel opens on that layer (fetchExternalLayerColumns memoizes per id anyway).
  const [extColumns, setExtColumns] = useState<Record<string, LayerColumn[]>>({})
  // Mirror `selected` + `rasterMeta` into refs for the once-bound map click handler (the multi-layer
  // identify reads current draw order + which ids are rasters). Raster extents (EPSG:4326 [w,s,e,n]) are
  // cached from /info when a raster renders, so the identify click offers only rasters covering the point.
  const selectedRef = useRef(selected)
  selectedRef.current = selected
  const rasterMetaRef = useRef(rasterMeta)
  rasterMetaRef.current = rasterMeta
  const rasterBoundsRef = useRef<Record<string, [number, number, number, number]>>({})
  // The caller's rasters (owned + shared) — powers the add picker + resolves restored ops' titles.
  // Mirror into a ref for the once-bound identify click handler — it resolves a raster's display title
  // (a saved map stores none, so rasterMeta[id].title can be the id on restore).
  const availRastersRef = useRef(availRasters)
  availRastersRef.current = availRasters
  // Live ref so the once-registered click handler always sees the current callback (the
  // filtersRef pattern) — a consumer's inline arrow must never force handler re-registration.
  // Declared HERE, with the other refs that same handler reads, rather than beside the legend
  // refs further down: the handler is bound ~3.3k lines below, and no-use-before-define is
  // positional by design (see eslint.config.js — the const-TDZ blank-route guard).
  const onFeatureClickRef = useRef(onFeatureClick)
  onFeatureClickRef.current = onFeatureClick
  // Raised once the map object exists (the build effect below). An effect that reconciles the map
  // against state gives up while there is no map, and re-runs only when something it lists changes —
  // so state that settled BEFORE the map was built (a raster named in the address, recorded as soon
  // as /me/rasters answers) was never applied, and the raster sat in the layer list undrawn until
  // the user touched it. Listing this makes such an effect run again once there is a map to act on.
  const [mapBuilt, setMapBuilt] = useState(0)
  const addRaster = (r: RasterSummary): void => {
    if (rasterMeta[r.id]) return
    setRasterMeta((m) => ({ ...m, [r.id]: { title: r.title, opacity: 1 } }))
    setVisible((v) => ({ ...v, [r.id]: true }))
    setSelected((s) => (s.includes(r.id) ? s : [...s, r.id])) // rasters land at the BOTTOM of the stack
  }
  const setRasterOpacity = (id: string, opacity: number): void =>
    setRasterMeta((m) => (m[id] ? { ...m, [id]: { ...m[id], opacity } } : m))
  const setRasterColormap = (id: string, colormap: string): void =>
    setRasterMeta((m) => (m[id] ? { ...m, [id]: { ...m[id], colormap: colormap || undefined } } : m))
  // Raster clip-export (V050+, decision-raster-export.md): the TOC "Export" action enters draw-box
  // mode; a drawn box opens the dialog with that bbox, "Whole raster" opens it with no bbox, Cancel/Esc
  // aborts. `drawingExport` toggles the on-map banner; the cleanup ref aborts an in-progress draw.
  const [drawingExport, setDrawingExport] = useState<{ id: string; title: string } | null>(null)
  const [exportTarget, setExportTarget] = useState<{ id: string; title: string; bbox: DrawBbox | null } | null>(null)
  const drawCleanupRef = useRef<(() => void) | null>(null)
  const rasterTitle = (id: string): string =>
    availRasters.find((r) => r.id === id)?.title ?? rasterMeta[id]?.title ?? 'raster'
  const beginRasterExport = (id: string): void => {
    const map = mapRef.current
    if (!map) return
    drawCleanupRef.current?.() // abort any prior draw
    const title = rasterTitle(id)
    setDrawingExport({ id, title })
    drawCleanupRef.current = startBoxDraw(map, {
      onComplete: (bbox) => {
        drawCleanupRef.current = null
        setDrawingExport(null)
        setExportTarget({ id, title, bbox })
      },
      onCancel: () => {
        drawCleanupRef.current = null
        setDrawingExport(null)
      },
    })
  }
  const exportWholeRaster = (): void => {
    if (!drawingExport) return
    drawCleanupRef.current?.()
    drawCleanupRef.current = null
    const { id, title } = drawingExport
    setDrawingExport(null)
    setExportTarget({ id, title, bbox: null })
  }
  const cancelRasterExport = (): void => {
    drawCleanupRef.current?.()
    drawCleanupRef.current = null
    setDrawingExport(null)
  }
  // Abort an in-flight draw if the map unmounts mid-selection.
  useEffect(() => () => drawCleanupRef.current?.(), [])
  // /map?raster=<id> — pre-add that raster to a fresh New Map (the catalog "open raster" action). Once
  // per id, after availRasters resolves so the TOC shows the real title. Skipped for a SAVED map
  // (webMapId) — its definition owns the layer set.
  const openedRasterRef = useRef<string | null>(null)
  useEffect(() => {
    if (webMapId) return
    const id = searchParams.get('raster')
    if (!id || openedRasterRef.current === id) return
    const r = availRasters.find((x) => x.id === id)
    if (!r) return // wait for availRasters; the effect re-runs when it loads
    openedRasterRef.current = id
    addRaster(r)
    // addRaster is render-scoped; openedRasterRef makes this once-per-id, and the deps that can
    // make it fire are listed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams, availRasters, webMapId])
  // Zoom to a raster's extent (rasters carry no features to bound, so fetch its bounds from /info).
  // Silent no-op on any failure — parity with the vector zoomToLayer.
  const zoomToRaster = (id: string): void => {
    const map = mapRef.current
    const token = tokenRef.current
    if (!map || !token) return
    void fetchRasterInfo(id, token)
      .then((info) => {
        const [w, s, e, n] = info.bounds
        map.fitBounds([[w, s], [e, n]], { padding: 48, maxZoom: 17 })
      })
      .catch(() => {
        /* extent unavailable — silent no-op */
      })
  }
  // Auto draw-order by geometry type (AGO convention): points on top, then lines, then polygons —
  // so a freshly-added polygon never buries existing points. `selected` is front=top, so lower rank
  // = nearer the top. Rasters are a separate path, always BELOW all vectors (render effect). This is
  // an INSERT rule for newly-added layers only — it never re-sorts a map the user already arranged.
  const GEOM_RANK: Record<string, number> = { point: 0, line: 1, polygon: 2 }
  // Rank for the auto-insert: point 0 (top) → line 1 → polygon 2 → raster 3 (bottom). A raster already
  // in `selected` ranks 3 so a newly-added vector inserts ABOVE it; the user can still reorder freely.
  const geomRank = (id: string): number =>
    rasterMeta[id] ? 3 : GEOM_RANK[viewable.find((d) => d.id === id)?.geometryType ?? 'polygon'] ?? 2
  // Insert `id` at the top of its own type group: before the first existing layer of equal-or-lower
  // rank (equal rank => newest-of-type on top); if none are, it lands at the bottom of the stack.
  const insertByRank = (s: string[], id: string): string[] => {
    const r = geomRank(id)
    const idx = s.findIndex((x) => geomRank(x) >= r)
    return idx === -1 ? [...s, id] : [...s.slice(0, idx), id, ...s.slice(idx)]
  }
  const addLayer = (id: string): void => setSelected((s) => (s.includes(id) ? s : insertByRank(s, id)))
  // Map-only display-name overrides (decision-map-rename.md), keyed by layerId; absent = the layer's
  // own name. Applied ONCE in selectedDefs (label replacement) so every map surface inherits it.
  // DECLARED ABOVE the memo that reads it — a useMemo above its useState dep is a const-TDZ blank
  // route that tsc can't catch (the standing lesson from the non-spatial-tables slice).
  const [layerTitles, setLayerTitles] = useState<Record<string, string>>({})
  const selectedDefs = useMemo(() => {
    const defById = new Map(viewable.map((d) => [d.id, d]))
    // VECTOR externals resolve to manufactured read-only defs (raster-kind externals ride rasterMeta).
    // NOT part of `viewable` — the Browse-catalog picker must never list them.
    for (const spec of Object.values(externals)) {
      if (spec.externalKind === 'arcgis-feature' || spec.externalKind === 'geojson') {
        defById.set(spec.externalId, buildExternalLayerDef(spec))
      }
    }
    return selected
      .map((id) => defById.get(id))
      .filter((d): d is LayerDef => d !== undefined)
      // Map-only rename (decision-map-rename.md): apply the display-name override HERE, the one choke
      // point — configs spread from these defs, so the TOC, popups, Locate headers, tool flyouts and
      // panel titles all inherit it. The layer item's own name is never touched.
      .map((d) => (layerTitles[d.id] ? { ...d, label: layerTitles[d.id] } : d))
  }, [viewable, selected, layerTitles, externals])

  const configs: LayerConfig[] = useMemo(
    () =>
      selectedDefs.map((d) => {
        const c = caps?.[d.id]
        return {
          ...d,
          canView: true,
          canEdit: c?.canEdit ?? false,
          editViaAdmin: c?.editViaAdmin ?? false,
          editOps: editOpsOf(c), // V109: all-on for a row that carries none
          canManage: c?.canManage ?? false,
          editable: c?.editable ?? false,
          renderer: c?.renderer ?? null,
          // The layer's EFFECTIVE display field (decision-layer-display-field.md) titles the popup + labels
          // Locate results — the starred column, or a server-derived first-text default. Falls back to the
          // static LayerDef default only when the layer read carries nothing (never for a provisioned slot).
          popupTitleField: c?.displayField ?? d.popupTitleField,
        }
      }),
    [selectedDefs, caps],
  )
  // The "Add layers" picker lists the viewable universe (caps), flagged with whether each is
  // already on the map; favorites first, then alphabetical. (Add-only; removal is in the TOC.)
  const addable = useMemo(
    () =>
      viewable
        .map((d) => ({
          id: d.id,
          label: caps?.[d.id]?.displayName ?? d.label,
          color: caps?.[d.id]?.displayColor ?? d.color,
          favorite: caps?.[d.id]?.favorite ?? false,
          added: selected.includes(d.id),
          // Catalog-parity scope tabs in the Add Layers panel (owner / shared-with-me / all / favorites).
          owner: caps?.[d.id]?.owner ?? false,
          sharedWithMe: caps?.[d.id]?.sharedWithMe ?? !(caps?.[d.id]?.owner ?? false),
          geomType: d.geometryType, // by-geometry glyph in the picker, parity with the TOC
        }))
        .sort((a, b) => Number(b.favorite) - Number(a.favorite) || a.label.localeCompare(b.label)),
    [viewable, caps, selected],
  )
  // The caller's rasters as addable items (V045+) — same shape as layers, shown in their own group in
  // the "Add layers" panel; adding one drops it at the bottom of the unified TOC.
  const addableRasters = useMemo(
    () =>
      availRasters
        .map((r) => ({
          id: r.id, label: r.title, color: '#7c8597', favorite: !!r.favorite, added: !!rasterMeta[r.id],
          // Rasters are a separate group (not scope-tab-filtered); owner/sharedWithMe satisfy the shared type.
          owner: false, sharedWithMe: false,
        }))
        .sort((a, b) => Number(b.favorite) - Number(a.favorite) || a.label.localeCompare(b.label)),
    [availRasters, rasterMeta],
  )
  // Tables on this map (decision-map-tables.md, part b): layer ids of geometry-less tables listed in the
  // pane's Tables section — nothing drawn. `openTableId` = the one table open in the docked grid panel.
  // Declared BEFORE the addableTables memo below (const TDZ — the memo reads mapTables at render).
  const [mapTables, setMapTables] = useState<string[]>([])
  const [openTableId, setOpenTableId] = useState<string | null>(null)
  const [tableMaximized, setTableMaximized] = useState(false) // docked panel ⇄ near-full-height
  // Map-extent scoping for the docked table (arc close-out 2026-07-03 — the dormant scopeBbox prop
  // goes live): toggle ON → the grid's fetches carry the CURRENT viewport bbox, refreshed on every
  // debounced moveend (each pan re-queries page 1 — the settled paging answer: reset, don't stitch).
  // Spatial layers only (a table has no geometry); resets when a different table opens.
  const [tableExtent, setTableExtent] = useState(false)
  const [tableBbox, setTableBbox] = useState<string | null>(null)
  useEffect(() => setTableExtent(false), [openTableId])
  // Docked-table row selection LIFTED per layer (decision-zoom-to-selected.md F2): the map (context
  // menu + highlight) and the grid read the same checked-row set, and it survives closing the panel.
  // Clears on layer removal, saved-map switch/restore, or the explicit "Clear selection" menu item;
  // never serialized into a Save (ephemeral working state, like the identify halo). Hosted spatial
  // layers only — a non-spatial table's grid keeps its internal selection (nothing on the map to read).
  const [tableSelections, setTableSelections] = useState<Record<string, Set<string>>>({})
  const tableSelectionsRef = useRef(tableSelections)
  tableSelectionsRef.current = tableSelections
  const setLayerSelection = (layerId: string, next: Set<string>): void =>
    setTableSelections((prev) => {
      if (next.size === 0) {
        if (!(layerId in prev)) return prev
        const copy = { ...prev }
        delete copy[layerId]
        return copy
      }
      return { ...prev, [layerId]: next }
    })

  // On-map Select tool (decision-select-features.md F1-F4): armed for ONE layer at a time; the
  // session is a chained startBoxDraw (drag = box, degenerate click = single pick via onPoint,
  // Esc = disarm) writing through setLayerSelection — highlight/menu/table all just work. Hits
  // come from queryRenderedFeatures on the layer's own render layers: what you see is what you
  // select (hidden/filtered-out features aren't rendered; off-screen rows stay the table's job).
  const [selectArmed, setSelectArmed] = useState<{ layerId: string } | null>(null)
  const selectArmedRef = useRef<string | null>(null)
  selectArmedRef.current = selectArmed?.layerId ?? null
  const selectCleanupRef = useRef<(() => void) | null>(null)
  const selectRenderIds = (map: maplibregl.Map, layerId: string): string[] =>
    [lyrId(layerId), `${lyrId(layerId)}-line`, `${lyrId(layerId)}-fill`, `${lyrId(layerId)}-hit`]
      .filter((l) => map.getLayer(l))
  const applySelectHits = (
    layerId: string,
    hits: maplibregl.MapGeoJSONFeature[],
    mods: { shiftKey: boolean; ctrlKey: boolean; metaKey: boolean },
  ): void => {
    const ids = hits
      .map((f) => f.properties?.id)
      .filter((v): v is string => typeof v === 'string')
    // Ctrl (⌘ on mac) = REMOVE, Shift = ADD (F2 as amended), plain = REPLACE.
    // Remove wins if both are held; either modifier on empty space keeps the selection (the
    // intent is surgical), while a plain empty click/box clears (Pro parity).
    if (mods.ctrlKey || mods.metaKey) {
      if (ids.length === 0) return
      const next = new Set(tableSelectionsRef.current[layerId] ?? [])
      for (const i of ids) next.delete(i)
      setLayerSelection(layerId, next)
      return
    }
    if (mods.shiftKey) {
      if (ids.length === 0) return
      const next = new Set(tableSelectionsRef.current[layerId] ?? [])
      for (const i of ids) next.add(i)
      setLayerSelection(layerId, next)
      return
    }
    setLayerSelection(layerId, new Set(ids))
  }
  const startSelectSession = (layerId: string): void => {
    const map = mapRef.current
    if (!map) return
    selectCleanupRef.current?.()
    selectCleanupRef.current = startBoxDraw(
      map,
      {
        onComplete: (bbox, mods) => {
          selectCleanupRef.current = null
          const m = mapRef.current
          if (m) {
            // The helper reports 4326; queryRenderedFeatures wants screen pixels.
            const a = m.project([bbox[0], bbox[1]])
            const b = m.project([bbox[2], bbox[3]])
            const px: [maplibregl.PointLike, maplibregl.PointLike] = [
              [Math.min(a.x, b.x), Math.min(a.y, b.y)],
              [Math.max(a.x, b.x), Math.max(a.y, b.y)],
            ]
            applySelectHits(
              layerId,
              m.queryRenderedFeatures(px, { layers: selectRenderIds(m, layerId) }),
              mods ?? { shiftKey: false, ctrlKey: false, metaKey: false },
            )
          }
          startSelectSession(layerId) // the tool STAYS armed across selects (F1, Pro behavior)
        },
        onPoint: (pt, mods) => {
          selectCleanupRef.current = null
          const m = mapRef.current
          if (m) {
            const px: [maplibregl.PointLike, maplibregl.PointLike] = [
              [pt.x - 4, pt.y - 4],
              [pt.x + 4, pt.y + 4],
            ]
            const hits = m.queryRenderedFeatures(px, { layers: selectRenderIds(m, layerId) })
            // Single pick = the TOPMOST hit only (queryRenderedFeatures returns top-first).
            applySelectHits(layerId, hits.slice(0, 1), mods)
          }
          startSelectSession(layerId)
        },
        onCancel: () => {
          // Esc (the only cancel path once onPoint absorbs degenerate clicks) = disarm.
          selectCleanupRef.current = null
          setSelectArmed(null)
        },
      },
      { idPrefix: '__select-draw', color: '#ffcc00' }, // rubber band in selection yellow
    )
  }
  const disarmSelect = (): void => {
    selectCleanupRef.current?.()
    selectCleanupRef.current = null
    setSelectArmed(null)
  }
  const armSelect = (layerId: string): void => {
    // Selection is a read tool — arming leaves edit mode so create/draw never contend for clicks.
    // (These setters are declared below with the editing-state cluster; this closure only runs on
    // user gestures, long after the component body has executed.)
    /* eslint-disable @typescript-eslint/no-use-before-define */
    setEditMode(false)
    setCreateLayerId(null)
    setDrawLayerId(null)
    /* eslint-enable @typescript-eslint/no-use-before-define */
    setSelectArmed({ layerId })
    startSelectSession(layerId)
  }
  // Abort an in-flight session if the map unmounts while armed.
  useEffect(() => () => selectCleanupRef.current?.(), [])
  useEffect(() => {
    const map = mapRef.current
    if (!map || !tableExtent || !openTableId) return
    const capture = (): void => {
      const b = map.getBounds()
      setTableBbox(`${b.getWest()},${b.getSouth()},${b.getEast()},${b.getNorth()}`)
    }
    capture() // seed immediately on toggle-on / table change
    let timer: number | undefined
    const onMoveEnd = (): void => {
      window.clearTimeout(timer)
      timer = window.setTimeout(capture, 300)
    }
    map.on('moveend', onMoveEnd)
    return () => {
      window.clearTimeout(timer)
      map.off('moveend', onMoveEnd)
    }
  }, [tableExtent, openTableId])
  // Right-click layer options (decision-map-layer-table.md, part c): the TOC row menu, opened at a
  // viewport point by a row's onContextMenu or its ⋯ kebab. v1 = "Open attribute table" only, which
  // widens openTableId to name ANY viewable layer (the panel + TableView already handle that).
  // kind 'layer' = a vector/view TOC row (Attribute Table / Zoom to / Rename); kind 'table' = a
  // Tables-section row (the part-(c) fast-follow — Attribute Table only; tables have no geometry to
  // zoom and aren't OperationalLayers to rename).
  const [layerMenu, setLayerMenu] = useState<{ kind: 'layer' | 'table'; layerId: string; x: number; y: number } | null>(null)
  // Standalone non-spatial tables (decision-map-tables.md): the caller's viewable tables as addable items —
  // a "Tables" group in the Add layers panel. Adding lists the table in the pane's Tables section.
  const addableTables = useMemo(
    () =>
      Object.values(caps ?? {})
        .filter((c) => c.canView && c.geomType === 'none')
        .map((c) => ({
          id: c.id,
          label: c.displayName ?? c.id,
          color: c.displayColor ?? '#7c8597',
          favorite: c.favorite ?? false,
          added: mapTables.includes(c.id),
          owner: c.owner ?? false,
          sharedWithMe: c.sharedWithMe ?? !(c.owner ?? false),
        }))
        .sort((a, b) => Number(b.favorite) - Number(a.favorite) || a.label.localeCompare(b.label)),
    [caps, mapTables],
  )
  const capsReady = caps !== null

  // Runtime basemap discovery (decision-basemap-provisioning.md F2). Resolved BEFORE the map is
  // constructed (the style needs the source URL): the anon GET /basemap says whether the portal
  // has a provisioned archive; otherwise probe the dev fallback. `null` = still resolving (the
  // map-construction effect waits); `basemapAbsent` drives the friendly unprovisioned notice.
  const [basemapUrl, setBasemapUrl] = useState<string | null>(null)
  const [basemapAbsent, setBasemapAbsent] = useState(false)
  // Read by the saved-map restore, which runs outside render (§16, G3: a downloaded look is kept
  // only while there is an area to draw it from).
  const basemapAbsentRef = useRef(false)
  basemapAbsentRef.current = basemapAbsent
  // V094 (decision-org-default-extent.md): the org default extent + units, stashed as REFS only —
  // map construction reads each exactly once and nothing renders from them, so state would be dead
  // weight. Always populated when read: the status fetch writes them BEFORE setBasemapUrl, and
  // construction gates on basemapUrl. On the api-unreachable path they keep their fail-soft
  // defaults (no extent, metric).
  const orgExtentRef = useRef<[number, number, number, number] | null>(null)
  const orgUnitsRef = useRef<MapUnits>('metric')
  // V115 (decision-streamed-basemap.md §14): the streamed maps this portal OFFERS, from the same
  // status fetch. State for the switcher; a ref for the paint path and the saved-map restore, which
  // run outside render. Written BEFORE setBasemapUrl like the V094 refs: construction gates on that.
  //
  // ★ EMPTY until the organization's answer is in, and it STAYS empty if the fetch fails. It used
  // to start as "what every install offered before V115" (imagery), which made the zero-external
  // setting fail OPEN: with the api down and the portal still served, the unprovisioned bootstrap
  // picked imagery and the paint check passed against the placeholder (security review of R1+R2,
  // 2026-11-10, finding 1). An unknown answer is not permission. The pre-V115 meaning — a
  // SUCCESSFUL response with no `streamed` key means imagery — still holds, inside
  // `offeredStreamed`, where it belongs.
  const [streamed, setStreamed] = useState<StreamedBasemap[]>([])
  const streamedRef = useRef<StreamedBasemap[]>([])
  // Whether the list above is the organization's answer yet (true only after a successful fetch).
  // The paint path never needs it — empty is the safe reading there. The saved-map baseline does:
  // it must not call a map "changed" on the strength of a list that has not arrived.
  const streamedKnownRef = useRef(false)
  // V116 (decision-streamed-basemap.md §16) — whether the organization LISTS its downloaded looks
  // (Light, Dark, Gray) in the switcher. It starts true and stays true on a failed fetch: listing a
  // look that draws from this server sends no browser anywhere, so "listed" is the safe reading of
  // an answer that has not arrived (the opposite of the offered list above, which starts empty).
  const [listsDownloaded, setListsDownloaded] = useState(true)
  const listsDownloadedRef = useRef(true)
  // What new maps open on, resolved against the offered list — the fall-back for a streamed look
  // the portal does not offer. Light until the organization's answer is in (Light fetches nothing).
  const orgDefaultRef = useRef<Basemap>('light')
  // The unprovisioned notice is dismissible per session — it sits over
  // the map center, and a crew that knows ("data layers still display") wants it gone.
  const [basemapNoteDismissed, setBasemapNoteDismissed] = useState(false)
  // Outside the downloaded basemap.
  // A downloaded basemap is an extract of one
  // area by design; pan or zoom to a place it does not hold and a downloaded look draws flat grey
  // with nothing to say why. `coverageRef` is the archive's own box [w, s, e, n] from its header
  // (null = not read, or a world-scale build that has no "outside"); the flag is true only while
  // the view lies WHOLLY outside it — a visible edge explains itself. Dismissible per page, like the
  // no-basemap note.
  const coverageRef = useRef<[number, number, number, number] | null>(null)
  const [outsideCoverage, setOutsideCoverage] = useState(false)
  const [coverageNoteDismissed, setCoverageNoteDismissed] = useState(false)
  // decision-saved-map-basemap-flash.md option C — hold the canvas on a SAVED-map route until the
  // restore has applied its basemap. The style is always built on Light and the restore needs its
  // own `fetchWebMap` round trip, so without this the map paints Light and then switches ~1s later:
  // a colour flip that reads as a bug. Initialised from `webMapId` so the hold is up from the very
  // first render — arming it later would race the map build it exists to cover.
  const [restoreHold, setRestoreHold] = useState<boolean>(!!webMapId)
  const restoreHoldTimerRef = useRef<number | null>(null)
  // Release is idempotent and called from every exit: applied, failed, or timed out. A hold that
  // outlives its restore would strand the canvas — worse than the flash it replaces.
  const releaseRestoreHold = useCallback((): void => {
    if (restoreHoldTimerRef.current !== null) {
      window.clearTimeout(restoreHoldTimerRef.current)
      restoreHoldTimerRef.current = null
    }
    setRestoreHold(false)
  }, [])
  // Arm the hold on ROUTE ENTRY, not when the fetch starts. The restore effect waits on `capsReady`,
  // so arming there left one stranding path: caps that never resolve mean the fetch never begins and
  // no timeout ever runs, leaving "Opening map…" up forever. Keyed on webMapId so navigating between
  // saved maps re-arms, and New Map (no id) never holds at all.
  useEffect(() => {
    if (!webMapId) {
      setRestoreHold(false)
      return
    }
    setRestoreHold(true)
    const t = window.setTimeout(() => {
      restoreHoldTimerRef.current = null
      setRestoreHold(false)
    }, RESTORE_HOLD_MS)
    restoreHoldTimerRef.current = t
    return () => {
      window.clearTimeout(t)
      if (restoreHoldTimerRef.current === t) restoreHoldTimerRef.current = null
    }
  }, [webMapId])
  useEffect(() => {
    let active = true
    ;(async () => {
      try {
        const status = await fetchBasemapStatus()
        const offered = offeredStreamed(status.streamed)
        const lists = downloadedListed(status.downloadedListed)
        // The api keeps this default to a map the portal lists; resolving it here as well means a
        // stale or hand-written value still cannot name a publisher the portal does not offer.
        const orgDefault = resolveOffered(sanitizeBasemap(status.defaultBasemap), offered)
        if (active) {
          streamedRef.current = offered
          streamedKnownRef.current = true
          setStreamed(offered)
          listsDownloadedRef.current = lists
          setListsDownloaded(lists)
          orgDefaultRef.current = orgDefault
        }
        // The org default seed (V093), applied BEFORE the installed/absent branches so it covers
        // every provisioned path — and so the F1 imagery bootstrap below, which runs later in this
        // same flow, still wins on an unprovisioned box (fork B). Ref AND state, for the reason the
        // F1 block spells out.
        //
        // NEW MAPS ONLY (`!webMapId`) — the setting is defined as "the basemap a NEW map opens on",
        // and a saved map owns its own. Seeding it on a /map/:webMapId route was visibly wrong, not
        // just redundant: the definition fetch resolves ~1s AFTER the style loads, so the canvas
        // painted the org default and then switched to the saved basemap in front of the user
        // (org default Gray + a map saved on Dark). F4.2 always made the saved
        // basemap win — the bug was that the org default got to paint first at all.
        if (active && !webMapId) {
          /* eslint-disable @typescript-eslint/no-use-before-define */
          const seed = orgDefault
          // The portal chose this look, not the user: an untouched new map stays untouched (C1).
          baselineSigRef.current = moveFreshBaseline(baselineSigRef.current, freshMapSignature, basemapRef.current, seed)
          basemapRef.current = seed
          setBasemap(seed)
          /* eslint-enable @typescript-eslint/no-use-before-define */
        }
        // V094 org defaults. Units apply on EVERY route — the embed scale bar (a saved map) is
        // their one consumer. The extent is !webMapId-guarded for TWO reasons; do not "simplify"
        // the guard away: (1) the V093 lesson above — on a saved-map route the definition fetch
        // resolves ~1s after the style loads, and an org value must never get to paint first; and
        // (2) it is what keeps the extent off every anonymous surface — both anon-capable routes
        // open saved maps (decision-org-default-extent.md §2.3).
        if (active) {
          orgUnitsRef.current = status.defaultUnits
          if (!webMapId) orgExtentRef.current = status.defaultExtent
        }
        if (status.installed) {
          if (active) setBasemapUrl(basemapArchiveUrl())
          return
        }
      } catch {
        // api unreachable / route absent — fall through to the dev fallback probe. The offered
        // list is deliberately left EMPTY here (see streamedRef above): nothing is streamed from
        // the internet on a status we could not read.
      }
      // Probe the dev fallback (DEV builds only — null in production): read its first bytes and
      // check the PMTiles magic. A missing public/ file comes back as the SPA's index.html (the
      // dev server's SPA fallback), so a status check alone would false-positive — the magic
      // bytes are the real test.
      if (BASEMAP_FALLBACK_URL !== null) {
        try {
          const res = await fetch(BASEMAP_FALLBACK_URL, { headers: { Range: 'bytes=0-6' } })
          if (res.ok) {
            const head = new Uint8Array(await (await res.blob()).slice(0, 7).arrayBuffer())
            const magic = String.fromCharCode(...head)
            if (magic === 'PMTiles') {
              if (active) setBasemapUrl(BASEMAP_FALLBACK_URL)
              return
            }
          }
        } catch {
          // unreachable too — absent
        }
      }
      if (active) {
        // Build the map anyway (data layers, imagery, and controls all still work) — the vector
        // basemap source just has nothing behind it; the notice explains the void.
        setBasemapAbsent(true)
        setBasemapUrl(basemapArchiveUrl())
        // Cold-start default (decision-basemap-cold-start.md F1): with no vector archive the canvas
        // is a gray void, and Admin → Settings → Basemap's "use last map view" has nothing to
        // capture — you cannot navigate to your jurisdiction on a blank map. USGS imagery gives
        // spatial context from the first second and breaks that bootstrap circularity.
        // CONDITIONAL on purpose: we are inside the ABSENT branch, so a provisioned org never sees
        // it — imagery stays a bootstrap aid, never a new product default. Skipped for embeds (F2:
        // a publishing surface whose owner already chose a basemap, not a bootstrap surface) and
        // when imagery is disabled (F5: an air-gapped box must not select a basemap that provably
        // cannot draw). A saved map's own basemap still wins — its restore applies at style load,
        // after this (F4.2). Seed-only, and it deliberately overrides the org default above:
        // a vector flavour on an unprovisioned box is a gray void (fork B).
        //
        // V115 (decision-streamed-basemap.md §14.5): the override now applies only when the seed is
        // one of the DOWNLOADED looks, which is the void this block exists for. A seed that is
        // already a streamed map — a new install opens on OpenStreetMap — draws without an
        // archive and is left alone. And the bootstrap picks from what the organization OFFERS:
        // imagery when it is offered (exactly the 2026-08-24 behaviour, which is what every
        // upgraded install still has), else the first streamed map it does offer, else nothing —
        // an organization that offers none has chosen to fetch no map background from the internet.
        const bootstrap = streamedRef.current.includes('imagery') ? 'imagery' : streamedRef.current[0]
        /* eslint-disable-next-line @typescript-eslint/no-use-before-define */
        if (bootstrap !== undefined && !embed && !isStreamedBasemap(basemapRef.current)) {
          // Set the ref synchronously as well as the state: the map-construction effect can commit
          // in this same batch, and the 'load' handler it binds reads basemapRef — not state, which
          // has not re-rendered yet. (The unconditional render-time assignment below re-syncs it.)
          // (basemapRef/setBasemap are declared below with the basemap cluster; this async IIFE
          // resolves after the component body has executed.)
          /* eslint-disable @typescript-eslint/no-use-before-define */
          if (!webMapId) {
            baselineSigRef.current = moveFreshBaseline(baselineSigRef.current, freshMapSignature, basemapRef.current, bootstrap)
          }
          basemapRef.current = bootstrap
          setBasemap(bootstrap)
          /* eslint-enable @typescript-eslint/no-use-before-define */
        }
      }
    })()
    return () => {
      active = false
    }
  }, [embed, webMapId])

  const containerRef = useRef<HTMLDivElement | null>(null)
  // (mapRef/tokenRef are declared up with the raster-export block that first references them.)
  const refreshRef = useRef<Record<string, number>>({})
  // Per-layer bound cursor handlers + interactive layer ids, so removeLayer can map.off them cleanly
  // (re-adding a removed layer must not leave stale, double-firing handlers behind). Clicks are handled
  // once at the map level (the multi-layer identify handler), not per layer — see renderLayerToDefRef.
  const layerHandlersRef = useRef<
    Record<
      string,
      {
        ids: string[]
        onEnter: () => void
        onLeave: () => void
      }
    >
  >({})
  // Reverse index render-layer id → logical LayerDef id, so the map-level identify handler maps a
  // queryRenderedFeatures hit back to its layer. Built in registerLayerOnMap, torn down in deregister.
  const renderLayerToDefRef = useRef<Record<string, string>>({})
  // The basemap layer ids currently on the map, so the switcher removes exactly the old flavor's
  // layers (robust whether or not flavors share ids) before adding the new one's.
  const basemapLayerIdsRef = useRef<string[]>([])
  const moveCoordsRef = useRef<[number, number] | null>(null)
  // Talus 18c: the ephemeral pin for the last geocode result (cleared on next search/dismiss).
  const geocodeMarkerRef = useRef<maplibregl.Marker | null>(null)
  const popupRef = useRef<maplibregl.Popup | null>(null)
  const popupRootRef = useRef<Root | null>(null)
  const popupLayerRef = useRef<string | null>(null)
  const moveListenersRef = useRef<MoveListeners | null>(null)
  const layersRef = useRef<LayerDef[]>(selectedDefs)
  layersRef.current = selectedDefs
  const configsRef = useRef<LayerConfig[]>(configs)
  configsRef.current = configs
  const byId = (layerId: string): LayerConfig | undefined =>
    configsRef.current.find((c) => c.id === layerId)

  // Per-layer attribute filters (decision-feature-filter.md), keyed by layerId. A filtered layer renders via
  // the GeoJSON/View path with the predicate applied; empty = unfiltered (MVT). `filtersRef` mirrors the
  // state so the stable map callbacks (register/refresh) read the live filter without being in their deps.
  const [layerFilters, setLayerFilters] = useState<Record<string, FilterCondition[]>>({})
  // ONE shared filter per layer (superseding the ephemeral table mirror): the
  // docked table's grid and the funnel FilterPanel are two EDITORS of this same state — a filter
  // applied in either persists until cleared (and saves with the map on the next Save).
  const filtersRef = useRef<Record<string, FilterCondition[]>>({})
  filtersRef.current = layerFilters
  // Per-layer labels (decision-map-labels.md), keyed by layerId; absent = off. A labeled SLOT renders via
  // the GeoJSON path (its tiles carry only geom+id); built-ins label straight off their MVT properties and
  // views are already GeoJSON. `labelsRef` mirrors the state for the stable map callbacks, like filtersRef.
  const [layerLabels, setLayerLabels] = useState<Record<string, WebMapLabelConfig>>({})
  const labelsRef = useRef<Record<string, WebMapLabelConfig>>({})
  labelsRef.current = layerLabels
  // Per-layer popup config (decision-map-popups.md), keyed by layerId; absent = default popups. The ref
  // mirrors state for the once-bound identify path (loadFeaturePage / openIdentifyPopup), like filters.
  const [layerPopups, setLayerPopups] = useState<Record<string, WebMapPopupConfig>>({})
  const popupsRef = useRef<Record<string, WebMapPopupConfig>>({})
  popupsRef.current = layerPopups
  // Slot owner-defined field defs + their domain values, fetched lazily per layer for popup row/title
  // resolution (labels + coded→domain-label) and cached as a PROMISE so concurrent popups dedupe. A
  // failed fetch caches null — popupConfig falls back to prettified raw props keys, never blocking.
  const slotDefsRef = useRef<Record<string, Promise<SlotFieldDefs | null>>>({})
  const anonSlotDefsRef = useRef<Record<string, Promise<SlotFieldDefs | null>>>({})
  // A-4 popup strip: per-layer "has attachments" probe, promise-cached once per layer per map session
  // (§3.1) so attachment-less layers never pay a per-click list request. Staleness accepted (reload refreshes).
  const hasAttachRef = useRef<Record<string, Promise<boolean>>>({})
  const filterParamsFor = (layerId: string): string[] =>
    (filtersRef.current[layerId] ?? []).map((c) => `${c.field}:${c.op}:${c.value}`)
  // Dynamic-MVT routing (decision-dynamic-mvt.md): a hosted layer rides /tiles-dyn when it needs
  // per-request tile logic — a view, an active filter, or slot labels (a slot's static tiles carry
  // only geom+id). Externals are isView-SHAPED but REMOTE — never ours to tile. Every helper below
  // reads only refs, so the stable registerLayerOnMap/reconcile callbacks always see live state.
  // ★ Choosing an attribute coloured the legend but the map all showed grey:
  // a unique-value renderer's `match` reads
  // its field OFF THE TILE, and a slot's static tiles carry only geom+id — so every feature fell
  // to the «No value» colour, on every hosted layer, since the baked-column built-ins retired
  // (2026-08-28). A slot styled by field now rides the dynamic tiles with that field requested
  // (the same door and validity set labels use; a props key or a real column both resolve).
  const rendererFieldFor = (layerId: string): string | null => {
    const r = configsRef.current.find((c) => c.id === layerId)?.renderer
    return r?.type === 'uniqueValue' && r.field && (r.stops?.length ?? 0) > 0 ? r.field : null
  }
  const ridesDynamicTiles = (layerId: string): boolean =>
    !externalsRef.current[layerId] &&
    (isViewId(layerId) ||
      (filtersRef.current[layerId]?.length ?? 0) > 0 ||
      (isSlotId(layerId) && (!!labelsRef.current[layerId] || rendererFieldFor(layerId) !== null)))
  // A view's tile ATTRIBUTES: its popup renders straight from tile properties (no per-feature
  // fetch), so its tiles must carry the view's columns — resolved once per view from
  // /layers/{id}/columns (token-optional, so an anon viewer gets the anon-scrubbed schema and never
  // requests a scrubbed column). Audit uuid columns are skipped (a tile attribute would be the raw
  // uuid, not the resolved editor name the GeoJSON path used to join in).
  const viewColsRef = useRef<Record<string, string[]>>({})
  // fields= for a dynamic tile URL: the label field (any layer kind) + a view's columns, capped at
  // the server's per-request field limit (mirrors the popup-field cap — an over-wide view popups a
  // subset rather than failing its tiles).
  const tileFieldsFor = (layerId: string): string[] => {
    const fields: string[] = []
    // The renderer's field FIRST: the colour must survive the per-request cap below.
    const rf = rendererFieldFor(layerId)
    if (rf) fields.push(rf)
    const lbl = labelsRef.current[layerId]
    if (lbl && !fields.includes(lbl.field)) fields.push(lbl.field)
    for (const c of viewColsRef.current[layerId] ?? []) if (!fields.includes(c)) fields.push(c)
    return fields.slice(0, 30)
  }
  // The CURRENT tile URL for a hosted layer — static pg_tileserv tiles by default, the dynamic
  // endpoint when filtered/labeled/view. v (refreshRef) busts the browser cache after edits.
  const hostedTileUrl = (layerId: string): string => {
    const v = refreshRef.current[layerId] ?? 0
    return ridesDynamicTiles(layerId)
      ? dynTileUrl(layerId, filterParamsFor(layerId), tileFieldsFor(layerId), v)
      : tileUrl(layerId, v)
  }
  // Resolve + cache a view's columns, then repoint its source at the attribute-carrying tile URL.
  // Failure keeps the geometry-only tiles (popup rows appear after a later successful reload).
  const ensureViewTileFields = (layerId: string): void => {
    if (viewColsRef.current[layerId]) return
    void fetchLayerColumns(layerId, tokenRef.current)
      .then((res) => {
        viewColsRef.current[layerId] = res.columns
          .map((c) => c.name)
          .filter((n) => n !== 'id' && n !== 'created_by' && n !== 'updated_by')
        const src = mapRef.current?.getSource(srcId(layerId))
        if (src && src.type === 'vector') {
          ;(src as maplibregl.VectorTileSource).setTiles([hostedTileUrl(layerId)])
        }
      })
      .catch(() => {})
  }
  // Late-bound restack (defined below); a ref avoids a temporal-dead-zone reference from
  // syncLabelLayer, which is declared before restack but only calls it at runtime (label re-add).
  const restackRef = useRef<((map: maplibregl.Map, order: string[]) => void) | null>(null)

  // Default newly-viewable layers to visible once /me/layers resolves, preserving
  // any toggles the user has already made.
  useEffect(() => {
    setVisible((prev) => {
      const next = { ...prev }
      let changed = false
      for (const d of selectedDefs) {
        if (!(d.id in next)) {
          next[d.id] = true
          changed = true
        }
      }
      return changed ? next : prev
    })
  }, [selectedDefs])
  const [editMode, setEditMode] = useState(false)
  const [addOpen, setAddOpen] = useState(false)
  // Add from file (decision-add-from-file.md): a picked file, previewed and held for the post-Finish
  // ingest; non-null opens the seeded create wizard.
  const [filePrep, setFilePrep] = useState<PreparedFile | null>(null)
  const [fileBusy, setFileBusy] = useState(false)
  const [fileError, setFileError] = useState<string | null>(null)
  // The refusal's way forward is Catalog → Import data: the panel offers it as a button.
  const [fileErrorOffersImport, setFileErrorOffersImport] = useState(false)
  // Live import status ("Importing… 10,000 of 34,924") with a spinner — separate from mapNotice so the
  // in-flight state animates and can't be dismissed mid-run; the final result lands in mapNotice.
  const [importProgress, setImportProgress] = useState<string | null>(null)
  // The org default basemap (V093 / decision-default-basemap.md fork A1) is the seed, applied by the
  // discovery effect above once GET /basemap resolves. 'light' here is only the pre-resolution
  // placeholder — it is also the column's own DEFAULT, so the two agree.
  //
  // RETIRED, Talus 41's UI-theme seed (dark theme → dark basemap). It conflated a chrome preference
  // with a cartographic one; a dark portal is a good default, a dark basemap almost never is.
  // An org that wants a dark basemap now picks Dark in Admin → Settings → Basemap,
  // which is the same outcome by a name someone can read.
  const [basemap, setBasemap] = useState<Basemap>('light')
  // Layer-first selection (handoff-map-2a §1, commit 2): ONE selected TOC layer drives the docked
  // inspector, and the tab picks which config surface shows — replacing the four per-panel layer
  // ids (styling/filtering/labeling/popupCfg) and the rail flyouts that set them.
  const [selectedLayerId, setSelectedLayerId] = useState<string | null>(null)
  const [inspectorTab, setInspectorTab] = useState<InspectorTab>('style')
  const [renamingLayerId, setRenamingLayerId] = useState<string | null>(null)
  // Rail parity for externals (decision-add-from-url.md): when an EXTERNAL layer is selected (any
  // inspector tab may need its schema) or its attribute table opens, lazily load its columns.
  useEffect(() => {
    for (const id of [selectedLayerId, openTableId]) {
      if (!id) continue
      const spec = externalsRef.current[id]
      if (!spec || extColumns[id]) continue
      void fetchExternalLayerColumns(spec, extGeojsonCacheRef.current)
        .then((cols) => setExtColumns((m) => (m[id] ? m : { ...m, [id]: cols })))
        .catch(() => setExtColumns((m) => (m[id] ? m : { ...m, [id]: [] })))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedLayerId, openTableId])
  const [createLayerId, setCreateLayerId] = useState<string | null>(null)
  // Line/polygon slice c6a: the layer currently being DRAWN into (terra-draw line/polygon mode). Mutually
  // exclusive with createLayerId (the point click-to-place mode); only one create affordance is active at once.
  const [drawLayerId, setDrawLayerId] = useState<string | null>(null)
  // Editing toolbar (decision-editing-toolbar.md): the picked target layer (sticky across gestures;
  // resolved against the current toolbar population at render) and the armed terra-draw gesture for
  // drawLayerId ('line'/'polygon'/'rectangle'/'circle'/'freehand'; point-place rides createLayerId).
  const [editTargetId, setEditTargetId] = useState<string | null>(null)
  const [drawTool, setDrawTool] = useState<Exclude<EditGesture, 'place'> | null>(null)
  // F4 slice (decision-editing-toolbar-f4.md F2): the vertices tool's armed pick mode. Stays true
  // across the reshape session it seeds, so a click on another target-layer feature re-seeds and
  // the toolbar icon reads active for the whole flow. Popup-launched reshape leaves this false.
  const [shapePick, setShapePick] = useState(false)
  const shapePickRef = useRef(false)
  shapePickRef.current = shapePick
  // The effective toolbar target (resolved at render below); ref for the map click handler.
  const editTargetRef = useRef<string | null>(null)
  const [form, setForm] = useState<FormState | null>(null)
  const [moving, setMoving] = useState<{ layerId: string; id: string; version: string } | null>(null)
  // Line/polygon slice c6b — vertex/ring editing of an existing feature via terra-draw select. `editing`
  // holds the in-progress target (with its single-part seed geometry). `editVersionRef` carries the T34
  // optimistic-concurrency token, re-read from each auto-commit's response so consecutive edits of the same
  // feature don't self-conflict; `editingRef` lets the (stable) auto-commit callback read the live target.
  const [editing, setEditing] = useState<
    { layerId: string; id: string; geomType: 'line' | 'polygon'; geometry: DrawnGeometry } | null
  >(null)
  const editVersionRef = useRef<string>('')
  const editingRef = useRef(editing)
  editingRef.current = editing
  // Web-editor snapping (decision-web-editor-snapping.md): master toggle (default ON when Edit
  // mode starts) + per-layer checks (absent = checked) + density-paused hints. The engine itself
  // is framework-free (created lazily on first edit-mode entry); React owns only this UI state.
  const [snapOn, setSnapOn] = useState(true)
  const [snapChecked, setSnapChecked] = useState<Record<string, boolean>>({})
  const [snapPaused, setSnapPaused] = useState<Record<string, boolean>>({})
  const snapEngineRef = useRef<SnapEngine | null>(null)
  const [conflict, setConflict] = useState<ConflictState | null>(null)
  // The record whose detail drawer is open (opened from a popup's "Details" action).
  // The drawer needs the layer's identity, not just its id: the EFFECTIVE displayField names the
  // record (§7.1) and geometry+colour draw the header mark. /layers/{id}/columns carries none of
  // that — only /me/layers does — so it is captured here, from the caps the map already holds.
  const [detail, setDetail] = useState<{
    layerId: string
    id: string
    label: string
    displayField?: string | null
    geomType?: GeometryType | 'none' | null
    displayColor?: string | null
  } | null>(null)
  const [busy, setBusy] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const [mapError, setMapError] = useState<string | null>(null)
  // Per-EXTERNAL-layer "more features exist than were loaded" flag (the remote fetch hit the bbox
  // cap): drives the "zoom in to load more" notice. Keyed by layer id; only externals appear here —
  // hosted layers ride tiles and are never viewport-capped (decision-dynamic-mvt.md).
  const [viewCapped, setViewCapped] = useState<Record<string, boolean>>({})
  // Talus 35 — a brief "saved offline" toast after a write is queued (auto-clears).
  const [offlineFlash, setOfflineFlash] = useState(false)
  const offlineFlashTimer = useRef<number | null>(null)
  // Talus 35 — the persistent offline-context notice is dismissible; it re-shows on each new offline
  // episode (reset when connectivity returns, below).
  const [offlineNoticeDismissed, setOfflineNoticeDismissed] = useState(false)

  // Talus 20 — save & own. `savedMap` is the web map currently being edited (null = unsaved New
  // Map); the Save button creates when null, updates in place when set. `mapNotice` carries the
  // F5 "N layers unavailable" message after opening a map with layers the caller can't view.
  const [savedMap, setSavedMap] = useState<{ id: string; title: string; canManage: boolean } | null>(null)
  const [saveOpen, setSaveOpen] = useState(false)
  // Map bookmarks (saved named viewpoints) — live inside the definition, persisted by the existing Save.
  // `bookmarksDirty` tracks unsaved edits so the panel can nudge "save the map to keep your bookmarks".
  const [bookmarks, setBookmarks] = useState<WebMapBookmark[]>([])
  // Talus Field "Field-ready" (decision-talus-field-f2.md §4.4): a map property; crews see the map in the
  // field app only when on. Restored from the definition, persisted by the existing Save.
  const [fieldReady, setFieldReady] = useState(false)
  const [bookmarksOpen, setBookmarksOpen] = useState(false)
  // Embed legend (decision-map-embed.md F2) — collapsed by default; toggles a bare read-only
  // LayerPane (visibility only). Embed mode never renders the full dock this replaces.
  const [embedLegendOpen, setEmbedLegendOpen] = useState(false)
  // Live coordinate readout — written straight to the DOM on mousemove via
  // this ref, never through React state (a re-render per mouse pixel would be absurd). The zoom
  // and ratio-scale cells of the status strip (handoff-map-2a §3.4) follow the same idiom.
  const coordsElRef = useRef<HTMLSpanElement | null>(null)
  const zoomElRef = useRef<HTMLSpanElement | null>(null)
  const scaleElRef = useRef<HTMLSpanElement | null>(null)
  // The GeolocateControl instance — its own button is hidden on the main map (the Navigate
  // cluster's locate button calls trigger()), but the control stays mounted so the
  // user-location dot and permission flow remain MapLibre's.
  const geolocateCtlRef = useRef<maplibregl.GeolocateControl | null>(null)
  const [bookmarksDirty, setBookmarksDirty] = useState(false)
  // Map export (decision-map-export.md) — the camera tool below Bookmarks: a two-tab panel
  // (Screenshot PNG / Print PDF). Left-rail sibling of Bookmarks; available to everyone.
  const [exportOpen, setExportOpen] = useState(false)
  // Basemap popout (lower-right-untangle, 2026-07-23) — the switcher moved off the crowded
  // bottom-right corner into the left rail: a globe icon toggling a small popout of the options.
  // The embed keeps the original floating pill (it has no rail).
  const [basemapOpen, setBasemapOpen] = useState(false)
  // Sketch layer (decision-sketch-layer.md): map notes drawn via terra-draw, persisted inside the
  // definition (the bookmarks pattern). Toolbar state (active tool / color / next-note label) +
  // the on-map selection and reshape session. Refs mirror what the once-bound click handler and
  // the (stable) draw-finish callback need to read live.
  const [sketch, setSketch] = useState<SketchFeature[]>([])
  const sketchRef = useRef(sketch)
  sketchRef.current = sketch
  const [sketchOpen, setSketchOpen] = useState(false)
  const [sketchTool, setSketchTool] = useState<SketchTool | null>(null)
  const sketchToolRef = useRef(sketchTool)
  sketchToolRef.current = sketchTool
  const [sketchColor, setSketchColor] = useState(EXT_PALETTE[0])
  const sketchColorRef = useRef(sketchColor)
  sketchColorRef.current = sketchColor
  const [sketchLabel, setSketchLabel] = useState('')
  const sketchLabelRef = useRef(sketchLabel)
  sketchLabelRef.current = sketchLabel
  const [sketchVisible, setSketchVisible] = useState(true)
  const [selectedSketchId, setSelectedSketchId] = useState<string | null>(null)
  const [reshapingSketchId, setReshapingSketchId] = useState<string | null>(null)
  const reshapingSketchRef = useRef(reshapingSketchId)
  reshapingSketchRef.current = reshapingSketchId
  const [saving, setSaving] = useState(false)
  const [savedFlash, setSavedFlash] = useState(false)
  const { confirm, confirmDialog, confirmOpen } = useConfirm()
  const [mapNotice, setMapNotice] = useState<string | null>(null)
  // Which webMapId we've already restored, so the restore effect runs once per id (and so a
  // save-then-navigate doesn't re-restore the map we just saved).
  const restoredRef = useRef<string | null>(null)
  // Draft autosave (G4.2): a draft the user chose to Restore, consumed by the load effect's next run in
  // place of the server definition; the nonce re-runs that effect; the offer drives the Restore/Discard bar.
  const pendingDraftRef = useRef<WebMapDefinition | null>(null)
  const [restoreNonce, setRestoreNonce] = useState(0)
  // The offer carries the draft itself: Restore must not depend on storage still holding it.
  const [draftOffer, setDraftOffer] = useState<{ savedAt: string; definition: WebMapDefinition } | null>(null)
  // switchBasemap is a plain closure (reads `basemap`); ref it so the restore effect can apply a
  // saved basemap without taking switchBasemapRef as an unstable dep.
  const switchBasemapRef = useRef<(b: Basemap) => void>(() => {})
  // Same idiom for the two canvas-level helpers the once-bound 'load' handler and the F3 re-probe
  // effect need: both are declared far below and must not become deps of anything up here.
  const paintBasemapRef = useRef<(map: maplibregl.Map, b: Basemap) => void>(() => {})
  const rebuildBasemapSourceRef = useRef<(map: maplibregl.Map) => void>(() => {})
  // A saved-map restore that resolves BEFORE the map is built (it races runtime basemap discovery)
  // parks its basemap+viewport apply here; the build effect's load handler runs it. Without this the
  // apply was dropped on a null mapRef and the saved basemap never took (B5b 2026-08-12).
  const pendingApplyViewRef = useRef<(() => void) | null>(null)

  // Mirror state into refs for the long-lived (once-bound) map handlers.
  const editModeRef = useRef(editMode)
  editModeRef.current = editMode
  const basemapRef = useRef(basemap)
  basemapRef.current = basemap
  // Unsaved-work guard (AGO parity): the signature a Save would write, captured at
  // load/reset/save; null until the first capture so a closing tab mid-load never warns. The live
  // dirty check is re-bound every render via dirtyCheckRef (below, after mapEditable) so the
  // once-bound beforeunload listener always reads current state.
  const baselineSigRef = useRef<string | null>(null)
  const dirtyCheckRef = useRef<() => boolean>(() => false)
  const createLayerIdRef = useRef(createLayerId)
  createLayerIdRef.current = createLayerId
  const drawLayerIdRef = useRef(drawLayerId)
  drawLayerIdRef.current = drawLayerId
  // Raster export box-draw owns its clicks (drag a clip box); mirror it into a ref so the map-level
  // identify handler doesn't open a popup mid-draw.
  const drawingExportRef = useRef(false)
  drawingExportRef.current = drawingExport !== null
  const busyClickRef = useRef(false)
  busyClickRef.current = form !== null || moving !== null || editing !== null
  const handlersRef = useRef<{
    onIdentify: (items: IdentifyItem[], coords: [number, number]) => void
    onCreateAt: (layerId: string, lon: number, lat: number) => void
    onWhatsHere: (lng: number, lat: number) => void
    onSketchClick: (id: string) => void
    onShapePick: (pt: { x: number; y: number }) => void
  }>({
    onIdentify: () => {},
    onCreateAt: () => {},
    onWhatsHere: () => {},
    onSketchClick: () => {},
    onShapePick: () => {},
  })

  // Line/polygon slice c6a — terra-draw create. When a line/polygon layer is the active draw target, the hook
  // runs terra-draw in the matching mode; on finish it hands back the drawn geometry (WGS-84), and we open the
  // attribute form (createGeom: attributes only, no coordinate entry) which attaches the geometry on submit.
  const onDrawFinish = useCallback((geometry: DrawnGeometry) => {
    const lid = drawLayerIdRef.current
    if (lid) setForm({ kind: 'createGeom', layerId: lid, geometry })
    setDrawLayerId(null) // leave draw mode; the form takes over
  }, [])
  // The engine's synchronous snap query, handed to both hosted terra-draw surfaces (an inactive
  // or empty-set engine returns undefined = no snap). Stable — the hooks ref-read it.
  const snapQuery = useCallback(
    (lng: number, lat: number) => snapEngineRef.current?.snap(lng, lat),
    [],
  )
  useFeatureDraw({
    map: mapRef.current,
    active: drawLayerId !== null && drawTool !== null,
    tool: drawLayerId !== null ? drawTool : null,
    onFinish: onDrawFinish,
    snapTo: snapQuery,
  })
  // Esc = cancel + disarm (decision-editing-toolbar.md §3.1): one Esc discards any in-progress
  // gesture (terra-draw teardown drops the scratch) and returns the map fully to idle.
  useEffect(() => {
    if (!editMode || confirmOpen) return // a confirm owns Esc while it is up (useConfirm)
    const onKey = (e: KeyboardEvent): void => {
      if (e.key !== 'Escape') return
      if (createLayerIdRef.current || drawLayerIdRef.current) {
        setCreateLayerId(null)
        setDrawLayerId(null)
      }
      if (shapePickRef.current) {
        setShapePick(false)
        exitEditShape() // ends the seeded session too (auto-commits already landed per gesture)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [editMode, confirmOpen])
  // Line/polygon slice c6b — terra-draw select edit. Active while a feature is being reshaped; each completed
  // edit gesture auto-commits the new geometry (see onEditShape). Inactive (editing===null) tears terra-draw down.
  useFeatureEdit({
    map: mapRef.current,
    active: editing !== null,
    geomType: editing?.geomType ?? null,
    geometry: editing?.geometry ?? null,
    onEdit: onEditShape,
    snapTo: snapQuery,
  })
  // The vertex-edited feature's own stored vertices would make the drag sticky — exclude it.
  useEffect(() => {
    snapEngineRef.current?.setExcluded(editing?.id ?? null)
  }, [editing])

  // ── Sketch layer (decision-sketch-layer.md): a SECOND terra-draw pair for map notes ──────────
  // Draw: the tool STAYS active after each finish (AGO notes style — draw several, then toggle
  // off/close). Color/label ride refs so the stable callback reads the live picks.
  const onSketchDrawFinish = useCallback((geometry: DrawnGeometry) => {
    const label = sketchLabelRef.current.trim()
    setSketch((prev) => [
      ...prev,
      {
        id: newSketchId(),
        ...geometryToSketch(geometry),
        color: sketchColorRef.current,
        ...(label ? { label } : {}),
      },
    ])
  }, [])
  useFeatureDraw({
    map: mapRef.current,
    active: sketchTool !== null,
    tool: sketchTool,
    onFinish: onSketchDrawFinish,
  })
  // Reshape: the existing select-mode editor over the note's geometry (point = whole-marker drag).
  // Each completed gesture writes straight back into sketch state. The seed geometry is captured
  // ONCE per session (memo on the id) — terra-draw keeps its own live copy between gestures, and a
  // per-render object identity would tear the session down every render.
  const reshapingSketch = reshapingSketchId
    ? (sketch.find((f) => f.id === reshapingSketchId) ?? null)
    : null
  const reshapeSeed = useMemo(
    () => (reshapingSketch ? sketchGeometry(reshapingSketch) : null),
    // Deliberately keyed on the ID, not the feature: the seed is captured ONCE per session
    // (terra-draw keeps its own live copy between gestures; reseeding after our own onEdit
    // write-back would tear the session down mid-drag).
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [reshapingSketchId],
  )
  const onSketchReshape = useCallback((geometry: DrawnGeometry) => {
    const id = reshapingSketchRef.current
    if (!id) return
    setSketch((prev) => prev.map((f) => (f.id === id ? { ...f, ...geometryToSketch(geometry) } : f)))
  }, [])
  useFeatureEdit({
    map: mapRef.current,
    active: reshapingSketch !== null,
    geomType: reshapingSketch?.geometryType ?? null,
    geometry: reshapeSeed,
    onEdit: onSketchReshape,
  })

  // Sketch render: one direct GeoJSON source + a fixed layer family (the identify-highlight
  // pattern — no LayerDef/registration). Re-runs on basemap switch to re-lift the layers above the
  // fresh basemap stack; hides the note being reshaped (terra-draw shows its editable copy).
  useEffect(() => {
    const map = mapRef.current
    if (!map) return
    const apply = (): void => {
      ensureSketchLayers(map)
      setSketchData(map, sketch, reshapingSketchId)
      setSketchVisibility(map, sketchVisible)
    }
    // isStyleLoaded() is transiently false during ANY style mutation, and 'load' only fires once
    // at map birth — so the not-loaded fallback must wait on 'idle' (which always fires again),
    // never 'load' (a queue that never drains post-birth).
    if (map.isStyleLoaded()) apply()
    else map.once('idle', apply)
    return () => {
      map.off('idle', apply)
    }
  }, [sketch, sketchVisible, reshapingSketchId, basemap, capsReady])

  // Ref-mirror of the validity-aware accessToken (expired session ⇒ undefined, so anon paths and
  // tile auth behave identically for a lapsed session and a true visitor). Runs before the map-init
  // effect on the first committed render, so tile requests never race a stale seed.
  useEffect(() => {
    tokenRef.current = accessToken
  }, [accessToken])

  // Load picklists once the user can edit OR manage at least one layer (cached). Managers


  function bumpTiles(layerId: string): void {
    refreshRef.current[layerId] = (refreshRef.current[layerId] ?? 0) + 1
    const source = mapRef.current?.getSource(srcId(layerId))
    if (source && source.type === 'vector') {
      // hostedTileUrl keeps the layer on its CURRENT path — an edited-while-filtered layer must
      // refresh its dynamic tiles, not fall back to the static URL.
      ;(source as maplibregl.VectorTileSource).setTiles([hostedTileUrl(layerId)])
    }
    // Every commit path funnels through here — the snap index must reflect the commit so a
    // just-created structure is snappable for the very next pipe run.
    snapEngineRef.current?.invalidate(layerId)
  }

  function closePopup(): void {
    popupRef.current?.remove() // fires 'close' -> unmounts the React root
  }

  // "What's here?" coordinate-readout popup (E2, decision-whats-here.md) — its own refs so it never collides
  // with the feature popup. Opened from the map's right-click context menu.
  const whatsHerePopupRef = useRef<maplibregl.Popup | null>(null)
  const whatsHereRootRef = useRef<Root | null>(null)
  // The "Search in" geocoder picker's current selection (null = All), reported up by GeocodeSearch — so
  // "What's here?" reverse-geocodes via the SAME locator (decision-reverse-geocode.md).
  const geocodeScopeRef = useRef<string | null>(null)
  function closeWhatsHere(): void {
    whatsHerePopupRef.current?.remove()
  }
  function openWhatsHere(lng: number, lat: number): void {
    const map = mapRef.current
    if (!map) return
    closeWhatsHere()
    const node = document.createElement('div')
    const root = createRoot(node)
    whatsHereRootRef.current = root
    const popup = new maplibregl.Popup({ closeOnClick: false, maxWidth: 'none', className: 'whats-here-popup' })
      .setLngLat([lng, lat])
      .setDOMContent(node)
      .addTo(map)
    whatsHerePopupRef.current = popup
    popup.on('close', () => {
      root.unmount()
      whatsHereRootRef.current = null
      whatsHerePopupRef.current = null
    })
    root.render(
      <WhatsHerePopup
        lng={lng}
        lat={lat}
        token={tokenRef.current}
        locator={geocodeScopeRef.current}
        onClose={closeWhatsHere}
      />,
    )
  }

  // Owner/admin saved (or reset) a layer's default renderer in the Styles panel. Update
  // caps so configs + the legend reflect it, and repaint the live map layer in place with a
  // new paint expression. A slot's static tiles carry only geom+id, so when the saved
  // renderer starts or stops reading a field, the SAME vector source is re-pointed at the
  // tile URL that carries it (dynamic ↔ static) — no teardown, no restack; MapLibre refetches
  // the viewport's tiles.
  function handleRendererSaved(layerId: string, renderer: Renderer | null): void {
    const map = mapRef.current
    const urlBefore = map ? hostedTileUrl(layerId) : null
    setCaps((prev) => (prev && prev[layerId] ? { ...prev, [layerId]: { ...prev[layerId], renderer } } : prev))
    // configsRef is rebuilt from caps on the next render; the tile-URL helpers read it NOW.
    configsRef.current = configsRef.current.map((c) => (c.id === layerId ? { ...c, renderer } : c))
    const def = layersRef.current.find((d) => d.id === layerId)
    if (map && def && !externalsRef.current[layerId]) {
      const urlAfter = hostedTileUrl(layerId)
      const src = map.getSource(srcId(layerId))
      if (urlAfter !== urlBefore && src && src.type === 'vector') {
        ;(src as maplibregl.VectorTileSource).setTiles([urlAfter])
      }
    }
    if (map && def) {
      const color = resolveColor(renderer, def.color)
      // c7 — repaint the layer-wide geometry style too (width/dash/fill-opacity/outline), not just colour.
      // A null dasharray clears any prior dash back to solid.
      const dash = lineDashOf(renderer) ?? null
      if (def.isView) {
        // Talus 38 — a view carries all three render layers (one per geometry class); repaint each
        // that exists (the circle id is lyrId, plus -line/-fill).
        if (map.getLayer(lyrId(layerId))) {
          map.setPaintProperty(lyrId(layerId), 'circle-color', color)
          map.setPaintProperty(lyrId(layerId), 'circle-radius', pointRadiusOf(renderer))
        }
        const line = `${lyrId(layerId)}-line`
        if (map.getLayer(line)) {
          map.setPaintProperty(line, 'line-color', outlineColorOf(renderer, color))
          map.setPaintProperty(line, 'line-width', lineWidthOf(renderer, outlineWidthOf(renderer, 1.5)))
          map.setPaintProperty(line, 'line-dasharray', dash)
        }
        const fill = `${lyrId(layerId)}-fill`
        if (map.getLayer(fill)) {
          map.setPaintProperty(fill, 'fill-color', color)
          map.setPaintProperty(fill, 'fill-opacity', fillOpacityOf(renderer, def.fillOpacity ?? 0.15))
        }
      } else if (def.geometryType === 'polygon') {
        if (map.getLayer(lyrId(layerId))) {
          map.setPaintProperty(lyrId(layerId), 'fill-color', color)
          map.setPaintProperty(lyrId(layerId), 'fill-opacity', fillOpacityOf(renderer, def.fillOpacity ?? 0.12))
        }
        const line = `${lyrId(layerId)}-line`
        if (map.getLayer(line)) {
          map.setPaintProperty(line, 'line-color', outlineColorOf(renderer, color))
          map.setPaintProperty(line, 'line-width', outlineWidthOf(renderer, 2))
        }
      } else if (def.geometryType === 'line') {
        if (map.getLayer(lyrId(layerId))) {
          map.setPaintProperty(lyrId(layerId), 'line-color', color)
          map.setPaintProperty(lyrId(layerId), 'line-width', lineWidthOf(renderer))
          map.setPaintProperty(lyrId(layerId), 'line-dasharray', dash)
        }
      } else if (map.getLayer(lyrId(layerId))) {
        map.setPaintProperty(lyrId(layerId), 'circle-color', color)
        map.setPaintProperty(lyrId(layerId), 'circle-radius', pointRadiusOf(renderer))
      }
      // The icon pair is re-pointed in place for EVERY geometry (a view carries a circle too), which
      // is why an icon change no longer tears the layer down.
      applyPointIconLayout(map, layerId, renderer, def.color)
    }
    // The docked inspector stays on the layer after a save (handoff-map-2a §1) — the float it
    // replaced used to close here.
  }

  // Move the active-feature halo to `geometry` (or clear it). Called as the pager changes pages and on
  // popup close. Kept above the feature layers (restack/adds may have inserted layers over it).
  function setHighlight(geometry: GeoJSON.Geometry | null): void {
    const map = mapRef.current
    // The removed-map guard matters during TEARDOWN: map.remove() fires every popup's 'close', whose
    // handler calls setHighlight(null) — touching the dead style crashed the route boundary (and a
    // boundary remount silently discards an unsaved map). MapLibre flags removal on the handle.
    if (!map || (map as unknown as { _removed?: boolean })._removed) return
    ensureHighlightLayers(map)
    const src = map.getSource(HL_SRC)
    if (!src || src.type !== 'geojson') return
    ;(src as maplibregl.GeoJSONSource).setData(
      geometry ? { type: 'Feature', geometry, properties: {} } : { type: 'FeatureCollection', features: [] },
    )
    for (const id of HL_LAYERS) if (map.getLayer(id)) map.moveLayer(id)
  }

  // Build the render model for the hit at `index` — the per-feature logic the single-feature popup used to
  // do inline. Read-only layers (Talus 38 views) render
  // straight from the click's tile props (no fetch, no edit affordances). A polygon SLOT (V055) is NOT
  // read-only here: it has a real /features CRUD route, so it falls through and fetches, rendering its props
  // read-only (geometryType !== 'point' keeps canEdit false), exactly like a line slot. Editable layers
  // fetch full props and get edit/move/delete/details. `coords` is the popup anchor (click point), for Move.
  // Rejects with a user-facing message on fetch failure; the pager renders it in the error slot.
  // Slot owner-defined field defs for popup resolution — lazily fetched once per layer (promise-cached;
  // concurrent popup pages share the in-flight fetch). Null on failure: popupConfig degrades to raw keys.
  // Per-layer has-attachments probe (§3.1), mirroring ensureSlotFieldDefs: the promise is cached so every
  // popup page for the layer this session shares one EXISTS. Token-optional (anon-when-public); any failure
  // (incl. anon-on-private 404) resolves false → the strip renders nothing.
  function ensureHasAttachments(layerId: string): Promise<boolean> {
    const cached = hasAttachRef.current[layerId]
    if (cached) return cached
    const p = fetchHasAttachments(layerId, tokenRef.current).catch(() => false)
    hasAttachRef.current[layerId] = p
    return p
  }

  function ensureSlotFieldDefs(layerId: string): Promise<SlotFieldDefs | null> {
    const cached = slotDefsRef.current[layerId]
    if (cached) return cached
    const token = tokenRef.current
    // Signed out: the field-definitions door needs a sign-in, but the public columns door lists a
    // public layer's active owner fields with their labels, in the owner's order. Kept in its own
    // cache so a session that signs in (or renews) afterwards still fetches the full definitions.
    if (!token) {
      const anonCached = anonSlotDefsRef.current[layerId]
      if (anonCached) return anonCached
      const a = fetchLayerColumns(layerId)
        .then((res): SlotFieldDefs => ({ fields: fieldDefsFromColumns(res), domainValues: {} }))
        .catch(() => null)
      anonSlotDefsRef.current[layerId] = a
      return a
    }
    const p = (async (): Promise<SlotFieldDefs | null> => {
      try {
        const res = await listCustomFields(layerId, token)
        const domainIds = [...new Set(res.fields.map((f) => f.domainId).filter((d): d is string => !!d))]
        const domainValues: SlotFieldDefs['domainValues'] = {}
        await Promise.all(
          domainIds.map(async (id) => {
            try {
              domainValues[id] = (await getCustomDomain(id, token)).values
            } catch {
              /* raw codes render for this domain's fields */
            }
          }),
        )
        return { fields: res.fields, domainValues }
      } catch {
        return null
      }
    })()
    slotDefsRef.current[layerId] = p
    return p
  }

  async function loadFeaturePage(hit: FeatureHit, coords: [number, number]): Promise<FeaturePage> {
    const config = byId(hit.defId)
    if (!config) throw new Error('Error loading feature')
    const noop = (): void => {}
    // Popup config resolution (decision-map-popups.md): the map's per-layer config + (for slots) the
    // owner-defined field defs turn fetched props into the page's title/eyebrow/rows here, so the popup
    // components stay data-only. Slots need the defs for BOTH the auto-default and configured rows.
    const popupCfg = popupsRef.current[hit.defId]
    const slotDefs = isSlotId(hit.defId) ? await ensureSlotFieldDefs(hit.defId) : null
    if (config.isView || (config.geometryType === 'polygon' && !isSlotId(config.id))) {
      return {
        kind: 'feature', config, props: hit.props,
        ...popupTitleFor(config, hit.props, popupCfg, slotDefs),
        rows: popupRowsFor(config, hit.props, popupCfg, slotDefs),
        canEdit: false, canMove: false, canDelete: false, onEdit: noop, onMove: noop, onDelete: noop,
      }
    }
    const token = tokenRef.current
    if (!token) {
      // A signed-out viewer of a public map (decision-public-access.md F2, rider 2026-11-06). The
      // single-feature door is signed-in only, so this used to stop here and every popup on an
      // ordinary layer read "Error loading feature". The feature is read through the list door
      // instead, which already answers an anonymous caller for a publicly shared layer and nothing
      // else. Read-only page: no edit actions, no Details drawer and no sheet icon (each needs a
      // sign-in); the attachments strip stays, its fetches being token-optional.
      if (!isSlotId(hit.defId)) throw new Error('Error loading feature')
      let anonProps: unknown
      try {
        anonProps = await fetchPublicSlotFeature(hit.defId, hit.featureId)
      } catch (err) {
        throw new Error(err instanceof ApiError ? `Error (HTTP ${err.status})` : 'Error loading feature', { cause: err })
      }
      return {
        kind: 'feature', config, props: anonProps,
        ...popupTitleFor(config, anonProps, popupCfg, slotDefs),
        rows: popupRowsFor(config, anonProps, popupCfg, slotDefs),
        canEdit: false, canMove: false, canDelete: false, onEdit: noop, onMove: noop, onDelete: noop,
        attachments: { layerId: hit.defId, featureId: hit.featureId, probe: ensureHasAttachments(hit.defId) },
      }
    }
    let props: unknown
    try {
      props = await config.api.fetchOne(hit.featureId, token)
    } catch (err) {
      throw new Error(err instanceof ApiError ? `Error (HTTP ${err.status})` : 'Error loading feature', { cause: err })
    }
    // Attribute-edit + delete apply to ALL editable feature layers (point/line/polygon) in edit mode (c6a).
    // MOVE is point-only — line/polygon geometry editing is the terra-draw select tool (c6b) — so the popup
    // hides Move for non-point while still offering Edit/Delete.
    // V109 (decision-layer-edit-capabilities.md): each affordance follows its own flag — Attributes ⇔
    // attributes, Move / Edit shape ⇔ geometry, Delete ⇔ delete. The doors decide; this only hides.
    const inEdit = config.canEdit && editModeRef.current
    const ops = config.editOps
    const canEdit = inEdit && ops.attributes
    const canMove = inEdit && ops.geometry && config.geometryType === 'point'
    const canDelete = inEdit && ops.delete
    return {
      kind: 'feature',
      config,
      props,
      ...popupTitleFor(config, props, popupCfg, slotDefs),
      rows: popupRowsFor(config, props, popupCfg, slotDefs),
      canEdit,
      canMove,
      canDelete,
      onEdit: () => {
        closePopup()
        setForm({ kind: 'edit', layerId: hit.defId, id: hit.featureId, props })
      },
      onMove: () => beginMove(hit.defId, hit.featureId, coords, versionOf(props)),
      onDelete: () => void doDelete(hit.defId, hit.featureId, versionOf(props)),
      onEditShape:
        inEdit && ops.geometry &&
        (config.geometryType === 'line' || config.geometryType === 'polygon') && !config.nativeStorage
          ? () => void beginEditShape(hit.defId, hit.featureId)
          : undefined,
      onDetails: () => {
        closePopup()
        setDetail({
          layerId: hit.defId,
          id: hit.featureId,
          label: config.label,
          displayField: config.popupTitleField,
          geomType: config.geometryType,
          displayColor: config.color,
        })
      },
      // F11 (decision-scheduled-reports.md): the Generate-sheet icon beside Details. The popup root has no
      // providers, so the token and navigation are handed in; the seam renders nothing unless a per-feature
      // report the caller can view sheets this layer (and nothing at all in the public build). The capture
      // closure is the SAME one the drawer gets — the popup is a DOM overlay, so the GL capture excludes it.
      titleActions: (
        <PopupSheetAction
          layerId={hit.defId}
          objectId={objectIdOf(props)}
          captureMap={() => (mapRef.current ? captureMapPng(mapRef.current) : Promise.resolve(null))}
          token={token}
          onOpenReport={(id) => {
            closePopup()
            navigate(`/report/${id}`)
          }}
        />
      ),
      // A-4: the lazy attachments strip. probe is the per-layer promise (cached) — the strip awaits it and
      // renders nothing on attachment-less layers; onDetails opens the same drawer for view-all / manage.
      attachments: {
        layerId: hit.defId,
        featureId: hit.featureId,
        token,
        probe: ensureHasAttachments(hit.defId),
        onDetails: () => {
          closePopup()
          setDetail({
          layerId: hit.defId,
          id: hit.featureId,
          label: config.label,
          displayField: config.popupTitleField,
          geomType: config.geometryType,
          displayColor: config.color,
        })
        },
      },
    }
  }

  // Resolve the page at `index` in the identify set — dispatch on kind. A raster page samples its value
  // lazily from GET /rasters/{id}/point (only fetched when paged to); a feature page defers to above.
  async function loadIdentifyPage(
    items: IdentifyItem[],
    index: number,
    coords: [number, number],
  ): Promise<FeaturePage> {
    const item = items[index]
    if (item.kind === 'raster') {
      const token = tokenRef.current
      if (!token) throw new Error('Error loading value')
      try {
        const r = await identifyRasterPoint(item.rasterId, coords[0], coords[1], token)
        return { kind: 'raster', title: item.title, values: r.values, outOfBounds: r.outOfBounds }
      } catch (err) {
        throw new Error(err instanceof ApiError ? `Error (HTTP ${err.status})` : 'Error loading value', { cause: err })
      }
    }
    return loadFeaturePage(item.hit, coords)
  }

  // Open the paged identify popup for a set of hits (topmost first). Mounts FeaturePopup into the same
  // MapLibre-popup + React-root machinery the single-feature popup used; the pager walks the set, each page
  // rendered by AssetPopup. The active page is highlighted on the map; the popup anchors at the click point.
  function openIdentifyPopup(rawItems: IdentifyItem[], coords: [number, number]): void {
    const map = mapRef.current
    // Popup on/off (decision-map-popups.md §4.3): a layer whose map config disables popups is skipped by
    // identify entirely — no pager page. One choke point for both the single- and multi-feature cases.
    const items = rawItems.filter(
      (it) => it.kind !== 'feature' || popupsRef.current[it.hit.defId]?.enabled !== false,
    )
    if (!map || items.length === 0) return
    closePopup()

    const node = document.createElement('div')
    const root = createRoot(node)
    popupRootRef.current = root
    // maxWidth 'none': width is CSS-driven (A-4 §3.5 auto-width) — global.css min/max-width on
    // .feature-popup .maplibregl-popup-content; the popup grows to content up to the max, no wider.
    const popup = new maplibregl.Popup({ maxWidth: 'none', className: 'feature-popup' })
      .setLngLat(coords)
      .setDOMContent(node)
      .addTo(map)
    popupRef.current = popup
    // Re-anchor whenever the content SIZES: MapLibre picks the anchor from the popup's dimensions,
    // but React renders (and each pager page fetches) after the popup measured an empty node — so a
    // tall popup near the frame edge ran off-screen. setLngLat(same) re-runs the auto-anchor with
    // the real height, flipping the popup to the side of the point that fits.
    const reanchor = new ResizeObserver(() => {
      if (popupRef.current === popup) popup.setLngLat(popup.getLngLat())
    })
    reanchor.observe(node)
    // Drag-by-header (A-4 §3.5): FeaturePopup's header reports pointer deltas; they accumulate into
    // popup.setOffset so the popup stays feature-anchored through pans. Offsets are clamped so the
    // popup can't strand outside the map container. On first drag the user takes control of position:
    // stop the resize re-anchor AND pin the current auto-anchor — a later anchor flip (recomputed on
    // every pan) would translate the popup by its own size, a visible jump. A new click builds a new
    // popup, so offset/anchor/re-anchor all reset for free.
    let dragOffset: [number, number] = [0, 0]
    const drag = {
      onStart: () => {
        reanchor.disconnect()
        const m = /maplibregl-popup-anchor-([a-z-]+)/.exec(popup.getElement().className)
        if (m) popup.options.anchor = m[1] as PositionAnchor
      },
      onMove: (dx: number, dy: number) => {
        const rect = popup.getElement().getBoundingClientRect()
        const box = map.getContainer().getBoundingClientRect()
        const margin = 8
        // Clamp each step so the drag can't push the popup out of the container. [lo, hi] is the
        // step range that keeps the rect inside; a popup already outside (opened tall near an edge,
        // or bigger than the map) may only move inward — never snapped in, never further out.
        const clamp = (step: number, lo: number, hi: number) => {
          if (lo > 0) return Math.min(Math.max(step, 0), Math.max(hi, 0))
          if (hi < 0) return Math.max(Math.min(step, 0), Math.min(lo, 0))
          return Math.min(Math.max(step, lo), hi)
        }
        const sx = clamp(dx, box.left + margin - rect.left, box.right - margin - rect.right)
        const sy = clamp(dy, box.top + margin - rect.top, box.bottom - margin - rect.bottom)
        dragOffset = [dragOffset[0] + sx, dragOffset[1] + sy]
        popup.setOffset(dragOffset)
      },
    }
    popup.on('close', () => {
      reanchor.disconnect()
      root.unmount()
      popupRootRef.current = null
      popupRef.current = null
      popupLayerRef.current = null
      setHighlight(null)
    })

    root.render(
      <FeaturePopup
        count={items.length}
        drag={drag}
        loadPage={(i) => loadIdentifyPage(items, i, coords)}
        onActive={(i) => {
          const item = items[i]
          // Track the active page's vector layer so removing/refreshing it still closes the popup; a raster
          // page has no such layer. Highlight the feature geometry, or a point marker at the raster sample.
          popupLayerRef.current = item?.kind === 'feature' ? item.hit.defId : null
          const geometry: GeoJSON.Geometry =
            item?.kind === 'feature' ? item.hit.geometry : { type: 'Point', coordinates: coords }
          setHighlight(item ? geometry : null)
        }}
      />,
    )
  }

  // E3 (decision-coord-entry.md): resolve a slot's create/move geometry to the backend's CRS shape. A NATIVE
  // layer stores {x,y} in its own CRS — reproject the 4326 click/drag point (approximate, fine for a
  // click/drag; exact survey values are TYPED in the form and bypass this). Everything else stays {lon,lat}.
  async function slotGeomPayload(
    config: LayerConfig,
    lon: number,
    lat: number,
  ): Promise<Record<string, number>> {
    const token = tokenRef.current
    if (config.nativeStorage && config.workingSrid && config.workingSrid !== 4326 && token) {
      const t = await transformPoint(lon, lat, config.workingSrid, token)
      return { x: t.x, y: t.y }
    }
    return { lon, lat }
  }

  // Upload each staged file to the (now-saved) feature; return the ones that failed so the caller can
  // keep the form open and let the user retry. decision-attachments-in-edit-form.md §3.3.
  async function uploadStaged(layerId: string, featureId: string, files: File[]): Promise<File[]> {
    const tok = accessToken
    if (!tok) return files // not signed in → every file counts as failed (can't upload)
    const failed: File[] = []
    for (const f of files) {
      try {
        await uploadFeatureAttachment(layerId, featureId, f, tok)
      } catch {
        failed.push(f)
      }
    }
    return failed
  }

  async function submitForm(attrs: unknown, attachments?: File[]): Promise<void> {
    if (!form) return
    const config = byId(form.layerId)
    if (!config) return
    setBusy(true)
    setFormError(null)
    try {
      // The feature write comes first; the id it yields is what the staged attachments attach to.
      let featureId: string | null = null
      if (form.kind === 'create' || form.kind === 'createGeom') {
        const validationError = config.validateCreate(attrs)
        if (validationError) {
          setFormError(validationError)
          setBusy(false)
          return
        }
        // createGeom (line/polygon slice c6a): the geometry was drawn with terra-draw (WGS-84) and the form
        // collected attributes only — attach the geometry to the attribute payload. Otherwise (point):
        // E3 — a slot's SlotForm emits backend-ready geometry in the working CRS already ({x,y} native / {lon,lat}),
        // so use attrs directly.
        const payload =
          form.kind === 'createGeom'
            ? { geometry: form.geometry, ...(attrs as Record<string, unknown>) }
            : config.workingSrid !== undefined
              ? (attrs as Record<string, unknown>)
              : config.buildCreate(form.lon, form.lat, attrs)
        const r = await submitWrite({
          kind: 'create',
          layerId: form.layerId,
          payload,
        })
        if (r.status === 'ok') {
          bumpTiles(config.id) // new dot becomes authoritative from the re-fetched tile
          const result = r.result as { id?: unknown } | undefined
          featureId = typeof result?.id === 'string' ? result.id : null
        } else if (r.status === 'offline') {
          flashOffline()
          return
        } else if (r.status === 'error') {
          setFormError(`${r.error.message} (HTTP ${r.error.status})`)
          return
        }
      } else {
        // Attribute edit. Since Talus 13 the tile carries the symbolizable attributes
        // that drive paint, so an edit CAN change a feature's color — bump the tile so the
        // re-fetched value repaints it. (Pre-13 this was a no-op: tiles held only id.) The
        // popup still reads fresh attributes via fetchOne regardless.
        const r = await submitWrite({
          kind: 'update',
          layerId: form.layerId,
          featureId: form.id,
          payload: attrs,
          baseVersion: versionOf(form.props),
        })
        if (r.status === 'ok') {
          bumpTiles(config.id)
          featureId = form.id
        } else if (r.status === 'offline') {
          flashOffline()
          return
        } else if (r.status === 'conflict') {
          // The record changed under the user — offer overwrite/discard, keep their edits (T34 inline panel).
          setConflict({ kind: 'edit', layerId: form.layerId, id: form.id, attrs, current: r.current ?? {} })
          return
        } else if (r.status === 'error') {
          setFormError(`${r.error.message} (HTTP ${r.error.status})`)
          return
        }
      }
      // Stage-and-upload-on-Save: the feature is written; now push any staged attachments to it. On a
      // partial failure keep the form open with an error so the user can retry (the feature stays saved).
      if (attachments?.length && featureId) {
        const failed = await uploadStaged(form.layerId, featureId, attachments)
        if (failed.length) {
          setFormError(
            `Saved. Couldn't upload: ${failed.map((f) => f.name).join(', ')} — check the file type/size and Save again.`,
          )
          return
        }
      }
      setForm(null)
    } finally {
      setBusy(false)
    }
  }

  function beginMove(layerId: string, id: string, coords: [number, number], version: string): void {
    const map = mapRef.current
    if (!map) return
    closePopup()
    moveCoordsRef.current = coords
    setMoving({ layerId, id, version }) // version captured at move-start, sent as If-Match on save (T34)

    const existing = map.getSource(EDIT_SOURCE)
    if (existing) {
      ;(existing as maplibregl.GeoJSONSource).setData(pointFC(coords))
    } else {
      map.addSource(EDIT_SOURCE, { type: 'geojson', data: pointFC(coords) })
      map.addLayer({
        id: EDIT_LAYER,
        type: 'circle',
        source: EDIT_SOURCE,
        paint: {
          'circle-radius': 9,
          'circle-color': '#2da7e8',
          'circle-stroke-color': '#ffffff',
          'circle-stroke-width': 2,
        },
      })
    }

    const down = (e: maplibregl.MapLayerMouseEvent) => {
      e.preventDefault() // suppress map pan during the drag
      map.getCanvas().style.cursor = 'grabbing'
      const onDrag = (ev: maplibregl.MapMouseEvent) => {
        const c: [number, number] = [ev.lngLat.lng, ev.lngLat.lat]
        moveCoordsRef.current = c
        ;(map.getSource(EDIT_SOURCE) as maplibregl.GeoJSONSource).setData(pointFC(c))
      }
      map.on('mousemove', onDrag)
      map.once('mouseup', () => {
        map.off('mousemove', onDrag)
        map.getCanvas().style.cursor = 'move'
      })
    }
    const enter = () => {
      map.getCanvas().style.cursor = 'move'
    }
    const leave = () => {
      map.getCanvas().style.cursor = ''
    }
    moveListenersRef.current = { down, enter, leave }
    map.on('mousedown', EDIT_LAYER, down)
    map.on('mouseenter', EDIT_LAYER, enter)
    map.on('mouseleave', EDIT_LAYER, leave)
  }

  function exitMove(): void {
    const map = mapRef.current
    if (map) {
      const listeners = moveListenersRef.current
      if (listeners) {
        map.off('mousedown', EDIT_LAYER, listeners.down)
        map.off('mouseenter', EDIT_LAYER, listeners.enter)
        map.off('mouseleave', EDIT_LAYER, listeners.leave)
      }
      if (map.getLayer(EDIT_LAYER)) map.removeLayer(EDIT_LAYER)
      if (map.getSource(EDIT_SOURCE)) map.removeSource(EDIT_SOURCE)
      map.getCanvas().style.cursor = ''
    }
    moveListenersRef.current = null
    moveCoordsRef.current = null
    setMoving(null)
  }

  async function saveMove(): Promise<void> {
    const coords = moveCoordsRef.current
    if (!moving || !coords) return
    const config = byId(moving.layerId)
    if (!config) return
    setBusy(true)
    setFormError(null)
    try {
      const { layerId, id } = moving
      // E3: a native slot stores {x,y} in its CRS — reproject the dragged 4326 point (default/static stay {lon,lat}).
      const r = await submitWrite({
        kind: 'move',
        layerId,
        featureId: id,
        payload: await slotGeomPayload(config, coords[0], coords[1]),
        baseVersion: moving.version,
      })
      if (r.status === 'ok') {
        exitMove()
        bumpTiles(layerId) // moved dot becomes authoritative from the re-fetched tile
      } else if (r.status === 'offline') {
        flashOffline() // stay in move mode so the user can retry once back online
      } else if (r.status === 'conflict') {
        exitMove() // hand off to the inline conflict panel
        setConflict({ kind: 'move', layerId, id, coords, current: r.current ?? {} })
      } else {
        setFormError(`${r.error.message} (HTTP ${r.error.status})`)
      }
    } finally {
      setBusy(false)
    }
  }

  // c6b — enter vertex/ring editing: fetch the feature's geometry (+ T34 version), unwrap to a single-part
  // seed, and hand off to terra-draw select via `editing`. Native-CRS and true multi-part shapes are refused.
  async function beginEditShape(layerId: string, id: string): Promise<void> {
    const token = tokenRef.current
    const config = byId(layerId)
    if (!token || !config) return
    const gt = config.geometryType
    if (gt !== 'line' && gt !== 'polygon') return
    // Native non-point is excluded from canvas editing (a WGS-84 drag isn't byte-exact in the layer's native
    // CRS) — mirrors the c6a draw exclusion; exact native values are typed, not dragged.
    if (config.nativeStorage) {
      setMapNotice('Shape editing isn’t available for native-CRS layers.')
      return
    }
    closePopup()
    setBusy(true)
    setFormError(null)
    try {
      const feature = await config.api.fetchOne(id, token)
      const seed = singlePartSeed((feature as { geometry?: unknown }).geometry, gt)
      if (seed === 'multipart') {
        setMapNotice('This is a multi-part shape — vertex editing isn’t supported yet.')
        return
      }
      if (!seed) {
        setMapNotice('Could not load this feature’s shape to edit.')
        return
      }
      editVersionRef.current = versionOf(feature)
      setEditing({ layerId, id, geomType: gt, geometry: seed })
    } catch (err) {
      setMapNotice(
        err instanceof ApiError ? `Could not load shape (HTTP ${err.status})` : 'Could not load shape',
      )
    } finally {
      setBusy(false)
    }
  }

  function exitEditShape(): void {
    setEditing(null)
    setFormError(null)
  }

  // c6b — a completed edit gesture: PATCH the reshaped geometry (kind 'move' routes to the same feature
  // update; the backend re-wraps with ST_Multi + guards the geom family). On success re-read the bumped
  // version so the next gesture chains cleanly; a 409 means a concurrent edit landed — back out + reload.
  function onEditShape(geometry: DrawnGeometry): void {
    const ed = editingRef.current
    if (!ed) return
    void (async () => {
      const r = await submitWrite({
        kind: 'move',
        layerId: ed.layerId,
        featureId: ed.id,
        payload: { geometry },
        baseVersion: editVersionRef.current,
      })
      if (r.status === 'ok') {
        editVersionRef.current = versionOf(r.result)
        bumpTiles(ed.layerId)
        setFormError(null)
      } else if (r.status === 'offline') {
        flashOffline()
      } else if (r.status === 'conflict') {
        exitEditShape()
        bumpTiles(ed.layerId)
        setMapNotice('This feature changed elsewhere — your last shape edit wasn’t saved. Reopen to edit.')
      } else {
        setFormError(`${r.error.message} (HTTP ${r.error.status})`)
      }
    })()
  }

  function doDelete(layerId: string, id: string, version: string): void {
    const config = byId(layerId)
    if (!config) return
    confirm({
      title: `Delete this ${config.noun}?`,
      // The delete carries this feature's If-Match version (T34), so a feature someone else changed
      // since the popup opened is refused and routed to the conflict panel rather than clobbered.
      consequence: `The ${config.noun} and its attachments are removed. This can’t be undone — and if someone else has edited it since you opened it, the delete is refused instead.`,
      confirmLabel: `Delete ${config.noun}`,
      onConfirm: async () => {
        const r = await submitWrite({ kind: 'delete', layerId, featureId: id, baseVersion: version })
        if (r.status === 'ok') {
          closePopup()
          bumpTiles(layerId) // dot disappears from the re-fetched tile
        } else if (r.status === 'offline') {
          flashOffline() // keep the popup so the user can retry once back online
        } else if (r.status === 'conflict') {
          closePopup() // hand off to the inline conflict panel
          setConflict({ kind: 'delete', layerId, id, current: r.current ?? {} })
        } else {
          setMapError(`Delete failed (HTTP ${r.error.status})`)
        }
      },
    })
  }

  // Resolve a 409 conflict (Talus 34). 'overwrite' re-applies the user's action using the
  // server's CURRENT version (a deliberate last-write); 'discard' abandons it and reloads the
  // latest. Either way the user chose — nothing was silently clobbered.
  async function resolveConflict(action: 'overwrite' | 'discard'): Promise<void> {
    if (!conflict) return
    const config = byId(conflict.layerId)
    const token = tokenRef.current
    if (action === 'discard') {
      if (conflict.kind === 'edit') {
        // reload the server's latest values into the still-open form
        setForm({ kind: 'edit', layerId: conflict.layerId, id: conflict.id, props: conflict.current })
      }
      setConflict(null)
      setFormError(null)
      bumpTiles(conflict.layerId) // ensure the map shows the latest
      return
    }
    // overwrite — re-apply using the server's current version
    const version = versionOf(conflict.current)
    if (!token || !config || !version) {
      setConflict(null)
      setMapError('Could not resolve the conflict — please reload and retry.')
      return
    }
    setBusy(true)
    try {
      if (conflict.kind === 'edit') {
        await config.api.update(conflict.id, conflict.attrs, token, version)
        setForm(null)
      } else if (conflict.kind === 'move') {
        await config.api.update(
          conflict.id,
          await slotGeomPayload(config, conflict.coords[0], conflict.coords[1]), // E3: native → {x,y}
          token,
          version,
        )
      } else {
        await config.api.remove(conflict.id, token, version)
      }
      setConflict(null)
      setFormError(null)
      bumpTiles(conflict.layerId)
    } catch (err) {
      if (err instanceof ConflictError && err.current) {
        // changed AGAIN between our refresh and this overwrite — re-present with the newest row
        setConflict({ ...conflict, current: err.current as Record<string, unknown> })
      } else {
        setConflict(null)
        setMapError(err instanceof ApiError ? `Save failed (HTTP ${err.status})` : 'Save failed')
      }
    } finally {
      setBusy(false)
    }
  }

  // Talus 35 — a brief "can't edit offline" toast when a write is attempted while disconnected.
  function flashOffline(): void {
    setOfflineFlash(true)
    if (offlineFlashTimer.current) window.clearTimeout(offlineFlashTimer.current)
    offlineFlashTimer.current = window.setTimeout(() => setOfflineFlash(false), 3500)
  }

  // Talus 35 — once we're back online, re-arm the dismissible offline notice for the next episode.
  useEffect(() => {
    if (online) setOfflineNoticeDismissed(false)
  }, [online])

  // Latest closures for the once-bound map click handlers.
  handlersRef.current.onIdentify = (items, coords) => openIdentifyPopup(items, coords)
  handlersRef.current.onCreateAt = (layerId, lon, lat) =>
    setForm({ kind: 'create', layerId, lon, lat })
  handlersRef.current.onWhatsHere = (lng, lat) => openWhatsHere(lng, lat)
  // F4 vertices pick: resolve the topmost target-layer feature at the click (the Select tool's
  // hit pattern) and seed — or re-seed — the reshape session on it. Empty space keeps the pick armed.
  handlersRef.current.onShapePick = (pt) => {
    const map = mapRef.current
    const target = editTargetRef.current
    if (!map || !target) return
    const box: [maplibregl.PointLike, maplibregl.PointLike] = [
      [pt.x - 4, pt.y - 4],
      [pt.x + 4, pt.y + 4],
    ]
    const hit = map
      .queryRenderedFeatures(box, { layers: selectRenderIds(map, target) })
      .find((f) => typeof f.properties?.id === 'string')
    if (!hit) return
    const id = hit.properties!.id as string
    if (editingRef.current?.layerId === target && editingRef.current.id === id) return
    void beginEditShape(target, id)
  }
  // Clicking a sketch note (tools idle) opens its edit card — the note's "popup".
  handlersRef.current.onSketchClick = (id) => {
    setReshapingSketchId(null)
    setSelectedSketchId(id)
  }

  // (Re)load an EXTERNAL layer's GeoJSON source for the current viewport (decision-add-from-url.md):
  // features come from the REMOTE server (browser-direct; no Talus token involved) — arcgis-feature =
  // paged bbox-scoped queries; geojson = the whole document, fetched once per map load (cached).
  // Bbox-scoped + capped at VIEW_FEATURE_CAP; `capped` flips the "zoom in to load more" flag.
  // Externals are the LAST GeoJSON-rendered layer kind: hosted views / filtered / labeled layers all
  // ride dynamic MVT now (decision-dynamic-mvt.md), so this refresh no longer touches them.
  // Stable ([] deps): reads refs + the passed-in map only, so registerLayerOnMap stays stable too.
  // Transient/offline fetch failures leave the existing data on the map (no flicker, no banner).
  const refreshExternalLayer = useCallback(async (map: maplibregl.Map, layerId: string): Promise<void> => {
    const src = map.getSource(srcId(layerId))
    if (!src || src.type !== 'geojson') return
    const ext = externalsRef.current[layerId]
    if (!ext) return
    const eb = map.getBounds()
    const ebbox = `${eb.getWest()},${eb.getSouth()},${eb.getEast()},${eb.getNorth()}`
    try {
      // The map's panel filter rides along (rail parity): remote-composed where for arcgis,
      // in-memory predicate for geojson.
      const { fc, capped } = await fetchExternalViewport(
        ext, ebbox, VIEW_FEATURE_CAP, extGeojsonCacheRef.current, filtersRef.current[layerId],
      )
      ;(src as maplibregl.GeoJSONSource).setData(fc)
      setViewCapped((prev) => ((prev[layerId] ?? false) === capped ? prev : { ...prev, [layerId]: capped }))
    } catch {
      // remote hiccup/offline — keep whatever is already drawn.
    }
  }, [])

  // Register one layer's source + render layer(s) + click/cursor handlers on the map.
  // Lifted verbatim out of the original load loop so it can be called both at initial
  // load AND for layers that become viewable later (a slot just created via the dialog).
  // Idempotent: if the source already exists, this is a no-op — safe to call repeatedly.
  const registerLayerOnMap = useCallback((map: maplibregl.Map, def: LayerDef): void => {
    if (map.getSource(srcId(def.id))) return // already registered — skip

    // Resolve the paint color from the layer-default renderer (Talus 13), falling back to
    // the static LayerDef color when none is set. configsRef is current at registration
    // time (caps load gates the map build), so the seeded/initial renderer is in hand.
    const renderer = configsRef.current.find((c) => c.id === def.id)?.renderer ?? null
    const paintColor = resolveColor(renderer, def.color)

    // Registration applies the CURRENT visibility itself, at add time — MapLibre layers are
    // visible by default, and every deferred/raced path (the build-time load loop registering a
    // restore's layers, the late-registration effect finding had=true) previously left a restored
    // `visible: false` layer drawing (B5b 2026-08-12: Sidewalk/dPipes ghosts). Called on every
    // exit path below; getLayer-guarded so ids a geometry lacks are no-ops.
    const applyInitialVisibility = (): void => {
      const vis = (visibleRef.current[def.id] ?? true) ? 'visible' : 'none'
      for (const lid of subLayerIds(def.id)) {
        if (map.getLayer(lid)) map.setLayoutProperty(lid, 'visibility', vis)
      }
    }

    // Geometry-AGNOSTIC render trio — fill + line + circle off ONE source; MapLibre renders each
    // only for its compatible geometry, so any geometry draws without knowing the type ahead of
    // time (polygons get fill+outline, lines get a line, points get a circle). Serves the two
    // layer kinds whose geometry family is unknown: EXTERNAL references (GeoJSON source) and VIEW
    // layers (vector source over dynamic MVT — `sl` names the tile's source-layer).
    const addAgnosticTrio = (sl?: string): void => {
      const fillId = `${lyrId(def.id)}-fill`
      const lineId = `${lyrId(def.id)}-line`
      const circleId = lyrId(def.id)
      const viewDash = lineDashOf(renderer)
      const slProp = sl ? { 'source-layer': sl } : {}
      map.addLayer({
        id: fillId, type: 'fill', source: srcId(def.id), ...slProp,
        paint: { 'fill-color': paintColor, 'fill-opacity': fillOpacityOf(renderer, def.fillOpacity ?? 0.15) },
      })
      map.addLayer({
        id: lineId, type: 'line', source: srcId(def.id), ...slProp,
        // c7 — the one -line sub-layer styles a line view OR a polygon's outline; prefer lineWidth, else
        // outlineWidth, else the original 1.5. Colour/dash apply the same renderer style.
        paint: {
          'line-color': outlineColorOf(renderer, paintColor),
          'line-width': lineWidthOf(renderer, outlineWidthOf(renderer, 1.5)),
          ...(viewDash ? { 'line-dasharray': viewDash } : {}),
        },
      })
      const pointFilter = ['==', ['geometry-type'], 'Point'] as maplibregl.FilterSpecification
      map.addLayer({
        id: circleId, type: 'circle', source: srcId(def.id), ...slProp,
        // Point features ONLY: MapLibre's circle bucket otherwise draws a dot at EVERY VERTEX of a
        // line/polygon feature (reported on external parcels — but it afflicted any
        // polygon/line on this shared path). fill/line stay unfiltered: fill renders only
        // polygons by nature, and the line layer intentionally outlines both lines and polygons.
        filter: pointFilter,
        maxzoom: rendererUsesIcon(renderer) ? iconHandoverZoom(renderer) : MAX_ZOOM,
        paint: pointCirclePaint(renderer, paintColor, def.strokeColor),
      })
      map.addLayer({
        id: iconLyrId(def.id), type: 'symbol', source: srcId(def.id), ...slProp,
        filter: pointFilter,
        minzoom: iconHandoverZoom(renderer),
        layout: pointIconLayout(map, renderer, def.color),
      })
      const interactiveIds = [fillId, lineId, circleId, iconLyrId(def.id)]
      const onEnter = (): void => {
        if (!createLayerIdRef.current) map.getCanvas().style.cursor = 'pointer'
      }
      const onLeave = (): void => {
        map.getCanvas().style.cursor = createLayerIdRef.current ? 'crosshair' : ''
      }
      for (const lid of interactiveIds) {
        renderLayerToDefRef.current[lid] = def.id
        map.on('mouseenter', lid, onEnter)
        map.on('mouseleave', lid, onLeave)
      }
      layerHandlersRef.current[def.id] = { ids: interactiveIds, onEnter, onLeave }
      const labelCfg = labelsRef.current[def.id]
      if (labelCfg) addLabelLayer(map, def, labelCfg)
    }

    // EXTERNAL reference (decision-add-from-url.md) — the one remaining GeoJSON-rendered kind:
    // its features live on a REMOTE server the browser fetches directly; never ours to tile.
    if (externalsRef.current[def.id]) {
      map.addSource(srcId(def.id), { type: 'geojson', data: { type: 'FeatureCollection', features: [] } })
      addAgnosticTrio()
      applyInitialVisibility()
      void refreshExternalLayer(map, def.id) // initial viewport load
      return
    }

    // Every HOSTED layer rides a vector-tile source (decision-dynamic-mvt.md): static pg_tileserv
    // tiles by default; the dynamic ST_AsMVT endpoint when a view / filter / slot-labels need
    // per-request logic. Filter and label flips are just a setTiles URL change (reconcileLayerRender)
    // — no source-type swap, no teardown.
    map.addSource(srcId(def.id), {
      type: 'vector',
      tiles: [hostedTileUrl(def.id)],
      minzoom: 0,
      maxzoom: 16,
    })

    // A VIEW layer's geometry family is unknown (nominal 'point' def) — render the agnostic trio
    // over its tiles, and resolve its columns so the tiles carry popup/label attributes.
    if (isViewId(def.id)) {
      addAgnosticTrio(def.sourceLayer)
      applyInitialVisibility()
      ensureViewTileFields(def.id)
      return
    }

    // POINT keeps the original circle layer byte-for-byte. POLYGON adds a fill
    // layer + a companion outline (line) layer from the SAME vector source. Both
    // polygon layers are interactive (so clicking the fill, not just the outline,
    // opens the popup) and both are toggled together by the layer toggle.
    const interactiveIds: string[] = []
    if (def.geometryType === 'polygon') {
      const fillId = lyrId(def.id)
      const lineId = `${lyrId(def.id)}-line`
      map.addLayer({
        id: fillId,
        type: 'fill',
        source: srcId(def.id),
        'source-layer': def.sourceLayer,
        paint: { 'fill-color': paintColor, 'fill-opacity': fillOpacityOf(renderer, def.fillOpacity ?? 0.12) },
      })
      map.addLayer({
        id: lineId,
        type: 'line',
        source: srcId(def.id),
        'source-layer': def.sourceLayer,
        // c7 — polygon outline: its own colour/width (default = the fill colour + 2px, as before).
        paint: {
          'line-color': outlineColorOf(renderer, paintColor),
          'line-width': outlineWidthOf(renderer, 2),
        },
      })
      interactiveIds.push(fillId, lineId)
    } else if (def.geometryType === 'line') {
      // LINE renders as a single MapLibre `line` layer off the same vector source (V055). The stroke
      // colour resolves from the renderer just like the polygon outline / point fill (paintColor).
      const lineId = lyrId(def.id)
      const lineDash = lineDashOf(renderer)
      map.addLayer({
        id: lineId,
        type: 'line',
        source: srcId(def.id),
        'source-layer': def.sourceLayer,
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: {
          'line-color': paintColor,
          // c7 — flat user width overrides the default zoom-ramp; dash applied only when set (else solid).
          'line-width': lineWidthOf(renderer),
          ...(lineDash ? { 'line-dasharray': lineDash } : {}),
        },
      })
      // A transparent, WIDE companion line so the thin stroke is actually clickable/tappable (a 1–4px line is
      // nearly impossible to hit precisely). It carries the click/cursor — the visible line stays crisp.
      const hitId = `${lyrId(def.id)}-hit`
      map.addLayer({
        id: hitId,
        type: 'line',
        source: srcId(def.id),
        'source-layer': def.sourceLayer,
        paint: { 'line-color': '#000000', 'line-opacity': 0, 'line-width': 14 },
      })
      interactiveIds.push(lineId, hitId)
    } else {
      map.addLayer({
        id: lyrId(def.id),
        type: 'circle',
        source: srcId(def.id),
        'source-layer': def.sourceLayer,
        maxzoom: rendererUsesIcon(renderer) ? iconHandoverZoom(renderer) : MAX_ZOOM,
        paint: pointCirclePaint(renderer, paintColor, def.strokeColor),
      })
      map.addLayer({
        id: iconLyrId(def.id),
        type: 'symbol',
        source: srcId(def.id),
        'source-layer': def.sourceLayer,
        minzoom: iconHandoverZoom(renderer),
        layout: pointIconLayout(map, renderer, def.color),
      })
      interactiveIds.push(lyrId(def.id), iconLyrId(def.id))
    }

    // Clicks are identified at the map level (one handler, all layers) — see the map-level identify
    // handler + collectFeatureHits. Here we only register the reverse index + the pointer cursor.
    const onEnter = (): void => {
      if (!createLayerIdRef.current) map.getCanvas().style.cursor = 'pointer'
    }
    const onLeave = (): void => {
      map.getCanvas().style.cursor = createLayerIdRef.current ? 'crosshair' : ''
    }
    for (const lid of interactiveIds) {
      renderLayerToDefRef.current[lid] = def.id
      map.on('mouseenter', lid, onEnter)
      map.on('mouseleave', lid, onLeave)
    }
    layerHandlersRef.current[def.id] = { ids: interactiveIds, onEnter, onLeave }
    // Labels ride the vector source itself: a built-in's baked V022 columns on static tiles, or the
    // requested label field the dynamic-MVT endpoint bakes per request (a labeled slot's tile URL
    // already carries fields=<label field> via hostedTileUrl above).
    const labelCfg = labelsRef.current[def.id]
    if (labelCfg) addLabelLayer(map, def, labelCfg)
    applyInitialVisibility()
    // Deliberately identity-stable: the once-only map build below lists this callback as a dep
    // BECAUSE it never changes. ensureViewTileFields/hostedTileUrl are render-scoped helpers;
    // adding them would churn the identity and re-trigger everything that depends on it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshExternalLayer])

  // Tear a layer back off the map: map.off its handlers, remove its render layer(s) + source.
  const deregisterLayerOnMap = useCallback((map: maplibregl.Map, id: string): void => {
    const h = layerHandlersRef.current[id]
    if (h) {
      for (const lid of h.ids) {
        map.off('mouseenter', lid, h.onEnter)
        map.off('mouseleave', lid, h.onLeave)
      }
    }
    // Sweep the handler ids AND the full sub-layer set: the -label symbol layer is non-interactive, so it
    // is never in h.ids but must come off before the source (getLayer-guards make extras harmless no-ops).
    for (const lid of new Set([...(h?.ids ?? []), ...subLayerIds(id)])) {
      delete renderLayerToDefRef.current[lid]
      if (map.getLayer(lid)) map.removeLayer(lid)
    }
    if (map.getSource(srcId(id))) map.removeSource(srcId(id))
    delete layerHandlersRef.current[id]
  }, [])

  // Reconcile ONE layer's render with the current effective predicate (filtersRef): if its
  // GeoJSON-vs-MVT status flips it re-registers that one layer; otherwise it just re-queries the
  // GeoJSON source. Shared by the funnel filter (applyLayerFilter) and the table mirror.
  const reconcileLayerRender = useCallback(
    (layerId: string): void => {
      const map = mapRef.current
      if (!map) return
      // EXTERNAL layers render GeoJSON — a filter change means re-querying the remote viewport.
      if (externalsRef.current[layerId]) {
        void refreshExternalLayer(map, layerId)
        return
      }
      // HOSTED layers always ride a vector source (decision-dynamic-mvt.md): applying/clearing a
      // filter or slot labels just repoints the SAME source at the right tile URL (dynamic ↔
      // static) — no source-type swap, no teardown, no restack. MapLibre refetches the viewport's
      // tiles; the render sub-layers are untouched.
      const src = map.getSource(srcId(layerId))
      if (src && src.type === 'vector') {
        ;(src as maplibregl.VectorTileSource).setTiles([hostedTileUrl(layerId)])
      }
    },
    // Same identity-stability contract as registerLayerOnMap above (hostedTileUrl is render-scoped).
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [refreshExternalLayer],
  )

  // Apply (or clear, with []) a layer's attribute filter (decision-feature-filter.md). Updates the persisted
  // state + the live ref and reconciles the render; the docked inspector stays open (its Cancel is the
  // close route). Persisted with the map on the next Save. Called by BOTH editors: the inspector
  // FilterPanel's Apply and the docked table's filter commit.
  const applyLayerFilter = useCallback(
    (layerId: string, conds: FilterCondition[]): void => {
      const next = { ...filtersRef.current }
      if (conds.length) next[layerId] = conds
      else delete next[layerId]
      filtersRef.current = next // update the ref FIRST so register/refresh in the reconcile read the new predicate
      setLayerFilters(next)
      reconcileLayerRender(layerId)
    },
    [reconcileLayerRender],
  )

  // Rebuild ONE layer's label symbol layer in place from labelsRef (remove + re-add; absent config = off).
  // Sources never flip anymore (decision-dynamic-mvt.md) — the label symbol layer is rebuilt in place
  // on every label apply/remove; reconcileLayerRender separately repoints the tile URL (fields=).
  const syncLabelLayer = useCallback((layerId: string): void => {
    const map = mapRef.current
    if (!map) return
    const def = layersRef.current.find((d) => d.id === layerId)
    if (!def) return
    const lid = `${lyrId(layerId)}-label`
    if (map.getLayer(lid)) map.removeLayer(lid)
    const cfg = labelsRef.current[layerId]
    if (cfg) {
      addLabelLayer(map, def, cfg)
      if (map.getLayer(lid)) {
        map.setLayoutProperty(lid, 'visibility', (visibleRef.current[layerId] ?? true) ? 'visible' : 'none')
        restackRef.current?.(map, selectedRef.current) // keep the TOC draw order (labels ride their layer)
      }
    }
  }, [])

  // Apply (or remove, with null) a layer's labels (decision-map-labels.md). Mirrors applyLayerFilter:
  // ref first (so the reconcile reads the new config), then state, close the panel, reconcile (the
  // tile URL gains/loses its fields= — decision-dynamic-mvt.md), and rebuild the symbol layer.
  const applyLayerLabels = useCallback(
    (layerId: string, cfg: WebMapLabelConfig | null): void => {
      const next = { ...labelsRef.current }
      if (cfg) next[layerId] = cfg
      else delete next[layerId]
      labelsRef.current = next
      setLayerLabels(next)
      reconcileLayerRender(layerId)
      syncLabelLayer(layerId)
    },
    [reconcileLayerRender, syncLabelLayer],
  )

  // Apply (or reset, with null) a layer's popup config (decision-map-popups.md). Pure state — popups
  // resolve at click time off popupsRef — but any OPEN popup was built under the old config, so close it.
  const applyLayerPopup = useCallback((layerId: string, cfg: WebMapPopupConfig | null): void => {
    setLayerPopups((prev) => {
      const next = { ...prev }
      if (cfg) next[layerId] = cfg
      else delete next[layerId]
      popupsRef.current = next
      return next
    })
    closePopup()
  }, [])

  // Re-apply `selected` as draw order: selected[0] is the TOP layer. Iterate bottom→top and move
  // each layer's sub-layers to the top (fill before line so a polygon's outline stays above its
  // fill). map.moveLayer(id) with no beforeId moves the layer to the very top of the stack.
  const restack = useCallback((map: maplibregl.Map, order: string[]): void => {
    for (const id of [...order].reverse()) {
      // Move base → fill → line so the LAST moved (line) ends on top: for an MVT polygon the base id
      // IS the fill (line above it); for a view the base id is the circle and -fill/-line sit above —
      // lines/outlines stay topmost everywhere. (Talus 38 adds the -fill rung.)
      for (const lid of [
        `raster-lyr-${id}`,
        lyrId(id),
        `${lyrId(id)}-icon`, // icons sit directly above their own circle (they alternate by zoom)
        `${lyrId(id)}-fill`,
        `${lyrId(id)}-line`,
        ...selLayerIds(id), // selection highlight above the layer's own paint (zoom-to-selected F3)
        `${lyrId(id)}-label`, // labels last — always above their own layer's fill/line/circle
      ]) {
        if (map.getLayer(lid)) map.moveLayer(lid)
      }
    }
  }, [])
  restackRef.current = restack

  // The symbol set is its own ~50 KB chunk (pointIcons.ts). Start it as soon as a map mounts so it
  // is normally resolved before any layer registers; a layer that beats it draws its circle and is
  // rebuilt here when the chunk lands.
  useEffect(() => {
    if (pointSymbolsLoaded()) return
    let cancelled = false
    void preloadPointSymbols().then(() => {
      const map = mapRef.current
      if (cancelled || !map) return
      // Layers that registered before the chunk landed drew their circles; now that the set is
      // usable, point their icon siblings at it. A layout update, not a rebuild.
      for (const cfg of configsRef.current) {
        if (!rendererUsesIcon(cfg.renderer)) continue
        const def = layersRef.current.find((d) => d.id === cfg.id)
        if (def) applyPointIconLayout(map, cfg.id, cfg.renderer, def.color)
      }
    })
    return () => {
      cancelled = true
    }
  }, [configs, registerLayerOnMap, deregisterLayerOnMap])

  // Selection-highlight sync (decision-zoom-to-selected.md F3): reconcile the per-layer highlight
  // trio with `tableSelections`. The trio rides the layer's OWN source filtered by id membership —
  // every tile family carries id::text, so there is no geometry fetch and the highlight scales with
  // the layer, not the selection. The circle rung keeps the Point-only guard (the agnostic trio's
  // vertex-dot lesson); fill renders only polygons by nature; line outlines lines AND polygons.
  // Layers are added lazily on first selection and torn off when a layer's selection clears
  // (removal/restore teardown also sweeps them — selLayerIds rides subLayerIds).
  useEffect(() => {
    const map = mapRef.current
    if (!map || (map as unknown as { _removed?: boolean })._removed) return
    for (const cfg of configsRef.current) {
      if (cfg.id in tableSelections) continue
      for (const lid of selLayerIds(cfg.id)) if (map.getLayer(lid)) map.removeLayer(lid)
    }
    let added = false
    for (const [id, sel] of Object.entries(tableSelections)) {
      if (!map.getSource(srcId(id))) continue // not (yet) on the map; removal/restore clears state
      const def = configsRef.current.find((c) => c.id === id)
      const slProp = def?.sourceLayer ? { 'source-layer': def.sourceLayer } : {}
      const idIn = ['in', ['get', 'id'], ['literal', Array.from(sel)]] as maplibregl.FilterSpecification
      const circleFilter = ['all', ['==', ['geometry-type'], 'Point'], idIn] as maplibregl.FilterSpecification
      const [fillId, lineId, circleId] = selLayerIds(id)
      if (map.getLayer(fillId)) {
        map.setFilter(fillId, idIn)
        map.setFilter(lineId, idIn)
        map.setFilter(circleId, circleFilter)
        continue
      }
      map.addLayer({
        id: fillId, type: 'fill', source: srcId(id), ...slProp, filter: idIn,
        paint: { 'fill-color': SEL_COLOR, 'fill-opacity': 0.18 },
      })
      map.addLayer({
        id: lineId, type: 'line', source: srcId(id), ...slProp, filter: idIn,
        paint: { 'line-color': SEL_COLOR, 'line-width': 3 },
      })
      map.addLayer({
        id: circleId, type: 'circle', source: srcId(id), ...slProp, filter: circleFilter,
        paint: {
          'circle-radius': ['interpolate', ['linear'], ['zoom'], 10, 5, 17, 11],
          'circle-color': 'rgba(255, 204, 0, 0.25)',
          'circle-stroke-color': SEL_COLOR,
          'circle-stroke-width': 2,
        },
      })
      added = true
    }
    // A fresh trio lands on top of everything; one restack folds it into the TOC draw order
    // (above its own layer's paint, below its labels).
    if (added) restackRef.current?.(map, selectedRef.current)
  }, [tableSelections])

  // Remove a layer from the New Map: drop it from `selected`, tear it off the map, and clear any
  // UI tied to it (mirrors toggleLayer's hide-cleanup so no edit/popup lingers on a gone layer).
  const removeLayer = (id: string): void => {
    // The Sketch row's ✕ = remove ALL notes (they're one TOC row; per-note delete lives on the card).
    if (id === '__sketch') {
      confirm({
        title: 'Remove all sketch notes from this map?',
        // Sketch notes live in the map document, so this is undone by simply not saving — the
        // reversible tone, not `danger`.
        tone: 'primary',
        consequence: 'Every note goes at once. Nothing is written until you save the map, so closing without saving brings them back.',
        confirmLabel: 'Remove all notes',
        onConfirm: () => {
          setSketch([])
          setSelectedSketchId(null)
          setReshapingSketchId(null)
        },
      })
      return
    }
    setSelected((s) => s.filter((x) => x !== id))
    setLayerSelection(id, new Set()) // F2 lifetime: a removed layer's row selection dies with it
    if (selectArmedRef.current === id) disarmSelect() // its armed select session dies with it too
    const map = mapRef.current
    if (rasterMeta[id]) {
      // Raster op: tear off its source+layer and drop its meta (the vector cleanup below is a no-op).
      if (map) {
        if (map.getLayer(`raster-lyr-${id}`)) map.removeLayer(`raster-lyr-${id}`)
        if (map.getSource(`raster-src-${id}`)) map.removeSource(`raster-src-${id}`)
      }
      setRasterMeta((m) => {
        if (!(id in m)) return m
        const next = { ...m }
        delete next[id]
        return next
      })
    } else if (map) {
      deregisterLayerOnMap(map, id)
    }
    // External reference (decision-add-from-url.md): drop its spec + cached document too, so a
    // re-add starts fresh and a Save no longer serializes it.
    if (externalsRef.current[id]) {
      const next = { ...externalsRef.current }
      delete next[id]
      externalsRef.current = next
      setExternals(next)
      delete extGeojsonCacheRef.current[id]
    }
    setVisible((v) => {
      if (!(id in v)) return v
      const next = { ...v }
      delete next[id]
      return next
    })
    if (popupLayerRef.current === id) closePopup()
    if (form?.layerId === id) {
      setForm(null)
      setFormError(null)
    }
    if (moving?.layerId === id) exitMove()
    if (editing?.layerId === id) exitEditShape()
    if (createLayerId === id) setCreateLayerId(null)
    if (drawLayerId === id) setDrawLayerId(null)
    // No stragglers: everything else presenting this layer closes with it —
    // the docked attribute table, an open layer-options menu, the inspector selection, and the
    // in-session filter predicate (so re-adding the layer starts clean, matching a save).
    if (selectedLayerId === id) setSelectedLayerId(null)
    if (openTableId === id) setOpenTableId(null)
    if (layerMenu?.layerId === id) setLayerMenu(null)
    setLayerFilters((f) => {
      if (!(id in f)) return f
      const next = { ...f }
      delete next[id]
      return next
    })
    setLayerLabels((l) => {
      if (!(id in l)) return l
      const next = { ...l }
      delete next[id]
      return next
    })
    setLayerPopups((p) => {
      if (!(id in p)) return p
      const next = { ...p }
      delete next[id]
      return next
    })
    if (renamingLayerId === id) setRenamingLayerId(null)
    setLayerTitles((t) => {
      if (!(id in t)) return t
      const next = { ...t }
      delete next[id]
      return next
    })
  }

  // Reorder in the TOC: dir -1 moves the layer up (toward the top of the list / draw stack), +1 down.
  const reorderLayer = (id: string, dir: -1 | 1): void =>
    setSelected((s) => {
      const i = s.indexOf(id)
      const j = i + dir
      if (i < 0 || j < 0 || j >= s.length) return s
      const next = [...s]
      ;[next[i], next[j]] = [next[j], next[i]]
      return next
    })

  // Zoom the map to a layer's extent — handy when a layer is added far from the current view (the
  // New Map opens US-wide). Client-side: bound the layer's features in the loaded tiles (at the
  // current view its tiles are loaded, which covers the just-added case) and fitBounds — no
  // layer-extent API needed. Empty result (layer hidden / tiles not yet loaded) is a silent no-op.
  const zoomToLayer = (id: string): void => {
    const map = mapRef.current
    const def = configsRef.current.find((c) => c.id === id)
    if (!map || !def) return
    const feats = map.querySourceFeatures(srcId(id), { sourceLayer: def.sourceLayer })
    if (feats.length === 0) return
    const bounds = new maplibregl.LngLatBounds()
    const extend = (coords: unknown): void => {
      if (typeof (coords as number[])[0] === 'number') bounds.extend(coords as [number, number])
      else for (const c of coords as unknown[]) extend(c)
    }
    for (const f of feats) {
      const geom = f.geometry
      if (geom && 'coordinates' in geom) extend((geom as { coordinates: unknown }).coordinates)
    }
    if (!bounds.isEmpty()) map.fitBounds(bounds, { padding: 48, maxZoom: 17 })
  }

  // Zoom to the SELECTED features' extent (decision-zoom-to-selected.md F1) — server-computed via
  // the ids-scoped /extent, because a table selection can span rows far outside any loaded tile;
  // zoomToLayer's querySourceFeatures bound would silently exclude them. bbox null (all ids since
  // deleted) or any fetch failure → silent no-op, parity with the client-side zooms.
  const zoomToSelected = (id: string): void => {
    const ids = tableSelections[id]
    if (!ids?.size || !accessToken) return
    void fetchLayerExtent(id, accessToken, Array.from(ids))
      .then(({ bbox }) => {
        if (!bbox) return
        mapRef.current?.fitBounds(
          [
            [bbox[0], bbox[1]],
            [bbox[2], bbox[3]],
          ],
          { padding: 48, maxZoom: 17 },
        )
      })
      .catch(() => undefined)
  }

  // External zoom (decision-add-from-url.md): zoom to the SERVICE'S full extent, not
  // the viewport-capped loaded features — arcgis kinds re-ask the service for a 4326-projected,
  // where-scoped extent (works even for a state-plane-native ImageServer); geojson uses its document
  // bbox; xyz has no knowable extent (silent no-op).
  const zoomToExternal = (spec: ExternalLayerSpec): void => {
    void externalExtent4326(spec, extGeojsonCacheRef.current).then((ext) => {
      if (ext) {
        mapRef.current?.fitBounds(
          [
            [ext[0], ext[1]],
            [ext[2], ext[3]],
          ],
          { padding: 48, maxZoom: 17 },
        )
      }
    })
  }
  // Unified TOC zoom: sketch → its notes' bbox; externals → the remote service's extent; rasters →
  // /info bounds; vectors → feature bounds.
  const zoomTo = (id: string): void => {
    if (id === '__sketch') {
      const b = sketchBounds(sketchRef.current)
      if (b) {
        mapRef.current?.fitBounds(
          [
            [b[0], b[1]],
            [b[2], b[3]],
          ],
          { padding: 48, maxZoom: 17 },
        )
      }
      return
    }
    const spec = externals[id]
    if (spec) return zoomToExternal(spec)
    return rasterMeta[id] ? zoomToRaster(id) : zoomToLayer(id)
  }

  // ── Add from file (decision-add-from-file.md): pick → preview → seeded wizard → ingest → zoom ──
  const onPickFile = (file: File): void => {
    setFileBusy(true)
    setFileError(null)
    setFileErrorOffersImport(false)
    void (async () => {
      try {
        const prep = await prepareFile(file, accessToken ?? '')
        // The browser can't split a zip/gpkg into batches — over the per-request cap is an honest no.
        if (
          (prep.format === 'shapefile' || prep.format === 'geopackage') &&
          prep.featureCount !== null &&
          prep.featureCap !== null &&
          prep.featureCount > prep.featureCap
        ) {
          throw new UseImportDataError(
            `This file has ${prep.featureCount.toLocaleString()} features. Add from file takes a ` +
              `${prep.format === 'shapefile' ? 'Shapefile' : 'GeoPackage'} of up to ${prep.featureCap.toLocaleString()}; ` +
              'Import data in the Catalog takes up to 2,000,000.',
          )
        }
        setFilePrep(prep)
        setAddOpen(false)
      } catch (e) {
        setFileError(e instanceof Error ? e.message : 'Could not read the file')
        setFileErrorOffersImport(e instanceof UseImportDataError)
      } finally {
        setFileBusy(false)
      }
    })()
  }

  // Post-Finish: ingest the retained file (one request, or 10k-feature batches for geojson/csv), then
  // add the new layer to the map and zoom to its extent. Ingest failure KEEPS the layer (fork §2.4) —
  // the schema/sharing survive and the data can be re-imported via the Catalog's Import features.
  const runFileIngest = async (prep: PreparedFile, layerId: string, ctx?: SeedIngestContext): Promise<void> => {
    // Read the token PER REQUEST, never once up front: a county-scale chunked ingest outlives the
    // ~5-min access token, and tokenRef is what silent renew keeps fresh (B5 finding, 2026-08-09).
    const token = () => tokenRef.current ?? accessToken ?? ''
    const total = prep.featureCount
    try {
      const payloads = buildIngestPayloads(prep, ctx)
      // Chunked formats (geojson/csv) update with real cumulative counts after each batch; a
      // whole-file format (shapefile/gpkg) is ONE server request, so only the total is knowable.
      setImportProgress(
        payloads.length > 1 && total !== null
          ? `Importing… 0 of ${total.toLocaleString()} features`
          : `Importing ${total !== null ? `${total.toLocaleString()} ` : ''}features…`,
      )
      let committed = 0
      let skipped = 0
      let sent = 0
      for (const p of payloads) {
        const res = await importFeatures(layerId, p, true, token())
        committed += res.committed
        skipped += res.error_count
        sent += res.total
        if (payloads.length > 1 && total !== null) {
          setImportProgress(`Importing… ${Math.min(sent, total).toLocaleString()} of ${total.toLocaleString()} features`)
        }
      }
      setImportProgress(null)
      setCapsVersion((v) => v + 1) // the new layer enters /me/layers → it renders + becomes addable
      addLayer(layerId)
      // Every other commit path bumps the tile version; the bulk import did not — so a layer whose EMPTY
      // tiles were already fetched (the slot exists before the rows do) kept drawing nothing until the map
      // was closed and reopened (found live 2026-08-13, 12,303 signs). Bump for the same reason as an edit.
      bumpTiles(layerId)
      setMapNotice(
        `${committed.toLocaleString()} features imported${skipped > 0 ? ` (${skipped.toLocaleString()} rows skipped)` : ''}.`,
      )
      try {
        const ext = await fetchLayerExtent(layerId, token())
        if (ext.bbox) {
          mapRef.current?.fitBounds(
            [
              [ext.bbox[0], ext.bbox[1]],
              [ext.bbox[2], ext.bbox[3]],
            ],
            { padding: 48, maxZoom: 17 },
          )
        }
      } catch {
        /* the zoom is best-effort — the layer is already on the map */
      }
    } catch (e) {
      setImportProgress(null)
      setCapsVersion((v) => v + 1)
      addLayer(layerId) // the layer exists and is valid — only the data import failed
      const msg = e instanceof Error ? e.message : 'import failed'
      setMapNotice(`Layer created, but the feature import failed: ${msg} — re-run it anytime via the Catalog’s “Import features”.`)
    }
  }

  // ── Add from URL (decision-add-from-url.md): the panel probed the service; land it on the map. A
  // vector external joins the geometry-ranked stack (its def registers on the GeoJSON path); a raster
  // external mirrors into rasterMeta with its remote tile template. Zoom to the probed extent when the
  // service declared a web-friendly SR.
  const addExternal = (spec: ExternalLayerSpec, extent: [number, number, number, number] | null): void => {
    const isVector = spec.externalKind === 'arcgis-feature' || spec.externalKind === 'geojson'
    externalsRef.current = { ...externalsRef.current, [spec.externalId]: spec } // sync — registration reads it
    setExternals(externalsRef.current)
    if (!isVector) {
      setRasterMeta((m) => ({
        ...m,
        [spec.externalId]: { title: spec.title, opacity: 1, externalTiles: externalRasterTemplate(spec) },
      }))
    }
    setVisible((v) => ({ ...v, [spec.externalId]: true }))
    setSelected((s) =>
      s.includes(spec.externalId) ? s : isVector ? insertByRank(s, spec.externalId) : [...s, spec.externalId],
    )
    if (extent) {
      mapRef.current?.fitBounds(
        [
          [extent[0], extent[1]],
          [extent[2], extent[3]],
        ],
        { padding: 48, maxZoom: 17 },
      )
    }
  }

  // ── Referenced catalog items (decision-external-catalog-items.md) ─────────────────────────────

  // Browse-picker add: seed an external op from the ITEM — id + a full spec snapshot, so the map
  // renders even if the item is later deleted/unshared. Zoom via the standard external-extent path
  // (the probe extent isn't available here; zoomToExternal asks the service, where-scoped).
  const addReference = (ref: ExternalRef): void => {
    const isVector = ref.externalKind === 'arcgis-feature' || ref.externalKind === 'geojson'
    const spec: ExternalLayerSpec = {
      externalId: newExternalId(),
      externalKind: ref.externalKind,
      url: ref.url,
      title: ref.title,
      ...(isVector
        ? { geometryType: ref.geometryType ?? undefined, color: ref.color ?? EXT_PALETTE[0] }
        : {}),
      ...(ref.where ? { where: ref.where } : {}),
      externalItemId: ref.id,
    }
    addExternal(spec, null)
    zoomToExternal(spec)
  }

  // Save-to-catalog from the Add-from-URL card (fork §3.6): create the item, surface it in the
  // picker immediately, hand the id back so the op links to it. Throws on failure — the panel
  // shows the honest error and adds nothing.
  const saveReference = async (payload: {
    title: string
    externalKind: ExternalLayerSpec['externalKind']
    url: string
    geometryType?: 'point' | 'line' | 'polygon'
    color?: string
    where?: string
  }): Promise<string> => {
    if (!accessToken) throw new Error('not signed in')
    const ref = await createExternalRef(payload, accessToken)
    setAvailRefs((prev) => [
      ...prev,
      { ...ref, owner: true, sharedWithMe: false, favorite: false, canManage: true },
    ])
    return ref.id
  }

  // Item ids already on the map (any op seeded from them) — disables their picker rows.
  const addableRefs = useMemo<AddableReference[]>(() => {
    const addedItemIds = new Set(
      Object.values(externals)
        .map((s) => s.externalItemId)
        .filter(Boolean),
    )
    return availRefs.map((r) => ({ ref: r, added: addedItemIds.has(r.id), favorite: r.favorite }))
  }, [availRefs, externals])

  // TOC link badges: ops seeded from a catalog reference (decision §2.4).
  const referencedIds = useMemo(
    () =>
      new Set(
        Object.values(externals)
          .filter((s) => s.externalItemId)
          .map((s) => s.externalId),
      ),
    [externals],
  )

  // The unified, ordered Contents list the TOC renders: each `selected` id becomes a raster row (if in
  // rasterMeta) or a viewable vector row — in `selected` (draw) order, so vectors + rasters interleave.
  const tocItems = useMemo<TocItem[]>(() => {
    const cfgById = new Map(configs.map((c) => [c.id, c]))
    const byId = new Map(availRasters.map((r) => [r.id, r]))
    const out: TocItem[] = []
    // Sketch notes draw above everything, so their ONE row sits at the top (decision-sketch-layer.md
    // §3.5: features are managed by clicking them on-map, not as TOC rows).
    if (sketch.length > 0) out.push({ kind: 'sketch', count: sketch.length })
    for (const id of selected) {
      if (rasterMeta[id]) {
        out.push({
          kind: 'raster',
          id,
          title: byId.get(id)?.title ?? rasterMeta[id].title ?? id,
          opacity: rasterMeta[id].opacity,
          bandCount: byId.get(id)?.bandCount ?? undefined,
          colormap: rasterMeta[id].colormap,
          // External rasters (remote tile templates) have no Talus export path.
          downloadable: rasterMeta[id].externalTiles ? false : (byId.get(id)?.downloadable ?? true),
        })
      } else {
        const c = cfgById.get(id)
        if (c && c.canView) out.push({ kind: 'layer', config: c })
      }
    }
    return out
  }, [selected, configs, rasterMeta, availRasters, sketch.length])

  // PAINT basemap `b` onto `map`: remove the current basemap layers (by tracked id), add the new
  // flavor's, then restack so the user's data layers stay on top. Source/sprite/glyphs unchanged.
  // Pure canvas work — no React state, and NO no-op guard: callers that must guarantee canvas and
  // state agree (the 'load' handler, the F3 source rebuild) need it to run even when the target
  // equals current state. `switchBasemap` below owns the guard and the state write.
  const paintBasemap = (map: maplibregl.Map, b: Basemap): void => {
    // Tear down the current basemap — vector flavor layers, OR the imagery raster + its label overlay.
    for (const id of basemapLayerIdsRef.current) if (map.getLayer(id)) map.removeLayer(id)
    if (map.getSource(IMAGERY_SOURCE)) map.removeSource(IMAGERY_SOURCE)

    // V115 — the choke point. Every basemap reaches the canvas through this function, so this is
    // where "a streamed map the organization does not offer is never fetched" is made true for
    // every caller at once (the seed, the switcher, a saved-map restore, the F3 rebuild), whatever
    // the state they were handed. Callers resolve too, so state and canvas agree; this is the
    // backstop that makes a missed caller harmless instead of a request to a withdrawn publisher.
    const draw = resolveOffered(b, streamedRef.current, orgDefaultRef.current)

    let ids: string[]
    if (isStreamedBasemap(draw)) {
      map.addSource(IMAGERY_SOURCE, streamedRasterSource(draw))
      // The look's own paint (V116): OpenStreetMap Gray and Dark are the standard tiles
      // recoloured on THIS layer only, so the data layers above keep their colours.
      map.addLayer({
        id: IMAGERY_LAYER,
        type: 'raster',
        source: IMAGERY_SOURCE,
        paint: streamedRasterPaint(draw),
      })
      if (draw === 'imagery') {
        // USGS imagery carries no names: an OSM roads/labels subset of our own pmtiles goes over it.
        // The two street maps (OpenStreetMap, USGS Topo) have their names in the tiles.
        const overlay = basemapLayers('protomaps', LIGHT, { lang: 'en' }).filter(
          (l) => l.type === 'symbol' || l.type === 'line',
        )
        for (const l of overlay) map.addLayer(l)
        ids = [IMAGERY_LAYER, ...overlay.map((l) => l.id)]
      } else {
        ids = [IMAGERY_LAYER]
      }
    } else {
      const next = basemapLayers('protomaps', VECTOR_FLAVORS[draw], { lang: 'en' })
      for (const l of next) map.addLayer(l) // added on top for now…
      ids = next.map((l) => l.id)
    }
    basemapLayerIdsRef.current = ids
    restack(map, selected) // …lift the data layers back above the basemap
  }
  paintBasemapRef.current = paintBasemap

  // Switch the basemap flavor in place (the user's pick, and the saved-map restore).
  const switchBasemap = (b: Basemap): void => {
    const map = mapRef.current
    if (!map || b === basemap) return
    paintBasemap(map, b)
    // Keep the ref synchronously authoritative: the 'load' handler runs the saved-map restore and
    // then reconciles the canvas against basemapRef in the SAME tick, before React re-renders.
    basemapRef.current = b
    setBasemap(b)
  }
  switchBasemapRef.current = switchBasemap

  // F3/F6 — rebuild the pmtiles source against a basemap that has landed since this page loaded.
  // MapLibre offers no supported "retry a failed source", so the rebuild is remove-layers →
  // remove-source → re-add → repaint. The archive URL is UNCHANGED (it was always
  // basemapArchiveUrl(); it just 404'd) — what changes is that it now serves bytes.
  //
  // Deliberately does NOT fitBounds to the new archive's header, unlike the first build below: by
  // now the user has almost certainly panned to their jurisdiction on the cold-start imagery, and
  // yanking the viewport out from under them is the opposite of the fix. Deliberately does NOT
  // change the user's basemap selection either (F4.1, seed-only): someone sitting on imagery keeps
  // imagery — its OSM label overlay simply starts drawing from a live archive instead of a dead one.
  const rebuildBasemapSource = (map: maplibregl.Map): void => {
    const url = basemapArchiveUrl()
    const archiveUrl = url.startsWith('http') ? url : new URL(url, window.location.origin).toString()
    // THE load-bearing line for F3. This page has been sitting on a poisoned PMTiles instance since
    // it loaded unprovisioned; re-adding the MapLibre source alone would resolve straight back to it
    // and the rebuild would silently do nothing. See evictPmtilesArchive.
    evictPmtilesArchive(archiveUrl)
    // Every layer on the protomaps source is a tracked basemap layer (the imagery flavor's label
    // overlay included), so this clears the way for removeSource — data layers ride their own sources.
    for (const id of basemapLayerIdsRef.current) if (map.getLayer(id)) map.removeLayer(id)
    basemapLayerIdsRef.current = []
    if (map.getSource(IMAGERY_SOURCE)) map.removeSource(IMAGERY_SOURCE)
    if (map.getSource('protomaps')) map.removeSource('protomaps')
    map.addSource('protomaps', {
      type: 'vector',
      url: `pmtiles://${archiveUrl}`,
      attribution:
        '<a href="https://protomaps.com">Protomaps</a> © <a href="https://openstreetmap.org">OpenStreetMap</a>',
    })
    paintBasemap(map, basemapRef.current)
  }
  rebuildBasemapSourceRef.current = rebuildBasemapSource

  // F3 — an install must reach an ALREADY-OPEN map page (B6 2026-08-22, reproduced on the F5 clean
  // box 2026-08-24). A page that loaded while the portal was unprovisioned holds a pmtiles source
  // pointed at a URL we knowingly handed MapLibre despite its 404 (see the probe above), and
  // MapLibre never retries a failed source — the tab stays gray for its whole life while every
  // FRESH load works. Re-probe when the tab regains focus while absent, and rebuild in place.
  //
  // Focus/visibility rather than a poll or a same-tab broadcast: /map is a routed element, so the
  // same-tab Admin → Map path ALREADY self-heals by remounting and re-probing. The case that is
  // actually broken is a second tab — or provisioning from another machine entirely — and only a
  // focus/visibility re-probe covers both without every unprovisioned map page polling forever.
  useEffect(() => {
    if (!basemapAbsent) return
    let active = true
    const recheck = (): void => {
      if (document.visibilityState === 'hidden') return
      void (async () => {
        let installed: boolean
        try {
          installed = (await fetchBasemapStatus()).installed
        } catch {
          return // api unreachable — stay absent and try again on the next focus
        }
        if (!active || !installed) return
        // Clearing `absent` says the notice is no longer TRUE — it is not a dismissal, and this
        // effect only ever sets the flag FALSE, so it can never resurrect a nudge the user already
        // dismissed. That is an explicit rider (F5).
        const done = (): void => setBasemapAbsent(false)
        const map = mapRef.current
        // No map yet (the probe resolved, construction has not run): nothing to rebuild — the
        // build below points at the now-live archive on its own, and fits to its bounds.
        if (!map) return done()
        const rebuild = (): void => {
          try {
            rebuildBasemapSourceRef.current(map)
          } catch {
            // F6 fallback. An in-place rebuild is not atomic in MapLibre, so if it throws we may be
            // left with a half-torn basemap — worse than the gray we started with. Say so plainly
            // rather than ship a silent failure; a reload rebuilds the style from scratch and the
            // fresh-load path has always worked.
            setMapNotice('Basemap installed — reload the page to see it.')
            return
          }
          done()
        }
        // Mid-build (focus landed between construction and style-load): touching layers/sources now
        // throws. 'idle' rather than 'load' for the same reason the saved-map restore uses it below
        // — 'load' fires once and a listener attached after it fired never runs.
        if (map.isStyleLoaded()) rebuild()
        else map.once('idle', () => { if (active) rebuild() })
      })()
    }
    window.addEventListener('focus', recheck)
    document.addEventListener('visibilitychange', recheck)
    return () => {
      active = false
      window.removeEventListener('focus', recheck)
      document.removeEventListener('visibilitychange', recheck)
    }
  }, [basemapAbsent])

  // --- Talus 20: serialize the composed map + Save / Update -------------------

  function currentDefinition(): WebMapDefinition {
    const map = mapRef.current
    const c = map?.getCenter()
    return buildDefinition(
      { basemap, order: selected, visible, rasterMeta, externals, layerFilters, layerLabels, layerPopups, layerTitles, mapTables, bookmarks, sketch, fieldReady },
      { center: c ? [c.lng, c.lat] : DEFAULT_CENTER, zoom: map?.getZoom() ?? DEFAULT_ZOOM },
    )
  }

  function flashSaved(): void {
    setSavedFlash(true)
    setBookmarksDirty(false) // a successful save persists the bookmarks too
    baselineSigRef.current = defSignature(currentDefinition()) // what was just saved IS the new baseline
    if (savedMap) clearDraft(savedMap.id) // the draft is now redundant with the server
    setDraftOffer(null)
    window.setTimeout(() => setSavedFlash(false), 1800)
  }

  async function createSavedMap(title: string, description: string): Promise<void> {
    const token = auth.user?.access_token
    if (!token) return
    setSaving(true)
    setMapNotice(null)
    try {
      const wm = await createWebMap(
        { title, description: description || null, definition: currentDefinition() },
        token,
      )
      restoredRef.current = wm.id // we already hold this state — don't let the restore effect re-fetch
      setSavedMap({ id: wm.id, title: wm.title, canManage: true }) // the creator owns it
      setSaveOpen(false)
      flashSaved()
      navigate(`/map/${wm.id}`) // reflect the saved map in the URL (deep-linkable + refresh-safe)
    } catch (err) {
      setMapNotice(err instanceof ApiError ? `Save failed: ${err.message}` : 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  async function updateSavedMap(): Promise<void> {
    const token = auth.user?.access_token
    if (!token || !savedMap) return
    setSaving(true)
    setMapNotice(null)
    try {
      await updateWebMap(savedMap.id, { title: savedMap.title, definition: currentDefinition() }, token)
      flashSaved()
    } catch (err) {
      setMapNotice(err instanceof ApiError ? `Save failed: ${err.message}` : 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  // Talus 18c: a geocode result was picked — fly there and drop a single ephemeral pin
  // (label in a popup). Replaces any prior pin so only the latest result is marked. The pin
  // is a maplibregl.Marker (a distinct "search result" affordance, not a data feature); it's
  // owned here, not in `selected`, so it never enters the TOC or tile pipeline.
  const flyToCandidate = useCallback((c: GeoCandidate): void => {
    const map = mapRef.current
    if (!map) return
    geocodeMarkerRef.current?.remove()
    geocodeMarkerRef.current = new maplibregl.Marker({ color: '#e8602d' })
      .setLngLat([c.lon, c.lat])
      .setPopup(
        new maplibregl.Popup({ closeButton: false, offset: 24, className: 'geocode-result-popup' }).setText(
          c.label,
        ),
      )
      .addTo(map)
    geocodeMarkerRef.current.togglePopup() // show the address label immediately
    map.flyTo({ center: [c.lon, c.lat], zoom: Math.max(map.getZoom(), 17), essential: true })
  }, [])

  const clearGeocodeMarker = useCallback((): void => {
    geocodeMarkerRef.current?.remove()
    geocodeMarkerRef.current = null
  }, [])

  // --- Map export (decision-map-export.md): capture the current view as a PNG (Print/PDF later) ---
  // Available to everyone (like Bookmarks) — it exports pixels the user can already see. The default
  // filename is `<map-title>-YYYYMMDD`; a brand-new map slugs to `map-…`.
  const exportDefaultName = (): string => defaultExportName(savedMap?.title)

  const handleDownloadPng = async (filename: string): Promise<string | null> => {
    const map = mapRef.current
    if (!map) return 'The map isn’t ready yet.'
    const { blob, error } = await exportScreenshotBlob(map, tocItems, visible)
    if (error || !blob) return error ?? 'Export failed. Please try again.'
    downloadBlob(blob, `${filename}.png`)
    return null
  }

  const handleDownloadPdf = async (opts: {
    title: string
    orientation: 'landscape' | 'portrait'
    dpi: number
  }): Promise<string | null> => {
    const map = mapRef.current
    if (!map) return 'The map isn’t ready yet.'
    // Same tile auth the live map uses (AssetMap init transformRequest is the source of truth) so the
    // offscreen high-DPI render can fetch gated MVT/raster tiles.
    const transformRequest: maplibregl.RequestTransformFunction = (url) => {
      if (url.startsWith(apiBaseAbsolute()) && (url.includes('/tiles/') || url.includes('/tiles-dyn/') || url.includes('/rasters/'))) {
        const token = tokenRef.current
        return token ? { url, headers: { Authorization: `Bearer ${token}` } } : { url }
      }
      return undefined
    }
    // Lazy chunk — jsPDF + the PDF layout code load only when someone actually prints.
    const { exportMapPdf } = await import('@/features/map/mapExportPdf')
    return exportMapPdf({
      source: map,
      items: tocItems,
      visible,
      title: opts.title,
      orientation: opts.orientation,
      dpi: opts.dpi,
      filename: defaultExportName(opts.title),
      transformRequest,
    })
  }

  // --- Map bookmarks: manage the in-memory list; the existing Save persists them ----------------
  // Editing rides the map's edit gate (same condition as the Save button): a publisher composing a
  // new map, or an editor/owner of a saved one. A viewer gets the list read-only.
  const canManageMap = isPublisher && (!savedMap || savedMap.canManage)

  const captureView = (): { center: [number, number]; zoom: number } => {
    const map = mapRef.current
    const c = map?.getCenter()
    return { center: c ? [c.lng, c.lat] : DEFAULT_CENTER, zoom: map?.getZoom() ?? DEFAULT_ZOOM }
  }

  const flyToBookmark = useCallback((bm: WebMapBookmark): void => {
    mapRef.current?.flyTo({ center: bm.center, zoom: bm.zoom, essential: true })
  }, [])

  const addBookmark = (name: string): void => {
    const v = captureView()
    setBookmarks((b) => [...b, { name, center: v.center, zoom: v.zoom }])
    setBookmarksDirty(true)
  }
  const renameBookmark = (i: number, name: string): void => {
    setBookmarks((b) => b.map((x, j) => (j === i ? { ...x, name } : x)))
    setBookmarksDirty(true)
  }
  const updateBookmarkExtent = (i: number): void => {
    const v = captureView()
    setBookmarks((b) => b.map((x, j) => (j === i ? { ...x, center: v.center, zoom: v.zoom } : x)))
    setBookmarksDirty(true)
  }
  const reorderBookmark = (i: number, dir: -1 | 1): void => {
    setBookmarks((b) => {
      const j = i + dir
      if (j < 0 || j >= b.length) return b
      const next = b.slice()
      const tmp = next[i]
      next[i] = next[j]
      next[j] = tmp
      return next
    })
    setBookmarksDirty(true)
  }
  const deleteBookmark = (i: number): void => {
    setBookmarks((b) => b.filter((_, j) => j !== i))
    setBookmarksDirty(true)
  }

  // Feature search (decision-feature-search.md) — jump to a Locate result. Points fly to the
  // representative point; lines/polygons fit the returned bbox (degenerate bbox ⇒ treat as a point).
  // The feature popup + highlight are wired separately (s3).
  const flyToFeature = useCallback((r: FeatureSearchResult): void => {
    const map = mapRef.current
    if (!map) return
    if (r.bbox) {
      const [minx, miny, maxx, maxy] = r.bbox
      if (minx !== maxx || miny !== maxy) {
        map.fitBounds(
          [
            [minx, miny],
            [maxx, maxy],
          ],
          { padding: 64, maxZoom: 18, essential: true },
        )
        return
      }
    }
    if (r.point) map.flyTo({ center: r.point, zoom: Math.max(map.getZoom(), 18), essential: true })
  }, [])

  // Feature search result click (s3): fly/fit, then open the SAME identify popup + active-feature highlight
  // the map click uses. We fetch the one feature (fetchOne carries geometry) to seed the hit's highlight
  // geometry; loadFeaturePage re-fetches fresh props for the popup body, so edit/move/delete/details all work
  // exactly as a clicked feature. Anchored at the representative point (r.point), inside the feature.
  async function openFeatureResult(r: FeatureSearchResult): Promise<void> {
    flyToFeature(r)
    const config = byId(r.layer_id)
    const token = tokenRef.current
    if (!config || !token || !r.point) return
    const coords: [number, number] = r.point
    try {
      const feature = await config.api.fetchOne(r.id, token)
      const geometry = (feature as { geometry?: GeoJSON.Geometry }).geometry
      const hit: FeatureHit = {
        defId: r.layer_id,
        featureId: r.id,
        props: feature as Record<string, unknown>,
        geometry: geometry ?? { type: 'Point', coordinates: coords },
      }
      openIdentifyPopup([{ kind: 'feature', hit }], coords)
    } catch (err) {
      // The fly already happened; a fetch failure just means no popup — surface it softly, don't throw.
      setMapNotice(
        err instanceof ApiError ? `Could not open feature (HTTP ${err.status})` : 'Could not open feature',
      )
    }
  }

  // Current map viewport as "xmin,ymin,xmax,ymax" (EPSG:4326) for geocode extent biasing —
  // read live at request time so results scope to whatever the user is looking at.
  const getViewportBBox = useCallback((): string | undefined => {
    const map = mapRef.current
    if (!map) return undefined
    const b = map.getBounds()
    return `${b.getWest()},${b.getSouth()},${b.getEast()},${b.getNorth()}`
  }, [])

  useEffect(() => {
    // Wait for /me/layers: layersRef.current must hold the viewable layers before
    // the 'load' handler builds sources, or we'd request tiles the api would 403.
    // Also wait for the runtime basemap resolution (one anon round-trip) — the style
    // is built once, with whichever archive this deployment actually has.
    if (!capsReady || basemapUrl === null) return
    const container = containerRef.current
    if (!container) return
    ensurePmtilesProtocol()
    const archiveUrl = basemapUrl.startsWith('http')
      ? basemapUrl
      : new URL(basemapUrl, window.location.origin).toString()
    // Unconditional, and deliberately not conditioned on "did we previously see a 404": a map must
    // never inherit an earlier map's failed header promise, whatever caused it (our own
    // unprovisioned build, or the api being briefly down). See evictPmtilesArchive.
    evictPmtilesArchive(archiveUrl)

    // Initial flavor is always Light (the switcher swaps it in place after build, so this
    // once-only effect needn't depend on `basemap` state).
    const baseLayers = basemapLayers('protomaps', LIGHT, { lang: 'en' })
    const map = new maplibregl.Map({
      container,
      style: {
        version: 8,
        glyphs: GLYPHS,
        sprite: SPRITE,
        sources: {
          protomaps: {
            type: 'vector',
            url: `pmtiles://${archiveUrl}`,
            attribution:
              '<a href="https://protomaps.com">Protomaps</a> © <a href="https://openstreetmap.org">OpenStreetMap</a>',
          },
        },
        layers: baseLayers,
      },
      // V094: a set org default extent beats the world frame (new-map routes only — the ref is
      // never populated on a saved-map route). padding matches the coverage fit's below, so a
      // seeded extent and a coverage fit frame the same bbox identically.
      ...(orgExtentRef.current !== null
        ? { bounds: orgExtentRef.current, fitBoundsOptions: { padding: 32 } }
        : { center: DEFAULT_CENTER, zoom: DEFAULT_ZOOM }),
      // Always the compact (i) button: the default is responsive
      // (full text bar on wide maps), and the coordinate readout is seated to its LEFT — a
      // deterministic 24px button keeps that geometry true at every viewport width.
      attributionControl: { compact: true },
      transformRequest: (url) => {
        // Auth-gated MVT vector tiles AND raster tilejson/tiles (V045+) are fetched with the bearer
        // token. Match by origin + path fragment so it catches BOTH the unversioned `/rasters/...`
        // tilejson the SPA requests AND the `/api/v1/rasters/.../tiles/...` URLs the tilejson points
        // at. MapLibre calls this for every request.
        if (url.startsWith(apiBaseAbsolute()) && (url.includes('/tiles/') || url.includes('/tiles-dyn/') || url.includes('/rasters/'))) {
          const token = tokenRef.current
          return token ? { url, headers: { Authorization: `Bearer ${token}` } } : { url }
        }
        return undefined
      },
    })
    mapRef.current = map
    setMapBuilt((n) => n + 1)
    // Dev-only handle for console/tooling debugging (never shipped behavior — tree-shaken in prod).
    if (import.meta.env.DEV) (window as unknown as { __talusMap?: maplibregl.Map }).__talusMap = map
    basemapLayerIdsRef.current = baseLayers.map((l) => l.id)
    // Embed only (visually unchanged, ruled 2026-08-25): the main map's zoom moved into the
    // Navigate cluster of the right rail (handoff-map-2a §3.1) and its scale into the status
    // strip; the chrome-less embed keeps the original bottom-left MapLibre stack.
    if (embed) map.addControl(new maplibregl.NavigationControl(), 'bottom-left')
    if (import.meta.env.DEV) {
      // Dev-only debugging handle (browser-pane/manual console inspection of sources/layers).
      ;(window as unknown as { __talusMap?: maplibregl.Map }).__talusMap = map
    }
    // One post-layout resize: in the embed shell the container can measure 0 at construction
    // (maplibre then pins its 400×300 default and, observed live 2026-07-23, does not recover
    // on its own). A frame later the shell has its real size; harmless everywhere else.
    requestAnimationFrame(() => map.resize())

    // Open ON the provisioned jurisdiction (clean-box tour finding, 2026-08-19): the basemap is
    // a bbox-capped extract by design (§6 — a jurisdiction, not a continent), so the CONUS
    // default showed a county-sized tile patch adrift in gray. Read the archive's own header
    // bounds and fit to them — an Ohio deployment opens on Ohio. Guards: skip when
    // unprovisioned (notice + DEFAULT_CENTER stand), skip world-scale archives (default view is
    // already right), and only fit while the view is still pristine — a saved map's
    // initialView jump or an early user pan wins.
    // V094: an org default extent applied at construction makes this fit unnecessary. The
    // pristine guard below would skip it anyway (bounds moved the centre off DEFAULT_CENTER),
    // but that is a coincidence of coordinates — state the intent with a real condition.
    // The same header read also tells the map where the downloaded basemap ENDS, so the read no
    // longer depends on the org extent; only the fit does. `checkCoverage` runs once the box is
    // known and after every move.
    const checkCoverage = (): void => {
      const cov = coverageRef.current
      if (cov === null) return setOutsideCoverage(false)
      const v = map.getBounds()
      setOutsideCoverage(
        v.getEast() < cov[0] || v.getWest() > cov[2] || v.getNorth() < cov[1] || v.getSouth() > cov[3],
      )
    }
    map.on('moveend', checkCoverage)
    if (!basemapAbsent) {
      new PMTiles(archiveUrl)
        .getHeader()
        .then((h) => {
          const spansWorld = h.maxLon - h.minLon > 120 // world/continent build — default view stands
          const degenerate = h.maxLon <= h.minLon || h.maxLat <= h.minLat
          coverageRef.current = spansWorld || degenerate ? null : [h.minLon, h.minLat, h.maxLon, h.maxLat]
          checkCoverage()
          if (orgExtentRef.current !== null) return // V094: the org extent already framed the view
          const c = map.getCenter()
          const pristine =
            Math.abs(c.lng - DEFAULT_CENTER[0]) < 1e-6 && Math.abs(c.lat - DEFAULT_CENTER[1]) < 1e-6
          if (spansWorld || degenerate || !pristine) return
          map.fitBounds(
            [
              [h.minLon, h.minLat],
              [h.maxLon, h.maxLat],
            ],
            { padding: 32, duration: 0 },
          )
        })
        .catch(() => {
          /* header unreadable — the DEFAULT_CENTER default stands */
        })
    }
    // Live scale bar — embed only (decision-map-embed.md F4); the main map shows the ratio
    // scale in the status strip instead. Units are the org default (V094 fork C — metric unless
    // an admin picks US Standard), riding the same anonymous fetch construction gates on.
    if (embed) map.addControl(new maplibregl.ScaleControl({ maxWidth: 120, unit: orgUnitsRef.current }), 'bottom-left')
    // Coordinate readout (main map only — embeds stay minimal): lat, lon at 4 dp, DOM-direct.
    map.on('mousemove', (e) => {
      const el = coordsElRef.current
      if (el) el.textContent = `${e.lngLat.lat.toFixed(4)}, ${e.lngLat.lng.toFixed(4)}`
    })
    // Status-strip zoom + ratio scale (handoff-map-2a §3.4), DOM-direct like the coordinate
    // cell. Scale is the web-mercator denominator at the view center for 96 css-px/inch,
    // rounded to 3 significant figures.
    const updateZoomScale = (): void => {
      const zEl = zoomElRef.current
      const sEl = scaleElRef.current
      if (!zEl || !sEl) return
      const z = map.getZoom()
      zEl.textContent = `z ${z.toFixed(1)}`
      const metersPerPx = (156543.03392 * Math.cos((map.getCenter().lat * Math.PI) / 180)) / 2 ** z
      const denom = (metersPerPx * 96) / 0.0254
      const mag = 10 ** Math.max(0, Math.floor(Math.log10(denom)) - 2)
      sEl.textContent = `1:${(Math.round(denom / mag) * mag).toLocaleString()}`
    }
    map.on('move', updateZoomScale)
    updateZoomScale()
    // Remember the last viewport (w,s,e,n) so Admin → Settings → Basemap can offer "use last
    // map view" as the extract bbox (decision-basemap-provisioning.md F6 — you can't draw a
    // bbox on a map you don't have yet, but a browsed-to viewport is a fine seed).
    map.on('moveend', () => {
      const b = map.getBounds()
      try {
        localStorage.setItem(
          'talus:lastMapViewport',
          `${b.getWest().toFixed(5)},${b.getSouth().toFixed(5)},${b.getEast().toFixed(5)},${b.getNorth().toFixed(5)}`,
        )
      } catch {
        // storage full/blocked — the grab button just stays unavailable
      }
    })
    // Geolocate ("locate me"): opt-in centering on the user's location (~z15 ≈ 1:25k) — the New
    // Map's "get me to my area" tool. NO auto-prompt on load; only fires when the user clicks it.
    // Kept as a real control (dot + permission flow are MapLibre's), but on the main map its
    // bottom-left button is hidden by CSS — the Navigate cluster's locate button trigger()s it.
    const geolocate = new maplibregl.GeolocateControl({
      positionOptions: { enableHighAccuracy: true },
      fitBoundsOptions: { maxZoom: 15 },
      trackUserLocation: false,
      showUserLocation: true,
    })
    geolocateCtlRef.current = geolocate
    map.addControl(geolocate, 'bottom-left')

    map.on('load', () => {
      for (const def of layersRef.current) registerLayerOnMap(map, def)
      // A saved-map restore that resolved before this map existed parked its basemap+viewport apply.
      // It runs FIRST so a saved map's own basemap always beats the cold-start default (F4.2).
      pendingApplyViewRef.current?.()
      pendingApplyViewRef.current = null
      // Reconcile canvas against state. The style above is ALWAYS constructed on LIGHT, and until
      // now nothing painted the seeded basemap onto it — so the T41 theme seed (`6a420da`) never
      // reached the canvas: a dark-theme New Map rendered Light while the switcher highlighted
      // Dark. This is the one place that guarantees they agree, and it is what makes the F1
      // cold-start imagery default visible at all. switchBasemap no-ops when its target already
      // equals state, so a parked restore may have painted nothing; paintBasemap has no such guard.
      if (basemapRef.current !== 'light') paintBasemapRef.current(map, basemapRef.current)
    })

    // One map-level click handler. In create mode it places a new feature (as before). Otherwise it is
    // the multi-layer IDENTIFY: gather every feature at the click across all interactive layers, deduped
    // and topmost-first, and open the popup. Today it opens the top hit (behaviour-preserving for N=1);
    // the paged popup (c3) will walk the whole set. The popup anchors at the click point for every hit.
    map.on('click', (e) => {
      // An armed vertices pick owns EVERY click, including while its seeded session is live
      // (busyClick would swallow the re-seed click) — decision-editing-toolbar-f4.md F2.
      if (shapePickRef.current) {
        handlersRef.current.onShapePick(e.point)
        return
      }
      if (busyClickRef.current) return
      // Armed Select owns EVERY click (decision-select-features.md F1): the box-draw session
      // handles mousedown/up, and this stops the trailing click from also firing identify.
      if (selectArmedRef.current) return
      const createId = createLayerIdRef.current
      if (createId) {
        // Place surface snapping (decision-web-editor-snapping.md surface 3): the click lands on
        // the exact stored target coordinate when one is in tolerance (engine off/empty = no-op).
        const snapped = snapEngineRef.current?.snap(e.lngLat.lng, e.lngLat.lat)
        handlersRef.current.onCreateAt(createId, snapped?.[0] ?? e.lngLat.lng, snapped?.[1] ?? e.lngLat.lat)
        return
      }
      // Line/polygon draw + raster export box-draw own their clicks — never identify under them.
      if (drawLayerIdRef.current || drawingExportRef.current) return
      // Sketch (decision-sketch-layer.md): an active tool/reshape session owns its clicks; with
      // tools idle, a click ON a note opens its edit card instead of identify (its "popup").
      if (sketchToolRef.current || reshapingSketchRef.current) return
      const skLayers = SKETCH_LAYERS.filter((l) => map.getLayer(l))
      if (skLayers.length > 0) {
        const box: [maplibregl.PointLike, maplibregl.PointLike] = [
          [e.point.x - 4, e.point.y - 4],
          [e.point.x + 4, e.point.y + 4],
        ]
        const hit = map
          .queryRenderedFeatures(box, { layers: skLayers })
          .find((f) => typeof f.properties?.id === 'string')
        if (hit) {
          handlersRef.current.onSketchClick(hit.properties!.id as string)
          return
        }
      }
      const lngLat: [number, number] = [e.lngLat.lng, e.lngLat.lat]
      const featureHits = collectFeatureHits(map, e.point, renderLayerToDefRef.current)
      // Selection tap (read via ref — this handler registers once): top hit or null-to-clear.
      onFeatureClickRef.current?.(featureHits[0] ?? null)
      const rasterHits = collectRasterHits(
        lngLat, selectedRef.current, rasterMetaRef.current, visibleRef.current, rasterBoundsRef.current,
        availRastersRef.current,
      )
      // Vectors first (topmost render order), then rasters (which always draw below vectors).
      const items: IdentifyItem[] = [
        ...featureHits.map((hit) => ({ kind: 'feature' as const, hit })),
        ...rasterHits,
      ]
      if (items.length === 0) return
      handlersRef.current.onIdentify(items, lngLat)
    })
    // Right-click anywhere on the map → "What's here?" coordinate readout (E2, decision-whats-here.md).
    // Suppressed during a create/move interaction so it never competes with placing a feature.
    map.on('contextmenu', (e) => {
      e.originalEvent?.preventDefault()
      // Armed Select also owns right-click: macOS surfaces Ctrl+click (the REMOVE modifier)
      // as a contextmenu event, so What's-here must stay quiet during a session.
      if (createLayerIdRef.current || busyClickRef.current || selectArmedRef.current) return
      handlersRef.current.onWhatsHere(e.lngLat.lng, e.lngLat.lat)
    })
    // Talus 35 (re-scoped to web-editor offline-resilience): offline, uncached map data fails to
    // fetch *by design* — the basemap PMTiles (range requests) and never-visited feature tiles
    // are not cached (see the offline notice below + decision-disconnected-editing.md §11). MapLibre
    // reports those as tile/source load errors here; swallow them so a momentary connectivity drop
    // doesn't raise the persistent "Map error" banner. Only genuine, non-network errors surface.
    map.on('error', (e) => {
      const err = (e as { error?: Error & { status?: number } }).error
      const networkish =
        !navigator.onLine ||
        'sourceId' in (e as object) ||
        typeof err?.status === 'number' ||
        /fetch|network|load|tile|no-?response|abort/i.test(err?.message ?? '')
      if (networkish) {
        if (import.meta.env.DEV) console.debug('[map] offline/tile load error (suppressed):', err?.message)
        return
      }
      setMapError(err?.message ?? 'Map error')
    })

    return () => {
      map.remove()
      mapRef.current = null
    }
    // capsReady and basemapUrl each settle once (false->true / null->string); the guard above
    // no-ops until BOTH are known, so the map is built exactly once — after the viewable layer
    // set AND the runtime basemap resolution.
    // registerLayerOnMap is a stable useCallback (never changes identity), so listing it
    // satisfies exhaustive-deps without re-running this once-only build. basemapAbsent is
    // deliberately NOT a dep: this effect's cleanup calls map.remove(), so re-running it on a
    // basemap install would destroy and rebuild the live map.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [capsReady, basemapUrl, registerLayerOnMap])

  // Register layers that become viewable AFTER the one-time map load — e.g. a slot just
  // claimed via the Create-layer dialog (caps refresh -> viewable grows). The load loop
  // above only runs once, so without this a new layer never gets a source/render layer/
  // handlers and can't draw or accept points until a reload. registerLayerOnMap's
  // getSource guard makes this idempotent: already-on-map layers are skipped, only the
  // new one registers. A freshly-registered layer gets its current visibility applied so
  // it honors the toggle; existing layers' visibility/toggle state is never touched.
  useEffect(() => {
    const map = mapRef.current
    if (!map) return
    const run = (): void => {
      for (const def of selectedDefs) {
        const had = map.getSource(srcId(def.id)) !== undefined
        registerLayerOnMap(map, def)
        if (!had) {
          const vis = visibleRef.current[def.id] ?? true
          for (const lid of subLayerIds(def.id)) {
            if (map.getLayer(lid)) {
              map.setLayoutProperty(lid, 'visibility', vis ? 'visible' : 'none')
            }
          }
        }
      }
    }
    // Registration is additive + idempotent (the getSource guard); the cleanup only drops a pending
    // wait so a re-run doesn't stack listeners.
    return whenStyleReady(map, run)
  }, [selectedDefs, registerLayerOnMap])

  // Sync vector-layer visibility REACTIVELY from `visible`. The registration effect above applies
  // visibility only to freshly-added sources ("existing layers are never touched" — right for the
  // caps-grew case it was built for), but a saved-map restore lands on layers the build-time load
  // loop ALREADY registered — their restored `visible: false` was skipped and they kept drawing
  // (B5b 2026-08-12: Sidewalk drew with its box unchecked). Idempotent over every selected def.
  useEffect(() => {
    const map = mapRef.current
    if (!map) return
    for (const def of selectedDefs) {
      const vis = (visible[def.id] ?? true) ? 'visible' : 'none'
      for (const lid of subLayerIds(def.id)) {
        if (map.getLayer(lid)) map.setLayoutProperty(lid, 'visibility', vis)
      }
    }
  }, [visible, selectedDefs])

  // Composition-surface handle (decision-story-maps.md s3): expose flyTo + a visibility override
  // through onMapApi. Overrides ride the SAME `visible` state the TOC uses, so the reactive sync
  // effect above applies them; the baseline snapshot (taken at the first override) lets null
  // restore the map's own visibility. Note: a layer not yet in `visible` when an override lands
  // defaults visible (the ?? true above) — consumers re-apply on step changes, which covers the
  // restore-in-progress window.
  const visBaselineRef = useRef<Record<string, boolean> | null>(null)
  // Live legend feed (ref for the same reason): recomputed only when the memoized TOC or the
  // visibility map actually changes — never per render.
  const onLegendRef = useRef(onLegend)
  onLegendRef.current = onLegend
  const onViewRestoredRef = useRef(onViewRestored)
  onViewRestoredRef.current = onViewRestored
  useEffect(() => {
    onLegendRef.current?.(buildLegendEntries(tocItems, { ...visible, __sketch: sketchVisible }))
  }, [tocItems, visible, sketchVisible])
  useEffect(() => {
    if (!onMapApi) return
    const readView = (map: maplibregl.Map): AssetMapView => {
      const c = map.getCenter()
      return { center: [c.lng, c.lat], zoom: map.getZoom(), bearing: map.getBearing(), pitch: map.getPitch() }
    }
    onMapApi({
      flyTo: (center, zoom, opts) => {
        const map = mapRef.current
        if (!map) return
        const target = {
          center,
          zoom,
          ...(opts?.bearing !== undefined ? { bearing: opts.bearing } : {}),
          ...(opts?.pitch !== undefined ? { pitch: opts.pitch } : {}),
        }
        if (opts?.animate === false) map.jumpTo(target)
        else map.flyTo(target)
      },
      getView: () => {
        const map = mapRef.current
        return map ? readView(map) : null
      },
      onMove: (cb) => {
        const map = mapRef.current
        if (!map) return () => {}
        const handler = (): void => cb(readView(map))
        map.on('move', handler)
        return () => {
          map.off('move', handler)
        }
      },
      setVisibleLayers: (ids) => {
        if (ids === null) {
          if (visBaselineRef.current !== null) {
            setVisible(visBaselineRef.current)
            visBaselineRef.current = null
          }
          return
        }
        if (visBaselineRef.current === null) visBaselineRef.current = { ...visibleRef.current }
        const want = new Set(ids)
        setVisible((prev) => {
          const next: Record<string, boolean> = {}
          for (const key of new Set([...Object.keys(prev), ...ids])) next[key] = want.has(key)
          return next
        })
      },
      setLayerFilters: (layerId, conds) => {
        // Deliberately NO membership guard: the filter state must land even before the layer's
        // source exists (a board with persisted cross-filters pushes at api-delivery time, which
        // races source registration). reconcileLayerRender no-ops safely on a missing source,
        // and the source-ADD path builds its tile URL from filtersRef — so an early push is
        // simply picked up when the layer arrives. A layerId never on this map stores inert
        // state and renders nothing.
        applyLayerFilter(layerId, conds ?? [])
      },
    })
  }, [onMapApi, applyLayerFilter])

  // Render raster operational layers (V045+). Per raster in `rasterMeta`: add/update a MapLibre raster
  // source+layer from its gated tilejson (auth via transformRequest), sync opacity (rasterMeta) +
  // visibility (the shared `visible` map); remove rasters no longer present. The tilejson URL carries
  // the colormap (single-band restyle) — a source can't change URL in place, so a colormap change
  // REBUILDS the source+layer (tracked via rasterUrlRef). Draw ORDER is owned by `restack`, so layers
  // are added without a beforeId and the restack effect interleaves them with vectors. Idempotent.
  const rasterUrlRef = useRef<Record<string, string>>({})
  useEffect(() => {
    const map = mapRef.current
    if (!map) return
    const rsrc = (id: string): string => `raster-src-${id}`
    const rlyr = (id: string): string => `raster-lyr-${id}`
    const tjUrl = (id: string): string => {
      const cm = rasterMeta[id].colormap
      return cm
        ? `${apiBaseAbsolute()}/rasters/${id}/tilejson.json?bidx=1&colormap=${encodeURIComponent(cm)}`
        : `${apiBaseAbsolute()}/rasters/${id}/tilejson.json`
    }
    const teardown = (id: string): void => {
      if (map.getLayer(rlyr(id))) map.removeLayer(rlyr(id))
      if (map.getSource(rsrc(id))) map.removeSource(rsrc(id))
      delete rasterUrlRef.current[id]
      delete rasterBoundsRef.current[id]
    }
    const apply = (): void => {
      const ids = Object.keys(rasterMeta)
      const wanted = new Set(ids)
      for (const layer of map.getStyle().layers ?? []) {
        if (layer.id.startsWith('raster-lyr-')) {
          const id = layer.id.slice('raster-lyr-'.length)
          if (!wanted.has(id)) teardown(id)
        }
      }
      for (const id of ids) {
        // An EXTERNAL raster (decision-add-from-url.md) renders from its remote tile TEMPLATE (xyz, or
        // ArcGIS export per 256px tile) instead of a Talus tilejson; no /info (extent unknown → it never
        // joins identify), no colormap/export. Everything else (opacity/visibility/order) is shared.
        const externalTiles = rasterMeta[id].externalTiles
        // Cache this raster's extent once (the identify click uses it to test whether a click covers it).
        if (!externalTiles && !rasterBoundsRef.current[id]) {
          const token = tokenRef.current
          if (token)
            void fetchRasterInfo(id, token)
              .then((info) => {
                rasterBoundsRef.current[id] = info.bounds
              })
              .catch(() => {})
        }
        const url = externalTiles ?? tjUrl(id)
        // Colormap changed => the source URL changed; MapLibre can't swap it in place, so rebuild.
        if (map.getSource(rsrc(id)) && rasterUrlRef.current[id] !== url) teardown(id)
        if (!map.getSource(rsrc(id))) {
          map.addSource(
            rsrc(id),
            externalTiles
              ? { type: 'raster', tiles: [externalTiles], tileSize: 256 }
              : { type: 'raster', url, tileSize: 256 },
          )
          rasterUrlRef.current[id] = url
        }
        if (!map.getLayer(rlyr(id))) {
          map.addLayer({ id: rlyr(id), type: 'raster', source: rsrc(id), paint: { 'raster-opacity': rasterMeta[id].opacity } })
        } else {
          map.setPaintProperty(rlyr(id), 'raster-opacity', rasterMeta[id].opacity)
        }
        map.setLayoutProperty(rlyr(id), 'visibility', (visible[id] ?? true) ? 'visible' : 'none')
      }
      // A rebuilt (colormap-changed) raster layer is re-added on top; re-apply the unified draw order
      // so it returns to its TOC position (a colormap change doesn't touch `selected`, so the dedicated
      // restack effect won't fire for it).
      restack(map, selected)
    }
    return whenStyleReady(map, apply)
    // mapBuilt: a raster recorded before the map existed is added once it does (see its declaration).
  }, [rasterMeta, visible, selected, restack, mapBuilt])

  // Refresh on-map EXTERNAL layers' GeoJSON for the current viewport on every pan/zoom (debounced),
  // since their features are bbox-scoped + capped. Externals are the only GeoJSON-refreshed kind
  // left — hosted views / filtered / labeled layers ride dynamic MVT (decision-dynamic-mvt.md), and
  // MapLibre fetches all tiled layers itself. The initial load fires in registerLayerOnMap; this is
  // the pan/zoom refresh.
  useEffect(() => {
    const map = mapRef.current
    if (!map) return
    const extIds = selectedDefs.filter((d) => !!externals[d.id]).map((d) => d.id)
    if (extIds.length === 0) return
    let timer: number | undefined
    const refreshAll = (): void => {
      for (const id of extIds) void refreshExternalLayer(map, id)
    }
    const onMoveEnd = (): void => {
      window.clearTimeout(timer)
      timer = window.setTimeout(refreshAll, 250)
    }
    map.on('moveend', onMoveEnd)
    return () => {
      window.clearTimeout(timer)
      map.off('moveend', onMoveEnd)
    }
  }, [selectedDefs, externals, refreshExternalLayer])

  // Keep the map's draw order in sync with `selected` after a reorder or a fresh add (new layers
  // register on top of the GL stack; restack re-applies the TOC order — selected[0] on top).
  useEffect(() => {
    const map = mapRef.current
    if (!map) return
    return whenStyleReady(map, () => restack(map, selected))
  }, [selected, restack])

  // Talus 20 — open a saved web map (/map/:webMapId). Runs ONCE per id (restoredRef guards it,
  // and a save-then-navigate pre-sets the ref so we don't re-fetch the map we just saved).
  // Restores the layer set (skipping any the caller can't view — F5), visibility, basemap, and
  // viewport. caps must be loaded first so the F5 skip is accurate.
  useEffect(() => {
    // No token required (decision-map-embed.md F0): an anon opener restores a PUBLIC map — the
    // server gates (404 private) and the doc carries layersMeta for its public layers.
    if (!webMapId || !capsReady) return
    // Already restored (a save-then-navigate pre-sets the ref): the basemap is known, nothing to
    // wait for — drop the hold rather than leaving it up for the timeout.
    if (restoredRef.current === webMapId) {
      releaseRestoreHold()
      return
    }
    restoredRef.current = webMapId
    let cancelled = false
    fetchWebMap(webMapId, accessToken)
      .then(async (wm) => {
        if (cancelled) return
        // Draft autosave (G4.2): a Restore replays this exact path with the draft's definition; the
        // server document still supplies layersMeta/caps and the baseline stays the SERVER's, so the
        // restored state reads as unsaved until the user saves it.
        const fromDraft = pendingDraftRef.current !== null
        const def: WebMapDefinition = pendingDraftRef.current ?? wm.definition
        pendingDraftRef.current = null
        // The view-gate source: authed = the caller's /me/layers caps; anon = the map doc's own
        // layersMeta (the public subset, server-computed with the same fail-closed probe). For
        // anon it ALSO becomes `caps`, so the viewable-defs memo manufactures slot/view defs.
        const capsFor: Record<string, MyLayer> = accessToken
          ? (caps ?? {})
          : Object.fromEntries((wm.layersMeta ?? []).map((m) => [m.id, m]))
        if (!accessToken) setCaps(capsFor)
        const ops = def.operationalLayers
        // Hybrid resolution (decision-external-catalog-items.md §2.2): resolve every referenced
        // item ONCE up front. Readable → the ITEM's connection spec wins (central url/where fixes
        // propagate); 404 (deleted OR unshared — indistinguishable by design) → the op's snapshot
        // renders. Best-effort and parallel; a failure never blocks the map.
        const resolvedRefs = new Map<string, ExternalRef>()
        const refIds = [
          ...new Set(
            ops.flatMap((o) => (o.kind === 'external' && o.externalItemId ? [o.externalItemId] : [])),
          ),
        ]
        if (refIds.length && accessToken) {  // anon: snapshot fallback by design (refs are authed)
          await Promise.all(
            refIds.map((id) =>
              fetchExternalRef(id, accessToken)
                .then((r) => resolvedRefs.set(id, r))
                .catch(() => {
                  /* snapshot fallback */
                }),
            ),
          )
        }
        if (cancelled) return
        // Rebuild the UNIFIED order in place: a vector op is kept iff the caller can view it (caps); a
        // raster op is always kept (its gated tiles 403 and draw nothing if the caller lacks access).
        const keptIds: string[] = []
        const vis: Record<string, boolean> = {}
        const flt: Record<string, FilterCondition[]> = {}
        const lbl: Record<string, WebMapLabelConfig> = {}
        const pop: Record<string, WebMapPopupConfig> = {}
        const ttl: Record<string, string> = {}
        const rmeta: Record<string, { title: string; opacity: number; colormap?: string; externalTiles?: string }> = {}
        const ext: Record<string, ExternalLayerSpec> = {}
        let vskipped = 0
        for (const o of ops) {
          if (o.kind === 'external' && o.externalId && o.externalKind && o.url) {
            // External references restore unconditionally — they're public remote URLs, not Talus
            // items (no caps gate). Raster kinds also mirror into rasterMeta for the render effect.
            // A resolved catalog item overrides the CONNECTION spec (url/where/geometryType — the
            // hybrid model's central-fix propagation); title/color stay the op's own (per-map
            // presentation, never written back). Unresolved (no item / 404) → the snapshot as-is.
            const item = o.externalItemId ? resolvedRefs.get(o.externalItemId) : undefined
            const spec: ExternalLayerSpec = {
              externalId: o.externalId,
              externalKind: o.externalKind,
              url: item?.url ?? o.url,
              title: o.title ?? o.externalId,
              geometryType: item ? (item.geometryType ?? undefined) : o.geometryType,
              color: o.color,
              where: item ? (item.where ?? undefined) : o.where,
              ...(o.externalItemId ? { externalItemId: o.externalItemId } : {}),
            }
            keptIds.push(o.externalId)
            vis[o.externalId] = o.visible
            ext[o.externalId] = spec
            // Rail parity: restore the external's saved filter/labels/popup (same pattern as layers).
            if (o.filters?.length) flt[o.externalId] = o.filters
            if (o.labels) lbl[o.externalId] = o.labels
            if (o.popup) pop[o.externalId] = o.popup
            if (o.externalKind === 'xyz' || o.externalKind === 'arcgis-image') {
              rmeta[o.externalId] = { title: spec.title, opacity: o.opacity ?? 1, externalTiles: externalRasterTemplate(spec) }
            }
          } else if (o.kind === 'raster' && o.rasterId) {
            keptIds.push(o.rasterId)
            vis[o.rasterId] = o.visible
            rmeta[o.rasterId] = { title: o.rasterId, opacity: o.opacity ?? 1, colormap: o.colormap }
          } else if (o.layerId && capsFor[o.layerId]?.canView) {
            keptIds.push(o.layerId)
            vis[o.layerId] = o.visible
            // Feature-filter: restore the layer's saved conditions (decision-feature-filter.md). Set the ref
            // synchronously too, so the registration fired by setSelected renders the layer already filtered.
            if (o.filters?.length) flt[o.layerId] = o.filters
            // Labels: same restore pattern — a labeled slot must register straight onto the GeoJSON path.
            if (o.labels) lbl[o.layerId] = o.labels
            // Popup config: click-time state, no registration dependency — restore for the identify path.
            if (o.popup) pop[o.layerId] = o.popup
            // Map-only rename: restore the display-name override.
            if (o.title) ttl[o.layerId] = o.title
          } else if (o.kind !== 'raster') {
            vskipped++
          }
        }
        filtersRef.current = flt
        setLayerFilters(flt)
        setTableSelections({}) // F2 lifetime: a map switch/restore clears every row selection
        disarmSelect()
        labelsRef.current = lbl
        setLayerLabels(lbl)
        popupsRef.current = pop
        setLayerPopups(pop)
        setLayerTitles(ttl)
        // Set the ref synchronously (like filters) — the registration fired by setSelected must find
        // the external spec when its initial refreshViewLayer runs.
        externalsRef.current = ext
        setExternals(ext)
        setSelected(keptIds)
        setVisible(vis)
        setRasterMeta(rmeta)
        // Tables (decision-map-tables.md): restore refs the caller can view that are still tables;
        // anything else is silently skipped into the same unavailable count (F5 behavior). Deduped —
        // a hand-crafted definition could repeat an id (duplicate React keys otherwise).
        const tblIds = [...new Set((def.tables ?? []).map((t) => t.layerId))]
        const keptTables = tblIds.filter((id) => capsFor[id]?.canView && capsFor[id]?.geomType === 'none')
        vskipped += tblIds.length - keptTables.length
        setMapTables(keptTables)
        setOpenTableId(null)
        setBookmarks(def.bookmarks ?? []) // default [] — pre-bookmarks maps have no key
        setBookmarksDirty(false)
        setFieldReady(def.field?.enabled === true) // absent = not a field map (opt-in)
        setSketch(def.sketch ?? []) // default [] — pre-sketch maps have no key
        setSketchVisible(true)
        setSelectedSketchId(null)
        setReshapingSketchId(null)
        setSavedMap({ id: wm.id, title: wm.title, canManage: wm.canManage })
        // Unsaved-work baseline: what a Save would write for the just-restored state — built from the
        // restore LOCALS, not React state (which hasn't rendered yet, and the saved basemap may only
        // apply once the style loads). Same builder as currentDefinition, so the compare is exact.
        baselineSigRef.current = defSignature(
          buildDefinition(
            {
              // Resolved against the offered list as the apply below is, so a map whose streamed
              // basemap has been withdrawn does not open "unsaved". Only once the list is KNOWN:
              // if this runs before the status fetch has landed (the race the parked apply exists
              // for), the saved name stands — the canvas is still resolved at apply time, and at
              // worst a withdrawn-basemap map then opens showing a change to save, which is true.
              basemap: streamedKnownRef.current
                ? resolveSaved(
                    sanitizeBasemap(def.basemap),
                    streamedRef.current,
                    orgDefaultRef.current,
                    listsDownloadedRef.current,
                    !basemapAbsentRef.current,
                  )
                : sanitizeBasemap(def.basemap),
              order: keptIds,
              visible: vis,
              rasterMeta: rmeta,
              externals: ext,
              layerFilters: flt,
              layerLabels: lbl,
              layerPopups: pop,
              layerTitles: ttl,
              mapTables: keptTables,
              bookmarks: def.bookmarks ?? [],
              sketch: def.sketch ?? [],
              fieldReady: def.field?.enabled === true,
            },
            def.initialView,
          ),
        )
        if (fromDraft) {
          baselineSigRef.current = '__draft__' // never equal to a real signature: dirty until saved
        } else if (accessToken && wm.canManage) {
          const d = readDraft(wm.id)
          if (d && defSignature(d.definition) !== baselineSigRef.current) setDraftOffer(d)
          else if (d) clearDraft(wm.id) // a draft that matches what's saved is just noise
        }
        const skipped = vskipped
        setMapNotice(
          skipped > 0
            ? `${skipped} layer${skipped > 1 ? 's' : ''} in this map ${skipped > 1 ? 'are' : 'is'} unavailable to you and ${skipped > 1 ? 'were' : 'was'} skipped.`
            : null,
        )
        const applyView = (): void => {
          const map = mapRef.current
          if (!map) {
            releaseRestoreHold() // no map to paint; never hold on a canvas that will not arrive
            return
          }
          // The map exists here, so the status fetch has landed and the offered list is real.
          const saved = sanitizeBasemap(def.basemap)
          // A streamed look the portal does not offer is replaced; a downloaded look is kept while
          // there is an area to draw it from, listed or not (§16, G3).
          const shown = resolveSaved(
            saved,
            streamedRef.current,
            orgDefaultRef.current,
            listsDownloadedRef.current,
            !basemapAbsentRef.current,
          )
          switchBasemapRef.current(shown)
          // Say why the map looks different from how it was saved — to the people who can act on
          // it. An embed's viewer can do nothing with the sentence, so it stays quiet there.
          if (shown !== saved && !embed)
            setMapNotice(
              `This map was saved on ${BASEMAP_LABELS[saved]}, which this portal cannot show. Showing ${BASEMAP_LABELS[shown]}.`,
            )
          map.jumpTo({ center: def.initialView.center, zoom: def.initialView.zoom })
          releaseRestoreHold() // the saved basemap and viewport are on the canvas — reveal it
          onViewRestoredRef.current?.() // composition surfaces may drive the camera from here
        }
        const map = mapRef.current
        if (map?.isStyleLoaded()) applyView()
        // 'idle', not 'load': 'load' fires ONCE per map — a listener attached after it fired
        // (isStyleLoaded() can read false during sprite/repaint work) never runs, and the saved
        // basemap + viewport were silently dropped (B5b 2026-08-12). 'idle' re-fires after any
        // pending work, so a late listener always lands.
        else if (map) map.once('idle', applyView)
        // No map yet (restore can win the race against runtime basemap discovery): park the apply
        // for the build effect's load handler.
        else pendingApplyViewRef.current = applyView
      })
      .catch((err) => {
        releaseRestoreHold() // the map will not restore; show the canvas and the notice
        if (!cancelled)
          setMapNotice(
            err instanceof ApiError
              ? `Could not open this map (HTTP ${err.status})`
              : 'Could not open this map',
          )
      })
    return () => {
      cancelled = true
    }
  }, [webMapId, capsReady, accessToken, caps, restoreNonce])

  // Plain /map = a fresh empty New Map: clear any previously-opened saved-map state (so leaving a
  // saved map via the "New Map" nav doesn't keep its layers/title around).
  useEffect(() => {
    if (webMapId) return
    setSelected([])
    setVisible({})
    filtersRef.current = {}
    setLayerFilters({})
    setTableSelections({}) // F2 lifetime: leaving for a fresh New Map drops every row selection
    disarmSelect()
    labelsRef.current = {}
    setLayerLabels({})
    popupsRef.current = {}
    setLayerPopups({})
    setLayerTitles({})
    setSavedMap(null)
    setBookmarks([])
    setBookmarksDirty(false)
    setBookmarksOpen(false)
    setExportOpen(false)
    setSketch([])
    setSketchOpen(false)
    setSketchTool(null)
    setSketchVisible(true)
    setSelectedSketchId(null)
    setReshapingSketchId(null)
    setMapTables([])
    setOpenTableId(null)
    setMapNotice(null)
    restoredRef.current = null
    // Unsaved-work baseline for the fresh New Map: empty content on the CURRENT basemap (via the ref —
    // adding `basemap` as a dep would wipe the map on every basemap switch).
    baselineSigRef.current = freshMapSignature(basemapRef.current)
  }, [webMapId])

  // Feature-filter (decision-feature-filter.md): whoever can edit the MAP manages its filters — a new map is
  // always editable; a saved map only if the opener can manage it. A read-only viewer sees filters applied
  // but no Filter tool. The rail Filter tool is the single entry point (no per-layer funnel in the TOC).
  const mapEditable = !savedMap || savedMap.canManage
  // Unsaved-work guard: dirty = the map's SAVEABLE content differs from the captured baseline (viewport
  // excluded — defSignature). Only warns when the user could actually save (mapEditable); a read-only
  // viewer toggling things loses nothing that was theirs to keep.
  dirtyCheckRef.current = () =>
    mapEditable && baselineSigRef.current !== null && defSignature(currentDefinition()) !== baselineSigRef.current
  // The native leave warning (reload / tab close / navigating off-site) — the AGO "Reload site?" parity.
  // In-app route changes unmount this component (listener removed by the cleanup); blocking THOSE needs
  // the data-router migration (queued — roadmap-backlog.md). Browsers show their own generic text.
  useEffect(() => {
    const onBeforeUnload = (e: BeforeUnloadEvent): void => {
      if (!dirtyCheckRef.current()) return
      e.preventDefault()
      e.returnValue = '' // legacy Chromium requirement for the dialog to appear
    }
    window.addEventListener('beforeunload', onBeforeUnload)
    return () => window.removeEventListener('beforeunload', onBeforeUnload)
  }, [])
  // The IN-APP guard on the same dirty rule (decision-nav-blocker.md): a router navigation that would
  // unmount a dirty map (nav-bar click, SPA back/forward) is held while the Stay/Leave modal asks.
  // Same-path navigations never block — the post-save navigate('/map/<id>') is clean by then anyway,
  // and a search-param change isn't leaving the map. beforeunload (above) covers reload/close/off-site.
  const navBlocker = useBlocker(
    ({ currentLocation, nextLocation }) =>
      currentLocation.pathname !== nextLocation.pathname && dirtyCheckRef.current(),
  )
  // Draft autosave (G4.2): a couple of seconds after the saveable inputs settle, snapshot a dirty saved
  // map to this browser; a clean one clears its draft. Same dirty rule as the leave guards.
  useEffect(() => {
    // While an offer is showing, the just-loaded (clean) server state must NOT clear the draft the
    // user is being asked about — found live 2026-09-18: the load's own settle wiped it before Restore.
    if (!savedMap || !mapEditable || !accessToken || draftOffer !== null) return
    const id = savedMap.id
    const t = window.setTimeout(() => {
      if (dirtyCheckRef.current()) writeDraft(id, currentDefinition())
      else clearDraft(id)
    }, 2000)
    return () => window.clearTimeout(t)
    // currentDefinition reads the same inputs listed here; it is a stable closure over refs + state.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [basemap, selected, visible, rasterMeta, externals, layerFilters, layerLabels, layerPopups, layerTitles, mapTables, bookmarks, sketch, fieldReady, savedMap, mapEditable, accessToken, draftOffer])
  function restoreDraft(): void {
    if (!savedMap || !draftOffer) return
    pendingDraftRef.current = draftOffer.definition
    setDraftOffer(null)
    restoredRef.current = null // let the load effect run again for this id
    setRestoreNonce((n) => n + 1)
  }
  function discardDraft(): void {
    if (savedMap) clearDraft(savedMap.id)
    setDraftOffer(null)
  }
  // The map's filterable vector layers (TOC order) for the rail Filter tool's layer picker.
  // The map's selectable layers for the rail Select tool — the same hosted-vector row set as
  // Filter/Labels (externals are kind 'external': their grid is read-only, no selection concept;
  // rasters/sketch/tables never reach kind 'layer'). An id-less view is armable but inert — its
  // tiles carry no id, so hits resolve to nothing, matching its checkbox-less table.
  // The map's selectable hosted vector layers (TOC order) — membership gates the rail's Select
  // toggle (it arms the CURRENTLY SELECTED layer; the flyout picker is gone — handoff-map-2a §1),
  // and the counts feed its total-selected badge. The inspector needs no per-tab lists: every
  // tocItems 'layer' row is configurable, and the tab dots read layerFilters/Labels/Popups.
  const selectToolLayers = useMemo(
    () =>
      tocItems
        .filter((it): it is Extract<TocItem, { kind: 'layer' }> => it.kind === 'layer')
        .map((it) => ({
          id: it.config.id,
          label: it.config.label,
          count: tableSelections[it.config.id]?.size ?? 0,
        })),
    [tocItems, tableSelections],
  )
  // TOC funnel marks (handoff-map-2a §4): which layers carry an active attribute filter.
  const filteredLayerIds = useMemo(() => new Set(Object.keys(layerFilters)), [layerFilters])

  // Create-mode cursor.
  useEffect(() => {
    const map = mapRef.current
    if (map) map.getCanvas().style.cursor = createLayerId ? 'crosshair' : ''
  }, [createLayerId])
  // F4 vertices pick cursor: pointer while hunting for a feature; the live session restores the
  // default (terra-draw's own vertex cursors take over).
  useEffect(() => {
    const map = mapRef.current
    if (!map || !shapePick || editing !== null) return
    map.getCanvas().style.cursor = 'pointer'
    return () => {
      const m = mapRef.current
      if (m) m.getCanvas().style.cursor = ''
    }
  }, [shapePick, editing])

  // Temporary marker showing where a new feature will drop (colored per layer).
  useEffect(() => {
    const map = mapRef.current
    if (!map || form?.kind !== 'create') return
    const color = byId(form.layerId)?.color ?? '#2da7e8'
    const marker = new maplibregl.Marker({ color }).setLngLat([form.lon, form.lat]).addTo(map)
    return () => {
      marker.remove()
    }
  }, [form])

  function toggleLayer(layerId: string, vis: boolean): void {
    if (layerId === '__sketch') {
      setSketchVisible(vis) // the render effect applies it to the sketch layer family
      return
    }
    const map = mapRef.current
    setVisible((prev) => ({ ...prev, [layerId]: vis }))
    // Toggle the point circle OR the polygon fill+line (or a view's fill+line+circle) together.
    for (const lid of subLayerIds(layerId)) {
      if (map && map.getLayer(lid)) {
        map.setLayoutProperty(lid, 'visibility', vis ? 'visible' : 'none')
      }
    }
    if (!vis) {
      // Confirmed behavior: hiding a layer cancels any in-progress edit on it.
      if (form?.layerId === layerId) {
        setForm(null)
        setFormError(null)
      }
      if (moving?.layerId === layerId) exitMove()
      if (editing?.layerId === layerId) exitEditShape()
      if (popupLayerRef.current === layerId) closePopup()
      if (createLayerId === layerId) setCreateLayerId(null)
      if (drawLayerId === layerId) setDrawLayerId(null)
      if (editTargetRef.current === layerId) setShapePick(false) // target falls back; pick dies with it
    }
  }

  const formConfig = form ? byId(form.layerId) : undefined
  const FormComponent = formConfig?.Form
  // POINT click-to-place create targets (the "Create <label>" buttons). Point-only by nature.
  const editableLayers = configs.filter((c) => c.canEdit && c.geometryType === 'point')
  const createableLayers = editableLayers.filter((c) => visible[c.id] ?? true)
  // Line/polygon slice c6a: layers drawn with terra-draw (line/polygon, editable, visible). Native-storage
  // non-point layers are EXCLUDED — drawing happens in WGS-84 on the map, which can't faithfully place a
  // byte-exact native (projected) geometry; such layers get features via ingest, not hand-drawing.
  const drawableLayers = configs.filter(
    (c) => c.canEdit && (c.geometryType === 'line' || c.geometryType === 'polygon') && !c.nativeStorage,
  )
  const drawCreateLayers = drawableLayers.filter((c) => visible[c.id] ?? true)
  // The map's currently-visible layers = the Locate panel's feature-search scope (id + label +
  // geometry for the result group headers/icons). The server view-gates + filters to owner slots.
  const locateLayers: LocateLayer[] = selectedDefs
    .filter((d) => visible[d.id] ?? true)
    .map((d) => ({
      id: d.id,
      label: d.label,
      geometryType: d.geometryType,
      // One title notion (decision-map-popups.md §4.2 + decision-layer-display-field.md): per-map popup
      // title override → the layer's effective display field (starred/derived) → the static default.
      titleField: layerPopups[d.id]?.titleField ?? caps?.[d.id]?.displayField ?? d.popupTitleField,
      // Externals search via the remote half below, never GET /search.
      external: !!externals[d.id],
    }))
  // The Locate panel's remote half (rail parity): search the VISIBLE vector externals, honoring each
  // layer's active panel filter. Per-layer failures are silent inside.
  const searchExternal = (text: string): Promise<FeatureSearchResult[]> =>
    searchExternalLayers(
      Object.values(externalsRef.current).filter(
        (sp) =>
          (visibleRef.current[sp.externalId] ?? true) &&
          selectedRef.current.includes(sp.externalId) &&
          (sp.externalKind === 'arcgis-feature' || sp.externalKind === 'geojson'),
      ),
      text,
      extGeojsonCacheRef.current,
      filtersRef.current,
    )
  // The edit toolbar (pencil) shows whenever ANY editable feature layer is on the map — so attribute-edit +
  // delete are reachable for point AND line/polygon (incl. native non-point, which can't be drawn but whose
  // ingested features must still be editable/deletable). The Create/Draw buttons keep their own narrower gates.
  const hasEditTools = configs.some((c) => c.canEdit && !c.isView && anyEditOp(c.editOps))
  // Editing-toolbar population (decision-editing-toolbar.md §3.1): exactly the layers that got rail
  // buttons before — visible createable (point) ∪ drawable (line/polygon, non-native) — in TOC order.
  // Empty → the toolbar doesn't render (the pencil still serves attribute-edit/delete via popups).
  // V109: a layer whose owner allows none of add / move-or-reshape / delete has nothing for the toolbar
  // (attribute edits ride the popup); otherwise it stays listed and the toolbar hides the buttons per flag.
  const editToolbarLayers: EditToolbarLayer[] = configs
    .filter(
      (c) =>
        (createableLayers.some((x) => x.id === c.id) || drawCreateLayers.some((x) => x.id === c.id)) &&
        (c.editOps.add || c.editOps.geometry || c.editOps.delete),
    )
    .map((c) => ({
      id: c.id, label: c.label, geometryType: c.geometryType,
      canAdd: c.editOps.add, canShape: c.editOps.geometry, canDelete: c.editOps.delete,
    }))
  // Sticky pick, resolved against the live population (falls back to the first layer when the pick
  // is hidden/removed — matching the hide-disarms rule above).
  const editTarget = editToolbarLayers.find((l) => l.id === editTargetId) ?? editToolbarLayers[0]

  // ── Web-editor snapping (decision-web-editor-snapping.md) ────────────────────────────────────
  // Reference snapping (field-found at the first test — the county-boundary expectation):
  // visible hosted vector layers OUTSIDE the editable population are snappable too, never
  // armable. Their reads ride the same view-gated /features route the map already renders them
  // from. View layers + externals stay out of v1 (§3.1).
  const snapExtraLayers: EditToolbarLayer[] = configs
    .filter(
      (c) =>
        !c.isView &&
        (visible[c.id] ?? true) &&
        !editToolbarLayers.some((l) => l.id === c.id),
    )
    .map((c) => ({ id: c.id, label: c.label, geometryType: c.geometryType }))
  // Engine lifecycle: runs while Edit mode is ON. The enabled set = master toggle × per-layer
  // checks over targets + reference layers; empty set = no fetching and snap() answers
  // undefined, so gating lives in ONE place. The engine is created lazily on the first
  // edit-mode entry (the map exists by then) and its index survives brief deactivations.
  const snapLayerIdsKey = [...editToolbarLayers, ...snapExtraLayers].map((l) => l.id).join('\n')
  const accessTokenRef = useRef(accessToken)
  accessTokenRef.current = accessToken
  useEffect(() => {
    if (!editMode) return
    const map = mapRef.current
    if (!map) return
    if (!snapEngineRef.current) {
      snapEngineRef.current = new SnapEngine({
        map,
        getToken: () => accessTokenRef.current ?? '',
        onStatus: setSnapPaused,
      })
    }
    const engine = snapEngineRef.current
    const ids = snapLayerIdsKey === '' ? [] : snapLayerIdsKey.split('\n')
    engine.setLayers(snapOn ? ids.filter((id) => snapChecked[id] ?? true) : [])
    engine.activate()
    return () => engine.deactivate()
  }, [editMode, snapOn, snapChecked, snapLayerIdsKey])
  // Hover indicator for the place surface — the terra-draw modes render their own built-in
  // snappingPoint; armed click-to-place has no terra-draw session, so this small circle layer
  // (SNAP_INDICATOR_STYLES palette) is the visible feedback at the would-snap target.
  useEffect(() => {
    const map = mapRef.current
    if (!map || !editMode || createLayerId === null) return
    const onMove = (ev: maplibregl.MapMouseEvent): void => {
      const hit = snapEngineRef.current?.snap(ev.lngLat.lng, ev.lngLat.lat)
      if (!map.getSource(SNAP_SOURCE)) {
        map.addSource(SNAP_SOURCE, { type: 'geojson', data: EMPTY_FC })
        map.addLayer({
          id: SNAP_LAYER,
          type: 'circle',
          source: SNAP_SOURCE,
          paint: {
            'circle-radius': SNAP_INDICATOR_STYLES.snappingPointWidth,
            'circle-color': SNAP_INDICATOR_STYLES.snappingPointColor,
            'circle-stroke-color': SNAP_INDICATOR_STYLES.snappingPointOutlineColor,
            'circle-stroke-width': SNAP_INDICATOR_STYLES.snappingPointOutlineWidth,
          },
        })
      }
      ;(map.getSource(SNAP_SOURCE) as maplibregl.GeoJSONSource).setData(hit ? pointFC(hit) : EMPTY_FC)
    }
    map.on('mousemove', onMove)
    return () => {
      map.off('mousemove', onMove)
      try {
        if (map.getLayer(SNAP_LAYER)) map.removeLayer(SNAP_LAYER)
        if (map.getSource(SNAP_SOURCE)) map.removeSource(SNAP_SOURCE)
      } catch {
        /* map may already be torn down */
      }
    }
  }, [editMode, createLayerId])
  editTargetRef.current = editTarget?.id ?? null
  const armedGesture: EditGesture | null =
    createLayerId !== null ? 'place' : drawLayerId !== null ? drawTool : null
  const exitShapeTool = (): void => {
    setShapePick(false)
    exitEditShape()
  }
  const pickEditTarget = (id: string): void => {
    setEditTargetId(id)
    setCreateLayerId(null) // switching targets disarms; modes re-arm one click away
    setDrawLayerId(null)
    setDrawTool(null)
    exitShapeTool()
  }
  const armGesture = (g: EditGesture | null): void => {
    exitShapeTool() // create gestures and the vertices pick are mutually exclusive
    if (!g || !editTarget) {
      setCreateLayerId(null)
      setDrawLayerId(null)
      setDrawTool(null)
    } else if (g === 'place') {
      setDrawLayerId(null)
      setDrawTool(null)
      setCreateLayerId(editTarget.id) // point click-to-place: the native click handler's mode
    } else {
      setCreateLayerId(null)
      setDrawTool(g)
      setDrawLayerId(editTarget.id) // terra-draw create in the chosen gesture
    }
  }
  // F4 vertices tool: arm the pick (disarming any create gesture) or exit the whole flow.
  const toggleShapeTool = (): void => {
    if (shapePick) {
      exitShapeTool()
    } else {
      setCreateLayerId(null)
      setDrawLayerId(null)
      setDrawTool(null)
      setShapePick(true)
    }
  }
  // F4 delete-selected (decision-editing-toolbar-f4.md F3): one confirm with the count, one
  // round-trip for If-Match tokens (PR #74's repeatable id), then sequential per-feature deletes.
  // Conflicts/failures are skipped and summarized — never silently overwritten; failed ids stay
  // selected. Target layer only (F4): other layers' selections are untouched.
  const deleteBusyRef = useRef(false)
  async function deleteSelected(): Promise<void> {
    const target = editTargetRef.current
    const token = tokenRef.current
    if (!target || !token || deleteBusyRef.current) return
    const config = byId(target)
    if (!config) return
    const ids = Array.from(tableSelectionsRef.current[target] ?? [])
    if (ids.length === 0) return
    const noun = ids.length === 1 ? config.noun : `${config.noun}s`
    confirm({
      title: `Delete ${ids.length} ${noun}?`,
      consequence: `All ${ids.length} — and their attachments — are removed. This can’t be undone. Any row someone else has changed since you selected it is skipped rather than overwritten, and the count afterwards says how many.`,
      confirmLabel: `Delete ${ids.length} ${noun}`,
      onConfirm: () => doDeleteSelected(target, token, ids),
    })
  }

  async function doDeleteSelected(target: string, token: string, ids: string[]): Promise<void> {
    deleteBusyRef.current = true
    setBusy(true)
    try {
      const fc = await queryLayerFeatures(target, { ids, limit: ids.length }, token)
      // The LIST endpoint returns the T34 token as the raw `row_version` column (only the
      // GET-one read aliases it to `version`) — accept either, stringified for If-Match.
      const returned = new Set<string>()
      const versions = new Map<string, string>()
      for (const f of fc.features) {
        const fid = typeof f.id === 'string' ? f.id : String(f.id ?? '')
        if (!fid) continue
        returned.add(fid)
        const props = f.properties as { version?: unknown; row_version?: unknown }
        const raw = typeof props.version === 'string' ? props.version : props.row_version
        if (raw !== null && raw !== undefined && raw !== '') versions.set(fid, String(raw))
      }
      let deleted = 0
      let alreadyGone = 0
      let conflicted = 0
      let failed = 0
      let offline = false
      const gone: string[] = []
      for (const id of ids) {
        const version = versions.get(id)
        if (!version) {
          if (returned.has(id)) {
            failed++ // present but no readable token — fail safe, keep it selected
          } else {
            alreadyGone++ // truly absent: deleted elsewhere since selection; prune
            gone.push(id)
          }
          continue
        }
        const r = await submitWrite({ kind: 'delete', layerId: target, featureId: id, baseVersion: version })
        if (r.status === 'ok') {
          deleted++
          gone.push(id)
        } else if (r.status === 'conflict') {
          conflicted++
        } else if (r.status === 'offline') {
          offline = true
          break
        } else {
          failed++
        }
      }
      if (gone.length > 0) {
        const next = new Set(tableSelectionsRef.current[target] ?? [])
        for (const id of gone) next.delete(id)
        setLayerSelection(target, next)
        bumpTiles(target)
      }
      if (offline) flashOffline()
      const parts = [`${deleted} deleted`]
      if (alreadyGone > 0) parts.push(`${alreadyGone} already gone`)
      if (conflicted > 0) parts.push(`${conflicted} changed elsewhere — not deleted`)
      if (failed > 0) parts.push(`${failed} failed`)
      if (offline) parts.push('went offline — the rest kept')
      setMapNotice(parts.join(' · '))
    } catch (err) {
      setMapNotice(err instanceof ApiError ? `Delete failed (HTTP ${err.status})` : 'Delete failed')
    } finally {
      deleteBusyRef.current = false
      setBusy(false)
    }
  }

  return (
    <div className={`map${embed ? ' map--embed' : ''}`}>
      {/* Document actions live in the header bar (handoff-map-2a §2); the embed is chrome-less. */}
      {!embed && (
        <MapHeaderBar
          title={savedMap ? savedMap.title : 'Untitled map'}
          titleEditable={savedMap !== null && savedMap.canManage}
          onTitle={(t) => setSavedMap((m) => (m ? { ...m, title: t } : m))}
          unsaved={savedMap === null}
          readOnly={savedMap !== null && !savedMap.canManage}
          savedFlash={savedFlash}
          fieldReady={fieldReady}
          onFieldReady={setFieldReady}
          packs={
            <FieldPacksAction
              mapId={savedMap?.id ?? null}
              fieldReady={fieldReady}
              canManage={savedMap?.canManage === true}
              getViewBbox={() => {
                const b = mapRef.current?.getBounds()
                return b ? [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()] : null
              }}
            />
          }
          addOpen={addOpen}
          onAddLayers={() => {
            setAddOpen((o) => !o)
            setExportOpen(false)
          }}
          sketchOpen={sketchOpen}
          onSketch={() => {
            if (sketchOpen) {
              setSketchOpen(false)
              setSketchTool(null)
            } else {
              setSketchOpen(true)
              setAddOpen(false)
              setDrawLayerId(null) // one drawing affordance at a time
              // One top-center slot (E6): opening Sketch exits Edit mode and its live sessions.
              setEditMode(false)
              setCreateLayerId(null)
              setShapePick(false)
              exitEditShape()
            }
          }}
          exportOpen={exportOpen}
          onExport={() =>
            setExportOpen((o) => {
              const next = !o
              if (next) {
                setBookmarksOpen(false)
                setAddOpen(false)
              }
              return next
            })
          }
          editing={editMode}
          canSave={isPublisher && (savedMap === null || savedMap.canManage)}
          saveDisabled={(selected.length === 0 && mapTables.length === 0 && sketch.length === 0) || saving}
          saving={saving}
          saveTitle={savedMap ? `Save “${savedMap.title}”` : 'Save map'}
          onSave={() => (savedMap ? void updateSavedMap() : setSaveOpen(true))}
        />
      )}
      {/* The body splits into the map main area and the docked inspector (handoff-map-2a §1);
          everything that floats anchors to the MAIN area. MapLibre's trackResize absorbs the
          width change when the inspector mounts. */}
      <div className="map__body">
      <div className="map__main">
      <div ref={containerRef} className="map__canvas" />
      {/* Saved-map open: cover the canvas until the restore has applied its basemap, so the map
          appears already-correct instead of flipping colour a second in
          (decision-saved-map-basemap-flash.md option C). Always released — applied, failed, or
          timed out — so this can never strand the pane. */}
      {restoreHold && (
        <div className="map__restore-hold" role="status">
          <span>Opening map…</span>
        </div>
      )}
      {/* Bottom status strip (handoff-map-2a §3.4): coords · zoom · scale · CRS, DOM-direct via
          refs; MapLibre's compact attribution keeps the strip's right edge. Main map only. */}
      {!embed && (
        <div className="map__statusbar" aria-hidden="true">
          <span ref={coordsElRef}>—</span>
          <span className="map__statusbar-sep">|</span>
          <span ref={zoomElRef}>z —</span>
          <span className="map__statusbar-sep">|</span>
          <span ref={scaleElRef}>1:—</span>
          <span className="map__statusbar-sep">|</span>
          <span className="map__statusbar-dim">EPSG:3857</span>
        </div>
      )}

      {/* OpenStreetMap's credit, kept in view while its tiles draw (decision-streamed-basemap.md
          §14.3). The map's own attribution is pinned to the compact (i) button for layout reasons,
          and OSM's tile policy asks that the credit not sit behind a toggle — so it is drawn here,
          on every surface, embeds included. */}
      {isStreamedBasemap(basemap) && publisherOf(basemap) === 'osm' && (
        <div className="map__osm-credit">
          ©{' '}
          <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">
            OpenStreetMap
          </a>{' '}
          contributors
        </div>
      )}

      {/* Unprovisioned empty state (decision-basemap-provisioning.md): a fresh portal with no
          archive shows a friendly notice instead of a silent void. Data layers still render. */}
      {showNoBasemapNote(basemapAbsent, basemapNoteDismissed, basemap) && (
        <div className="map__basemap-note" role="note">
          No basemap installed yet — an administrator can set one up in Admin&nbsp;▸&nbsp;Basemap.
          Data layers still display.
          <CloseButton
            onClick={() => setBasemapNoteDismissed(true)}
            label="Dismiss"
            className="map__notice-x"
          />
        </div>
      )}

      {/* Outside the downloaded basemap: say why the map is grey, and where the fix is. Only for a
          downloaded look (a streamed map covers everywhere), never in an embed (a member of the
          public cannot act on it), and never beside the no-basemap note above. */}
      {outsideCoverage && !basemapAbsent && !embed && !isStreamedBasemap(basemap) && !coverageNoteDismissed && (
        <div className="map__basemap-note" role="note">
          This area is outside the downloaded basemap — an administrator can download it, or offer a
          streamed map, in Admin&nbsp;▸&nbsp;Basemap. Data layers still display.
          <CloseButton
            onClick={() => setCoverageNoteDismissed(true)}
            label="Dismiss"
            className="map__notice-x"
          />
        </div>
      )}

      {/* EMBED chrome (decision-map-embed.md F2): one-line title bar (title + legend toggle +
          open-full-map link) and nothing else — the full dock/rails below never render. */}
      {embed && (
        <div className="map__embedbar">
          <span className="map__embedbar-title">{savedMap ? savedMap.title : ''}</span>
          <button
            type="button"
            className={`map__embedbar-btn${embedLegendOpen ? ' is-active' : ''}`}
            aria-pressed={embedLegendOpen}
            onClick={() => setEmbedLegendOpen((o) => !o)}
          >
            Legend
          </button>
          {webMapId && (
            <a
              className="map__embedbar-link"
              href={`/map/${webMapId}`}
              target="_blank"
              rel="noopener noreferrer"
              title="Open the full map in a new tab"
            >
              Open full map ↗
            </a>
          )}
          {mapNotice !== null && <span className="map__embedbar-notice">{mapNotice}</span>}
        </div>
      )}
      {embed && embedLegendOpen && (
        <div className="map__embedlegend">
          <LayerPane
            items={tocItems}
            visible={{ ...visible, __sketch: sketchVisible }}
            onToggle={toggleLayer}
          />
        </div>
      )}

      {/* Left dock = MAP CONTROLS (AGO "controls on the left"): the LAYERS panel, then a small
          icon rail beneath it. The rail is ALWAYS visible (LayerPane hides itself when the map is
          empty), so Add layers is reachable on a fresh map. */}
      {!embed && (
      <div className="map__leftdock">
        {/* The map title moved to the header bar (E8) — editable where it is shown. */}
        {/* Notices dock to the RIGHT of the Layers box so they never overlap it.
            Map-state overlays (loading/error/empty) live here too. */}
        <div className="map__docknotices">
          {!capsReady && capsError === null && (
            <div className="map__overlay">Loading your layers…</div>
          )}
          {capsError !== null && (
            <div className="map__overlay map__overlay--error">Could not load your layers ({capsError})</div>
          )}
          {mapError !== null && <div className="map__overlay map__overlay--error">Map error: {mapError}</div>}
          {capsReady && configs.length === 0 && mapTables.length === 0 && Object.keys(rasterMeta).length === 0 && capsError === null && (
            <div className="map__overlay">
              {viewable.length === 0
                ? 'No layers available to you.'
                : 'Your map is empty — choose “Add layers” at the top to put something on it.'}
            </div>
          )}
          {importProgress !== null && (
            <div className="map__notice" role="status">
              <span className="map__notice-spinner" aria-hidden />
              <span>{importProgress}</span>
            </div>
          )}
          {/* Talus 20 — F5 "layers unavailable" notice (and save errors / import results), dismissible. */}
          {mapNotice !== null && (
            <div className="map__notice" role="status">
              <span>{mapNotice}</span>
              <CloseButton onClick={() => setMapNotice(null)} label="Dismiss" className="map__notice-x" />
            </div>
          )}
          {/* Draft autosave (G4.2): unsaved changes this browser kept for this map — restore or drop them. */}
          {draftOffer !== null && savedMap !== null && (
            <div className="map__notice" role="status">
              <span>
                Unsaved changes from {new Date(draftOffer.savedAt).toLocaleString()} were recovered on this device.
              </span>
              <button type="button" className="btn btn--xs btn--accent" onClick={restoreDraft}>Restore</button>
              <button type="button" className="btn btn--xs" onClick={discardDraft}>Discard</button>
            </div>
          )}
          {/* "Zoom in to load more" guard — a visible EXTERNAL layer has more features in view than
              the bbox cap loaded. Externals are the only capped GeoJSON kind left (hosted layers all
              ride tiles — decision-dynamic-mvt.md); the honest-limit hint stays for them. Self-clears
              on zoom-in (viewCapped re-evaluates per refresh). */}
          {selectedDefs.some(
            (d) => !!externals[d.id] && viewCapped[d.id] && (visible[d.id] ?? true),
          ) && (
            <div className="map__notice" role="status">
              <span>
                Showing the first {VIEW_FEATURE_CAP.toLocaleString()} features in view — zoom in to load
                more.
              </span>
            </div>
          )}
        </div>

        <LayerPane
          items={tocItems}
          visible={{ ...visible, __sketch: sketchVisible }}
          onToggle={toggleLayer}
          selectedId={selectedLayerId}
          onSelect={(id) => setSelectedLayerId((cur) => (cur === id ? null : id))}
          filteredIds={filteredLayerIds}
          // The swatch keeps its Pro-convention style route — it now lands on the inspector's
          // Style tab. Editable maps only (the inspector is the only styling surface here).
          onStyle={
            mapEditable
              ? (id) => {
                  setSelectedLayerId(id)
                  setInspectorTab('style')
                }
              : undefined
          }
          tables={mapTables.map((id) => ({ id, label: caps?.[id]?.displayName ?? id }))}
          openTableId={openTableId}
          onOpenTable={(id) => setOpenTableId((cur) => (cur === id ? null : id))}
          onRemoveTable={
            mapEditable
              ? (id) => {
                  setMapTables((prev) => prev.filter((x) => x !== id))
                  setOpenTableId((cur) => (cur === id ? null : cur))
                }
              : undefined
          }
          onRemove={removeLayer}
          onReorder={reorderLayer}
          onZoom={zoomTo}
          onOpacity={setRasterOpacity}
          onColormap={setRasterColormap}
          onExport={beginRasterExport}
          onOpenMenu={(layerId, x, y) => setLayerMenu({ kind: 'layer', layerId, x, y })}
          onOpenTableMenu={(tableId, x, y) => setLayerMenu({ kind: 'table', layerId: tableId, x, y })}
          referencedIds={referencedIds}
        />
        {/* The control rail dissolved (handoff-map-2a §2/§3.1): document actions (Add layers,
            Save, Export) live in the header bar; Bookmarks and Basemap are view affordances and
            moved to the right rail's Tools cluster. */}
      </div>
      )}

      {/* Right rail (handoff-map-2a §3.1) — grouped clusters, view/navigate only. Document
          actions live in the header bar; editing's create affordances live in the top-center
          EditToolbar (decision-editing-toolbar.md). */}
      {!embed && (
      <div className="map__tools">
        {/* Navigate cluster: zoom + locate. Zoom drives the map directly; locate trigger()s the
            hidden GeolocateControl so the dot and permission flow stay MapLibre's. */}
        <div className="map__cluster" role="group" aria-label="Navigate">
          <button
            type="button"
            className="map__iconbtn"
            aria-label="Zoom in"
            title="Zoom in"
            onClick={() => mapRef.current?.zoomIn()}
          >
            <Plus size={16} aria-hidden="true" />
          </button>
          <button
            type="button"
            className="map__iconbtn"
            aria-label="Zoom out"
            title="Zoom out"
            onClick={() => mapRef.current?.zoomOut()}
          >
            <Minus size={16} aria-hidden="true" />
          </button>
          <button
            type="button"
            className="map__iconbtn"
            aria-label="My location"
            title="My location"
            onClick={() => geolocateCtlRef.current?.trigger()}
          >
            <LocateFixed size={16} aria-hidden="true" />
          </button>
        </div>
        {/* Tools cluster: view affordances + (until the docked inspector lands — restructure
            commit 2) the four layer-config flyout tools it replaces. */}
        <div className="map__cluster" role="group" aria-label="Tools">
        {hasEditTools && (
          <button
            type="button"
            // Amber = armed (one colour rule, §3.2): Edit mode is an armed session, not an open panel.
            className={`map__iconbtn${editMode ? ' map__iconbtn--armed' : ''}`}
            aria-label="Edit mode"
            aria-pressed={editMode}
            title={editMode ? 'Editing — click to exit' : 'Edit mode'}
            onClick={() => {
              setEditMode((m) => !m)
              setCreateLayerId(null)
              setDrawLayerId(null)
              setShapePick(false)
              exitEditShape() // a toolbar-seeded reshape session dies with Edit mode
              disarmSelect() // edit and select must never contend for map clicks
              // One top-center slot (E6): Edit mode and the Sketch toolbar are mutually exclusive.
              setSketchOpen(false)
              setSketchTool(null)
            }}
          >
            <Pencil size={16} aria-hidden="true" />
          </button>
        )}
        {/* Feature search (decision-feature-search.md) — the Locate tool; the search box flies out
            to the LEFT. Available to everyone (read-only friendly). */}
        <LocatePanel
          accessToken={accessToken}
          layers={locateLayers}
          searchExternal={searchExternal}
          onPick={(r) => void openFeatureResult(r)}
        />
        {/* Select toggle (decision-select-features.md F1; handoff-map-2a §1) — arms an on-map
            box/click selection session for the layer SELECTED in the Layers panel; the old
            layer-picker flyout is gone (the TOC had already answered "which layer?"). Still
            viewer-available: selecting is a READ affordance, and the amber armed state follows
            the one colour rule (§3.2). Filter/Labels/Popup config moved to the docked inspector. */}
        {selectToolLayers.length > 0 &&
          (() => {
            const armed = selectArmed !== null
            const armable =
              selectedLayerId !== null && selectToolLayers.some((l) => l.id === selectedLayerId)
            const total = selectToolLayers.reduce((n, l) => n + l.count, 0)
            return (
              <div className="rail-flyout">
                <button
                  type="button"
                  className={`map__iconbtn${armed ? ' map__iconbtn--armed' : ''}${total > 0 && !armed ? ' rail-flyout__btn--on' : ''}`}
                  aria-label="Select features on the map"
                  aria-pressed={armed}
                  disabled={!armed && !armable}
                  title={
                    armed
                      ? 'Selecting — Shift adds, Ctrl removes, Esc or click here to exit'
                      : armable
                        ? `Select features in ${byId(selectedLayerId!)?.label ?? 'the selected layer'}`
                        : 'Select features — click a layer name in the Layers panel first'
                  }
                  onClick={() => {
                    if (armed) disarmSelect()
                    else if (selectedLayerId) armSelect(selectedLayerId)
                  }}
                >
                  <SquareDashedMousePointer size={16} aria-hidden="true" />
                </button>
                {total > 0 && (
                  <span className="rail-flyout__badge" aria-hidden="true">
                    {total}
                  </span>
                )}
              </div>
            )
          })()}
        {/* Bookmarks (saved named viewpoints) — a view affordance, so it lives here now. Always
            available: a viewer uses bookmarks read-only; editing is gated inside the panel. */}
        <button
          type="button"
          className={`map__iconbtn${bookmarksOpen ? ' is-active' : ''}`}
          aria-label="Bookmarks"
          aria-pressed={bookmarksOpen}
          title="Bookmarks"
          onClick={() => {
            setBookmarksOpen((o) => !o)
            setExportOpen(false)
          }}
        >
          <Bookmark size={16} aria-hidden="true" />
        </button>
        {/* Basemap — bottom of the Tools cluster; the popout now opens to the LEFT of the rail.
            Selecting keeps the popout open so flavors can be compared; the icon closes it. */}
        <div className="map__basemap-anchor">
          <button
            type="button"
            className={`map__iconbtn${basemapOpen ? ' is-active' : ''}`}
            aria-label="Basemap"
            aria-pressed={basemapOpen}
            title="Basemap"
            onClick={() => setBasemapOpen((o) => !o)}
          >
            <Layers2 size={16} aria-hidden="true" />
          </button>
          {basemapOpen && (
            <div className="map__basemap-pop">
              <BasemapSwitcher
                value={basemap}
                onChange={switchBasemap}
                streamed={streamed}
                downloaded={listsDownloaded}
                variant="pop"
              />
            </div>
          )}
        </div>
        </div>
      </div>
      )}

      {/* Top-center editing toolbar (decision-editing-toolbar.md) — replaces the old per-layer
          Create/Draw rail stack: one target-layer flyout + that layer's create gestures. Hidden
          while the raster clip-export banner owns the slot. */}
      {!embed && editMode && !drawingExport && editTarget && (
        <EditToolbar
          layers={editToolbarLayers}
          targetId={editTarget.id}
          armed={armedGesture}
          onTarget={pickEditTarget}
          onArm={armGesture}
          shapeAvailable={editTarget.geometryType !== 'point' && editTarget.canShape !== false}
          shapeActive={shapePick}
          onShape={toggleShapeTool}
          selectedCount={tableSelections[editTarget.id]?.size ?? 0}
          onDeleteSelected={() => void deleteSelected()}
          snapOn={snapOn}
          snapExtras={snapExtraLayers}
          snapChecked={snapChecked}
          snapPaused={snapPaused}
          onSnapToggle={setSnapOn}
          onSnapCheck={(id, on) => setSnapChecked((prev) => ({ ...prev, [id]: on }))}
        />
      )}

      {/* Address search (Talus 18c) — consumes the /geocode proxy; fly-to + ephemeral pin. */}
      {!embed && (
      <GeocodeSearch
        accessToken={accessToken}
        onPick={flyToCandidate}
        onClear={clearGeocodeMarker}
        getViewportBBox={getViewportBBox}
        onScopeChange={(loc) => {
          geocodeScopeRef.current = loc
        }}
      />
      )}

      {addOpen && (
        <AddLayersPanel
          layers={addable}
          rasters={addableRasters}
          tables={addableTables}
          onAdd={addLayer}
          onAddRaster={(id) => {
            const r = availRasters.find((x) => x.id === id)
            if (r) addRaster(r)
          }}
          onAddTable={(id) => setMapTables((prev) => (prev.includes(id) ? prev : [...prev, id]))}
          onClose={() => setAddOpen(false)}
          onPickFile={onPickFile}
          canAddFromFile={isPublisher}
          fileBusy={fileBusy}
          fileError={fileError}
          fileErrorOffersImport={fileErrorOffersImport}
          onAddExternal={addExternal}
          references={addableRefs}
          onAddReference={addReference}
          canSaveToCatalog={isPublisher}
          onSaveReference={saveReference}
          onSketch={() => {
            setSketchOpen(true)
            setAddOpen(false)
            setDrawLayerId(null) // one drawing affordance at a time
            // One top-center slot (E6): opening Sketch exits Edit mode and its live sessions.
            setEditMode(false)
            setCreateLayerId(null)
            setShapePick(false)
            exitEditShape()
          }}
        />
      )}

      {/* Sketch layer (decision-sketch-layer.md): the notes toolbar + the clicked note's edit card. */}
      {sketchOpen && (
        <SketchToolbar
          tool={sketchTool}
          onTool={(t) => {
            setSketchTool(t)
            if (t) {
              setDrawLayerId(null)
              setSelectedSketchId(null)
              setReshapingSketchId(null)
            }
          }}
          color={sketchColor}
          onColor={setSketchColor}
          label={sketchLabel}
          onLabel={setSketchLabel}
          onClose={() => {
            setSketchOpen(false)
            setSketchTool(null)
          }}
        />
      )}
      {/* The clicked note's edit card docks into the inspector region now (handoff-map-2a §2,
          commit 4) — see the right-dock block at the end of the body. */}

      {/* Add-from-file: the create wizard seeded from the picked file (decision-add-from-file.md).
          Finish creates the layer (existing deferred-commit path), then the SAME file ingests. */}
      {filePrep !== null && (
        <CreateLayerWizard
          open
          accessToken={accessToken ?? ''}
          seed={seedFromPrepared(filePrep)}
          onClose={() => setFilePrep(null)}
          onCreated={(layerId, ingest) => {
            const prep = filePrep
            setFilePrep(null)
            if (prep !== null) void runFileIngest(prep, layerId, ingest)
          }}
        />
      )}

      {bookmarksOpen && (
        <BookmarksPanel
          bookmarks={bookmarks}
          canEdit={canManageMap}
          dirty={bookmarksDirty}
          onNavigate={flyToBookmark}
          onAdd={addBookmark}
          onRename={renameBookmark}
          onUpdateExtent={updateBookmarkExtent}
          onReorder={reorderBookmark}
          onDelete={deleteBookmark}
          onClose={() => setBookmarksOpen(false)}
        />
      )}

      {exportOpen && (
        <MapExportPanel
          defaultName={exportDefaultName()}
          defaultTitle={savedMap?.title ?? 'Untitled map'}
          onDownloadPng={handleDownloadPng}
          onDownloadPdf={handleDownloadPdf}
          pitched={isPitched(mapRef.current?.getPitch() ?? 0)}
          onClose={() => setExportOpen(false)}
        />
      )}

      {/* Embed keeps the floating bottom-right pill (no rail there); the main map's switcher now
          lives in the left rail's basemap popout above. */}
      {embed && (
        <BasemapSwitcher
          value={basemap}
          onChange={switchBasemap}
          streamed={streamed}
          downloaded={listsDownloaded}
        />
      )}

      {form !== null && conflict === null && FormComponent && (
        <div className="map__panel">
          <FormComponent
              mode={form.kind === 'edit' ? 'edit' : 'create'}
              initial={form.kind === 'edit' ? form.props : null}
              busy={busy}
              error={formError}
              layerId={form.layerId}
              accessToken={accessToken}
              noun={byId(form.layerId)?.noun}
              editViaAdmin={byId(form.layerId)?.editViaAdmin}
              attachmentsEnabled={caps?.[form.layerId]?.attachmentsEnabled ?? false}
              coord={
                form.kind === 'create'
                  ? {
                      lon: form.lon,
                      lat: form.lat,
                      srid: byId(form.layerId)?.workingSrid ?? 4326,
                      native: !!byId(form.layerId)?.nativeStorage,
                    }
                  : undefined
              }
              onSubmit={(attrs, attachments) => void submitForm(attrs, attachments)}
              onCancel={() => {
                setForm(null)
                setFormError(null)
              }}
          />
        </div>
      )}

      {moving !== null && (
        <div className="map__panel map__panel--move">
          <p>Drag the highlighted feature to its new location, then save.</p>
          {formError !== null && <p className="sign-form__error">{formError}</p>}
          <div className="sign-form__actions">
            <button className="btn btn--sm" onClick={() => exitMove()} disabled={busy}>
              Cancel
            </button>
            <button className="btn btn--sm" onClick={() => void saveMove()} disabled={busy}>
              {busy ? 'Saving…' : 'Save location'}
            </button>
          </div>
        </div>
      )}

      {/* Line/polygon slice c6b — vertex/ring editing. terra-draw select handles the on-map interaction;
          each completed gesture auto-saves (onEditShape), so this panel is just a hint + a Done button. */}
      {editing !== null && (
        <div className="map__panel map__panel--edit-shape">
          <p>
            Drag a vertex to move it · drag a midpoint to add one · right-click a vertex to remove it. Changes
            save automatically.
          </p>
          {formError !== null && <p className="sign-form__error">{formError}</p>}
          <div className="sign-form__actions">
            <button className="btn btn--sm" onClick={() => exitEditShape()} disabled={busy}>
              Done
            </button>
          </div>
        </div>
      )}

      {conflict !== null &&
        (() => {
          const cfg = byId(conflict.layerId)
          const noun = cfg?.noun ?? 'record'
          const updatedAt =
            typeof conflict.current.updated_at === 'string' ? conflict.current.updated_at : null
          const when = updatedAt ? new Date(updatedAt).toLocaleString() : null
          const overwriteLabel = conflict.kind === 'delete' ? 'Delete anyway' : 'Overwrite with mine'
          const discardLabel = conflict.kind === 'edit' ? 'Discard mine & reload' : 'Cancel'
          return (
            <div className="map__panel map__panel--conflict" role="alertdialog" aria-label="Edit conflict">
              <p className="map__panel-conflict-title">This {noun} changed while you had it open</p>
              <p>
                Another editor saved changes{when ? ` (last updated ${when})` : ''}. Your work isn’t lost —
                choose how to continue.
              </p>
              <div className="sign-form__actions">
                <button className="btn btn--sm" onClick={() => void resolveConflict('discard')} disabled={busy}>
                  {discardLabel}
                </button>
                <button
                  className={conflict.kind === 'delete' ? 'btn btn--sm btn--danger' : 'btn btn--sm'}
                  onClick={() => void resolveConflict('overwrite')}
                  disabled={busy}
                >
                  {busy ? 'Working…' : overwriteLabel}
                </button>
              </div>
            </div>
          )
        })()}

      {offlineFlash && (
        <div className="map__notice map__notice--offline" role="status">
          <span>You’re offline — reconnect to edit. Your change wasn’t saved.</span>
        </div>
      )}

      {/* Talus 35 — honest offline context: editing is online-only, and the map shows only what the
          service worker cached while you were online (the vector basemap and never-visited areas may
          be blank until reconnect). Dismissible (re-arms on the next offline episode). */}
      {!online && !offlineNoticeDismissed && (
        <div className="map__notice map__notice--offline-map" role="status">
          <span>Offline — viewing only (areas you’ve already loaded). Reconnect to edit.</span>
          <CloseButton
            onClick={() => setOfflineNoticeDismissed(true)}
            label="Dismiss offline notice"
            className="map__notice-x"
          />
        </div>
      )}

      {/* Right-click layer options (decision-map-layer-table.md): opening a table is a READ action, so
          the menu shows for every viewer — edit affordances inside the grid stay per-layer canEdit.
          "Zoom to" moved here from the row's action cluster; Rename = the map-only
          display-name override (decision-map-rename.md). A TABLES-section row (kind 'table', the
          part-(c) fast-follow) gets Attribute Table only — no geometry to zoom, not an
          OperationalLayer to rename. */}
      {layerMenu && (
        <LayerContextMenu
          x={layerMenu.x}
          y={layerMenu.y}
          items={[
            // Vector externals get the READ-ONLY remote grid (AGO
            // parity; ExternalTableView pages the remote service). Raster-kind externals have no
            // rows, so only they hide the item.
            ...(externals[layerMenu.layerId] &&
            externals[layerMenu.layerId].externalKind !== 'arcgis-feature' &&
            externals[layerMenu.layerId].externalKind !== 'geojson'
              ? []
              : [
                  {
                    label: 'Attribute Table',
                    icon: <TableIcon />,
                    onSelect: () => {
                      setOpenTableId(layerMenu.layerId)
                      setLayerMenu(null)
                    },
                  },
                ]),
            ...(layerMenu.kind === 'layer'
              ? [
                  {
                    label: 'Zoom to',
                    icon: <Crosshair size={14} aria-hidden="true" />,
                    onSelect: () => {
                      zoomTo(layerMenu.layerId)
                      setLayerMenu(null)
                    },
                  },
                  // Zoom to selected + Clear selection (decision-zoom-to-selected.md F4): hosted
                  // vector layers only — externals' grid is read-only (no selection concept),
                  // sketch has its own notes model, rasters have no rows. Empty selection →
                  // disabled (greyed, no hint copy); Clear only renders when one exists,
                  // because the highlight deliberately survives closing the table panel.
                  ...(!externals[layerMenu.layerId] &&
                  !rasterMeta[layerMenu.layerId] &&
                  layerMenu.layerId !== '__sketch'
                    ? [
                        {
                          label: 'Zoom to selected',
                          icon: <Crosshair size={14} aria-hidden="true" />,
                          disabled: !(layerMenu.layerId in tableSelections),
                          onSelect: () => {
                            zoomToSelected(layerMenu.layerId)
                            setLayerMenu(null)
                          },
                        },
                        ...(layerMenu.layerId in tableSelections
                          ? [
                              {
                                label: 'Clear selection',
                                icon: <Square size={13} aria-hidden="true" />,
                                onSelect: () => {
                                  setLayerSelection(layerMenu.layerId, new Set<string>())
                                  setLayerMenu(null)
                                },
                              },
                            ]
                          : []),
                      ]
                    : []),
                  // No Style/Labels/Filter/Pop-up entries: they were the
                  // routes to the retired flyouts, kept through map-2a as tab shortcuts — but the
                  // row click opens the inspector with all four tabs one click away, and the swatch
                  // keeps its Pro-convention Style route. The menu stays the CONTEXTUAL actions.
                  // Map-only rename (decision-map-rename.md): authoring, so editable maps only —
                  // viewers keep the READ items above.
                  ...(mapEditable
                    ? [
                        {
                          label: 'Rename',
                          icon: <Pencil size={16} aria-hidden="true" />,
                          onSelect: () => {
                            setRenamingLayerId(layerMenu.layerId)
                            setLayerMenu(null)
                          },
                        },
                      ]
                    : []),
                ]
              : []),
          ]}
          onClose={() => setLayerMenu(null)}
        />
      )}

      {/* Docked table panel (decision-map-tables.md; widened by decision-map-layer-table.md to host any
          viewable layer's attribute table): the FULL existing Data grid — server-side filter/sort/export
          + row add/edit/delete (canEdit-gated), one table at a time. */}
      {openTableId && caps?.[openTableId] && (
        <div
          className={`map__tablepanel${tableMaximized ? ' map__tablepanel--max' : ''}`}
          role="region"
          aria-label="Table"
        >
          <div className="map__tablepanel-hdr">
            <span className="map__tablepanel-title">
              {/* Geometry glyph so a layer's table vs a non-spatial table reads at a glance ('none' → TableIcon). */}
              <GeometryIcon geomType={caps[openTableId].geomType} className="map__tablepanel-glyph" />
              {/* Map-only rename applies here too — the docked table is a map surface. */}
              {layerTitles[openTableId] ?? caps[openTableId].displayName ?? openTableId}
            </span>
            <div className="map__tablepanel-btns">
              {/* Map-extent scoping (spatial layers only — a table has no geometry to scope by). */}
              {caps[openTableId].geomType !== 'none' && (
                <button
                  type="button"
                  className={`map__tablepanel-btn map__tablepanel-btn--text${tableExtent ? ' map__tablepanel-btn--on' : ''}`}
                  aria-pressed={tableExtent}
                  title={tableExtent ? 'Showing rows in the current map view — click to show all' : 'Scope rows to the current map view'}
                  onClick={() => setTableExtent((v) => !v)}
                >
                  Map extent
                </button>
              )}
              <button
                type="button"
                className="map__tablepanel-btn"
                aria-label={tableMaximized ? 'Restore table size' : 'Maximize table'}
                title={tableMaximized ? 'Restore' : 'Maximize'}
                aria-pressed={tableMaximized}
                onClick={() => setTableMaximized((m) => !m)}
              >
                {tableMaximized ? <Minimize2 size={14} aria-hidden="true" /> : <Maximize2 size={14} aria-hidden="true" />}
              </button>
              <CloseButton onClick={() => setOpenTableId(null)} label="Close table" />
            </div>
          </div>
          <div className="map__tablepanel-body">
            {/* Filter sync: a SPATIAL layer's grid seeds from + writes through to the layer's ONE
                shared map filter (persists after close). A non-spatial table has nothing on the map
                to filter, and the map panel never offers Import (imports live in the Catalog). */}
            <TableView
              layer={caps[openTableId]}
              hideImport
              scopeBbox={
                tableExtent && caps[openTableId].geomType !== 'none' ? (tableBbox ?? undefined) : undefined
              }
              initialFilters={
                caps[openTableId].geomType !== 'none' ? (layerFilters[openTableId] ?? []) : undefined
              }
              onApplyFilters={
                caps[openTableId].geomType !== 'none'
                  ? (conds) => applyLayerFilter(openTableId, conds)
                  : undefined
              }
              // Lifted selection (decision-zoom-to-selected.md F2): spatial layers only — a
              // non-spatial table has nothing on the map to zoom/highlight, so its grid keeps
              // the internal selection it always had.
              selection={
                caps[openTableId].geomType !== 'none'
                  ? (tableSelections[openTableId] ?? EMPTY_SELECTION)
                  : undefined
              }
              onSelectionChange={
                caps[openTableId].geomType !== 'none'
                  ? (next) => setLayerSelection(openTableId, next)
                  : undefined
              }
              // Selection actions the MAP already owns, re-homed into the grid's selection bar
              // (handoff-record-table-3 §4.3). Zoom to is the TOC context-menu action; Delete is
              // the EditToolbar's, so it is offered on exactly the same terms — edit mode, on the
              // current edit target. No new capability, no new data path.
              onZoomToSelected={
                caps[openTableId].geomType !== 'none' ? () => zoomToSelected(openTableId) : undefined
              }
              onDeleteSelected={
                editMode && editTarget?.id === openTableId && caps[openTableId].canEdit &&
                editOpsOf(caps[openTableId]).delete
                  ? () => void deleteSelected()
                  : undefined
              }
            />
          </div>
        </div>
      )}

      {/* External layer's attribute table (decision-add-from-url.md):
          the same docked chrome, a LEAN read-only grid inside — server-paged straight off the remote
          service (no Talus data surface). Row click → highlight + zoom to that feature. */}
      {openTableId && externals[openTableId] && (
        <div
          className={`map__tablepanel${tableMaximized ? ' map__tablepanel--max' : ''}`}
          role="region"
          aria-label="Table"
        >
          <div className="map__tablepanel-hdr">
            <span className="map__tablepanel-title">
              <GeometryIcon geomType={externals[openTableId].geometryType} className="map__tablepanel-glyph" />
              {layerTitles[openTableId] ?? externals[openTableId].title}
            </span>
            <div className="map__tablepanel-btns">
              <button
                type="button"
                className="map__tablepanel-btn"
                aria-label={tableMaximized ? 'Restore table size' : 'Maximize table'}
                title={tableMaximized ? 'Restore' : 'Maximize'}
                aria-pressed={tableMaximized}
                onClick={() => setTableMaximized((m) => !m)}
              >
                {tableMaximized ? <Minimize2 size={14} aria-hidden="true" /> : <Maximize2 size={14} aria-hidden="true" />}
              </button>
              <CloseButton onClick={() => setOpenTableId(null)} label="Close table" />
            </div>
          </div>
          <div className="map__tablepanel-body">
            <ExternalTableView
              spec={externals[openTableId]}
              conds={layerFilters[openTableId] ?? []}
              onOpenFilters={
                mapEditable
                  ? () => {
                      setSelectedLayerId(openTableId)
                      setInspectorTab('filter')
                    }
                  : undefined
              }
              onLocate={(geometry) => {
                setHighlight(geometry)
                const bbox = geojsonBbox([{ type: 'Feature', geometry, properties: {} }])
                if (bbox) {
                  mapRef.current?.fitBounds(
                    [
                      [bbox[0], bbox[1]],
                      [bbox[2], bbox[3]],
                    ],
                    { padding: 96, maxZoom: 18 },
                  )
                }
              }}
            />
          </div>
        </div>
      )}

      {/* The four floating config panels are gone — their bodies mount inside the docked
          inspector below (handoff-map-2a §1). */}

      {saveOpen && (
        <SaveMapDialog
          busy={saving}
          onCancel={() => setSaveOpen(false)}
          onSave={(t, d) => void createSavedMap(t, d)}
        />
      )}

      {navBlocker.state === 'blocked' && (
        <LeaveMapDialog onStay={() => navBlocker.reset()} onLeave={() => navBlocker.proceed()} />
      )}

      {renamingLayerId &&
        (() => {
          const cfg = byId(renamingLayerId)
          if (!cfg) return null
          // Map-only rename (decision-map-rename.md): cfg.label is already the EFFECTIVE name (the
          // selectedDefs override), so the input seeds with what the user currently sees.
          return (
            <RenameLayerDialog
              currentName={cfg.label}
              hasOverride={renamingLayerId in layerTitles}
              onCancel={() => setRenamingLayerId(null)}
              onReset={() => {
                setLayerTitles((t) => {
                  const next = { ...t }
                  delete next[renamingLayerId]
                  return next
                })
                setRenamingLayerId(null)
              }}
              onSave={(name) => {
                setLayerTitles((t) => ({ ...t, [renamingLayerId]: name }))
                setRenamingLayerId(null)
              }}
            />
          )
        })()}

      {detail && (
        <RecordDetailLoader
          layerId={detail.layerId}
          layerLabel={detail.label}
          recordId={detail.id}
          onClose={() => setDetail(null)}
          displayField={detail.displayField}
          geomType={detail.geomType}
          displayColor={detail.displayColor}
          captureMap={() => (mapRef.current ? captureMapPng(mapRef.current) : Promise.resolve(null))}
        />
      )}

      {/* Raster clip-export: a banner while the user draws the clip box (or picks Whole raster). */}
      {/* One-click clear for EVERY layer's selection — same action
          and glyph as the per-layer context-menu item, surfaced while any selection exists (the
          highlight deliberately outlives the table panel, so it needs an always-visible way out). */}
      {Object.keys(tableSelections).length > 0 && (
        <button
          type="button"
          className="map__clearsel"
          title="Clear all selected features"
          onClick={() => setTableSelections({})}
        >
          <Square size={13} aria-hidden="true" />
          Clear selection ·{' '}
          {Object.values(tableSelections).reduce((n, s) => n + s.size, 0).toLocaleString()}
        </button>
      )}

      {/* No armed-state banner (Pro has no selection directions): the crosshair
          cursor + active rail button carry the state; Esc / the rail button / Edit toggle exit. */}
      {drawingExport && (
        <div className="map__drawbanner" role="status">
          <span>
            Drag a box on the map to clip <strong>{drawingExport.title}</strong>, or
          </span>
          <button type="button" className="btn btn--sm" onClick={exportWholeRaster}>
            Whole raster
          </button>
          <button type="button" className="btn btn--sm" onClick={cancelRasterExport}>
            Cancel
          </button>
        </div>
      )}

      {exportTarget && (
        <ExportRasterDialog
          rasterId={exportTarget.id}
          title={exportTarget.title}
          bbox={exportTarget.bbox}
          accessToken={accessToken ?? ''}
          onClose={() => setExportTarget(null)}
        />
      )}
      </div>

      {/* The right dock — ONE slot (E6). A selected sketch NOTE takes it with its edit card
          (handoff-map-2a §2, commit 4 — the card was a float that collided with the toolbar);
          otherwise the layer inspector (§1), the per-layer configuration surface driven by the
          TOC selection. Inspector on editable maps only — viewers still SEE filters, labels and
          pop-ups applied; configuring is authoring. Panels are keyed by layer so each selection
          change remounts them on that layer's current config (they seed state from `initial`). */}
      {!embed &&
        (() => {
          const sketchSel = selectedSketchId
            ? (sketch.find((f) => f.id === selectedSketchId) ?? null)
            : null
          if (!sketchSel && !mapEditable) return null
          const cfg = !sketchSel && selectedLayerId ? byId(selectedLayerId) : undefined
          // A layer the user can't style never lands on a disabled Style tab (the default) —
          // display-coerce to Filter; their explicit tab choices stand everywhere else.
          const tab: InspectorTab =
            cfg && (inspectorTab === 'style' || inspectorTab === 'form') && !cfg.canManage ? 'filter' : inspectorTab
          return (
            <InspectorDock
              hasContent={!!sketchSel || !!cfg}
              ariaLabel={sketchSel ? 'Sketch note' : 'Layer configuration'}
            >
              {sketchSel ? (
                <SketchFeatureCard
                  feature={sketchSel}
                  reshaping={reshapingSketchId === sketchSel.id}
                  onColor={(c) =>
                    setSketch((prev) =>
                      prev.map((f) => (f.id === sketchSel.id ? { ...f, color: c } : f)),
                    )
                  }
                  onLabel={(v) =>
                    setSketch((prev) =>
                      prev.map((f) =>
                        f.id === sketchSel.id ? { ...f, label: v.trim() ? v : null } : f,
                      ),
                    )
                  }
                  onReshape={() => {
                    setSketchTool(null)
                    setReshapingSketchId((cur) => (cur === sketchSel.id ? null : sketchSel.id))
                  }}
                  onDelete={() => {
                    setSketch((prev) => prev.filter((f) => f.id !== sketchSel.id))
                    setSelectedSketchId(null)
                    setReshapingSketchId(null)
                  }}
                  onClose={() => {
                    setSelectedSketchId(null)
                    setReshapingSketchId(null)
                  }}
                />
              ) : (
                <LayerInspector
              layer={
                cfg
                  ? {
                      id: cfg.id,
                      label: cfg.label,
                      geometryType: cfg.geometryType,
                      color: caps?.[cfg.id]?.displayColor ?? cfg.color,
                      canStyle: cfg.canManage,
                      canForm: cfg.canManage && isSlotId(cfg.id),
                      external: !!externals[cfg.id],
                    }
                  : null
              }
              tab={tab}
              onTab={setInspectorTab}
              configured={{
                style: !!cfg?.renderer,
                labels: !!(cfg && layerLabels[cfg.id]),
                filter: !!(cfg && (layerFilters[cfg.id]?.length ?? 0) > 0),
                popup: !!(cfg && layerPopups[cfg.id]),
                form: !!(cfg && caps?.[cfg.id]?.formConfig),
              }}
              onClose={() => setSelectedLayerId(null)}
            >
              {cfg && tab === 'style' && cfg.canManage && (
                <StylesPanel
                  key={cfg.id}
                  layerId={cfg.id}
                  layerLabel={cfg.label}
                  geometryType={cfg.geometryType}
                  fallbackColor={caps?.[cfg.id]?.displayColor ?? cfg.color}
                  renderer={cfg.renderer}
                  accessToken={accessToken ?? ''}
                  onSaved={(r) => handleRendererSaved(cfg.id, r)}
                  onClose={() => setSelectedLayerId(null)}
                />
              )}
              {cfg && tab === 'filter' && (
                // One shared filter, two editors: the tab always opens showing
                // the layer's current filter — including one applied from the docked table's grid.
                <FilterPanel
                  key={cfg.id}
                  layerId={cfg.id}
                  columnsOverride={externals[cfg.id] ? (extColumns[cfg.id] ?? []) : undefined}
                  layerLabel={cfg.label}
                  accessToken={accessToken ?? ''}
                  initial={layerFilters[cfg.id] ?? []}
                  onApply={(conds) => applyLayerFilter(cfg.id, conds)}
                  onClose={() => setSelectedLayerId(null)}
                />
              )}
              {cfg && tab === 'labels' && (
                <LabelsPanel
                  key={cfg.id}
                  layerId={cfg.id}
                  columnsOverride={externals[cfg.id] ? (extColumns[cfg.id] ?? []) : undefined}
                  layerLabel={cfg.label}
                  accessToken={accessToken ?? ''}
                  initial={layerLabels[cfg.id] ?? null}
                  onApply={(c) => applyLayerLabels(cfg.id, c)}
                  onClose={() => setSelectedLayerId(null)}
                />
              )}
              {cfg && tab === 'form' && cfg.canManage && isSlotId(cfg.id) && (
                <FormPanel
                  key={cfg.id}
                  layerId={cfg.id}
                  layerLabel={cfg.label}
                  noun={cfg.geometryType === 'line' ? 'line' : cfg.geometryType === 'polygon' ? 'polygon' : 'point'}
                  accessToken={accessToken ?? ''}
                  initial={caps?.[cfg.id]?.formConfig ?? null}
                  attachmentsEnabled={!!caps?.[cfg.id]?.attachmentsEnabled}
                  onSaved={(fc) =>
                    setCaps((prev) => (prev && prev[cfg.id] ? { ...prev, [cfg.id]: { ...prev[cfg.id], formConfig: fc } } : prev))
                  }
                  onClose={() => setSelectedLayerId(null)}
                />
              )}
              {cfg && tab === 'popup' && (
                <PopupsPanel
                  key={cfg.id}
                  layerId={cfg.id}
                  columnsOverride={externals[cfg.id] ? (extColumns[cfg.id] ?? []) : undefined}
                  layerLabel={cfg.label}
                  accessToken={accessToken ?? ''}
                  initial={layerPopups[cfg.id] ?? null}
                  onApply={(c) => applyLayerPopup(cfg.id, c)}
                  onClose={() => setSelectedLayerId(null)}
                />
              )}
                </LayerInspector>
              )}
            </InspectorDock>
          )
        })()}
      </div>
      {confirmDialog}
    </div>
  )
}

/** Talus 20 — the "name your map" step on first save. Title required, description optional;
 * subsequent saves update in place with no dialog. Reuses the modal + sign-form styles. */
/** In-app leave confirm (decision-nav-blocker.md) — shown while useBlocker holds a navigation away
 * from a dirty map. Stay (the safe default — backdrop/× land here) resets the blocker; Leave proceeds
 * and the unsaved changes are discarded by the unmount. */
function LeaveMapDialog({ onStay, onLeave }: { onStay: () => void; onLeave: () => void }) {
  return (
    <div className="modal__backdrop" role="presentation" onClick={onStay}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label="Leave this map?"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal__header">
          <h2 className="modal__title">Leave this map?</h2>
          <CloseButton onClick={onStay} />
        </div>
        <section className="modal__section">
          <p>You have unsaved changes — they’ll be lost if you leave.</p>
        </section>
        <div className="sign-form__actions">
          <button type="button" className="btn btn--sm btn--danger" onClick={onLeave}>
            Leave
          </button>
          <button type="button" className="btn btn--sm" autoFocus onClick={onStay}>
            Stay
          </button>
        </div>
      </div>
    </div>
  )
}

/** Map-only layer rename (decision-map-rename.md) — a small SaveMapDialog-idiom modal: input seeded
 * with the current EFFECTIVE name, Save / Cancel, and Reset-to-original when an override exists. The
 * 80-char cap mirrors the server's _clean_label bound on OperationalLayer.title. */
function RenameLayerDialog({
  currentName,
  hasOverride,
  onCancel,
  onSave,
  onReset,
}: {
  currentName: string
  hasOverride: boolean
  onCancel: () => void
  onSave: (name: string) => void
  onReset: () => void
}) {
  const [name, setName] = useState(currentName)
  const trimmed = name.trim()
  const invalid = trimmed === '' || trimmed.length > 80
  return (
    <div className="modal__backdrop" role="presentation" onClick={onCancel}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label="Rename layer on this map"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal__header">
          <h2 className="modal__title">Rename on this map</h2>
          <CloseButton onClick={onCancel} />
        </div>
        <section className="modal__section">
          <label className="sign-form__field">
            <span>Display name (this map only)</span>
            <input
              type="text"
              value={name}
              maxLength={80}
              autoFocus
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !invalid) onSave(trimmed)
              }}
            />
          </label>
          <p className="styles-panel__muted">
            Layer rename only applies to this map.
          </p>
        </section>
        <div className="sign-form__actions">
          {hasOverride && (
            <button type="button" className="btn btn--sm labels-panel__remove" onClick={onReset}>
              Reset to original name
            </button>
          )}
          <button type="button" className="btn btn--sm" onClick={onCancel}>
            Cancel
          </button>
          <button type="button" className="btn btn--sm" disabled={invalid} onClick={() => onSave(trimmed)}>
            Save
          </button>
        </div>
      </div>
    </div>
  )
}

function SaveMapDialog({
  busy,
  onCancel,
  onSave,
}: {
  busy: boolean
  onCancel: () => void
  onSave: (title: string, description: string) => void
}) {
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [touched, setTouched] = useState(false)
  const missing = title.trim() === ''
  return (
    <div className="modal__backdrop" role="presentation" onClick={onCancel}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label="Save map"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal__header">
          <h2 className="modal__title">Save map</h2>
          <CloseButton onClick={onCancel} />
        </div>
        <section className="modal__section">
          <label className="sign-form__field">
            <span>Title *</span>
            <input
              type="text"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              onBlur={() => setTouched(true)}
              autoFocus
            />
          </label>
          {touched && missing && <p className="modal__field-error">Title is required</p>}
          <label className="sign-form__field">
            <span>Description</span>
            <input
              type="text"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </label>
        </section>
        <div className="sign-form__actions">
          <button type="button" className="btn btn--sm" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn--sm"
            disabled={busy || missing}
            onClick={() => (missing ? setTouched(true) : onSave(title.trim(), description.trim()))}
          >
            {busy ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  )
}

// --- toolbar icons (Talus 20 — AGO-style icon buttons; currentColor for theming) -----








/* Docked table panel maximize/restore (decision-map-tables.md) — same 16×16 currentColor family. */

