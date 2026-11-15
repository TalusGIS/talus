import type { FilterCondition } from '@/lib/api/client'
import type { FeatureSearchResult, LayerColumn } from '@/lib/api/types'
import { convertEsriDateProps, esriDateFieldNames } from './esriDates'

/**
 * Add-from-URL plumbing (decision-add-from-url.md). An EXTERNAL layer is a reference to a remote
 * public service — nothing ingested, no catalog item; the map persists a URL (`kind:'external'` in
 * the web_map jsonb) and the VIEWER'S browser fetches it directly (browser-direct, no proxy — CORS
 * is the honest gate, surfaced as a paste-time error). Four kinds, each riding an existing MapLibre
 * source type: `arcgis-feature`/`geojson` → a read-only GeoJSON source (the view-layer path);
 * `xyz`/`arcgis-image` → a raster tile source (exportImage consumed per-256px-tile via MapLibre's
 * `{bbox-epsg-3857}` template — probed live against a tile-cache-less county ImageServer).
 */

export type ExternalKind = 'arcgis-feature' | 'geojson' | 'xyz' | 'arcgis-image'

/** What the map persists per external op (mirrors the server's OperationalLayer external arm). */
export interface ExternalLayerSpec {
  externalId: string // ext_<8hex> — the map-source/TOC key
  externalKind: ExternalKind
  url: string // the normalized service/layer/template URL (never the pasted /query?... form)
  title: string
  geometryType?: 'point' | 'line' | 'polygon' // vector kinds (styling; from the probe)
  color?: string // vector kinds
  where?: string // arcgis-feature: a where-clause carried from a pasted /query URL (read-only in v1)
  // Link to a shared catalog ITEM (decision-external-catalog-items.md): set when this op was seeded
  // from a reference item (Browse picker, or save-to-catalog at add time). The other fields double
  // as the SNAPSHOT — on map open the item is resolved and its spec wins when readable; a 404
  // (deleted/unshared) falls back to these values, so the map never breaks.
  externalItemId?: string // xref_<8hex>
}

/** External-layer display colors (vector kinds) — the wizard's palette, teal-first. Shared by the
 * map's Add-from-URL card and the Catalog's create-reference dialog. */
export const EXT_PALETTE = ['#2a9d8f', '#7b5cd6', '#c0392b', '#16a085', '#d35400', '#8e44ad']

/** Human labels for the four kinds — confirm cards, catalog rows, Manage dialogs. */
export const EXT_KIND_LABEL: Record<ExternalKind, string> = {
  'arcgis-feature': 'ArcGIS feature layer',
  geojson: 'GeoJSON',
  xyz: 'XYZ tiles',
  'arcgis-image': 'ArcGIS image/map service',
}

/** The paste-time probe result: a ready spec (sans id/color) + add-time niceties (not persisted). */
export interface ExternalProbe {
  externalKind: ExternalKind
  url: string
  title: string
  geometryType?: 'point' | 'line' | 'polygon'
  where?: string
  extent4326: [number, number, number, number] | null // zoom-at-add; null when the SR isn't web-friendly
  featureCount: number | null // arcgis-feature/geojson: what the service says is there
  /** arcgis-feature: the fields the service declares as dates. Read only by the import copy, which
   *  must convert them (the service writes a date in GeoJSON as epoch milliseconds). Never persisted. */
  dateFields?: string[]
}

export const isExternalId = (id: string): boolean => /^ext_[0-9a-f]{8}$/.test(id)

export function newExternalId(): string {
  const b = new Uint8Array(4)
  crypto.getRandomValues(b)
  return 'ext_' + [...b].map((x) => x.toString(16).padStart(2, '0')).join('')
}

const ARCGIS_LAYER_RE = /^(.*\/(?:FeatureServer|MapServer)\/\d+)/i
const ARCGIS_IMAGE_RE = /^(.*\/(?:ImageServer|MapServer))(?:\/)?$/i

