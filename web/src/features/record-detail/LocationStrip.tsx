import { MapPin } from 'lucide-react'

import type { LayerFeature } from '@/lib/api'

/**
 * Where the record IS (handoff-record-table-3 §3.2 / findings §F2). The drawer showed no geometry
 * type, no coordinates and no CRS for any feature on any layer — on a GIS product, for a record
 * opened from a map. The data was always there: `geometry` rides the same /features response the
 * drawer already reads, so this needs no endpoint and no extra request.
 *
 * Renders nothing for a geometry-less hosted table (§3.6 keeps that fence). A non-point geometry
 * shows the centre of its extent, LABELLED as such — a bbox centre is not a centroid and calling
 * it one would be a quiet lie on a surveying tool. Coordinates are the ones actually in hand:
 * GeoJSON is WGS 84, and the strip says so rather than implying the layer's working CRS.
 */
export default function LocationStrip({ geometry }: { geometry: LayerFeature['geometry'] }) {
  if (!geometry) return null
  const at = representativePoint(geometry)
  if (!at) return null
  const exact = geometry.type === 'Point'

  return (
    <div className="record-detail__location">
      <MapPin size={13} aria-hidden="true" className="record-detail__location-icon" />
      <span className="record-detail__coords">
        {at.lat.toFixed(6)}, {at.lon.toFixed(6)}
      </span>
      <span className="record-detail__crs">
        {geometry.type}
        {exact ? '' : ' · centre of extent'} · WGS 84 (EPSG:4326)
      </span>
    </div>
  )
}

/** A point to show for any geometry: the point itself, else the centre of its bounding box.
 * Walks the nested coordinate arrays without caring which geometry type produced them. */
function representativePoint(
  geometry: NonNullable<LayerFeature['geometry']>,
): { lon: number; lat: number } | null {
  if (geometry.type === 'Point') {
    const c = geometry.coordinates as unknown
    return isPosition(c) ? { lon: c[0], lat: c[1] } : null
  }
  let minLon = Infinity
  let minLat = Infinity
  let maxLon = -Infinity
  let maxLat = -Infinity
  const walk = (node: unknown): void => {
    if (isPosition(node)) {
      minLon = Math.min(minLon, node[0])
      maxLon = Math.max(maxLon, node[0])
      minLat = Math.min(minLat, node[1])
      maxLat = Math.max(maxLat, node[1])
      return
    }
    if (Array.isArray(node)) node.forEach(walk)
  }
  walk(geometry.coordinates)
  if (!Number.isFinite(minLon) || !Number.isFinite(minLat)) return null
  return { lon: (minLon + maxLon) / 2, lat: (minLat + maxLat) / 2 }
}

function isPosition(v: unknown): v is [number, number, ...number[]] {
  return Array.isArray(v) && v.length >= 2 && typeof v[0] === 'number' && typeof v[1] === 'number'
}
