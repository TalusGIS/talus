import { useEffect, useRef } from 'react'
import type { Map as MapLibreMap } from 'maplibre-gl'
import {
  TerraDraw,
  TerraDrawLineStringMode,
  TerraDrawPointMode,
  TerraDrawPolygonMode,
  TerraDrawSelectMode,
  type GeoJSONStoreFeatures,
  type TerraDrawMouseEvent,
} from 'terra-draw'
import { TerraDrawMapLibreGLAdapter } from 'terra-draw-maplibre-gl-adapter'

import type { DrawnGeometry, SnapQuery } from '@/features/map/useFeatureDraw'

// Vertex/feature editing affordances enabled per mode (applied to both linestring + polygon features):
// move the whole shape, drag a vertex, drag a midpoint to insert one, right-click a vertex to delete it
// (terra-draw select-mode `coordinates.deletable` binds deletion to contextmenu, not click — B5b 2026-08-11).
// `snapping` = the vertex-drag snap hook (decision-web-editor-snapping.md surface 2); typed
// structurally — terra-draw doesn't re-export its `Snapping` type from the package root.
const editFlags = (snapping: { toCustom: (event: TerraDrawMouseEvent) => [number, number] | undefined }) => ({
  feature: {
    draggable: true,
    coordinates: { draggable: true, midpoints: true, deletable: true, snappable: snapping },
  },
})
// A point has no vertices — "reshape" = drag the whole marker (the sketch layer's point move).
// No coordinates → no snappable hook; hosted point features move via the separate Move flow anyway.
const POINT_FLAGS = { feature: { draggable: true } }

const MODE_BY_GEOM = { point: 'point', line: 'linestring', polygon: 'polygon' } as const

/**
 * Drives a TerraDraw SELECT instance for EDITING the vertices/rings of ONE existing line/polygon feature
 * (line/polygon slice c6b). When `active` with a geom family + a single-part seed geometry, it loads the
 * feature into terra-draw, enters select mode, and selects it — enabling vertex drag/insert/delete (+
 * whole-shape drag). On each COMPLETED edit gesture (terra-draw `finish` with an edit action — anything but
 * the initial `draw`), it hands the reshaped geometry (WGS-84) to `onEdit`, which auto-commits it via the
 * feature PATCH. Drawing NEW features is the sibling `useFeatureDraw` (c6a); this hook only edits. terra-draw
 * is created lazily and ONLY while editing, so it never interferes with normal map interaction.
 *
 * The seed is single-part (LineString/Polygon): slot geometry is stored Multi*, but terra-draw edits a
 * single part, and the backend re-wraps with ST_Multi on save (decision-line-polygon-layers.md c4). The
 * caller unwraps a 1-part Multi* and refuses true multi-part features before activating this hook.
 */
export function useFeatureEdit(opts: {
  map: MapLibreMap | null
  active: boolean
  geomType: 'point' | 'line' | 'polygon' | null
  geometry: DrawnGeometry | null
  onEdit: (geometry: DrawnGeometry) => void
  /** Optional snap query for vertex drags (ref-read; live-togglable mid-session). */
  snapTo?: SnapQuery | null
}): void {
  const { map, active, geomType, geometry } = opts
  // onEdit via ref so the effect doesn't re-run (and tear down terra-draw) on every parent render.
  const onEditRef = useRef(opts.onEdit)
  onEditRef.current = opts.onEdit
  const snapToRef = useRef(opts.snapTo)
  snapToRef.current = opts.snapTo

  useEffect(() => {
    if (!map || !active || !geomType || !geometry) return
    let draw: TerraDraw | null = null
    let cancelled = false

    // The map style must be loaded before terra-draw can add its layers; guard for both cases.
    const begin = () => {
      if (cancelled) return
      const modeName = MODE_BY_GEOM[geomType]
      const flags = editFlags({ toCustom: (e) => snapToRef.current?.(e.lng, e.lat) })
      draw = new TerraDraw({
        // 18 (not the 9 default): an untouched vertex must survive the seed→store→PATCH round
        // trip bit-exact, and a snapped one must keep its target's exact stored coordinate
        // (decision-web-editor-snapping.md §3.2).
        adapter: new TerraDrawMapLibreGLAdapter({ map, coordinatePrecision: 18 }),
        modes: [
          new TerraDrawPointMode(),
          new TerraDrawLineStringMode(),
          new TerraDrawPolygonMode(),
          new TerraDrawSelectMode({
            flags: { point: POINT_FLAGS, linestring: flags, polygon: flags },
          }),
        ],
      })
      draw.start()
      // Load the existing feature, then enter select mode and select it so its vertices become editable.
      // (Cast: terra-draw validates the feature at runtime; our single-part seed satisfies its store shape.)
      draw.addFeatures([
        { type: 'Feature', geometry, properties: { mode: modeName } } as unknown as GeoJSONStoreFeatures,
      ])
      const seeded = draw.getSnapshot()[0]
      draw.setMode('select')
      if (seeded?.id !== undefined) draw.selectFeature(seeded.id, 'select')

      draw.on('finish', (finishedId, context) => {
        if (context.action === 'draw') return // a fresh draw, not an edit of the seeded feature
        const feature = draw?.getSnapshot().find((f) => f.id === finishedId)
        const g = feature?.geometry
        if (g && (g.type === 'Point' || g.type === 'LineString' || g.type === 'Polygon')) {
          onEditRef.current(g as DrawnGeometry)
        }
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
  }, [map, active, geomType, geometry])
}
