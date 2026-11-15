import type { ExpressionSpecification } from 'maplibre-gl'

import type { Renderer } from '@/lib/api'

/**
 * Resolve a layer's renderer into a MapLibre color value for circle-color / fill-color /
 * line-color (Talus 13). Resolution order is the caller's: pass the renderer if present,
 * else the single-symbol/LayerDef fallback color.
 *
 *  - null renderer            -> the flat fallback color (single-symbol from displayColor,
 *                               or the static LayerDef constant).
 *  - { type: 'simple' }       -> its one symbol color.
 *  - { type: 'uniqueValue' }  -> a `match` on the categorizing field. The field is read
 *                               through `to-string` so a boolean/date tile value matches the
 *                               string stop values uniformly, and a missing/null value
 *                               (coerced to '') falls through to the default («No value»)
 *                               color — never an error.
 */
export function resolveColor(
  renderer: Renderer | null | undefined,
  fallback: string,
): string | ExpressionSpecification {
  if (!renderer) return fallback
  if (renderer.type === 'simple') return renderer.symbol?.color ?? fallback

  const stops = renderer.stops ?? []
  if (!renderer.field || stops.length === 0) return fallback
  const defaultColor = renderer.default?.color ?? fallback

  // ['match', ['to-string', ['get', field]], v1, c1, v2, c2, ..., defaultColor]
  const match: unknown[] = ['match', ['to-string', ['get', renderer.field]]]
  for (const s of stops) match.push(s.value, s.color)
  match.push(defaultColor)
  return match as unknown as ExpressionSpecification
}

/**
 * c7 (decision-line-polygon-layers.md §8) — LAYER-WIDE geometry style, read off the renderer with the map's
 * existing hardcoded values as fallbacks. Unlike color these don't vary by category, so they're plain scalars
 * (no `match` expression). The map (registerLayerOnMap / handleRendererSaved) and the legend share these so
 * they never drift. `lineWidth` falls back to the zoom-ramp the line layer shipped with (preserves today's look
 * when no width is set); a flat user width overrides it.
 */
export const DEFAULT_LINE_WIDTH: ExpressionSpecification = [
  'interpolate', ['linear'], ['zoom'], 10, 1, 14, 2, 17, 4,
]

export function lineWidthOf(
  r: Renderer | null | undefined,
  fallback: number | ExpressionSpecification = DEFAULT_LINE_WIDTH,
): number | ExpressionSpecification {
  return r?.lineWidth ?? fallback
}

/** MapLibre `line-dasharray`, or undefined for a solid line (so the caller omits / clears the property). */
export function lineDashOf(r: Renderer | null | undefined): number[] | undefined {
  return r?.lineDash && r.lineDash.length ? r.lineDash : undefined
}

export function fillOpacityOf(r: Renderer | null | undefined, fallback: number): number {
  return r?.fillOpacity ?? fallback
}

export function outlineColorOf(
  r: Renderer | null | undefined,
  fallback: string | ExpressionSpecification,
): string | ExpressionSpecification {
  return r?.outlineColor ?? fallback
}

export function outlineWidthOf(r: Renderer | null | undefined, fallback: number): number {
  return r?.outlineWidth ?? fallback
}

/** The point radius ramp every data circle layer shipped with — kept as the fallback so a layer with
 *  no explicit size looks exactly as it did before point-size existed. */
export const DEFAULT_POINT_RADIUS: ExpressionSpecification = [
  'interpolate', ['linear'], ['zoom'], 10, 1.5, 13, 3, 17, 7,
]

/** MapLibre `circle-radius` for a point layer. `pointSize` is a DIAMETER in px (the ArcGIS/QGIS
 *  meaning of "size"), so it is halved here — this is the ONLY place that conversion happens. A flat
 *  user size overrides the zoom ramp, exactly as `lineWidth` overrides `DEFAULT_LINE_WIDTH`. */
export function pointRadiusOf(
  r: Renderer | null | undefined,
  fallback: number | ExpressionSpecification = DEFAULT_POINT_RADIUS,
): number | ExpressionSpecification {
  const size = r?.pointSize
  return size != null ? size / 2 : fallback
}
