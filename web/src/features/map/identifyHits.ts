/**
 * Which drawn features a click on the map identifies.
 *
 * The map asks MapLibre twice: for everything drawn inside a small box around the click, and for
 * everything drawn at the click itself. The box is there so a point or a thin line is easy to hit.
 * A polygon needs no such help: it has an inside, and the click is in it or it is not. Before this
 * rule the box counted for polygons too, so a click inside one parcel also returned every neighbour
 * within a few pixels, and the popup could open on a neighbour.
 *
 * The rule: a point or a line counts if it is in the box; a polygon counts only if it is under the
 * click itself. A click on a shared edge still returns both sides, which is the honest answer.
 */

/** One drawn feature as the map reports it: `key` names the logical feature (layer + id), and the
 *  same key can arrive more than once (fill and outline, or the two sides of a tile seam). */
export interface DrawnHit {
  key: string
  /** GeoJSON geometry type of the drawn feature, e.g. 'Polygon', 'MultiPolygon', 'Point'. */
  geometryType: string
}

/** Pixels each side of the click that still hit a point or a line. */
export const IDENTIFY_TOLERANCE_PX = 5

export function isPolygonType(geometryType: string): boolean {
  return geometryType === 'Polygon' || geometryType === 'MultiPolygon'
}

/** The hits to keep, in the order given (the map reports the topmost first), each key once. */
export function keepIdentifyHits<T extends DrawnHit>(inBox: T[], keysAtPoint: ReadonlySet<string>): T[] {
  const seen = new Set<string>()
  const kept: T[] = []
  for (const h of inBox) {
    if (seen.has(h.key)) continue
    if (isPolygonType(h.geometryType) && !keysAtPoint.has(h.key)) continue
    seen.add(h.key)
    kept.push(h)
  }
  return kept
}
