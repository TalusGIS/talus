// Print (PDF) path (decision-map-export.md §2.2, §3.3, §3.7). Lazy-loaded — AssetMap reaches this
// module via `await import(...)` so jsPDF (and this code) land in a separate chunk the Screenshot
// path and the main map bundle never pay for.
//
// Architecture bet that makes 600 DPI tractable AND sharp: the FURNITURE (title, legend, scale bar,
// north arrow, attribution) is drawn with jsPDF's own vector + text primitives — crisp at any DPI
// for free. Only the MAP is a raster, produced by an extent-preserving OFFSCREEN re-render at the
// chosen DPI. Because the basemap (Protomaps) + MVT layers are vector, that re-render gains real
// detail (sharp labels/lines), not upscaling; only true raster op-layers stay at cached-tile detail.

import { jsPDF } from 'jspdf'
import maplibregl from 'maplibre-gl'

import type { TocItem } from '@/features/map/LayerToggle'
import {
  buildLegendEntries,
  captureGlCanvas,
  gatherAttribution,
  isPitched,
  type LegendEntry,
  type VisibleMap,
} from '@/features/map/mapExport'
import { groundCorners, registerGeospatialViewport, type GroundCorners } from '@/features/map/mapExportGeoRef'

export type Orientation = 'landscape' | 'portrait'

export interface PdfExportOptions {
  /** The live map — its style/center/zoom are cloned into the offscreen render. */
  source: maplibregl.Map
  items: TocItem[]
  visible: VisibleMap
  /** Document title (title block + used nowhere else). */
  title: string
  orientation: Orientation
  /** Target DPI for the map raster. 0/≤96 ⇒ screen-ish; furniture is vector regardless. */
  dpi: number
  /** Filename WITHOUT extension (we append ".pdf"). */
  filename: string
  /** Reuse the live map's tile auth so the offscreen instance can fetch gated MVT/raster tiles. */
  transformRequest?: maplibregl.RequestTransformFunction
}

// US Letter, inches. jsPDF unit = 'in'.
const LETTER = { w: 8.5, h: 11 }
const MARGIN = 0.5
const TITLE_H = 0.62 // title block height (title + date)
const FOOTER_H = 0.28 // attribution strip
const GAP = 0.16 // gap between map and legend column
const LEGEND_MIN = 1.4
const LEGEND_MAX = 2.7

// Offscreen render guards — keep the high-DPI canvas within safe browser/GPU limits. 600-DPI Letter
// map areas land ~16–19 MP (under these caps on desktop); the caps degrade gracefully on extremes.
const MAX_PX = 24_000_000
const MAX_DIM = 8192

// Light, print-safe furniture palette (matches mapExport.ts).
const INK: [number, number, number] = [24, 24, 27]
const MUTED: [number, number, number] = [82, 82, 91]
const PANEL: [number, number, number] = [255, 255, 255]
const BORDER: [number, number, number] = [212, 212, 216]

function hexToRgb(hex: string): [number, number, number] {
  const h = hex.replace('#', '')
  const s = h.length === 3 ? h.replace(/(.)/g, '$1$1') : h
  const n = parseInt(s.slice(0, 6), 16)
  if (Number.isNaN(n)) return [128, 128, 128]
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255]
}

/** Meters per PDF inch at the map's current center+zoom. Independent of DPI and page size: the
 *  offscreen render keeps the same zoom, so physical scale is fixed by zoom+latitude. Derived from
 *  the live map via unproject (authoritative regardless of tile size) — 96 CSS px = 1 inch. */
function metersPerInch(map: maplibregl.Map): number {
  const c = map.getCanvas()
  const y = c.clientHeight / 2
  const x = c.clientWidth / 2
  const a = map.unproject([x - 100, y])
  const b = map.unproject([x + 100, y])
  const metersPerCssPx = a.distanceTo(b) / 200
  return metersPerCssPx * 96
}

/** Largest 1/2/5 × 10ⁿ ≤ x. */
function niceNum(x: number): number {
  if (x <= 0) return 0
  const p = Math.pow(10, Math.floor(Math.log10(x)))
  const f = x / p
  const n = f >= 5 ? 5 : f >= 2 ? 2 : 1
  return n * p
}

