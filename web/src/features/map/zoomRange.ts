// A layer's visible zoom range (decision-layer-zoom-range.md). Pure — no MapLibre import — so the rules
// are unit-tested on their own (zoomRange.test.ts) and AssetMap only applies what these return.
//
// Two places hold a range: the LAYER's default (its renderer's visibleMinZoom/visibleMaxZoom, set by the
// layer's owner) and a MAP's own pair (the operationalLayers entry, set by whoever edits the map). The
// rules (§3.1, ruled 2026-11-23):
//   - the map's pair, when it has either end, REPLACES the layer's as a whole — never one end from each;
//   - otherwise the layer's default, read live (a later change by the owner reaches the map);
//   - otherwise every zoom.
// MapLibre's own semantics: drawn from `min` (inclusive) up to, not including, `max`.

import type { TocItem } from './LayerToggle'

/** The top of the zoom scale — matches AssetMap's MAX_ZOOM and the server's 0–24 bound. */
export const ZOOM_CEILING = 24

/** A stored pair, from either place. Absent or null = no bound at that end. */
export interface ZoomPair {
  visibleMinZoom?: number | null
  visibleMaxZoom?: number | null
}

/** The range actually applied: always both ends, 0 ≤ min ≤ max ≤ ZOOM_CEILING. */
export interface ZoomRange {
  min: number
  max: number
}

export const FULL_RANGE: ZoomRange = { min: 0, max: ZOOM_CEILING }

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

/** True when the pair sets at least one end — i.e. it is a range, not "use the default". */
export function hasRange(p: ZoomPair | null | undefined): boolean {
  return !!p && (isNum(p.visibleMinZoom) || isNum(p.visibleMaxZoom))
}

/** Clamp a stored pair into a usable range. A pair whose ends cross (only possible from a
 *  hand-edited document — the api refuses it) collapses to an empty range at `min`, so the layer
 *  draws nowhere rather than everywhere. */
function toRange(p: ZoomPair): ZoomRange {
  const clamp = (v: number): number => Math.min(ZOOM_CEILING, Math.max(0, v))
  const min = isNum(p.visibleMinZoom) ? clamp(p.visibleMinZoom) : 0
  const max = isNum(p.visibleMaxZoom) ? clamp(p.visibleMaxZoom) : ZOOM_CEILING
  return { min, max: Math.max(min, max) }
}

/** The range in force for one layer on one map (§3.1). */
export function rangeInForce(
  mapPair: ZoomPair | null | undefined,
  layerDefault: ZoomPair | null | undefined,
): ZoomRange {
  if (hasRange(mapPair)) return toRange(mapPair as ZoomPair)
  if (hasRange(layerDefault)) return toRange(layerDefault as ZoomPair)
  return FULL_RANGE
}

/** Whether `zoom` is inside `r` (MapLibre's rule: min inclusive, max exclusive; the ceiling counts
 *  as open so a layer with no top bound is in range at the map's deepest zoom). */
export function inRange(r: ZoomRange, zoom: number): boolean {
  return zoom >= r.min && (zoom < r.max || r.max >= ZOOM_CEILING)
}

/** Intersect [lo, hi) with the range in force, never producing lo > hi (an empty band is [x, x)). */
function within(r: ZoomRange, lo: number, hi: number): ZoomRange {
  const min = Math.min(r.max, Math.max(r.min, lo))
  const max = Math.max(min, Math.min(r.max, hi))
  return { min, max }
}

/** The bands for a layer's render pieces (§4.1). Points split at the icon zoom: circles below it,
 *  icons from it (only when the layer has an icon — without one the circle spans the whole range
 *  and the icon sibling, which draws nothing, keeps the icon zoom as before). Labels start at
 *  their own zoom or the layer's lowest, whichever is higher; a label never shows where its layer
 *  does not. Everything else — fill, line, hit target, selection highlights, a raster — is the
 *  range itself. */
