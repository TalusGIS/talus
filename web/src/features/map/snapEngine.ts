import type { Map as MapLibreMap } from 'maplibre-gl'

import { queryLayerFeatures, type LayerFeature } from '@/lib/api'

/**
 * Snap engine (decision-web-editor-snapping.md §3.1) — answers terra-draw's SYNCHRONOUS
 * `toCustom` callback (and the point-place surface) by nearest-vertex search over TRUE stored
 * geometry, never rendered tiles: while active it bbox-fetches each enabled editable layer's
 * viewport features through the existing view-gated `GET /features` route and indexes every
 * vertex. Snapped coordinates are the exact floats the API served, so a snapped write
 * round-trips bit-identical (with the §3.2 read-precision bump). Density guardrail: a layer
 * with more than the page cap in-viewport (or a runaway vertex count) is PAUSED — no paging,
 * no snapping, a "zoom in to snap" hint via onStatus. Framework-free; AssetMap owns the React
 * state (master toggle + per-layer checks) and hands the engine the enabled set.
 */

/** One snappable vertex: the exact WGS-84 coordinate as served. */
interface SnapVertex {
  lng: number
  lat: number
  fid: string
  /** A point-feature target — beats any line/polygon vertex in the tie-break (§3.3). */
  isPoint: boolean
}

interface LayerState {
  vertices: SnapVertex[]
  /** The padded bbox the vertices cover; a viewport still inside it skips the refetch. */
  bounds: { w: number; s: number; e: number; n: number } | null
  paused: boolean
  ctrl: AbortController | null
}

export const SNAP_TOLERANCE_PX = 12
// Server _MAX_LIMIT — one page covers the whole viewport or the layer is too dense to snap.
const PAGE_CAP = 5000
// Line/polygon layers can out-vertex the feature cap (5k parcels ≫ 50k vertices) — same pause.
const VERTEX_CAP = 50000
const FETCH_PAD = 0.25 // fraction of the viewport span padded each side (small pans don't refetch)
const MOVE_DEBOUNCE_MS = 250

export class SnapEngine {
  private map: MapLibreMap
  private getToken: () => string
  private onStatus?: (paused: Record<string, boolean>) => void
  private layers = new Map<string, LayerState>()
  private enabled: string[] = []
  private excludedFid: string | null = null
  private suppressed = false // Alt held — bypass mid-gesture without disturbing the toggle
  private active = false
  private moveTimer: ReturnType<typeof setTimeout> | null = null

  constructor(opts: {
    map: MapLibreMap
    getToken: () => string
    onStatus?: (paused: Record<string, boolean>) => void
  }) {
    this.map = opts.map
    this.getToken = opts.getToken
    this.onStatus = opts.onStatus
  }

  /** Start listening (map moves refresh the index; Alt suppresses). Idempotent. */
  activate(): void {
    if (this.active) return
    this.active = true
    this.map.on('moveend', this.handleMoveEnd)
    window.addEventListener('keydown', this.handleKeyDown)
    window.addEventListener('keyup', this.handleKeyUp)
    window.addEventListener('blur', this.handleBlur)
    this.refresh()
  }

  /** Stop listening and abort in-flight fetches. The index survives for a quick re-activate. */
  deactivate(): void {
    if (!this.active) return
    this.active = false
    this.map.off('moveend', this.handleMoveEnd)
    window.removeEventListener('keydown', this.handleKeyDown)
    window.removeEventListener('keyup', this.handleKeyUp)
    window.removeEventListener('blur', this.handleBlur)
    if (this.moveTimer) clearTimeout(this.moveTimer)
    this.moveTimer = null
    this.suppressed = false
    for (const st of this.layers.values()) st.ctrl?.abort()
  }

  /** The enabled snappable layer set (master toggle × per-layer checks, resolved upstream). */
  setLayers(ids: string[]): void {
    this.enabled = ids
    for (const id of [...this.layers.keys()]) {
      if (!ids.includes(id)) {
        this.layers.get(id)?.ctrl?.abort()
        this.layers.delete(id)
      }
    }
    if (this.active) this.refresh()
  }

  /** The feature being vertex-edited — its own stored vertices would make the drag sticky. */
  setExcluded(fid: string | null): void {
    this.excludedFid = fid
  }

  /** A commit landed on the layer (bumpTiles) — the index must reflect it for the next gesture. */
  invalidate(layerId: string): void {
    const st = this.layers.get(layerId)
    if (st) st.bounds = null
    if (this.active && this.enabled.includes(layerId)) void this.fetchLayer(layerId)
  }

  /** Layers currently density-paused (for the "zoom in to snap" hint). */
  getPaused(): Record<string, boolean> {
    const out: Record<string, boolean> = {}
    for (const [id, st] of this.layers) if (st.paused) out[id] = true
    return out
  }