/** URL-shape detection. A pasted ArcGIS `/query?where=…` URL is PARSED — base layer + carried where. */
export function detectExternalUrl(raw: string): { kind: ExternalKind; url: string; where?: string } | null {
  const trimmed = raw.trim()
  if (!/^https?:\/\//i.test(trimmed)) return null
  if (trimmed.includes('{z}') && trimmed.includes('{x}') && trimmed.includes('{y}')) {
    return { kind: 'xyz', url: trimmed }
  }
  const [beforeQuery, queryString] = trimmed.split('?', 2)
  const layerMatch = beforeQuery.match(ARCGIS_LAYER_RE)
  if (layerMatch) {
    let where: string | undefined
    if (queryString) {
      const params = new URLSearchParams(queryString)
      where = params.get('where')?.trim() || undefined
      if (where === '1=1') where = undefined
    }
    return { kind: 'arcgis-feature', url: layerMatch[1], where }
  }
  if (ARCGIS_IMAGE_RE.test(beforeQuery)) {
    return { kind: 'arcgis-image', url: beforeQuery.replace(/\/$/, '') }
  }
  return { kind: 'geojson', url: trimmed }
}

const ESRI_FAMILY: Record<string, 'point' | 'line' | 'polygon'> = {
  esriGeometryPoint: 'point', esriGeometryMultipoint: 'point',
  esriGeometryPolyline: 'line', esriGeometryPolygon: 'polygon',
}
const GEOJSON_FAMILY: Record<string, 'point' | 'line' | 'polygon'> = {
  Point: 'point', MultiPoint: 'point',
  LineString: 'line', MultiLineString: 'line',
  Polygon: 'polygon', MultiPolygon: 'polygon',
}

/** Web-mercator meters → lon/lat. The only extent math we do client-side; other SRs → no zoom-at-add. */
function mercToLonLat(x: number, y: number): [number, number] {
  const lon = (x / 20037508.342789244) * 180
  const lat = (Math.atan(Math.exp((y / 20037508.342789244) * Math.PI)) * 360) / Math.PI - 90
  return [lon, lat]
}

function esriExtentTo4326(ext: unknown): [number, number, number, number] | null {
  const e = ext as { xmin?: number; ymin?: number; xmax?: number; ymax?: number; spatialReference?: { wkid?: number; latestWkid?: number } }
  if (e?.xmin == null || e.ymin == null || e.xmax == null || e.ymax == null) return null
  const wkid = e.spatialReference?.latestWkid ?? e.spatialReference?.wkid
  if (wkid === 4326) return [e.xmin, e.ymin, e.xmax, e.ymax]
  if (wkid === 3857 || wkid === 102100) {
    const [w, s] = mercToLonLat(e.xmin, e.ymin)
    const [x, n] = mercToLonLat(e.xmax, e.ymax)
    return [w, s, x, n]
  }
  return null // an exotic SR (e.g. a state plane) — the add simply doesn't zoom
}

/** The service's extent ALREADY PROJECTED to 4326, via the ArcGIS query op (`returnExtentOnly` +
 *  `outSR=4326`) — works even when the service's native SR is exotic (a state plane the client can't
 *  convert), and honors a carried where-clause (a filtered layer zooms to its FILTERED extent).
 *  Null when the service lacks the Query capability or errors — callers fall back / no-op. */
export async function fetchArcgisExtent4326(
  url: string,
  where?: string,
): Promise<[number, number, number, number] | null> {
  try {
    const q = new URLSearchParams({ where: where ?? '1=1', returnExtentOnly: 'true', outSR: '4326', f: 'json' })
    const res = await fetch(`${url}/query?${q}`)
    if (!res.ok) return null
    const doc = (await res.json()) as { extent?: unknown; error?: unknown }
    if (doc.error) return null
    return esriExtentTo4326(doc.extent)
  } catch {
    return null
  }
}

/** Zoom-time extent for ANY external kind: arcgis kinds re-ask the service (projected, filtered);
 *  geojson uses the loaded document's bbox (fetching it if the cache is cold); xyz has none. */
export async function externalExtent4326(
  spec: ExternalLayerSpec,
  geojsonCache: Record<string, GeoJSON.FeatureCollection>,
): Promise<[number, number, number, number] | null> {
  if (spec.externalKind === 'xyz') return null
  if (spec.externalKind === 'geojson') {
    if (!geojsonCache[spec.externalId]) {
      try {
        const res = await fetch(spec.url)
        if (!res.ok) return null
        const doc = (await res.json()) as GeoJSON.FeatureCollection
        if (doc?.type === 'FeatureCollection') geojsonCache[spec.externalId] = doc
        else return null
      } catch {
        return null
      }
    }
    return geojsonBbox(geojsonCache[spec.externalId].features ?? [])
  }
  const projected = await fetchArcgisExtent4326(spec.url, spec.where)
  if (projected) return projected
  try {
    const meta = await fetchServiceJson(spec.url)
    return esriExtentTo4326(meta.extent ?? meta.fullExtent ?? meta.initialExtent)
  } catch {
    return null
  }
}

async function fetchServiceJson(url: string): Promise<Record<string, unknown>> {
  let res: Response
  try {
    res = await fetch(`${url}?f=json`)
  } catch {
    throw new Error(`Could not reach ${new URL(url).host} — the server may be down, or it doesn't allow browser access (CORS)`)
  }
  if (!res.ok) throw new Error(`The server answered HTTP ${res.status}`)
  const doc = (await res.json()) as Record<string, unknown>
  const err = doc.error as { message?: string } | undefined
  if (err) throw new Error(`The service answered: ${err.message ?? 'error'}`)
  return doc
}

/** Probe a detected URL: confirm it's real, name it, and gather the wizard-card facts. Throws with a
 *  user-facing message on any failure — a bad paste dies here, never as a silently empty map. */
export async function probeExternalUrl(raw: string): Promise<ExternalProbe> {
  const det = detectExternalUrl(raw)
  if (det === null) throw new Error('That doesn’t look like an http(s) URL')
  const host = new URL(det.url).host

  if (det.kind === 'xyz') {
    // A template can't be probed generically (no metadata endpoint) — accept it as pasted.
    return { externalKind: 'xyz', url: det.url, title: host, extent4326: null, featureCount: null }
  }

  if (det.kind === 'arcgis-image') {
    const meta = await fetchServiceJson(det.url)
    // A bare MapServer/ImageServer must be able to export an image (the tile-template path).
    const caps = String(meta.capabilities ?? '')
    if (!/image|map/i.test(caps) && !meta.supportedImageFormatTypes && !meta.mapName) {
      throw new Error('This service doesn’t look like an image/map service')
    }
    return {
      externalKind: 'arcgis-image',
      url: det.url,
      title: String(meta.name ?? meta.mapName ?? host),
      // Prefer the server-projected extent (works for exotic native SRs like a state plane);
      // fall back to converting the metadata extent when the service lacks Query.
      extent4326: (await fetchArcgisExtent4326(det.url)) ?? esriExtentTo4326(meta.extent ?? meta.fullExtent ?? meta.initialExtent),
      featureCount: null,
    }
  }

  if (det.kind === 'arcgis-feature') {
    const meta = await fetchServiceJson(det.url)
    const family = ESRI_FAMILY[String(meta.geometryType ?? '')]
    if (!family) throw new Error(`Unsupported geometry type '${String(meta.geometryType ?? 'unknown')}'`)
    if (!String(meta.supportedQueryFormats ?? '').toLowerCase().includes('geojson')) {
      throw new Error('This service can’t return GeoJSON (an older ArcGIS Server) — not supported yet')
    }
    let featureCount: number | null = null
    try {
      const q = new URLSearchParams({ where: det.where ?? '1=1', returnCountOnly: 'true', f: 'json' })
      const c = (await (await fetch(`${det.url}/query?${q}`)).json()) as { count?: number }
      featureCount = typeof c.count === 'number' ? c.count : null
    } catch {
      /* count is a nicety */
    }
    return {
      externalKind: 'arcgis-feature',
      url: det.url,
      title: String(meta.name ?? host),
      geometryType: family,
      where: det.where,
      // Server-projected + where-scoped (a filtered layer zooms to its FILTERED extent at add).
      extent4326: (await fetchArcgisExtent4326(det.url, det.where)) ?? esriExtentTo4326(meta.extent),
      featureCount,
      dateFields: esriDateFieldNames(meta.fields),
    }
  }

  // geojson — fetch it (it's what rendering will do anyway); count/family/bbox from the document.
  let doc: unknown
  try {
    const res = await fetch(det.url)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    doc = await res.json()
  } catch (e) {
    throw new Error(
      e instanceof Error && e.message.startsWith('HTTP')
        ? `The server answered ${e.message}`
        : `Could not fetch GeoJSON from ${host} — bad URL, invalid JSON, or the server doesn't allow browser access (CORS)`,
      { cause: e },
    )
  }
  const d = doc as { type?: string; features?: Array<{ geometry?: { type?: string } }> }
  const features = d?.type === 'FeatureCollection' && Array.isArray(d.features) ? d.features : d?.type === 'Feature' ? [d as never] : null
  if (features === null) throw new Error('That URL isn’t a GeoJSON FeatureCollection or Feature')
  const firstType = features.find((f) => f?.geometry?.type)?.geometry?.type
  const bbox = geojsonBbox(features as GeoJSON.Feature[])
  const name = det.url.split('/').pop()?.split('?')[0]?.replace(/\.(geo)?json$/i, '') || host
  return {
    externalKind: 'geojson',
    url: det.url,
    title: name,
    geometryType: GEOJSON_FAMILY[firstType ?? ''] ?? 'point',
    extent4326: bbox,
    featureCount: features.length,
  }
}

export function geojsonBbox(features: GeoJSON.Feature[]): [number, number, number, number] | null {
  let w = Infinity, s = Infinity, e = -Infinity, n = -Infinity
  const walk = (coords: unknown): void => {
    if (typeof (coords as number[])[0] === 'number') {
      const [x, y] = coords as [number, number]
      if (x < w) w = x
      if (x > e) e = x
      if (y < s) s = y
      if (y > n) n = y
    } else for (const c of coords as unknown[]) walk(c)
  }
  for (const f of features) {
    const g = f?.geometry as { coordinates?: unknown } | null
    if (g?.coordinates) walk(g.coordinates)
  }
  return w <= e && s <= n ? [w, s, e, n] : null
}

// --- external attribute table (the read-only grid over remote rows) ----
// arcgis-feature = SERVER-paged, exactly AGO's pattern: returnCountOnly for the total,
// resultOffset/resultRecordCount + returnGeometry=false for pages, orderByFields for sort, the
// carried where composed in. geojson = the already-cached document, sorted/paged in memory.

export interface ExternalTableColumn {
  name: string
  alias: string
}

async function ensureGeojsonDoc(
  spec: ExternalLayerSpec,
  cache: Record<string, GeoJSON.FeatureCollection>,
): Promise<GeoJSON.FeatureCollection> {
  if (!cache[spec.externalId]) {
    const res = await fetch(spec.url)
    if (!res.ok) throw new Error(`The server answered HTTP ${res.status}`)
    const doc = (await res.json()) as GeoJSON.FeatureCollection
    const feats = doc?.type === 'FeatureCollection' ? doc.features : [doc as unknown as GeoJSON.Feature]
    cache[spec.externalId] = { type: 'FeatureCollection', features: ensureFeatureIds(feats ?? []) }
  }
  return cache[spec.externalId]
}

// esriFieldType* with no tabular value — never a grid column.
const ESRI_SKIP_FIELD = /Geometry|Blob|Raster|XML/i

/** The grid's columns + the row-identity field. arcgis → metadata field list (aliases preserved);
 *  geojson → the union of the first rows' property keys. */
export async function fetchExternalColumns(
  spec: ExternalLayerSpec,
  cache: Record<string, GeoJSON.FeatureCollection>,
): Promise<{ columns: ExternalTableColumn[]; objectIdField: string | null }> {
  if (spec.externalKind === 'geojson') {
    const doc = await ensureGeojsonDoc(spec, cache)
    const keys: string[] = []
    for (const f of doc.features.slice(0, 50)) {
      for (const k of Object.keys(f?.properties ?? {})) if (!keys.includes(k)) keys.push(k)
    }
    return { columns: keys.map((k) => ({ name: k, alias: k })), objectIdField: 'id' }
  }
  const meta = await fetchServiceJson(spec.url)
  const fields = (meta.fields as Array<{ name?: string; alias?: string; type?: string }> | undefined) ?? []
  return {
    columns: fields
      .filter((f) => f.name && !ESRI_SKIP_FIELD.test(f.type ?? ''))
      .map((f) => ({ name: f.name as string, alias: f.alias || (f.name as string) })),
    objectIdField: typeof meta.objectIdField === 'string' ? meta.objectIdField : null,
  }
}

// --- rail-tool parity: filters / labels / popups / search on externals -----------
// The panels consume a LayerColumn[] (normally GET /layers/{id}/columns); externals supply the same
// shape from the SERVICE — esri field types mapped onto the information_schema strings the panels'
// kind-bucketing already understands. FilterConditions translate per kind: arcgis → a where-clause
// sent to the REMOTE server (never our SQL); geojson → an in-memory predicate.

const ESRI_TO_INFOSCHEMA: Record<string, string> = {
  esriFieldTypeString: 'text', esriFieldTypeGUID: 'text', esriFieldTypeGlobalID: 'text',
  esriFieldTypeInteger: 'bigint', esriFieldTypeSmallInteger: 'bigint',
  esriFieldTypeBigInteger: 'bigint', esriFieldTypeOID: 'bigint',
  esriFieldTypeDouble: 'double precision', esriFieldTypeSingle: 'double precision',
  esriFieldTypeDate: 'timestamp with time zone',
}

const extColumnsMemo: Record<string, LayerColumn[]> = {}

/** The panels' column schema for an external layer (memoized per externalId). */
export async function fetchExternalLayerColumns(
  spec: ExternalLayerSpec,
  cache: Record<string, GeoJSON.FeatureCollection>,
): Promise<LayerColumn[]> {
  if (extColumnsMemo[spec.externalId]) return extColumnsMemo[spec.externalId]
  let cols: LayerColumn[]
  if (spec.externalKind === 'geojson') {
    const doc = await ensureGeojsonDoc(spec, cache)
    const byKey: Record<string, string> = {}
    for (const f of doc.features.slice(0, 50)) {
      for (const [k, v] of Object.entries(f?.properties ?? {})) {
        if (byKey[k] && byKey[k] !== 'text') continue
        if (typeof v === 'number') byKey[k] = byKey[k] ?? 'double precision'
        else if (typeof v === 'boolean') byKey[k] = byKey[k] ?? 'boolean'
        else if (v != null) byKey[k] = 'text'
      }
    }
    cols = Object.entries(byKey).map(([name, type]) => ({ name, type, domain: null }))
  } else {
    const meta = await fetchServiceJson(spec.url)
    const fields = (meta.fields as Array<{ name?: string; alias?: string; type?: string }> | undefined) ?? []
    cols = fields
      .filter((f) => f.name && !ESRI_SKIP_FIELD.test(f.type ?? ''))
      .map((f) => ({
        name: f.name as string,
        type: ESRI_TO_INFOSCHEMA[f.type ?? ''] ?? 'text',
        domain: null,
        label: f.alias && f.alias !== f.name ? f.alias : undefined,
      }))
  }
  extColumnsMemo[spec.externalId] = cols
  return cols
}

const NUMERIC_TYPES = new Set(['bigint', 'double precision'])
const ISO_DATE_RE = /^\d{4}-\d{2}-\d{2}$/

function whereLiteral(col: LayerColumn | undefined, v: string): string {
  const s = v.trim()
  if (col && NUMERIC_TYPES.has(col.type) && s !== '' && !Number.isNaN(Number(s))) return s
  if (col?.type.startsWith('timestamp') && ISO_DATE_RE.test(s)) return `DATE '${s}'`
  return `'${s.replace(/'/g, "''")}'`
}

/** FilterConditions → an ArcGIS where fragment (AND-joined; '' when none). Values are escaped and
 *  typed by the column; this string goes to the REMOTE service's query API only. */
export function conditionsToWhere(conds: FilterCondition[], columns: LayerColumn[]): string {
  const byName = new Map(columns.map((c) => [c.name, c]))
  const parts: string[] = []
  for (const c of conds) {
    const col = byName.get(c.field)
    const f = c.field.replace(/[^A-Za-z0-9_.]/g, '') // field names are validated upstream; belt anyway
    if (!f) continue
    if (c.op === 'eq') parts.push(`${f} = ${whereLiteral(col, c.value)}`)
    else if (c.op === 'ne') parts.push(`${f} <> ${whereLiteral(col, c.value)}`)
    else if (c.op === 'gte') parts.push(`${f} >= ${whereLiteral(col, c.value)}`)
    else if (c.op === 'lte') parts.push(`${f} <= ${whereLiteral(col, c.value)}`)
    else if (c.op === 'ilike') parts.push(`UPPER(${f}) LIKE UPPER('%${c.value.replace(/'/g, "''")}%')`)
    else if (c.op === 'in') {
      const items = c.value.split(',').map((x) => whereLiteral(col, x)).filter((x) => x !== "''")
      if (items.length) parts.push(`${f} IN (${items.join(', ')})`)
    }
  }
  return parts.join(' AND ')
}

/** The composed where for an arcgis external: the pasted-in carried filter AND the map's panel filter. */
function composedWhere(spec: ExternalLayerSpec, conds?: FilterCondition[], columns?: LayerColumn[]): string {
  const parts = [spec.where, conds?.length && columns ? conditionsToWhere(conds, columns) : '']
    .filter((p): p is string => !!p)
    .map((p) => `(${p})`)
  return parts.length ? parts.join(' AND ') : '1=1'
}

/** The in-memory mirror of conditionsToWhere for geojson-kind externals (SQL null semantics: a null
 *  property fails every condition). */
export function matchesConditions(props: Record<string, unknown>, conds: FilterCondition[]): boolean {
  for (const c of conds) {
    const pv = props[c.field]
    if (pv == null) return false
    const sv = String(pv)
    const numeric = typeof pv === 'number' && !Number.isNaN(Number(c.value))
    if (c.op === 'eq' && sv !== c.value) return false
    if (c.op === 'ne' && sv === c.value) return false
    if (c.op === 'gte' && (numeric ? (pv as number) < Number(c.value) : sv < c.value)) return false
    if (c.op === 'lte' && (numeric ? (pv as number) > Number(c.value) : sv > c.value)) return false
    if (c.op === 'ilike' && !sv.toLowerCase().includes(c.value.toLowerCase())) return false
    if (c.op === 'in' && !c.value.split(',').map((x) => x.trim()).includes(sv)) return false
  }
  return true
}

/** Zero-config feature search over the map's VISIBLE external layers (the Locate panel's remote half):
 *  arcgis = one query per layer, OR-of-LIKEs across its string fields (composed with the carried +
 *  panel filters); geojson = an in-memory scan. Per-layer failures are silent (a flaky county server
 *  never breaks the whole search). */
export async function searchExternalLayers(
  specs: ExternalLayerSpec[],
  text: string,
  cache: Record<string, GeoJSON.FeatureCollection>,
  condsById: Record<string, FilterCondition[]>,
): Promise<FeatureSearchResult[]> {
  const PER_LAYER = 5
  const results = await Promise.allSettled(
    specs.map(async (spec): Promise<FeatureSearchResult[]> => {
      if (spec.externalKind === 'geojson') {
        const doc = await ensureGeojsonDoc(spec, cache)
        const needle = text.toLowerCase()
        const out: FeatureSearchResult[] = []
        for (const f of doc.features) {
          const props = (f?.properties ?? {}) as Record<string, unknown>
          if (condsById[spec.externalId]?.length && !matchesConditions(props, condsById[spec.externalId])) continue
          if (!Object.values(props).some((v) => v != null && String(v).toLowerCase().includes(needle))) continue
          const bbox = geojsonBbox([f])
          out.push({
            layer_id: spec.externalId,
            id: String(props.id ?? out.length),
            label: String(Object.values(props).find((v) => typeof v === 'string' && v) ?? spec.title),
            properties: props,
            rank: out.length,
            match_kind: 'exact', // external substring search — no fuzzy/wildcard tier, shown unbadged (v1 parity)
            point: bbox ? [(bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2] : null,
            bbox,
          })
          if (out.length >= PER_LAYER) break
        }
        return out
      }
      if (spec.externalKind !== 'arcgis-feature') return []
      const columns = await fetchExternalLayerColumns(spec, cache)
      const stringCols = columns.filter((c) => c.type === 'text').slice(0, 8)
      if (stringCols.length === 0) return []
      const needle = text.replace(/'/g, "''")
      const like = stringCols.map((c) => `UPPER(${c.name}) LIKE UPPER('%${needle}%')`).join(' OR ')
      const where = `${composedWhere(spec, condsById[spec.externalId], columns)} AND (${like})`
      const q = new URLSearchParams({
        where, outFields: '*', f: 'geojson', outSR: '4326', returnGeometry: 'true',
        resultRecordCount: String(PER_LAYER),
      })
      const res = await fetch(`${spec.url}/query?${q}`)
      if (!res.ok) return []
      const doc = (await res.json()) as GeoJSON.FeatureCollection & { error?: unknown }
      if (doc.error || !Array.isArray(doc.features)) return []
      return ensureFeatureIds(doc.features).map((f, i) => {
        const props = (f.properties ?? {}) as Record<string, unknown>
        const bbox = geojsonBbox([f])
        return {
          layer_id: spec.externalId,
          id: String(props.id ?? i),
          label: String(Object.values(props).find((v) => typeof v === 'string' && v) ?? spec.title),
          properties: props,
          rank: i,
          match_kind: 'exact', // external substring search — no fuzzy/wildcard tier, shown unbadged (v1 parity)
          point: bbox ? [(bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2] : null,
          bbox,
        }
      })
    }),
  )
  return results.flatMap((r) => (r.status === 'fulfilled' ? r.value : []))
}

/** The total row count under the layer's carried + panel filters (the "308,076 records" header). */
export async function fetchExternalRowCount(
  spec: ExternalLayerSpec,
  cache: Record<string, GeoJSON.FeatureCollection>,
  conds?: FilterCondition[],
): Promise<number | null> {
  if (spec.externalKind === 'geojson') {
    const doc = await ensureGeojsonDoc(spec, cache)
    if (!conds?.length) return doc.features.length
    return doc.features.filter((f) => matchesConditions((f?.properties ?? {}) as Record<string, unknown>, conds)).length
  }
  try {
    const columns = conds?.length ? await fetchExternalLayerColumns(spec, cache) : []
    const q = new URLSearchParams({ where: composedWhere(spec, conds, columns), returnCountOnly: 'true', f: 'json' })
    const doc = (await (await fetch(`${spec.url}/query?${q}`)).json()) as { count?: number }
    return typeof doc.count === 'number' ? doc.count : null
  } catch {
    return null
  }
}

/** One page of attribute rows (no geometry). Sorting is server-side for arcgis (throws when the
 *  service refuses orderByFields — the grid falls back to unsorted), in-memory for geojson. */
export async function fetchExternalRows(
  spec: ExternalLayerSpec,
  opts: { offset: number; limit: number; orderBy?: string; orderDir?: 'asc' | 'desc' },
  cache: Record<string, GeoJSON.FeatureCollection>,
  conds?: FilterCondition[],
): Promise<Record<string, unknown>[]> {
  if (spec.externalKind === 'geojson') {
    const doc = await ensureGeojsonDoc(spec, cache)
    let rows = doc.features.map((f) => (f?.properties ?? {}) as Record<string, unknown>)
    if (conds?.length) rows = rows.filter((r) => matchesConditions(r, conds))
    if (opts.orderBy) {
      const k = opts.orderBy
      const dir = opts.orderDir === 'desc' ? -1 : 1
      rows = [...rows].sort((a, b) => {
        const av = a[k], bv = b[k]
        if (av == null) return 1
        if (bv == null) return -1
        return (typeof av === 'number' && typeof bv === 'number' ? av - bv : String(av).localeCompare(String(bv))) * dir
      })
    }
    return rows.slice(opts.offset, opts.offset + opts.limit)
  }
  const columns = conds?.length ? await fetchExternalLayerColumns(spec, cache) : []
  const q = new URLSearchParams({
    where: composedWhere(spec, conds, columns),
    outFields: '*',
    returnGeometry: 'false',
    f: 'json',
    resultOffset: String(opts.offset),
    resultRecordCount: String(opts.limit),
  })
  if (opts.orderBy) q.set('orderByFields', `${opts.orderBy} ${opts.orderDir === 'desc' ? 'DESC' : 'ASC'}`)
  const res = await fetch(`${spec.url}/query?${q}`)
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  const doc = (await res.json()) as { features?: Array<{ attributes?: Record<string, unknown> }>; error?: { message?: string } }
  if (doc.error) throw new Error(doc.error.message ?? 'query failed')
  return (doc.features ?? []).map((f) => f.attributes ?? {})
}

/** One row's geometry (4326) for the highlight-and-zoom row click. */
export async function fetchExternalRowGeometry(
  spec: ExternalLayerSpec,
  objectIdField: string,
  id: unknown,
  cache: Record<string, GeoJSON.FeatureCollection>,
): Promise<GeoJSON.Geometry | null> {
  if (spec.externalKind === 'geojson') {
    const doc = await ensureGeojsonDoc(spec, cache)
    return doc.features.find((f) => (f?.properties as Record<string, unknown> | null)?.id === String(id))?.geometry ?? null
  }
  try {
    const q = new URLSearchParams({ objectIds: String(id), outFields: objectIdField, f: 'geojson', outSR: '4326' })
    const res = await fetch(`${spec.url}/query?${q}`)
    if (!res.ok) return null
    const doc = (await res.json()) as GeoJSON.FeatureCollection
    return doc?.features?.[0]?.geometry ?? null
  } catch {
    return null
  }
}

/** The raster tile template for a raster-kind external: XYZ verbatim; ArcGIS export per 256px tile
 *  via MapLibre's `{bbox-epsg-3857}` token (ImageServer speaks /exportImage, MapServer /export). */
const ARCGIS_PAGE = 1000 // per-request page; the service may clamp lower (maxRecordCount)

/** EVERY feature of an ArcGIS feature layer (or a GeoJSON document) as one FeatureCollection, for
 *  the Catalog's "Import from a URL" source.
 *  Browser-direct like every other external fetch
 *  (the no-proxy fence: the Talus server never calls the other server; the browser pages through
 *  `/query?f=geojson` and hands the result to the ordinary GeoJSON import door as an upload).
 *  Pages by resultOffset until the service stops saying exceededTransferLimit; `onProgress` reports
 *  features fetched so far; `signal` aborts between pages.
 *
 *  ★ Date fields are converted here, page by page (esriDates.ts). The service writes a date as epoch
 *  milliseconds; uploaded as-is, the import door can only see a 13-digit whole number, and the
 *  first real layer copied this way had every row refused (2026-11-06). */
export async function fetchExternalAllFeatures(
  probe: ExternalProbe,
  onProgress?: (fetched: number) => void,
  signal?: AbortSignal,
): Promise<GeoJSON.FeatureCollection> {
  if (probe.externalKind === 'geojson') {
    const res = await fetch(probe.url, { signal })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const doc = (await res.json()) as GeoJSON.FeatureCollection | GeoJSON.Feature
    const features = doc.type === 'FeatureCollection' ? doc.features : [doc]
    onProgress?.(features.length)
    return { type: 'FeatureCollection', features }
  }
  if (probe.externalKind !== 'arcgis-feature') throw new Error('Only a feature layer or a GeoJSON document can be imported')
  const out: GeoJSON.Feature[] = []
  for (let offset = 0; ; offset += ARCGIS_PAGE) {
    const q = new URLSearchParams({
      where: probe.where ?? '1=1',
      outFields: '*',
      f: 'geojson',
      outSR: '4326',
      resultOffset: String(offset),
      resultRecordCount: String(ARCGIS_PAGE),
    })
    const res = await fetch(`${probe.url}/query?${q}`, { signal })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const page = (await res.json()) as GeoJSON.FeatureCollection & {
      error?: { message?: string }
      properties?: { exceededTransferLimit?: boolean }
      exceededTransferLimit?: boolean
    }
    if (page.error) throw new Error(`The service answered: ${page.error.message ?? 'error'}`)
    const feats = Array.isArray(page.features) ? page.features : []
    convertEsriDateProps(feats, probe.dateFields ?? [])
    out.push(...feats)
    onProgress?.(out.length)
    const more = page.exceededTransferLimit ?? page.properties?.exceededTransferLimit ?? false
    if (feats.length === 0 || !more) break
  }
  return { type: 'FeatureCollection', features: out }
}

/** A size estimate for a full copy of an ArcGIS feature layer (bytes of GeoJSON), from one sample
 *  page: bytes-per-feature × the probe's count. The copy is assembled in the browser before upload
 *  (a large layer can exhaust the browser's memory),
 *  so the dialog shows this beside the count and
 *  refuses past the import door's own 512 MiB GeoJSON guard. null when nothing can be measured. */
export async function estimateExternalCopyBytes(probe: ExternalProbe, signal?: AbortSignal): Promise<number | null> {
  if (probe.externalKind !== 'arcgis-feature' || probe.featureCount === null || probe.featureCount === 0) return null
  // 25 features: enough to size a row, small enough that a slow server answers in a few seconds.
  const sample = Math.min(25, probe.featureCount)
  const q = new URLSearchParams({
    where: probe.where ?? '1=1', outFields: '*', f: 'geojson', outSR: '4326',
    resultOffset: '0', resultRecordCount: String(sample),
  })
  try {
    const res = await fetch(`${probe.url}/query?${q}`, { signal })
    if (!res.ok) return null
    const text = await res.text()
    const got = (JSON.parse(text) as { features?: unknown[] }).features?.length ?? 0
    if (got === 0) return null
    return Math.round((text.length / got) * probe.featureCount)
  } catch {
    return null
  }
}

export function externalRasterTemplate(spec: ExternalLayerSpec): string {
  if (spec.externalKind === 'xyz') return spec.url
  const op = /\/ImageServer$/i.test(spec.url) ? 'exportImage' : 'export'
  return `${spec.url}/${op}?bbox={bbox-epsg-3857}&bboxSR=3857&imageSR=3857&size=256,256&format=png32&transparent=true&f=image`
}


/** Identify (collectFeatureHits) keys a hit on a STRING properties.id — ArcGIS f=geojson carries the
 *  OBJECTID as a numeric feature-level `id` and raw GeoJSON may have neither, so fold one in (the
 *  same pattern the filtered-slot path uses for /features' top-level ids). */
function ensureFeatureIds(feats: GeoJSON.Feature[]): GeoJSON.Feature[] {
  return feats.map((f, i) => {
    const props = (f?.properties ?? {}) as Record<string, unknown>
    if (typeof props.id === 'string') return f
    const id = props.id != null ? String(props.id) : f?.id != null ? String(f.id) : `f${i}`
    return { ...f, properties: { ...props, id } }
  })
}

/** Viewport features for a VECTOR external → a FeatureCollection + a capped flag (feeds the existing
 *  "zoom in to load more" notice). arcgis-feature = paged bbox-scoped f=geojson queries against the
 *  remote server; geojson = the whole document, fetched once and cached by the caller. */
export async function fetchExternalViewport(
  spec: ExternalLayerSpec,
  bbox: string, // 'w,s,e,n' in 4326 (the current viewport)
  cap: number,
  geojsonCache: Record<string, GeoJSON.FeatureCollection>,
  conds?: FilterCondition[], // the map's panel filter (decision-feature-filter.md) — remote-composed / in-memory
): Promise<{ fc: GeoJSON.FeatureCollection; capped: boolean }> {
  if (spec.externalKind === 'geojson') {
    const doc = await ensureGeojsonDoc(spec, geojsonCache)
    if (!conds?.length) return { fc: doc, capped: false }
    return {
      fc: {
        type: 'FeatureCollection',
        features: doc.features.filter((f) => matchesConditions((f?.properties ?? {}) as Record<string, unknown>, conds)),
      },
      capped: false,
    }
  }
  // arcgis-feature
  const columns = conds?.length ? await fetchExternalLayerColumns(spec, geojsonCache) : []
  const out: GeoJSON.Feature[] = []
  let capped = false
  for (let offset = 0; out.length < cap; offset += ARCGIS_PAGE) {
    const q = new URLSearchParams({
      where: composedWhere(spec, conds, columns),
      outFields: '*',
      f: 'geojson',
      outSR: '4326',
      inSR: '4326',
      geometry: bbox,
      geometryType: 'esriGeometryEnvelope',
      spatialRel: 'esriSpatialRelIntersects',
      resultOffset: String(offset),
      resultRecordCount: String(Math.min(ARCGIS_PAGE, cap - out.length)),
    })
    const res = await fetch(`${spec.url}/query?${q}`)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const page = (await res.json()) as GeoJSON.FeatureCollection & { properties?: { exceededTransferLimit?: boolean }; exceededTransferLimit?: boolean }
    const feats = Array.isArray(page.features) ? page.features : []
    out.push(...feats)
    const more = page.exceededTransferLimit ?? page.properties?.exceededTransferLimit ?? false
    if (feats.length === 0 || !more) break
    if (out.length >= cap) {
      capped = true
      break
    }
  }
  return { fc: { type: 'FeatureCollection', features: ensureFeatureIds(out) }, capped }
}
