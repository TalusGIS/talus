/**
 * Point-icon rasterization (decision-point-icons.md fork A2, ruled 2026-08-27).
 *
 * WHY CANVAS AND NOT SDF. MapLibre's `icon-color` and `icon-halo-*` only apply to SDF images, which
 * would have tinted an icon at runtime for free. We don't use them: SDF thresholds an image's alpha,
 * and these markers are drawn for a 15px box with thin features — hydrant bonnets, valve stems, pole
 * crossarms — which is exactly what SDF erases. So each (icon, colour) pair is rasterized once, at
 * full detail, with its halo baked in by stroking the path before filling it.
 *
 * WHY 64px. Rasterizing at a fixed size and letting `icon-size` scale DOWN keeps the size control
 * live with zero re-rasterization — scaling a bitmap down is well filtered, scaling it up is what
 * looks soft. Only a COLOUR change produces a new image.
 *
 * THE HALO is not decoration: a monochrome marker tinted a dark colour is illegible on the dark
 * basemap and on aerial imagery. It is the direct descendant of the circle layer's
 * `circle-stroke-color`, and it is why one icon reads on every basemap.
 */
import type { ExpressionSpecification, Map as MapLibreMap } from 'maplibre-gl'

import type { Renderer } from '@/lib/api'

import type { PointSymbol } from '@/features/map/point-symbols/manifest'

/** Rasterization box, in CSS px. `icon-size` scales down from here (see the header note). */
export const ICON_RASTER_PX = 64
/** Halo width as a fraction of the raster box. Tuned 0.055 → 0.04: at
 *  0.055 the median icon across all 224 was 51% halo pixels and thin glyphs (towers, antennas,
 *  turbines) read as white blobs on the dark flavour; 0.04 brings the median to 43% with only 4
 *  icons above 60%, while the ring still separates chunky icons from a dark tile at small sizes. */
const HALO_FRACTION = 0.04
/** Device-pixel oversampling, capped: past 2x the extra memory buys nothing visible for a marker. */
const dpr = (): number => Math.min(2, Math.max(1, Math.round(globalThis.devicePixelRatio || 1)))

/** A stable MapLibre image id for one (icon, colour, halo) triple. Colour is part of the id because
 *  the tint is BAKED — two categories of the same icon are two images (bounded by the stop count). */
export function iconImageId(name: string, color: string, halo: string): string {
  return `tps:${name}:${color.toLowerCase()}:${halo.toLowerCase()}`
}

/**
 * Draw `sym` into an ImageData at ICON_RASTER_PX * dpr, tinted `color` with a `halo` outline.
 *
 * The icon is scaled by ITS OWN viewBox, never a hardcoded 15 — six Temaki icons are 50x50 and would
 * otherwise render at roughly a third size (decision-point-icons.md §2). Returns null when a 2D
 * context is unavailable (a headless or memory-starved browser); callers fall back to the circle.
 */
export function rasterizeSymbol(
  sym: PointSymbol,
  color: string,
  halo: string,
): { data: ImageData; pixelRatio: number } | null {
  const scale = dpr()
  const px = ICON_RASTER_PX * scale
  const canvas = document.createElement('canvas')
  canvas.width = px
  canvas.height = px
  const ctx = canvas.getContext('2d')
  if (!ctx) return null

  const [, , vbW, vbH] = sym.viewBox.split(/[\s,]+/).map(Number)
  if (!vbW || !vbH) return null

  // Fit the viewBox into the square box, preserving aspect, with room for the halo stroke to sit
  // fully inside the bitmap (a stroke centred on the edge would otherwise be clipped in half).
  const haloPx = px * HALO_FRACTION
  const inset = haloPx
  const fit = Math.min((px - inset * 2) / vbW, (px - inset * 2) / vbH)
  ctx.setTransform(fit, 0, 0, fit, (px - vbW * fit) / 2, (px - vbH * fit) / 2)

  // Halo first (stroked at 2x width so half of it survives outside the fill), then the fill over it.
  ctx.lineJoin = 'round'
  ctx.lineCap = 'round'
  ctx.strokeStyle = halo
  ctx.lineWidth = (haloPx * 2) / fit // back out of the transform: stroke width is in user units
  for (const d of sym.paths) ctx.stroke(new Path2D(d))
  ctx.fillStyle = color
  for (const d of sym.paths) ctx.fill(new Path2D(d))

  ctx.setTransform(1, 0, 0, 1, 0, 0)
  return { data: ctx.getImageData(0, 0, px, px), pixelRatio: scale }
}

/**
 * Ensure the image for (sym, color, halo) exists on `map`, and return its id.
 *
 * Idempotent via `hasImage`, so it is safe to call on every repaint. Images added at runtime survive
 * a basemap switch — `paintBasemap` removes and adds LAYERS within the existing style and never
 * calls `setStyle()` (decision-point-icons.md §3.4) — so this is not re-run on a flavour change.
 */