/** A round scale-bar distance for US municipal readers (feet, rolling up to miles) + its length in
 *  inches on the page. `maxIn` bounds the bar width. */
function scaleBar(mPerIn: number, maxIn: number): { label: string; lengthIn: number } | null {
  if (!(mPerIn > 0)) return null
  const ftPerIn = mPerIn * 3.28084
  const maxFeet = ftPerIn * maxIn
  if (maxFeet >= 5280) {
    const mi = niceNum(maxFeet / 5280)
    return { label: `${fmtNum(mi)} mi`, lengthIn: (mi * 5280) / ftPerIn }
  }
  const ft = niceNum(maxFeet)
  if (ft <= 0) return null
  return { label: `${fmtNum(ft)} ft`, lengthIn: ft / ftPerIn }
}

function fmtNum(n: number): string {
  return n >= 1 ? n.toLocaleString('en-US') : String(n)
}

// ---------------------------------------------------------------------------
// Offscreen high-DPI map render (extent-preserving)
// ---------------------------------------------------------------------------

/** Render the map to a PNG data URL sized `widthIn × heightIn` at `dpi`, keeping the live map's
 *  center+zoom (so extent is WYSIWYG-centered; aspect differs from the live viewport by design,
 *  §3.7). A throwaway MapLibre instance in a hidden container with `preserveDrawingBuffer:true` (safe
 *  — it's disposed at once, no permanent GPU tax on the live map). Returns the data URL + the DPI
 *  actually used (may be reduced by the canvas clamp) + the image's four ground corners (read from
 *  THIS instance — its aspect is the page's, not the viewport's — for the PDF's georeferencing). */
async function renderMapImage(
  source: maplibregl.Map,
  widthIn: number,
  heightIn: number,
  dpi: number,
  transformRequest?: maplibregl.RequestTransformFunction,
): Promise<{ dataUrl: string; dpiUsed: number; corners: GroundCorners } | { error: string }> {
  let dpiUsed = Math.max(72, dpi)
  const fit = (): void => {
    const pxW = widthIn * dpiUsed
    const pxH = heightIn * dpiUsed
    const areaScale = Math.sqrt(MAX_PX / (pxW * pxH))
    const dimScale = Math.min(MAX_DIM / pxW, MAX_DIM / pxH)
    const scale = Math.min(1, areaScale, dimScale)
    if (scale < 1) dpiUsed = Math.max(72, Math.floor(dpiUsed * scale))
  }
  fit()

  const holder = document.createElement('div')
  holder.style.cssText = `position:absolute;left:-99999px;top:0;width:${widthIn * 96}px;height:${heightIn * 96}px;pointer-events:none;`
  document.body.appendChild(holder)

  let style: maplibregl.StyleSpecification
  try {
    style = source.getStyle()
  } catch {
    holder.remove()
    return { error: 'Could not read the map style.' }
  }

  let off: maplibregl.Map
  try {
    off = new maplibregl.Map({
      container: holder,
      style,
      center: source.getCenter(),
      zoom: source.getZoom(),
      bearing: source.getBearing(),
      pitch: source.getPitch(),
      interactive: false,
      attributionControl: false,
      // Offscreen instance only — safe to preserve the buffer for toDataURL; it's disposed at once, so
      // no permanent GPU-memory tax on the live map (which stays render-callback capture, §4).
      canvasContextAttributes: { preserveDrawingBuffer: true },
      pixelRatio: dpiUsed / 96,
      fadeDuration: 0,
      transformRequest,
    })
  } catch (e) {
    holder.remove()
    return { error: `Could not create the render surface (${e instanceof Error ? e.message : String(e)}).` }
  }

  // Capture the first source/style/tile error so a failed raster load surfaces a real message
  // instead of a blank (or a bare throw). MapLibre reports these as 'error' events, not exceptions.
  let firstError = ''
  off.on('error', (ev) => {
    const m = ev?.error?.message
    if (m && !firstError) firstError = m
  })

  const cleanup = (): void => {
    try {
      off.remove()
    } catch {
      /* already gone */
    }
    holder.remove()
  }

  try {
    let idled = false
    await new Promise<void>((resolve) => {
      let done = false
      const finish = (): void => {
        if (done) return
        done = true
        resolve()
      }
      // `idle` = style loaded, all tiles in view fetched, no transitions pending.
      off.once('idle', () => {
        idled = true
        finish()
      })
      // Best-effort ceiling: capture what we have rather than hang if a tile source stalls.
      setTimeout(finish, 20000)
    })
    const canvas = off.getCanvas()
    let dataUrl: string
    try {
      dataUrl = canvas.toDataURL('image/png')
    } catch {
      cleanup()
      return {
        error:
          'The map has a layer from another site that blocks image export. Remove that layer, or use a same-origin source.',
      }
    }
    // Ground corners of the image, in CSS px of the offscreen container (unproject's frame). Read
    // before the instance is removed.
    const corners = mapGroundCorners(off, widthIn * 96, heightIn * 96)
    cleanup()
    // Never idled AND a source errored → the raster/tiles likely didn't load; report the real cause.
    if (!idled && firstError) return { error: `A map layer failed to load (${firstError}).` }
    return { dataUrl, dpiUsed, corners }
  } catch (e) {
    cleanup()
    const why = firstError || (e instanceof Error ? e.message : String(e))
    return { error: `Could not render the map (${why}). Try a lower DPI.` }
  }
}

