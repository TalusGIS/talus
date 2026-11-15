// Map export shared core (decision-map-export.md) — capture the GL canvas, draw a vector legend of
// the VISIBLE operational layers, gather basemap/data attribution, and composite it all onto a 2D
// canvas. Consumed by the Screenshot (PNG) path today and the Print (PDF) path later in this slice;
// both share this capture + legend + attribution code so the two outputs never drift.
//
// Furniture (legend column, attribution footer) is drawn in a fixed LIGHT palette regardless of the
// app theme: an exported map is a document — it lands in an email, a council packet, a permit file —
// where a light, printable panel reads correctly. The MAP pixels are WYSIWYG (whatever basemap the
// user is looking at); only the furniture is normalized.

import type maplibregl from 'maplibre-gl'

import type { TocItem } from '@/features/map/LayerToggle'
import { symbolByName } from '@/features/map/pointIcons'

/** Visibility map shape shared with LayerToggle: `visible[id] ?? true`. */
export type VisibleMap = Record<string, boolean>

// ---------------------------------------------------------------------------
// Raster colormap ramps — APPROXIMATE (decision-map-export.md §3.1)
// ---------------------------------------------------------------------------
// The client holds only the colormap NAME (LayerToggle.RASTER_COLORMAPS); rio-tiler colorizes the
// tiles server-side with a 256-entry LUT the browser never sees. These curated control stops let us
// draw a compact ramp that communicates the low→high color DIRECTION. They are a visual
// approximation of the server LUT — NOT a pixel-exact match. Keep in sync with RASTER_COLORMAPS.
export const RASTER_COLORMAP_STOPS: Record<string, string[]> = {
  '': ['#000000', '#ffffff'], // default / grayscale
  viridis: ['#440154', '#3b528b', '#21918c', '#5ec962', '#fde725'],
  magma: ['#000004', '#3b0f70', '#8c2981', '#de4968', '#fe9f6d', '#fcfdbf'],
  plasma: ['#0d0887', '#6a00a8', '#b12a90', '#e16462', '#fca636', '#f0f921'],
  inferno: ['#000004', '#420a68', '#932667', '#dd513a', '#fca50a', '#fcffa4'],
  cividis: ['#00204d', '#31446b', '#666970', '#958f78', '#cbba69', '#ffea46'],
  turbo: ['#30123b', '#4145ab', '#4675ed', '#1ae4b6', '#a2fc3c', '#f9ba38', '#e5460b', '#7a0403'],
  ylorrd: ['#ffffb2', '#fecc5c', '#fd8d3c', '#f03b20', '#bd0026'],
  rdylgn: ['#a50026', '#f46d43', '#fee08b', '#a6d96a', '#1a9850'],
  spectral: ['#9e0142', '#f46d43', '#fee08b', '#66c2a5', '#5e4fa2'],
  terrain: ['#333399', '#0099ff', '#33cc66', '#ffff66', '#cc9966', '#ffffff'],
}

// ---------------------------------------------------------------------------
// Filename helpers
// ---------------------------------------------------------------------------

/** A filesystem-safe slug: lowercase, non-alphanumerics → single dashes, trimmed. Falls back to
 *  "map" when the input reduces to nothing (matches the server-side _clean_label intent, local). */
export function slugify(name: string): string {
  const s = name
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
  return s || 'map'
}

