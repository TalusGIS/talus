import type maplibregl from 'maplibre-gl'

import type { SketchFeature } from '@/lib/api'
import type { DrawnGeometry } from '@/features/map/useFeatureDraw'

/**
 * Sketch-layer plumbing (decision-sketch-layer.md): AGO-style map notes. Sketches render from ONE
 * direct GeoJSON source + a small fixed layer family (the identify-highlight pattern — no LayerDef,
 * no registration machinery), with data-driven per-feature color (`['get','color']`) and an
 * optional static text label (`['get','label']`, the no-expression-engine fence). Everything here
 * is map-scoped presentation: nothing is served, exported, or visible to interop.
 */

export const SKETCH_SRC = 'sketch-notes'
// Circle is POINT-FILTERED (the standing MapLibre lesson: an unfiltered circle layer dots every
// line/polygon VERTEX); fill draws polygon washes; line draws lines AND polygon outlines.
export const SKETCH_LAYERS = [
  `${SKETCH_SRC}-fill`,
  `${SKETCH_SRC}-line`,
  `${SKETCH_SRC}-circle`,
  `${SKETCH_SRC}-label`,
] as const

export const isSketchId = (id: string): boolean => /^skt_[0-9a-f]{8}$/.test(id)

export function newSketchId(): string {
  const b = new Uint8Array(4)
  crypto.getRandomValues(b)
  return 'skt_' + [...b].map((x) => x.toString(16).padStart(2, '0')).join('')
}

/** SketchFeature -> the GeoJSON geometry terra-draw / MapLibre speak. */
export function sketchGeometry(f: SketchFeature): DrawnGeometry {
  const type = f.geometryType === 'point' ? 'Point' : f.geometryType === 'line' ? 'LineString' : 'Polygon'
  return { type, coordinates: f.coordinates } as DrawnGeometry
}

/** A drawn/reshaped geometry -> the persisted (geometryType, coordinates) pair. */
export function geometryToSketch(g: DrawnGeometry): Pick<SketchFeature, 'geometryType' | 'coordinates'> {
  const geometryType = g.type === 'Point' ? 'point' : g.type === 'LineString' ? 'line' : 'polygon'
  return { geometryType, coordinates: g.coordinates }
}

/** The FeatureCollection the sketch source renders. `except` hides one feature while terra-draw's
 *  select mode shows its own editable copy of it (the reshape session). Properties carry the STRING
 *  id (identify lesson), the paint color, and the label the symbol layer reads. */
export function sketchCollection(
  sketch: SketchFeature[],
  except?: string | null,
): GeoJSON.FeatureCollection {
  return {
    type: 'FeatureCollection',
    features: sketch
      .filter((f) => f.id !== except)
      .map((f) => ({
        type: 'Feature' as const,
        geometry: sketchGeometry(f) as GeoJSON.Geometry,
        properties: { id: f.id, color: f.color, ...(f.label ? { label: f.label } : {}) },
      })),
  }
}

/** [w, s, e, n] over every position of every sketch feature (the TOC zoom-to), or null when empty. */
export function sketchBounds(sketch: SketchFeature[]): [number, number, number, number] | null {
  let w = Infinity, s = Infinity, e = -Infinity, n = -Infinity
  const eat = (c: unknown): void => {
    if (!Array.isArray(c)) return
    if (typeof c[0] === 'number' && typeof c[1] === 'number') {
      w = Math.min(w, c[0]); e = Math.max(e, c[0])
      s = Math.min(s, c[1]); n = Math.max(n, c[1])
      return
    }
    for (const part of c) eat(part)
  }
  for (const f of sketch) eat(f.coordinates)
  return Number.isFinite(w) ? [w, s, e, n] : null
}

/** Idempotently add the sketch source + its four layers (fill / line / point circle / label symbol),
 *  then lift them to the top of the style so notes draw over data layers (and survive a basemap
 *  switch, which re-adds basemap layers above everything). Paint is data-driven off `color`. */
export function ensureSketchLayers(map: maplibregl.Map): void {
  if (!map.getSource(SKETCH_SRC)) {
    map.addSource(SKETCH_SRC, {
      type: 'geojson',
      data: { type: 'FeatureCollection', features: [] },
    })
  }
  const color = ['get', 'color'] as unknown as string
  if (!map.getLayer(`${SKETCH_SRC}-fill`)) {
    map.addLayer({
      id: `${SKETCH_SRC}-fill`,
      type: 'fill',
      source: SKETCH_SRC,
      filter: ['==', ['geometry-type'], 'Polygon'],
      paint: { 'fill-color': color, 'fill-opacity': 0.18 },
    })
    map.addLayer({
      id: `${SKETCH_SRC}-line`,
      type: 'line',
      source: SKETCH_SRC,
      filter: ['in', ['geometry-type'], ['literal', ['LineString', 'Polygon']]],
      paint: { 'line-color': color, 'line-width': 2.5 },
    })
    map.addLayer({
      id: `${SKETCH_SRC}-circle`,
      type: 'circle',
      source: SKETCH_SRC,
      filter: ['==', ['geometry-type'], 'Point'],
      paint: {
        'circle-color': color,
        'circle-radius': 6,
        'circle-stroke-color': '#ffffff',
        'circle-stroke-width': 1.5,
      },
    })
    map.addLayer({
      id: `${SKETCH_SRC}-label`,
      type: 'symbol',
      source: SKETCH_SRC,
      filter: ['has', 'label'],
      layout: {
        'text-field': ['get', 'label'],
        'text-size': 13,
        'text-font': ['Noto Sans Medium'],
        'symbol-placement': 'point',
        'text-offset': [0, 1.1],
        'text-anchor': 'top',
      },
      paint: {
        'text-color': color,
        'text-halo-color': '#ffffff',
        'text-halo-width': 1.3,
      },
    })
  }
  // Lift above whatever the last restack/basemap switch put on top (idempotent, cheap).
  for (const id of SKETCH_LAYERS) if (map.getLayer(id)) map.moveLayer(id)
}

/** Push the current sketch state into the source (hiding `except` during a reshape session). */
export function setSketchData(
  map: maplibregl.Map,
  sketch: SketchFeature[],
  except?: string | null,
): void {
  const src = map.getSource(SKETCH_SRC) as maplibregl.GeoJSONSource | undefined
  src?.setData(sketchCollection(sketch, except))
}

/** Show/hide the whole sketch family (the TOC row's visibility toggle). */
export function setSketchVisibility(map: maplibregl.Map, visible: boolean): void {
  for (const id of SKETCH_LAYERS) {
    if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', visible ? 'visible' : 'none')
  }
}
