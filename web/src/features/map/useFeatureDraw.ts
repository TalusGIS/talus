import { useEffect, useRef } from 'react'
import type { Map as MapLibreMap } from 'maplibre-gl'
import {
  TerraDraw,
  TerraDrawCircleMode,
  TerraDrawFreehandMode,
  TerraDrawLineStringMode,
  TerraDrawPointMode,
  TerraDrawPolygonMode,
  TerraDrawRectangleMode,
  type TerraDrawMouseEvent,
} from 'terra-draw'
import { TerraDrawMapLibreGLAdapter } from 'terra-draw-maplibre-gl-adapter'

/** The engine's synchronous snap query (decision-web-editor-snapping.md): exact stored
 * coordinate of the nearest in-tolerance target, or undefined. Null/absent = snapping off. */
export type SnapQuery = (lng: number, lat: number) => [number, number] | undefined

/** Snap-indicator styling (terra-draw's built-in `snappingPoint*` knobs) — a distinct color
 * from the #ffcc00 selection yellow and the identify cyan, white-ringed for visibility. */
export const SNAP_INDICATOR_STYLES = {
  snappingPointColor: '#c026d3',
  snappingPointWidth: 5,
  snappingPointOutlineColor: '#ffffff',
  snappingPointOutlineWidth: 2,
} as const

/** A GeoJSON geometry as terra-draw emits it (always WGS-84 lng/lat off the MapLibre adapter). */
export interface DrawnGeometry {
  type: 'Point' | 'LineString' | 'Polygon'
  coordinates: number[] | number[][] | number[][][]
}

/** A create gesture: the three geometry families plus the polygon-emitting shape gestures
 * (decision-editing-toolbar.md F3 — rectangle/circle/freehand all finish as ordinary Polygons). */
export type DrawTool = 'point' | 'line' | 'polygon' | 'rectangle' | 'circle' | 'freehand'

const MODE_BY_TOOL = {
  point: 'point',
  line: 'linestring',
  polygon: 'polygon',
  rectangle: 'rectangle',
  circle: 'circle',
  freehand: 'freehand',
} as const

/**
 * Drives a TerraDraw instance for CREATING one feature on the map (line/polygon slice c6a; point mode joined
 * for the sketch layer, decision-sketch-layer.md; rectangle/circle/freehand joined for the editing toolbar).
 * When `active` with a tool, it starts terra-draw in the matching mode; on the draw's `finish` it hands the
 * drawn geometry (WGS-84) to `onFinish` and clears the scratch geometry — the real feature appears after the
 * create round-trip + tile refresh (or, for a sketch, from the sketch GeoJSON source). Tearing down
 * (active→false / unmount) stops terra-draw and removes its layers. terra-draw is created lazily and ONLY
 * while drawing, so it never interferes with normal map interaction (popups, point click-create).
 * Vertex/ring editing of EXISTING features is c6b (select mode).
 */
export function useFeatureDraw(opts: {
  map: MapLibreMap | null
  active: boolean
  tool: DrawTool | null
  onFinish: (geometry: DrawnGeometry) => void
  /** Optional snap query (line/polygon click-vertex modes only — the shape gestures and point
   * mode have no terra-draw snapping hook). Ref-read, so toggling snapping mid-session applies
   * live without tearing the draw session down. */
  snapTo?: SnapQuery | null
}): void {
  const { map, active, tool } = opts
  // onFinish via ref so the draw effect doesn't re-run (and tear down terra-draw) on every parent render.
  const onFinishRef = useRef(opts.onFinish)
  onFinishRef.current = opts.onFinish
  const snapToRef = useRef(opts.snapTo)
  snapToRef.current = opts.snapTo

  useEffect(() => {
    if (!map || !active || !tool) return
    let draw: TerraDraw | null = null
    let cancelled = false

    // The map style must be loaded before terra-draw can add its layers; guard for both cases.
    const begin = () => {
      if (cancelled) return
      // Structurally matches terra-draw's `Snapping` (the type isn't re-exported from the root).
      const snapping = {
        toCustom: (e: TerraDrawMouseEvent) => snapToRef.current?.(e.lng, e.lat),
      }
      draw = new TerraDraw({
        // coordinatePrecision defaults to 9 — which would round a snapped vertex away from the
        // target's exact stored coordinate and break the ST_Equals contract
        // (decision-web-editor-snapping.md §3.2). 18 keeps any CONUS double intact.
        adapter: new TerraDrawMapLibreGLAdapter({ map, coordinatePrecision: 18 }),
        modes: [
          new TerraDrawPointMode(),
          // pointerDistance halves terra-draw's 40 px default on the click-vertex modes: within this
          // radius of the last vertex a click means close/finish (hand cursor), not "drop a vertex",
          // and 40 px blocked tight vertex spacing on detailed polygons.
          // The flip side — closing/finishing needs a click within 20 px — was the accepted trade.
          new TerraDrawLineStringMode({
            pointerDistance: 20,
            snapping,
            styles: { ...SNAP_INDICATOR_STYLES },
          }),
          new TerraDrawPolygonMode({
            pointerDistance: 20,
            snapping,
            styles: { ...SNAP_INDICATOR_STYLES },
          }),
          new TerraDrawRectangleMode(),
          new TerraDrawCircleMode(),
          new TerraDrawFreehandMode(),
        ],
      })
      draw.start()
      draw.setMode(MODE_BY_TOOL[tool])
      draw.on('finish', (id) => {
        const feature = draw?.getSnapshot().find((f) => f.id === id)
        const geometry = feature?.geometry
        if (
          geometry &&
          (geometry.type === 'Point' || geometry.type === 'LineString' || geometry.type === 'Polygon')
        ) {
          onFinishRef.current(geometry as DrawnGeometry)
        }
        draw?.clear() // drop the scratch drawing; the persisted feature renders from its tile
      })
    }

    if (map.isStyleLoaded()) begin()
    else map.once('load', begin)

    return () => {
      cancelled = true
      try {
        draw?.stop() // removes terra-draw's layers + event handlers
      } catch {
        /* map may already be torn down */
      }
    }
  }, [map, active, tool])
}