export function layerBands(
  r: ZoomRange,
  opts: { iconZoom: number; hasIcon: boolean; labelMinZoom?: number | null },
): { base: ZoomRange; circle: ZoomRange; icon: ZoomRange; label: ZoomRange } {
  const circle = opts.hasIcon ? within(r, 0, opts.iconZoom) : r
  const icon = within(r, opts.iconZoom, ZOOM_CEILING)
  const label = within(r, isNum(opts.labelMinZoom) ? opts.labelMinZoom : 0, ZOOM_CEILING)
  return { base: r, circle, icon, label }
}

// --- the control (decision-layer-zoom-range.md §4.2, ruled (ii)) ---------------------------------

/** Named stops for local government, not for the world (§4.2). MapLibre zooms: its tiles are 512 px,
 *  so its zoom N draws what a 256-px-tile client (ArcGIS's levels) calls level N+1. Chosen so the
 *  map window, roughly 1,200 px wide, spans about that much ground at mid-latitudes. */
export const ZOOM_STOPS: readonly { name: string; zoom: number }[] = [
  { name: 'Region', zoom: 8 },
  { name: 'County', zoom: 10 },
  { name: 'City', zoom: 12 },
  { name: 'Neighbourhood', zoom: 14 },
  { name: 'Streets', zoom: 15 },
  { name: 'Parcels', zoom: 16 },
  { name: 'Buildings', zoom: 18 },
]

const EARTH_CIRCUMFERENCE_M = 40075016.686
const METRES_PER_SCREEN_PIXEL = 0.0254 / 96 // the CSS pixel, which is what a 1:N figure is read against

/** The scale denominator at `zoom` on a map centred at `lat` — shown only, never stored, because on a
 *  web map the scale of a given zoom changes with latitude (§4.2). */
export function scaleAt(zoom: number, lat: number): number {
  const cos = Math.cos((Math.max(-85, Math.min(85, lat)) * Math.PI) / 180)
  const metresPerPixel = (EARTH_CIRCUMFERENCE_M * cos) / (512 * 2 ** zoom)
  return metresPerPixel / METRES_PER_SCREEN_PIXEL
}

/** "1:14,000" — two significant figures, because the figure is approximate by nature. */
export function formatScale(denominator: number): string {
  if (!Number.isFinite(denominator) || denominator <= 0) return ''
  const digits = Math.floor(Math.log10(denominator)) + 1
  const unit = 10 ** Math.max(0, digits - 2)
  return `1:${(Math.round(denominator / unit) * unit).toLocaleString('en-US')}`
}

/** What the editor holds: each end a zoom, or null for "no limit" at that end. */
export interface EditorRange {
  min: number | null
  max: number | null
}

/** A stored pair as the editor shows it. 0 at the bottom and 24 at the top read as "no limit". */
export function editorFrom(p: ZoomPair | null | undefined): EditorRange {
  const min = p && isNum(p.visibleMinZoom) && p.visibleMinZoom > 0 ? p.visibleMinZoom : null
  const max = p && isNum(p.visibleMaxZoom) && p.visibleMaxZoom < ZOOM_CEILING ? p.visibleMaxZoom : null
  return { min, max }
}

/** Why the editor's range cannot be saved, or null when it can. */
export function editorProblem(e: EditorRange): string | null {
  const bad = (v: number | null): boolean => v !== null && (!Number.isFinite(v) || v < 0 || v > ZOOM_CEILING)
  if (bad(e.min) || bad(e.max)) return `Zoom runs from 0 to ${ZOOM_CEILING}.`
  if (e.min !== null && e.max !== null && e.min >= e.max) return 'The lowest zoom must be below the highest.'
  return null
}

/** The pair to store for a LAYER default: an unset end is left out; no limit at either end = no default. */
export function layerPairFrom(e: EditorRange): ZoomPair | null {
  if (e.min === null && e.max === null) return null
  return { visibleMinZoom: e.min, visibleMaxZoom: e.max }
}