export function ensureIconImage(
  map: MapLibreMap,
  sym: PointSymbol,
  color: string,
  halo: string,
): string | null {
  const id = iconImageId(sym.name, color, halo)
  if (map.hasImage(id)) return id
  const raster = rasterizeSymbol(sym, color, halo)
  if (!raster) return null
  map.addImage(id, raster.data, { pixelRatio: raster.pixelRatio })
  return id
}

/** `icon-size` for a rendered diameter of `sizePx`: the raster is ICON_RASTER_PX wide, so this is
 *  always a scale DOWN for any sane marker size. */
export function iconSizeFor(sizePx: number): number {
  return sizePx / ICON_RASTER_PX
}

// ---------------------------------------------------------------------------
// Lazy manifest — the symbol set is ~50 KB gzipped, so it rides its own chunk
// and never lands on the portal home / catalog / admin critical path. AssetMap
// preloads it on mount, so it is normally resolved before any layer registers;
// a layer that registers first simply draws its circle and is rebuilt when the
// chunk lands (the same rebuild path an icon change already needs — fork B).
// ---------------------------------------------------------------------------

type Manifest = typeof import('@/features/map/point-symbols/manifest')

let manifest: Manifest | null = null
let pending: Promise<void> | null = null

export function pointSymbolsLoaded(): boolean {
  return manifest !== null
}

/** Start (or join) the manifest load. Resolves when the set is usable; never rejects — a failed
 *  chunk load leaves icons unavailable and every point layer drawing its circle. */
export function preloadPointSymbols(): Promise<void> {
  if (manifest) return Promise.resolve()
  pending ??= import('@/features/map/point-symbols/manifest')
    .then((m) => {
      manifest = m
    })
    .catch(() => {
      pending = null // let a later attempt retry
    })
  return pending
}

/** Synchronous lookup. Undefined when the manifest has not loaded yet OR the name is unknown —
 *  a renderer saved against an icon later dropped from the set degrades to a circle, never throws. */
export function symbolByName(name: string | null | undefined): PointSymbol | undefined {
  return manifest && name ? manifest.pointSymbol(name) : undefined
}

export function pointSymbolCategories(): { key: string; label: string }[] {
  return manifest?.POINT_SYMBOL_CATEGORIES ?? []
}

export function allPointSymbols(): PointSymbol[] {
  return manifest?.POINT_SYMBOLS ?? []
}

// ---------------------------------------------------------------------------
// Renderer -> MapLibre `icon-image`
// ---------------------------------------------------------------------------

/** The halo colour. White is the conventional marker halo (Maki, AGO, QGIS) because the halo's job
 *  is contrast against the BASEMAP, not against the icon — a dark halo like the circle layer's
 *  `circle-stroke-color` would vanish behind a dark-tinted icon on the dark flavour. */
export const ICON_HALO = '#ffffff'

/**
 * Resolve a renderer into an `icon-image` value, adding every image it needs to `map` first.
 *
 * `simple` yields one image id. `uniqueValue` yields a `match` over the SAME field expression
 * `resolveColor` uses, mapping each class to its own tinted image — so one icon paints N class
 * colours, which is fork C and costs nothing extra (the tint is baked, so a class is just another
 * image). Returns null when there is no icon, the manifest has not loaded, or the name is unknown.
 */
export function resolveIconImage(
  map: MapLibreMap,
  renderer: Renderer | null | undefined,
  fallbackColor: string,
): string | ExpressionSpecification | null {
  const sym = symbolByName(renderer?.pointIcon)
  if (!renderer || !sym) return null

  if (renderer.type === 'simple') {
    return ensureIconImage(map, sym, renderer.symbol?.color ?? fallbackColor, ICON_HALO)
  }

  const stops = renderer.stops ?? []
  if (!renderer.field || stops.length === 0) {
    return ensureIconImage(map, sym, fallbackColor, ICON_HALO)
  }
  const fallbackId = ensureIconImage(map, sym, renderer.default?.color ?? fallbackColor, ICON_HALO)
  if (!fallbackId) return null

  const match: unknown[] = ['match', ['to-string', ['get', renderer.field]]]
  for (const s of stops) {
    const id = ensureIconImage(map, sym, s.color, ICON_HALO)
    if (!id) continue
    match.push(s.value, id)
  }
  match.push(fallbackId)
  return match as unknown as ExpressionSpecification
}

/** Does this renderer paint an icon that is actually available right now? The layer TYPE depends on
 *  it (circle vs symbol), and MapLibre cannot mutate a layer's type — so a change here means a
 *  rebuild, not a paint update (fork B). */
export function rendererUsesIcon(renderer: Renderer | null | undefined): boolean {
  return symbolByName(renderer?.pointIcon) !== undefined
}