/** The four ground corners of a map's canvas (UL, LL, LR, UR). `fallbackW/H` stand in when the
 *  canvas reports no CSS size (a hidden container). */
function mapGroundCorners(map: maplibregl.Map, fallbackW: number, fallbackH: number): GroundCorners {
  const c = map.getCanvas()
  const w = c.clientWidth || fallbackW
  const h = c.clientHeight || fallbackH
  return groundCorners((x, y) => map.unproject([x, y]), w, h)
}

// ---------------------------------------------------------------------------
// jsPDF furniture (vector + text — crisp at any DPI)
// ---------------------------------------------------------------------------

/** Width (in) the legend column needs for its widest entry, clamped to [LEGEND_MIN, LEGEND_MAX]. */
function legendWidthIn(doc: jsPDF, entries: LegendEntry[]): number {
  const pad = 0.12
  const swatch = 0.13
  const gap = 0.07
  const rampW = 0.34
  doc.setFont('helvetica', 'bold')
  doc.setFontSize(10)
  let maxW = doc.getTextWidth('Legend')
  for (const e of entries) {
    if (e.kind === 'header') {
      doc.setFont('helvetica', 'bold')
      doc.setFontSize(9)
      maxW = Math.max(maxW, doc.getTextWidth(e.label))
    } else if (e.kind === 'ramp') {
      doc.setFont('helvetica', 'normal')
      doc.setFontSize(9)
      maxW = Math.max(maxW, rampW + gap + doc.getTextWidth(e.label))
    } else {
      doc.setFont('helvetica', 'normal')
      doc.setFontSize(9)
      const indent = e.indent ? 0.1 : 0
      maxW = Math.max(maxW, indent + swatch + gap + doc.getTextWidth(e.label))
    }
  }
  return Math.min(LEGEND_MAX, Math.max(LEGEND_MIN, pad + maxW + pad))
}

