import maplibregl from 'maplibre-gl'

/**
 * Draw-a-rectangle interaction, dependency-free (no MapboxDraw): the user drags one box; we report
 * its bbox in EPSG:4326 ([w, s, e, n]). A rubber-band polygon is drawn via a transient GeoJSON
 * source while dragging; Escape cancels; a degenerate click cancels — or reports its screen point
 * when the caller provides `onPoint`. Two callers: raster clip-export (decision-raster-export.md
 * c6, the original) and the Select tool's box/click session (decision-select-features.md F2, which
 * also reads the mouseup's Shift for add-to-selection).
 *
 * Returns a cleanup function (idempotent) — call it to abort the interaction (the component does on
 * unmount). dragPan/boxZoom are disabled during the draw and restored on cleanup.
 */
export type DrawBbox = [number, number, number, number]

/** Modifier state at mouseup — the select tool's add/remove intent riders. */
export interface DrawMods {
  shiftKey: boolean
  ctrlKey: boolean
  metaKey: boolean
}

interface Rect {
  w: number
  s: number
  e: number
  n: number
}

function rect(a: maplibregl.LngLat, b: maplibregl.LngLat): Rect {
  return {
    w: Math.min(a.lng, b.lng),
    e: Math.max(a.lng, b.lng),
    s: Math.min(a.lat, b.lat),
    n: Math.max(a.lat, b.lat),
  }
}

function polygon(r: Rect): GeoJSON.Feature {
  return {
    type: 'Feature',
    properties: {},
    geometry: {
      type: 'Polygon',
      coordinates: [[[r.w, r.s], [r.w, r.n], [r.e, r.n], [r.e, r.s], [r.w, r.s]]],
    },
  }
}

export function startBoxDraw(
  map: maplibregl.Map,
  cb: {
    /** `mods` carries the mouseup's modifier state (select tool: Shift adds, Ctrl/⌘ removes —
     *  F2 as amended); the original raster-export caller ignores it. */
    onComplete: (bbox: DrawBbox, mods?: DrawMods) => void
    onCancel: () => void
    /** When provided, a degenerate (no-drag) click reports its SCREEN point here instead of
     *  cancelling — the select tool's single-feature pick (decision-select-features.md F2). */
    onPoint?: (point: { x: number; y: number }, mods: DrawMods) => void
    /** Fired on each drag frame with the in-progress bbox — the ExtentPicker's live write-through
     *  (decision-extent-picker.md fork D). The other callers omit it; nothing else changes. */
    onProgress?: (bbox: DrawBbox) => void
  },
  opts?: { idPrefix?: string; color?: string },
): () => void {
  // Parameterized per caller (decision-select-features.md): the select tool draws its own
  // rubber band in selection yellow; the raster-export defaults are unchanged.
  const SRC = opts?.idPrefix ?? '__export-draw'
  const FILL = `${SRC}-fill`
  const LINE = `${SRC}-line`
  const color = opts?.color ?? '#1f4d7a'
  let start: maplibregl.LngLat | null = null
  const canvas = map.getCanvas()

  const setData = (geo: GeoJSON.Feature | GeoJSON.FeatureCollection): void => {
    ;(map.getSource(SRC) as maplibregl.GeoJSONSource | undefined)?.setData(geo)
  }

  if (!map.getSource(SRC)) {
    map.addSource(SRC, { type: 'geojson', data: { type: 'FeatureCollection', features: [] } })
    map.addLayer({
      id: FILL,
      type: 'fill',
      source: SRC,
      paint: { 'fill-color': color, 'fill-opacity': 0.15 },
    })
    map.addLayer({
      id: LINE,
      type: 'line',
      source: SRC,
      paint: { 'line-color': color, 'line-width': 2, 'line-dasharray': [2, 1] },
    })
  }

  map.dragPan.disable()
  map.boxZoom.disable()
  canvas.style.cursor = 'crosshair'

  let done = false
  const cleanup = (): void => {
    if (done) return
    done = true
    map.off('mousedown', onDown)
    map.off('mousemove', onMove)
    map.off('mouseup', onUp)
    window.removeEventListener('keydown', onKey)
    map.dragPan.enable()
    map.boxZoom.enable()
    canvas.style.cursor = ''
    if (map.getLayer(LINE)) map.removeLayer(LINE)
    if (map.getLayer(FILL)) map.removeLayer(FILL)
    if (map.getSource(SRC)) map.removeSource(SRC)
  }

  function onDown(e: maplibregl.MapMouseEvent): void {
    start = e.lngLat
  }
  function onMove(e: maplibregl.MapMouseEvent): void {
    if (!start) return
    const r = rect(start, e.lngLat)
    setData(polygon(r))
    cb.onProgress?.([r.w, r.s, r.e, r.n])
  }
  function onUp(e: maplibregl.MapMouseEvent): void {
    if (!start) return
    const r = rect(start, e.lngLat)
    cleanup()
    const mods: DrawMods = {
      shiftKey: !!e.originalEvent?.shiftKey,
      ctrlKey: !!e.originalEvent?.ctrlKey,
      metaKey: !!e.originalEvent?.metaKey,
    }
    // A click without a drag (degenerate box): the select tool's single-feature pick when the
    // caller listens for it; a cancel otherwise (never a 1-pixel export).
    if (Math.abs(r.e - r.w) < 1e-9 || Math.abs(r.n - r.s) < 1e-9) {
      if (cb.onPoint) cb.onPoint({ x: e.point.x, y: e.point.y }, mods)
      else cb.onCancel()
      return
    }
    cb.onComplete([r.w, r.s, r.e, r.n], mods)
  }
  function onKey(ev: KeyboardEvent): void {
    if (ev.key === 'Escape') {
      cleanup()
      cb.onCancel()
    }
  }

  map.on('mousedown', onDown)
  map.on('mousemove', onMove)
  map.on('mouseup', onUp)
  window.addEventListener('keydown', onKey)

  return cleanup
}