/** The pair to store for a MAP's own range. A map that chose its own range must keep one even when it
 *  is "every zoom" — that is how a map widens past a layer default — so the lowest end is then stored
 *  as 0 rather than left out (both ends absent would mean "use the layer's", §3.1). */
export function mapPairFrom(e: EditorRange): ZoomPair {
  return { visibleMinZoom: e.min ?? (e.max === null ? 0 : null), visibleMaxZoom: e.max }
}

/** True when a map's own range starts further out than the layer default does — the case the panel
 *  warns about ("drawing it further out can be slow", §3.1) and does not prevent. */
export function widensOut(mapPair: ZoomPair | null | undefined, layerDefault: ZoomPair | null | undefined): boolean {
  if (!hasRange(mapPair) || !hasRange(layerDefault)) return false
  return rangeInForce(mapPair, null).min < rangeInForce(null, layerDefault).min
}

/** One line for a range: "Every zoom", "From zoom 14", "Up to zoom 12", "Zoom 14 to 18". */
export function describeRange(r: ZoomRange): string {
  const lo = r.min > 0
  const hi = r.max < ZOOM_CEILING
  const z = (v: number): string => String(Math.round(v * 10) / 10)
  if (r.min >= r.max) return 'Not drawn at any zoom'
  if (lo && hi) return `Zoom ${z(r.min)} to ${z(r.max)}`
  if (lo) return `From zoom ${z(r.min)}`
  if (hi) return `Up to zoom ${z(r.max)}`
  return 'Every zoom'
}

// --- the list and the edges (decision-layer-zoom-range.md §5, §6, §7) ----------------------------

const zText = (v: number): string => String(Math.round(v * 10) / 10)

/** The line under a layer's name when the map is outside its range (§5): "Shown from zoom 14" when
 *  zoomed out past it, "Shown up to zoom 12" when zoomed in past it; null when it is drawn here. */
export function outOfRangeNote(r: ZoomRange, zoom: number): string | null {
  if (r.min >= r.max) return 'Not drawn at any zoom'
  if (inRange(r, zoom)) return null
  if (zoom < r.min) return `Shown from zoom ${zText(r.min)}`
  return `Shown up to zoom ${zText(r.max)}`
}

/** The zoom nearest to `zoom` at which a layer with range `r` is drawn — `zoom` itself when it already
 *  is. Used to zoom into range from the list (§5) and to clamp a "zoom to" (§6). The top is exclusive,
 *  so a zoom past it comes back a tenth below it, which also reads as below it in the status bar. */
export function nearestInRange(r: ZoomRange, zoom: number): number {
  if (r.min >= r.max || inRange(r, zoom)) return zoom
  if (zoom < r.min) return r.min
  return r.max - r.min > 0.1 ? r.max - 0.1 : (r.min + r.max) / 2
}

/** The range in force for one layer-list row, as applyLayerZoomRange draws it: a vector layer's from
 *  the map's pair or its renderer, a raster's from the map's pair only; the sketch has none. */
export function tocItemRange(it: TocItem, mapRanges: Record<string, ZoomPair>): { id: string; range: ZoomRange } | null {
  if (it.kind === 'sketch') return null
  if (it.kind === 'raster') return { id: it.id, range: rangeInForce(mapRanges[it.id], null) }
  return { id: it.config.id, range: rangeInForce(mapRanges[it.config.id], it.config.renderer) }
}

/** The visibility the legend is built from (decision-layer-zoom-range.md §7): the tick boxes, with a
 *  layer outside its range at `zoom` left off as if unticked, because the legend describes what is
 *  drawn. The printed legend, the PNG's legend and the live legend feed (dashboards) all read this. */
export function legendVisibility(
  items: TocItem[],
  visible: Record<string, boolean>,
  mapRanges: Record<string, ZoomPair>,
  zoom: number | null,
): Record<string, boolean> {
  if (zoom === null) return visible
  const out = { ...visible }
  for (const it of items) {
    const r = tocItemRange(it, mapRanges)
    if (r && !inRange(r.range, zoom)) out[r.id] = false
  }
  return out
}