function drawLegend(doc: jsPDF, entries: LegendEntry[], x: number, y: number, w: number, h: number): void {
  doc.setFillColor(...PANEL)
  doc.setDrawColor(...BORDER)
  doc.setLineWidth(0.008)
  doc.rect(x, y, w, h, 'FD')

  const pad = 0.12
  const rowH = 0.2
  const swatch = 0.13
  let cy = y + pad

  doc.setFont('helvetica', 'bold')
  doc.setFontSize(10)
  doc.setTextColor(...INK)
  doc.text('Legend', x + pad, cy + rowH * 0.6)
  cy += rowH + 0.04

  const bottom = y + h - pad
  let drawn = 0
  for (let i = 0; i < entries.length; i++) {
    const isLast = i === entries.length - 1
    if (cy + rowH > bottom - (isLast ? 0 : rowH)) {
      doc.setFont('helvetica', 'italic')
      doc.setFontSize(8.5)
      doc.setTextColor(...MUTED)
      doc.text(`…${entries.length - drawn} more`, x + pad, cy + rowH * 0.6)
      return
    }
    const e = entries[i]
    const midY = cy + rowH / 2
    if (e.kind === 'header') {
      doc.setFont('helvetica', 'bold')
      doc.setFontSize(9)
      doc.setTextColor(...INK)
      doc.text(clip(doc, e.label, w - pad * 2), x + pad, midY + 0.03)
    } else if (e.kind === 'ramp') {
      const rampW = 0.34
      drawRamp(doc, e.stops, x + pad, midY - swatch / 2, rampW, swatch)
      doc.setFont('helvetica', 'normal')
      doc.setFontSize(9)
      doc.setTextColor(...INK)
      doc.text(clip(doc, e.label, w - pad * 2 - rampW - 0.07), x + pad + rampW + 0.07, midY + 0.03)
    } else {
      const ix = x + pad + (e.indent ? 0.1 : 0)
      drawGeomSwatch(doc, e.geom, e.color, ix, midY, swatch)
      doc.setFont('helvetica', 'normal')
      doc.setFontSize(9)
      doc.setTextColor(...INK)
      const tx = ix + swatch + 0.07
      doc.text(clip(doc, e.label, x + w - pad - tx), tx, midY + 0.03)
    }
    cy += rowH
    drawn++
  }
}

function drawGeomSwatch(
  doc: jsPDF,
  geom: 'point' | 'line' | 'polygon',
  color: string,
  x: number,
  midY: number,
  size: number,
): void {
  const rgb = hexToRgb(color)
  if (geom === 'line') {
    doc.setDrawColor(...rgb)
    doc.setLineWidth(0.025)
    doc.line(x, midY, x + size, midY)
  } else if (geom === 'polygon') {
    doc.setFillColor(...rgb)
    doc.setDrawColor(...rgb)
    doc.setLineWidth(0.01)
    doc.rect(x, midY - size / 2, size, size, 'FD')
  } else {
    doc.setFillColor(...rgb)
    doc.circle(x + size / 2, midY, size / 2, 'F')
  }
}

/** A ramp as evenly-interpolated vertical slivers (jsPDF has no native gradient); stays vector. */
function drawRamp(doc: jsPDF, stops: string[], x: number, y: number, w: number, h: number): void {
  const N = 48
  const rgbStops = stops.map(hexToRgb)
  for (let i = 0; i < N; i++) {
    const t = i / (N - 1)
    const rgb = sampleStops(rgbStops, t)
    doc.setFillColor(...rgb)
    doc.rect(x + (i / N) * w, y, w / N + 0.002, h, 'F')
  }
  doc.setDrawColor(...BORDER)
  doc.setLineWidth(0.006)
  doc.rect(x, y, w, h, 'S')
}

function sampleStops(stops: [number, number, number][], t: number): [number, number, number] {
  if (stops.length === 1) return stops[0]
  const pos = t * (stops.length - 1)
  const lo = Math.floor(pos)
  const hi = Math.min(lo + 1, stops.length - 1)
  const f = pos - lo
  const a = stops[lo]
  const b = stops[hi]
  return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f]
}

/** Truncate to fit `maxW` inches in the current jsPDF font. */
function clip(doc: jsPDF, text: string, maxW: number): string {
  if (maxW <= 0) return ''
  if (doc.getTextWidth(text) <= maxW) return text
  let lo = 0
  let hi = text.length
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1
    if (doc.getTextWidth(text.slice(0, mid) + '…') <= maxW) lo = mid
    else hi = mid - 1
  }
  return text.slice(0, lo) + '…'
}