/** YYYYMMDD for the current local date — the filename date suffix (`<title>-YYYYMMDD.ext`). */
export function dateStamp(d: Date = new Date()): string {
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}${m}${day}`
}

/** Default filename stem for an export: `<title-slug>-YYYYMMDD` (no extension). */
/** True when the view is tilted enough that the PDF's flat georeferencing would be wrong
 *  (decision-geospatial-pdf-export.md §4.2). Lives in this always-loaded module so the Print tab's
 *  note (AssetMap) and the lazy PDF exporter share one rule without the main bundle paying for the
 *  PDF chunk. */
export function isPitched(pitchDegrees: number): boolean {
  return Math.abs(pitchDegrees) > 0.01
}

export function defaultExportName(mapTitle: string | null | undefined): string {
  return `${slugify(mapTitle ?? 'map')}-${dateStamp()}`
}

// ---------------------------------------------------------------------------
// Attribution (decision-map-export.md §2.3 — MANDATORY, legal/licensing)
// ---------------------------------------------------------------------------

/** Plain-text attribution credits gathered from every style source's `attribution` (HTML), tags
 *  stripped and de-duplicated. Order-stable. Baked into the export footer so basemap/data terms
 *  travel with the image. */
export function gatherAttribution(map: maplibregl.Map): string[] {
  const out: string[] = []
  const seen = new Set<string>()
  let sources: Record<string, unknown>
  try {
    sources = (map.getStyle()?.sources ?? {}) as Record<string, unknown>
  } catch {
    return out
  }
  for (const src of Object.values(sources)) {
    const html = (src as { attribution?: unknown })?.attribution
    if (typeof html !== 'string' || !html.trim()) continue
    const text = htmlToText(html)
    if (text && !seen.has(text)) {
      seen.add(text)
      out.push(text)
    }
  }
  return out
}

/** Strip HTML to its visible text (attribution strings carry <a> links). textContent only — never
 *  interpreted as markup beyond parsing, so no injection surface. */
function htmlToText(html: string): string {
  try {
    const doc = new DOMParser().parseFromString(html, 'text/html')
    return (doc.body.textContent ?? '').replace(/\s+/g, ' ').trim()
  } catch {
    return html.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim()
  }
}

// ---------------------------------------------------------------------------
// GL canvas capture (render-callback — preserveDrawingBuffer is NOT set)
// ---------------------------------------------------------------------------

/** The result of a capture: the map pixels copied into a detached 2D canvas, plus the device→CSS
 *  pixel `scale` the GL canvas was rendered at (devicePixelRatio, incl. any map pixelRatio). The
 *  scale MUST be read from the live (DOM-attached) canvas — the copy is detached, so its own
 *  `clientWidth` is 0 and would collapse the scale to 1×, shrinking the furniture on retina. */
export interface Capture {
  canvas: HTMLCanvasElement
  scale: number
}

/** Copy the live GL canvas pixels into a detached 2D canvas. Because `preserveDrawingBuffer` is off
 *  (decision-map-export.md §4), the drawing buffer is only readable within a render tick — so we
 *  read inside a one-shot `render` handler and force a repaint. The copy is independent of the GL
 *  buffer afterward. Rejects if the map is gone. (Taint doesn't surface here — it surfaces at
 *  toBlob/toDataURL readback, handled by the caller.) */
export function captureGlCanvas(map: maplibregl.Map): Promise<Capture> {
  return new Promise((resolve, reject) => {
    const gl = map.getCanvas()
    if (!gl) {
      reject(new Error('Map canvas unavailable'))
      return
    }
    // Read the scale off the LIVE canvas (attached → has clientWidth) before we copy.
    const css = gl.clientWidth
    const rawScale = css > 0 ? gl.width / css : (typeof window !== 'undefined' ? window.devicePixelRatio : 1)
    const scale = Number.isFinite(rawScale) && rawScale > 0 ? rawScale : 1
    const onRender = (): void => {
      try {
        const copy = document.createElement('canvas')
        copy.width = gl.width
        copy.height = gl.height
        const ctx = copy.getContext('2d')
        if (!ctx) {
          reject(new Error('2D context unavailable'))
          return
        }
        ctx.drawImage(gl, 0, 0) // synchronous readback inside the render tick
        resolve({ canvas: copy, scale })
      } catch (err) {
        reject(err instanceof Error ? err : new Error(String(err)))
      }
    }
    map.once('render', onRender)
    map.triggerRepaint()
  })
}

/** The live map as a bounded PNG blob (longest side ≤ `maxPx`) — the record sheet's map
 *  thumbnail (Talus+ reports F7-A). Built on captureGlCanvas, so it works without
 *  preserveDrawingBuffer; resolves null when the canvas is tainted or the encode fails — a
 *  missing thumbnail must never block a sheet. */
export async function captureMapPng(map: maplibregl.Map, maxPx = 1600): Promise<Blob | null> {
  try {
    const { canvas } = await captureGlCanvas(map)
    const scale = Math.min(1, maxPx / Math.max(canvas.width, canvas.height))
    const out = document.createElement('canvas')
    out.width = Math.max(1, Math.round(canvas.width * scale))
    out.height = Math.max(1, Math.round(canvas.height * scale))
    const ctx = out.getContext('2d')
    if (!ctx) return null
    ctx.drawImage(canvas, 0, 0, out.width, out.height)
    return await new Promise<Blob | null>((resolve) => out.toBlob((b) => resolve(b), 'image/png'))
  } catch {
    return null
  }
}

// ---------------------------------------------------------------------------
// Legend model — flattened, drawn from the symbology model (never DOM)
// ---------------------------------------------------------------------------

export type LegendEntry =
  | { kind: 'header'; label: string }
  | {
      kind: 'swatch'
      geom: 'point' | 'line' | 'polygon'
      color: string
      label: string
      indent: boolean
      /** Bundled marker name for a point layer with an icon — the legend draws the ICON, not a dot,
       *  or the feature is unreadable on paper (decision-point-icons.md fork D). */
      icon?: string | null
    }
  | { kind: 'ramp'; stops: string[]; label: string }

/** Flatten the VISIBLE tocItems into drawable legend entries. Hidden layers are excluded by
 *  construction (we never see them). uniqueValue layers become a header + one swatch per stop
 *  (+ the «default» bucket); simple/no-renderer layers become a single geometry swatch; rasters
 *  become an approximate colormap ramp. */
export function buildLegendEntries(items: TocItem[], visible: VisibleMap): LegendEntry[] {
  const entries: LegendEntry[] = []
  for (const it of items) {
    if (it.kind === 'sketch') {
      if ((visible.__sketch ?? true) && it.count > 0) {
        entries.push({ kind: 'swatch', geom: 'point', color: '#e11d48', label: `Sketch notes (${it.count})`, indent: false })
      }
      continue
    }
    if (it.kind === 'raster') {
      if (!(visible[it.id] ?? true)) continue
      const stops = RASTER_COLORMAP_STOPS[it.colormap ?? ''] ?? RASTER_COLORMAP_STOPS['']
      entries.push({ kind: 'ramp', stops, label: it.title })
      continue
    }
    // kind === 'layer'
    const c = it.config
    if (!c.canView || !(visible[c.id] ?? true)) continue
    const r = c.renderer
    const stops = r?.type === 'uniqueValue' ? (r.stops ?? []) : []
    // One icon paints every class (fork C), so it rides each stop's swatch as well as the single one.
    const icon = c.geometryType === 'point' ? (r?.pointIcon ?? null) : null
    if (stops.length > 0) {
      entries.push({ kind: 'header', label: c.label })
      for (const s of stops) {
        entries.push({ kind: 'swatch', geom: c.geometryType, color: s.color, label: s.label ?? s.value, indent: true, icon })
      }
      if (r?.default) {
        entries.push({ kind: 'swatch', geom: c.geometryType, color: r.default.color, label: r.default.label ?? 'Other', indent: true, icon })
      }
    } else {
      const color = r?.type === 'simple' ? (r.symbol?.color ?? c.color) : c.color
      entries.push({ kind: 'swatch', geom: c.geometryType, color, label: c.label, indent: false, icon })
    }
  }
  return entries
}

// Furniture palette (light, print-safe) + layout metrics in CSS px (scaled by `s`).
const FURN = {
  panelBg: '#ffffff',
  panelBorder: '#d4d4d8',
  text: '#18181b',
  muted: '#52525b',
}
const MIN_LEGEND_W = 120 // css px — floor (fits the "Legend" heading with breathing room)
const MAX_LEGEND_W = 300 // css px — ceiling (also capped to a fraction of the map width, below)
const PAD = 12
const ROW_H = 20
const SWATCH = 13
const FONT = 12
const HEAD_FONT = 13
const FOOTER_H = 22

/** Draw the legend column into `[x, y, w, h]`. Returns the count of entries that didn't fit (drawn
 *  as a "…N more" line at the foot when > 0), so the column never overruns its height. */
function drawLegendColumn(
  ctx: CanvasRenderingContext2D,
  entries: LegendEntry[],
  x: number,
  y: number,
  w: number,
  h: number,
  s: number,
): void {
  // Panel background + left divider.
  ctx.fillStyle = FURN.panelBg
  ctx.fillRect(x, y, w, h)
  ctx.strokeStyle = FURN.panelBorder
  ctx.lineWidth = Math.max(1, s)
  ctx.beginPath()
  ctx.moveTo(x + 0.5 * s, y)
  ctx.lineTo(x + 0.5 * s, y + h)
  ctx.stroke()

  const pad = PAD * s
  const rowH = ROW_H * s
  const swatch = SWATCH * s
  let cy = y + pad

  // Heading.
  ctx.fillStyle = FURN.text
  ctx.font = `600 ${HEAD_FONT * s}px sans-serif`
  ctx.textBaseline = 'middle'
  ctx.fillText('Legend', x + pad, cy + rowH / 2)
  cy += rowH + pad * 0.25

  const bottomLimit = y + h - pad
  const reserve = rowH // keep room for a possible "…N more"
  let drawn = 0
  for (let i = 0; i < entries.length; i++) {
    const isLast = i === entries.length - 1
    // If the next row won't fit — and it isn't the final entry — stop and summarize the remainder.
    if (cy + rowH > bottomLimit - (isLast ? 0 : reserve)) {
      const remaining = entries.length - drawn
      ctx.fillStyle = FURN.muted
      ctx.font = `italic ${FONT * s}px sans-serif`
      ctx.fillText(`…${remaining} more`, x + pad, cy + rowH / 2)
      return
    }
    const e = entries[i]
    const midY = cy + rowH / 2
    if (e.kind === 'header') {
      ctx.fillStyle = FURN.text
      ctx.font = `600 ${FONT * s}px sans-serif`
      ctx.fillText(clip(ctx, e.label, w - pad * 2), x + pad, midY)
    } else if (e.kind === 'ramp') {
      const rw = swatch * 2.6
      drawRamp(ctx, e.stops, x + pad, midY - swatch / 2, rw, swatch)
      ctx.fillStyle = FURN.text
      ctx.font = `${FONT * s}px sans-serif`
      ctx.fillText(clip(ctx, e.label, w - pad * 2 - rw - 6 * s), x + pad + rw + 6 * s, midY)
    } else {
      const ix = x + pad + (e.indent ? 10 * s : 0)
      drawGeomSwatch(ctx, e.geom, e.color, ix, midY, swatch, s, e.icon)
      ctx.fillStyle = FURN.text
      ctx.font = `${FONT * s}px sans-serif`
      const textX = ix + swatch + 7 * s
      ctx.fillText(clip(ctx, e.label, x + w - pad - textX), textX, midY)
    }
    cy += rowH
    drawn++
  }
}

/** Truncate `text` with an ellipsis to fit `maxW` px in the current ctx font. */
function clip(ctx: CanvasRenderingContext2D, text: string, maxW: number): string {
  if (maxW <= 0) return ''
  if (ctx.measureText(text).width <= maxW) return text
  let lo = 0
  let hi = text.length
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1
    if (ctx.measureText(text.slice(0, mid) + '…').width <= maxW) lo = mid
    else hi = mid - 1
  }
  return text.slice(0, lo) + '…'
}

/** Geometry glyph tinted to the symbology color — point dot / line stroke / filled ring, matching
 *  the TOC legend convention (LayerToggle). Centered vertically at `midY`. */
function drawGeomSwatch(
  ctx: CanvasRenderingContext2D,
  geom: 'point' | 'line' | 'polygon',
  color: string,
  x: number,
  midY: number,
  size: number,
  s: number,
  icon?: string | null,
): void {
  ctx.save()
  if (geom === 'line') {
    ctx.strokeStyle = color
    ctx.lineWidth = Math.max(2 * s, 2)
    ctx.lineCap = 'round'
    ctx.beginPath()
    ctx.moveTo(x, midY)
    ctx.lineTo(x + size, midY)
    ctx.stroke()
  } else if (geom === 'polygon') {
    ctx.fillStyle = color
    ctx.globalAlpha = 0.5
    ctx.fillRect(x, midY - size / 2, size, size)
    ctx.globalAlpha = 1
    ctx.strokeStyle = color
    ctx.lineWidth = Math.max(1.4 * s, 1)
    ctx.strokeRect(x + 0.5, midY - size / 2 + 0.5, size - 1, size - 1)
  } else if (icon && drawPointIcon(ctx, icon, color, x, midY, size)) {
    // the icon drew itself
  } else {
    // point
    ctx.fillStyle = color
    ctx.beginPath()
    ctx.arc(x + size / 2, midY, size / 2 - s, 0, Math.PI * 2)
    ctx.fill()
    ctx.strokeStyle = 'rgba(0,0,0,0.35)'
    ctx.lineWidth = Math.max(1, s)
    ctx.stroke()
  }
  ctx.restore()
}

/** Draw the bundled marker `name` into the swatch box, tinted `color`. Returns false when the
 *  symbol set has not loaded or the name is unknown, so the caller falls back to the dot — an
 *  export must never fail because a symbol chunk was slow. */
function drawPointIcon(
  ctx: CanvasRenderingContext2D,
  name: string,
  color: string,
  x: number,
  midY: number,
  size: number,
): boolean {
  const sym = symbolByName(name)
  if (!sym) return false
  const [, , vbW, vbH] = sym.viewBox.split(/[\s,]+/).map(Number)
  if (!vbW || !vbH) return false
  ctx.save()
  const fit = Math.min(size / vbW, size / vbH)
  ctx.translate(x + (size - vbW * fit) / 2, midY - (vbH * fit) / 2)
  ctx.scale(fit, fit)
  ctx.fillStyle = color
  for (const d of sym.paths) ctx.fill(new Path2D(d))
  ctx.restore()
  return true
}

/** A horizontal colormap ramp from evenly-spaced control stops (approximate — see
 *  RASTER_COLORMAP_STOPS). */
function drawRamp(ctx: CanvasRenderingContext2D, stops: string[], x: number, y: number, w: number, h: number): void {
  const grad = ctx.createLinearGradient(x, 0, x + w, 0)
  const n = stops.length
  stops.forEach((c, i) => grad.addColorStop(n === 1 ? 0 : i / (n - 1), c))
  ctx.fillStyle = grad
  ctx.fillRect(x, y, w, h)
  ctx.strokeStyle = 'rgba(0,0,0,0.35)'
  ctx.lineWidth = 1
  ctx.strokeRect(x + 0.5, y + 0.5, w - 1, h - 1)
}

/** Draw the attribution footer strip across the full composite width. */
function drawAttributionFooter(
  ctx: CanvasRenderingContext2D,
  credits: string[],
  x: number,
  y: number,
  w: number,
  h: number,
  s: number,
): void {
  ctx.fillStyle = FURN.panelBg
  ctx.fillRect(x, y, w, h)
  ctx.strokeStyle = FURN.panelBorder
  ctx.lineWidth = Math.max(1, s)
  ctx.beginPath()
  ctx.moveTo(x, y + 0.5 * s)
  ctx.lineTo(x + w, y + 0.5 * s)
  ctx.stroke()
  ctx.fillStyle = FURN.muted
  ctx.font = `${11 * s}px sans-serif`
  ctx.textBaseline = 'middle'
  const text = credits.length ? credits.join('  ·  ') : ''
  ctx.fillText(clip(ctx, text, w - PAD * 2 * s), x + PAD * s, y + h / 2)
}

// ---------------------------------------------------------------------------
// Screenshot (PNG) composite
// ---------------------------------------------------------------------------

/** Compose the map pixels + legend column + attribution footer into one 2D canvas at native
 *  resolution (decision-map-export.md §2.1). Layout:
 *      [ map image | legend column ]   (legend = full map height)
 *      [ attribution footer strip   ]   (full width)
 *  The legend column is omitted when no layer is visible (empty map → basemap + attribution). */
/** The device-px width the legend column needs to fit its widest entry (heading, swatch rows, ramp
 *  rows), before clamping. Auto-sizing keeps a one-layer legend snug instead of a wide empty band,
 *  while a busy legend caps out (composeScreenshot clamps to [MIN, min(MAX, 45% of map width)]). */
function measureLegendWidth(entries: LegendEntry[], s: number): number {
  const ctx = document.createElement('canvas').getContext('2d')
  if (!ctx) return (MIN_LEGEND_W + MAX_LEGEND_W) * 0.5 * s
  const pad = PAD * s
  const swatch = SWATCH * s
  const rampW = swatch * 2.6
  ctx.font = `600 ${HEAD_FONT * s}px sans-serif`
  let maxContent = ctx.measureText('Legend').width
  for (const e of entries) {
    if (e.kind === 'header') {
      ctx.font = `600 ${FONT * s}px sans-serif`
      maxContent = Math.max(maxContent, ctx.measureText(e.label).width)
    } else if (e.kind === 'ramp') {
      ctx.font = `${FONT * s}px sans-serif`
      maxContent = Math.max(maxContent, rampW + 6 * s + ctx.measureText(e.label).width)
    } else {
      ctx.font = `${FONT * s}px sans-serif`
      const indent = e.indent ? 10 * s : 0
      maxContent = Math.max(maxContent, indent + swatch + 7 * s + ctx.measureText(e.label).width)
    }
  }
  return pad + maxContent + pad
}

export function composeScreenshot(
  mapCanvas: HTMLCanvasElement,
  scale: number,
  entries: LegendEntry[],
  credits: string[],
): HTMLCanvasElement {
  const s = scale > 0 ? scale : 1
  const mapW = mapCanvas.width
  const mapH = mapCanvas.height
  const hasLegend = entries.length > 0
  let legendW = 0
  if (hasLegend) {
    const raw = measureLegendWidth(entries, s)
    const maxW = Math.min(MAX_LEGEND_W * s, mapW * 0.45)
    legendW = Math.round(Math.max(MIN_LEGEND_W * s, Math.min(maxW, raw)))
  }
  const footerH = Math.round(FOOTER_H * s)

  const out = document.createElement('canvas')
  out.width = mapW + legendW
  out.height = mapH + footerH
  const ctx = out.getContext('2d')
  if (!ctx) throw new Error('2D context unavailable')

  ctx.drawImage(mapCanvas, 0, 0)
  if (hasLegend) drawLegendColumn(ctx, entries, mapW, 0, legendW, mapH, s)
  drawAttributionFooter(ctx, credits, 0, mapH, out.width, footerH, s)
  return out
}

/** Full Screenshot path: capture → compose → PNG blob. Returns `{ error }` (user-facing message) on
 *  failure, most importantly a tainted-canvas SecurityError (a cross-origin source crept in) — we
 *  surface it rather than shipping a silent black image (decision-map-export.md §4). */
export async function exportScreenshotBlob(
  map: maplibregl.Map,
  items: TocItem[],
  visible: VisibleMap,
): Promise<{ blob?: Blob; error?: string }> {
  let capture: Capture
  try {
    capture = await captureGlCanvas(map)
  } catch {
    return { error: 'Could not capture the map. Please try again.' }
  }
  const entries = buildLegendEntries(items, visible)
  const credits = gatherAttribution(map)
  let composite: HTMLCanvasElement
  try {
    composite = composeScreenshot(capture.canvas, capture.scale, entries, credits)
  } catch {
    return { error: 'Could not build the image. Please try again.' }
  }
  const blob = await new Promise<Blob | null>((resolve) => {
    try {
      composite.toBlob((b) => resolve(b), 'image/png')
    } catch {
      resolve(null) // SecurityError → tainted canvas
    }
  })
  if (!blob) {
    return {
      error:
        'The map has a layer from another site that blocks image export. Remove that layer, or use a same-origin source.',
    }
  }
  return { blob }
}

/** Trigger a browser download of a blob under `filename`. */
export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  // Revoke after the click has been handled.
  setTimeout(() => URL.revokeObjectURL(url), 0)
}