  /**
   * The snap query — synchronous (terra-draw `toCustom` cannot await). Returns the exact
   * stored coordinate of the nearest in-tolerance target, point features first, or undefined.
   */
  snap(lng: number, lat: number): [number, number] | undefined {
    if (this.suppressed || this.enabled.length === 0) return undefined
    const canvas = this.map.getCanvas()
    const w = canvas.clientWidth || 1
    const h = canvas.clientHeight || 1
    const b = this.map.getBounds()
    // Cheap degree-window prefilter so the px projection only runs on a handful of candidates.
    const lngWin = SNAP_TOLERANCE_PX * ((b.getEast() - b.getWest()) / w) * 1.5
    const latWin = SNAP_TOLERANCE_PX * ((b.getNorth() - b.getSouth()) / h) * 1.5
    const cursor = this.map.project([lng, lat])
    const tol2 = SNAP_TOLERANCE_PX * SNAP_TOLERANCE_PX
    let bestPoint: SnapVertex | null = null
    let bestPointD2 = Infinity
    let bestVertex: SnapVertex | null = null
    let bestVertexD2 = Infinity
    for (const id of this.enabled) {
      const st = this.layers.get(id)
      if (!st || st.paused) continue
      for (const v of st.vertices) {
        if (Math.abs(v.lng - lng) > lngWin || Math.abs(v.lat - lat) > latWin) continue
        if (v.fid === this.excludedFid) continue
        const p = this.map.project([v.lng, v.lat])
        const dx = p.x - cursor.x
        const dy = p.y - cursor.y
        const d2 = dx * dx + dy * dy
        if (d2 > tol2) continue
        if (v.isPoint) {
          if (d2 < bestPointD2) {
            bestPoint = v
            bestPointD2 = d2
          }
        } else if (d2 < bestVertexD2) {
          bestVertex = v
          bestVertexD2 = d2
        }
      }
    }
    const hit = bestPoint ?? bestVertex
    return hit ? [hit.lng, hit.lat] : undefined
  }

  private refresh(): void {
    for (const id of this.enabled) void this.fetchLayer(id)
  }

  private handleMoveEnd = (): void => {
    if (this.moveTimer) clearTimeout(this.moveTimer)
    this.moveTimer = setTimeout(() => this.refresh(), MOVE_DEBOUNCE_MS)
  }

  private handleKeyDown = (e: KeyboardEvent): void => {
    if (e.key === 'Alt') this.suppressed = true
  }

  private handleKeyUp = (e: KeyboardEvent): void => {
    if (e.key === 'Alt') this.suppressed = false
  }

  // Alt-Tab away eats the keyup — never leave snapping stuck off.
  private handleBlur = (): void => {
    this.suppressed = false
  }

  private async fetchLayer(layerId: string): Promise<void> {
    const b = this.map.getBounds()
    const view = { w: b.getWest(), s: b.getSouth(), e: b.getEast(), n: b.getNorth() }
    let st = this.layers.get(layerId)
    if (!st) {
      st = { vertices: [], bounds: null, paused: false, ctrl: null }
      this.layers.set(layerId, st)
    }
    // A PAUSED layer must never satisfy the cache: its cached bounds are the too-dense viewport,
    // and zooming in stays INSIDE them — the early return would make the pause permanent (the
    // field-found Sidewalk bug). Paused layers re-probe on every settled move instead.
    const cached = st.bounds
    if (
      !st.paused &&
      cached &&
      view.w >= cached.w && view.s >= cached.s && view.e <= cached.e && view.n <= cached.n
    )
      return
    st.ctrl?.abort()
    const ctrl = new AbortController()
    st.ctrl = ctrl
    const padLng = (view.e - view.w) * FETCH_PAD
    const padLat = (view.n - view.s) * FETCH_PAD
    const padded = {
      w: Math.max(view.w - padLng, -180),
      s: Math.max(view.s - padLat, -90),
      e: Math.min(view.e + padLng, 180),
      n: Math.min(view.n + padLat, 90),
    }
    const bbox = `${padded.w},${padded.s},${padded.e},${padded.n}`
    try {
      if (st.paused) {
        // Cheap density probe (numberMatched rides any limit) — don't ship 5000 features per
        // pan just to learn the viewport is still too dense.
        const probe = await queryLayerFeatures(layerId, { bbox, limit: 1 }, this.getToken(), ctrl.signal)
        if (ctrl.signal.aborted) return
        if (probe.numberMatched > PAGE_CAP) {
          st.bounds = padded
          return // still too dense — the hint stands
        }
      }
      const fc = await queryLayerFeatures(layerId, { bbox, limit: PAGE_CAP }, this.getToken(), ctrl.signal)
      if (ctrl.signal.aborted) return
      const wasPaused = st.paused
      if (fc.numberMatched > PAGE_CAP) {
        st.vertices = []
        st.paused = true
      } else {
        // Clip to the fetch bbox: the server returns the WHOLE geometry of anything intersecting
        // it, so a city-spanning line would otherwise flood the index (and the cap) with
        // off-screen vertices.
        const vertices = collectVertices(fc.features, padded)
        st.paused = vertices === null
        st.vertices = vertices ?? []
      }
      st.bounds = padded
      if (st.paused !== wasPaused) this.onStatus?.(this.getPaused())
    } catch {
      // snapping is assistance — a failed fetch silently means "no snaps from here for now"
    }
  }
}

/** Flatten every IN-BBOX vertex of every feature; null = over the vertex cap (density-paused). */
function collectVertices(
  features: LayerFeature[],
  bbox: { w: number; s: number; e: number; n: number },
): SnapVertex[] | null {
  const out: SnapVertex[] = []
  for (const f of features) {
    const g = f.geometry
    if (!g) continue
    const fid = f.id ?? ''
    const add = (pos: unknown, isPoint: boolean): void => {
      const [lng, lat] = pos as [number, number]
      if (lng < bbox.w || lng > bbox.e || lat < bbox.s || lat > bbox.n) return
      out.push({ lng, lat, fid, isPoint })
    }
    const c = g.coordinates
    switch (g.type) {
      case 'Point':
        add(c, true)
        break
      case 'MultiPoint':
        for (const p of c as unknown[]) add(p, true)
        break
      case 'LineString':
        for (const p of c as unknown[]) add(p, false)
        break
      case 'MultiLineString':
      case 'Polygon':
        for (const part of c as unknown[][]) for (const p of part) add(p, false)
        break
      case 'MultiPolygon':
        for (const poly of c as unknown[][][]) for (const ring of poly) for (const p of ring) add(p, false)
        break
    }
    if (out.length > VERTEX_CAP) return null
  }
  return out
}