/** Scale bar with a light backing box, bottom-left of the map area. */
function drawScaleBar(doc: jsPDF, bar: { label: string; lengthIn: number }, mapX: number, mapBottom: number): void {
  const inset = 0.1
  const barH = 0.05
  const boxPad = 0.05
  const textH = 0.13
  const boxW = Math.max(bar.lengthIn, doc.getTextWidth(bar.label)) + boxPad * 2
  const boxH = barH + textH + boxPad * 2
  const bx = mapX + inset
  const by = mapBottom - inset - boxH
  doc.setFillColor(...PANEL)
  doc.setDrawColor(...BORDER)
  doc.setLineWidth(0.006)
  doc.rect(bx, by, boxW, boxH, 'FD')
  // The bar: a filled rule with end ticks.
  const barY = by + boxPad + textH
  doc.setFillColor(...INK)
  doc.rect(bx + boxPad, barY, bar.lengthIn, barH, 'F')
  doc.setFont('helvetica', 'normal')
  doc.setFontSize(7.5)
  doc.setTextColor(...INK)
  doc.text(bar.label, bx + boxPad, by + boxPad + textH - 0.03)
}

/** North arrow with a light backing, top-right of the map area. Points to true north on the page:
 *  the map renders at its bearing (clockwise degrees of the view's "up" from north), so the arrow
 *  turns counter-clockwise by that bearing. At bearing 0 it points straight up. The "N" stays
 *  upright beneath it. */
function drawNorthArrow(doc: jsPDF, mapRight: number, mapTop: number, bearing: number): void {
  const inset = 0.1
  const s = 0.26
  const cx = mapRight - inset - s / 2
  const top = mapTop + inset
  doc.setFillColor(...PANEL)
  doc.setDrawColor(...BORDER)
  doc.setLineWidth(0.006)
  // Square enough that the triangle fits at any rotation (its corners reach ~0.6·s from the centre).
  doc.rect(cx - s / 2 - 0.05, top - 0.03, s + 0.1, s + 0.21, 'FD')
  // Arrow triangle, rotated about its centre. jsPDF's y grows downward, so a positive angle here is
  // clockwise on the page; north sits at −bearing from "up".
  const cy = top + s / 2
  const a = (-bearing * Math.PI) / 180
  const cos = Math.cos(a)
  const sin = Math.sin(a)
  const rot = (x: number, y: number): [number, number] => [
    cx + (x - cx) * cos - (y - cy) * sin,
    cy + (x - cx) * sin + (y - cy) * cos,
  ]
  const [ax, ay] = rot(cx, top)
  const [bx, by] = rot(cx - s / 3, top + s)
  const [qx, qy] = rot(cx + s / 3, top + s)
  doc.setFillColor(...INK)
  doc.triangle(ax, ay, bx, by, qx, qy, 'F')
  doc.setFont('helvetica', 'bold')
  doc.setFontSize(8)
  doc.setTextColor(...INK)
  doc.text('N', cx, top + s + 0.15, { align: 'center' })
}

// ---------------------------------------------------------------------------
// Orchestration
// ---------------------------------------------------------------------------

/** Build and download the one-page PDF. Returns an error message on failure, or null on success.
 *  Wraps `buildPdf` so any thrown error is logged AND surfaced to the user as a real message —
 *  never swallowed into the panel's generic "Export failed" (which hid the true cause). */
export async function exportMapPdf(opts: PdfExportOptions): Promise<string | null> {
  try {
    return await buildPdf(opts)
  } catch (err) {
    console.error('[map-export] PDF export threw:', err)
    const msg = err instanceof Error ? err.message : String(err)
    return `Could not build the PDF (${msg}). Try a lower DPI, or remove the layer if it persists.`
  }
}

async function buildPdf(opts: PdfExportOptions): Promise<string | null> {
  const { source, items, visible, title, orientation, dpi, filename, transformRequest } = opts
  const pageW = orientation === 'landscape' ? LETTER.h : LETTER.w
  const pageH = orientation === 'landscape' ? LETTER.w : LETTER.h

  const doc = new jsPDF({ unit: 'in', format: 'letter', orientation })
  const entries = buildLegendEntries(items, visible)
  const credits = gatherAttribution(source)

  // Layout bands (inches).
  const bandTop = MARGIN + TITLE_H
  const bandBottom = pageH - MARGIN - FOOTER_H
  const bandH = bandBottom - bandTop
  const bandLeft = MARGIN
  const bandW = pageW - MARGIN * 2

  const legendW = entries.length > 0 ? legendWidthIn(doc, entries) : 0
  const mapW = bandW - (legendW > 0 ? legendW + GAP : 0)
  const mapH = bandH
  const mapX = bandLeft
  const mapY = bandTop

  // Prefer the high-DPI offscreen render; if it fails (a fragile source, a second-WebGL-context
  // limit, etc.), fall back to the PROVEN live-canvas capture the PNG path uses — at screen DPI, so
  // the export still succeeds (softer + letterboxed) rather than hard-failing. The reason is logged.
  let mapUrl: string
  let imgAspect: number
  let corners: GroundCorners
  const render = await renderMapImage(source, mapW, mapH, dpi, transformRequest)
  if ('error' in render) {
    console.warn('[map-export] high-DPI render failed; falling back to screen capture:', render.error)
    try {
      const cap = await captureGlCanvas(source)
      mapUrl = cap.canvas.toDataURL('image/png')
      imgAspect = cap.canvas.width / cap.canvas.height
      // The capture IS the live viewport, so its corners are the live map's.
      const live = source.getCanvas()
      corners = mapGroundCorners(source, live.clientWidth, live.clientHeight)
    } catch {
      return render.error
    }
  } else {
    mapUrl = render.dataUrl
    imgAspect = mapW / mapH // offscreen render is already shaped to the map-area aspect
    corners = render.corners
  }

  // Letterbox the image within the map area so nothing is distorted. No-op for the offscreen render
  // (it matches the area aspect); the screen-capture fallback carries the live viewport's aspect.
  const areaAspect = mapW / mapH
  let drawW = mapW
  let drawH = mapH
  if (imgAspect > areaAspect) drawH = mapW / imgAspect
  else drawW = mapH * imgAspect
  const dx = mapX + (mapW - drawW) / 2
  const dy = mapY + (mapH - drawH) / 2

  // 'FAST' Flate-compresses the image stream — without it jsPDF embeds raw RGB, which balloons a
  // 300-DPI page to ~14 MB. FAST captures nearly all of SLOW's size win at far lower CPU.
  doc.addImage(mapUrl, 'PNG', dx, dy, drawW, drawH, undefined, 'FAST')
  doc.setDrawColor(...BORDER)
  doc.setLineWidth(0.008)
  doc.rect(mapX, mapY, mapW, mapH, 'S')

  // Georeference the image's rectangle (decision-geospatial-pdf-export.md). A pitched view is a
  // perspective, not a ground mapping, so it is exported as seen and left unregistered (§4.2); the
  // Print tab says so. Bearing is fine — four corners register a rotated image exactly.
  if (!isPitched(source.getPitch())) registerGeospatialViewport(doc, { x: dx, y: dy, w: drawW, h: drawH }, corners)

  // Map overlays (drawn over the raster).
  const bar = scaleBar(metersPerInch(source), Math.min(1.8, mapW * 0.35))
  if (bar) drawScaleBar(doc, bar, mapX, mapY + mapH)
  drawNorthArrow(doc, mapX + mapW, mapY, source.getBearing())

  // Legend column.
  if (legendW > 0) drawLegend(doc, entries, mapX + mapW + GAP, bandTop, legendW, bandH)

  // Title block.
  doc.setFont('helvetica', 'bold')
  doc.setFontSize(15)
  doc.setTextColor(...INK)
  doc.text(clip(doc, title || 'Map', bandW - 2.2), MARGIN, MARGIN + 0.26)
  doc.setFont('helvetica', 'normal')
  doc.setFontSize(9)
  doc.setTextColor(...MUTED)
  const dateStr = new Date().toLocaleDateString('en-US', { year: 'numeric', month: 'long', day: 'numeric' })
  doc.text(dateStr, pageW - MARGIN, MARGIN + 0.26, { align: 'right' })

  // Attribution footer.
  doc.setFont('helvetica', 'normal')
  doc.setFontSize(7.5)
  doc.setTextColor(...MUTED)
  const credit = credits.length ? credits.join('   ·   ') : ''
  doc.text(clip(doc, credit, bandW), MARGIN, pageH - MARGIN + 0.02)

  doc.save(`${filename}.pdf`)
  return null
}
