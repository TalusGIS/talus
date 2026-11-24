import type {
  ConstraintViolationCount,
  ContingentValues,
  CreateViewPayload,
  CreateViewResponse,
  CustomDomain,
  CustomDomainDetail,
  CustomField,
  CustomFieldCreate,
  CustomFieldsResponse,
  CustomFieldUpdate,
  DomainBinding,
  DomainCreate,
  DomainUpdate,
  DomainValue,
  DomainValuePatch,
  DomainValueSeed,
  EsriXmlImportResult,
  EligibleLocatorLayer,
  FeatureAttachment,
  FeatureAttachmentsResponse,
  FeatureHistoryResponse,
  FeatureCollection,
  FeatureSearchResponse,
  FieldConstraints,
  FieldValuesResponse,
  FormConfig,
  GeoCandidate,
  Geocoder,
  GeocoderTestResult,
  GeocoderType,
  GeocoderWrite,
  GeoSuggestion,
  LayerColumnsResponse,
  MappableField,
  GeometryType,
  LayerFeatureCollection,
  LayerQuery,
  LayerRelate,
  MyLayer,
  MyLayersResponse,
  RelatePatch,
  RelateSeed,
  Renderer,
  PreviewImportResult,
  SchemaImportResult,
  ShareLevel,
  SlotCreate,
  SlotProperties,
  SlotUpdate,
  JoinTableColumnsResponse,
  SymbolizableFieldsResponse,
  ViewSourceColumnsResponse,
  EditOps,
  LayerEditing,
} from '@/lib/api/types'
import { slotFeatureFromList, type PublicSlotFeature } from '@/lib/api/publicFeature'

/**
 * Talus API client — a pure module. No React, no knowledge of how the access
 * token is obtained or stored: the caller passes it in (e.g. from
 * react-oidc-context's `auth.user?.access_token`).
 *
 * Base URL resolution (backlog 9a): runtime /config.json wins (set once at boot via
 * _setApiBaseUrl, before React mounts), then VITE_API_BASE_URL, then the local api.
 * `let` + ESM live bindings on purpose: every `${API_BASE_URL}...` template across the
 * app sees the runtime value without threading config through call sites. Under edge
 * the runtime value is RELATIVE ("/api/v1") — fetches stay same-origin through Caddy.
 */
export let API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'

/** Boot-time hook for runtimeConfig (main.tsx). Never call after mount. */
export function _setApiBaseUrl(base: string): void {
  API_BASE_URL = base.replace(/\/+$/, '')
}

/**
 * The deployment's api ORIGIN, absolute — for display/copy surfaces (desktop connect
 * panels, JSON-LD download urls) that append their own /api/v1 and must never show a
 * relative base. Dev: http://localhost:8000. Edge (base "/api/v1"): the portal origin.
 */
export function apiOrigin(): string {
  return new URL(API_BASE_URL, window.location.origin).origin
}

/** API base as an ABSOLUTE url. MapLibre-facing URLs (tile templates, tilejson) MUST be absolute —
 *  MapLibre never resolves relative source urls, so under the 9a relative base ("/api/v1") tiles are
 *  silently never requested. A function (live binding), same rule as basemapArchiveUrl. */
export function apiBaseAbsolute(): string {
  return new URL(API_BASE_URL, window.location.origin).toString().replace(/\/$/, '')
}

/** Thrown on a non-2xx response; `status` lets callers special-case 401, etc. */
export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

/**
 * Thrown on a 409 from an optimistic-concurrency-guarded write (Talus 34): the row
 * changed since the client read it. `current` is the server's CURRENT row (carries a
 * fresh `version`) so the UI can offer refresh/overwrite without losing the user's work.
 */
export class ConflictError<P = unknown> extends ApiError {
  readonly current: P | null

  constructor(message: string, current: P | null) {
    super(409, message)
    this.name = 'ConflictError'
    this.current = current
  }
}

/** Build the right error for a failed write: ConflictError (with the current row) on 409,
 * otherwise a plain ApiError. Reads the response body at most once. */
async function writeError(res: Response): Promise<ApiError> {
  if (res.status === 409) {
    try {
      const body: unknown = await res.json()
      const detail = (body as { detail?: unknown })?.detail
      if (detail !== null && typeof detail === 'object') {
        const d = detail as { message?: unknown; current?: unknown }
        const msg = typeof d.message === 'string' ? d.message : 'This record changed since you opened it.'
        return new ConflictError(msg, (d.current as unknown) ?? null)
      }
    } catch {
      // non-JSON / unexpected shape — fall through to a bare conflict
    }
    return new ConflictError('This record changed since you opened it.', null)
  }
  return new ApiError(res.status, await errorDetail(res))
}

const _JSON_HEADERS = { 'Content-Type': 'application/json', Accept: 'application/json' }

export interface FetchListParams {
  /** EPSG:4326 bounding box: "lon_min,lat_min,lon_max,lat_max". */
  bbox: string
  /** Max features to return (server clamps to 1..5000; default 1000). */
  limit?: number
  /** Pagination offset (default 0). */
  offset?: number
}

/**
 * The CRUD surface one asset layer exposes, bound to a base path
 * (`/layers/{id}/features`). One implementation — the seam the map's
 * LayerConfig consumes so adding a layer doesn't touch the map.
 */
export interface AssetApi<P, C, U> {
  fetchList: (params: FetchListParams, accessToken: string, signal?: AbortSignal) => Promise<FeatureCollection<P>>
  fetchOne: (id: string, accessToken: string) => Promise<P>
  /** `idempotencyKey` (Talus 35) — the offline outbox's `clientOpId`, sent as `Idempotency-Key` so a
   *  replayed create (after a crash/network-drop mid-send) returns the existing feature, never a
   *  duplicate. Omitted for a plain online create. */
  create: (payload: C, accessToken: string, idempotencyKey?: string) => Promise<P>
  /** `version` is the optimistic-concurrency token read from the feature (sent as If-Match,
   *  Talus 34). Throws ConflictError on 409. */
  update: (id: string, payload: U, accessToken: string, version: string) => Promise<P>
  remove: (id: string, accessToken: string, version: string) => Promise<void>
}

/** Build an AssetApi over a REST resource that follows the feature-collection shape. */
export function makeAssetApi<P, C, U>(basePath: string): AssetApi<P, C, U> {
  return {
    async fetchList(params, accessToken, signal) {
      const query = new URLSearchParams({ bbox: params.bbox })
      if (params.limit !== undefined) query.set('limit', String(params.limit))
      if (params.offset !== undefined) query.set('offset', String(params.offset))
      const res = await fetch(`${API_BASE_URL}${basePath}?${query.toString()}`, {
        method: 'GET',
        headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
        signal,
      })
      if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
      return (await res.json()) as FeatureCollection<P>
    },

    async fetchOne(id, accessToken) {
      const res = await fetch(`${API_BASE_URL}${basePath}/${encodeURIComponent(id)}`, {
        method: 'GET',
        headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
      })
      if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
      return (await res.json()) as P
    },

    async create(payload, accessToken, idempotencyKey) {
      const res = await fetch(`${API_BASE_URL}${basePath}`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${accessToken}`,
          ..._JSON_HEADERS,
          ...(idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : {}),
        },
        body: JSON.stringify(payload),
      })
      if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
      return (await res.json()) as P
    },

    async update(id, payload, accessToken, version) {
      const res = await fetch(`${API_BASE_URL}${basePath}/${encodeURIComponent(id)}`, {
        method: 'PATCH',
        headers: { Authorization: `Bearer ${accessToken}`, 'If-Match': version, ..._JSON_HEADERS },
        body: JSON.stringify(payload),
      })
      if (!res.ok) throw await writeError(res)
      return (await res.json()) as P
    },

    async remove(id, accessToken, version) {
      const res = await fetch(`${API_BASE_URL}${basePath}/${encodeURIComponent(id)}`, {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${accessToken}`, 'If-Match': version },
      })
      if (!res.ok) throw await writeError(res)
    },
  }
}

/**
 * Build an AssetApi for a publisher slot layer (assets.tal_<8hex>, V019). Same AssetApi
 * surface the map consumes, but the slot route puts the layer id in the MIDDLE of the
 * path (/layers/{layerId}/features[/{fid}]), not appended like makeAssetApi. layerId is
 * captured in the closure — one api per slot, exactly as basePath is in makeAssetApi.
 */
export function makeSlotApi(layerId: string): AssetApi<SlotProperties, SlotCreate, SlotUpdate> {
  return {
    async fetchList() {
      // The map draws slots from MVT tiles, not list calls — never hit by the map.
      return { type: 'FeatureCollection', features: [] }
    },

    async fetchOne(fid, accessToken) {
      const res = await fetch(
        `${API_BASE_URL}/layers/${encodeURIComponent(layerId)}/features/${encodeURIComponent(fid)}`,
        {
          method: 'GET',
          headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
        },
      )
      if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
      return (await res.json()) as SlotProperties
    },

    async create(payload, accessToken, idempotencyKey) {
      const res = await fetch(`${API_BASE_URL}/layers/${encodeURIComponent(layerId)}/features`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${accessToken}`,
          ..._JSON_HEADERS,
          ...(idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : {}),
        },
        body: JSON.stringify(payload),
      })
      if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
      // Backend returns only { id }; AssetMap ignores create's return value.
      return (await res.json()) as unknown as SlotProperties
    },

    async update(fid, payload, accessToken, version) {
      const res = await fetch(
        `${API_BASE_URL}/layers/${encodeURIComponent(layerId)}/features/${encodeURIComponent(fid)}`,
        {
          method: 'PATCH',
          headers: { Authorization: `Bearer ${accessToken}`, 'If-Match': version, ..._JSON_HEADERS },
          body: JSON.stringify(payload),
        },
      )
      if (!res.ok) throw await writeError(res)
      // Backend returns { id, version }; AssetMap ignores update's return value.
      return (await res.json()) as unknown as SlotProperties
    },

    async remove(fid, accessToken, version) {
      const res = await fetch(
        `${API_BASE_URL}/layers/${encodeURIComponent(layerId)}/features/${encodeURIComponent(fid)}`,
        {
          method: 'DELETE',
          headers: { Authorization: `Bearer ${accessToken}`, 'If-Match': version },
        },
      )
      if (!res.ok) throw await writeError(res)
    },
  }
}

/** Per-asset API instances the LayerConfigs reference. */
/** An AssetApi with its per-layer P/C/U erased — what the offline sync engine dispatches against
 *  when it only has an op's `layerId` (not the map's typed LayerConfig). */
export type AnyAssetApi = AssetApi<unknown, unknown, unknown>

/**
 * Resolve the AssetApi for a layer id (Talus 35): a fresh slot api for a publisher slot
 * (`tal_<8hex>`, V019). The slot-id shape is inlined (this pure lib module must not import from
 * features/) and mirrors `isSlotId` in features/map/layers.ts. Returns null for anything else.
 * The bespoke signs/street_lights instances this used to branch on were retired with the pilot
 * built-ins (decision-retire-builtins.md phase 2).
 */
export function resolveAssetApi(layerId: string): AnyAssetApi | null {
  if (/^tal_[0-9a-f]{8}$/.test(layerId)) return makeSlotApi(layerId) as unknown as AnyAssetApi
  return null
}

export async function fetchMyLayers(accessToken: string): Promise<MyLayersResponse> {
  const res = await fetch(`${API_BASE_URL}/me/layers`, {
    method: 'GET',
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as MyLayersResponse
}

/**
 * PUT/DELETE /me/favorites/{itemType}/{itemId} — star/unstar a catalog item for the caller
 * (Talus 18, generalized to any item type in Talus 24). `itemType` is 'layer' | 'web_map'. ADD is
 * view-gated server-side; both are idempotent (204). Favorite state is read back via the `favorite`
 * flag on GET /me/layers and GET /me/web-maps.
 */
export type FavoriteItemType = 'layer' | 'web_map' | 'raster' | 'external' | 'field_project'

export async function addFavorite(
  itemType: FavoriteItemType,
  itemId: string,
  accessToken: string,
): Promise<void> {
  const res = await fetch(
    `${API_BASE_URL}/me/favorites/${itemType}/${encodeURIComponent(itemId)}`,
    { method: 'PUT', headers: { Authorization: `Bearer ${accessToken}` } },
  )
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
}

export async function removeFavorite(
  itemType: FavoriteItemType,
  itemId: string,
  accessToken: string,
): Promise<void> {
  const res = await fetch(
    `${API_BASE_URL}/me/favorites/${itemType}/${encodeURIComponent(itemId)}`,
    { method: 'DELETE', headers: { Authorization: `Bearer ${accessToken}` } },
  )
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
}

// --- layer symbology (Talus 13) -------------------------------------------------

/** GET /layers/{id}/fields — the fields this layer can be symbolized on (DB-introspected;
 * coded fields carry their domain table name). View-or-manage gated. */
export async function fetchLayerFields(
  layerId: string,
  accessToken: string,
): Promise<SymbolizableFieldsResponse> {
  const res = await fetch(`${API_BASE_URL}/layers/${layerId}/fields`, {
    method: 'GET',
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as SymbolizableFieldsResponse
}

/** GET /layers/{id}/field-values/{field} — capped distinct values for a non-coded field.
 * `limit` (server-clamped to 2000) lets the filter suggestion list ask for more than the
 * symbology default (200). */
export async function fetchFieldValues(
  layerId: string,
  field: string,
  accessToken: string,
  limit?: number,
): Promise<FieldValuesResponse> {
  const q = limit !== undefined ? `?limit=${limit}` : ''
  const res = await fetch(`${API_BASE_URL}/layers/${layerId}/field-values/${encodeURIComponent(field)}${q}`, {
    method: 'GET',
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as FieldValuesResponse
}

/** PUT /layers/{id}/renderer — set the layer-default renderer (owner/admin). */
export async function setLayerRenderer(
  layerId: string,
  renderer: Renderer,
  accessToken: string,
): Promise<void> {
  const res = await fetch(`${API_BASE_URL}/layers/${layerId}/renderer`, {
    method: 'PATCH',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify(renderer),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
}

/** DELETE /layers/{id}/renderer — clear back to single-symbol fallback (owner/admin). */
export async function clearLayerRenderer(layerId: string, accessToken: string): Promise<void> {
  const res = await fetch(`${API_BASE_URL}/layers/${layerId}/renderer`, {
    method: 'DELETE',
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
}

/** POST /layers body — claim a slot and name/colour it (publisher only). */
export interface CreateLayerPayload {
  display_name: string
  display_color: string
  working_srid?: number // the layer's working CRS (default 4326); storage stays canonical 4326
  native_storage?: boolean // opt-in: physically store geom in working_srid (byte-exact). Requires working_srid<>4326.
  // 'point' (default) | 'line' | 'polygon' (decision-line-polygon-layers.md); 'none' = a non-spatial
  // TABLE (V059, decision-nonspatial-tables.md) — the union the server actually accepts.
  geom_type?: GeometryType | 'none'
}

/** POST /layers response — the claimed slot's stable id + stored presentation. */
export interface CreateLayerResponse {
  layer_id: string
  display_name: string
  display_color: string | null
  working_srid?: number
  native_storage?: boolean
  geom_type?: GeometryType | 'none' // echoes the request — 'none' for a non-spatial table
  status: string
}

/** One coordinate reference system for the wizard's Spatial Reference picker (GET /spatial-refs). */
export interface SpatialRef {
  srid: number
  authName: string
  name: string
}

/** GET /spatial-refs?q= — search coordinate systems (empty q → a curated common set). Any authenticated user. */
export async function searchSpatialRefs(q: string, accessToken: string): Promise<SpatialRef[]> {
  const res = await fetch(`${API_BASE_URL}/spatial-refs?q=${encodeURIComponent(q)}`, {
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return ((await res.json()) as { results: SpatialRef[] }).results
}

/** POST /layers/{id}/features/import result — validate-first dry run (commit=false) or commit (E4). */
export interface FeatureImportResult {
  total: number
  valid: number
  error_count: number
  errors: { index: number; reason: string }[]
  unmatched_columns: string[]
  committed: number
}

/** A parsed upload for feature ingest: GeoJSON text, or a base64'd GeoPackage / zipped Shapefile (E4).
 *  source_srid overrides the file's declared CRS (used when a Shapefile .prj has no EPSG code).
 *  csv_x_column/csv_y_column (both or neither) override the coordinate-column auto-detect — the
 *  add-from-file wizard's manual pick (decision-add-from-file.md §2.3). */
export type FeatureImportPayload =
  | { format: 'geojson'; geojson_text: string }
  | { format: 'geopackage'; gpkg_base64: string; source_srid?: number }
  | { format: 'shapefile'; shp_base64: string; source_srid?: number }
  | { format: 'csv'; csv_text: string; source_srid?: number; csv_x_column?: string; csv_y_column?: string }
  | { format: 'geoparquet'; parquet_base64: string; source_srid?: number }

/** POST /layers/{id}/features/import — bulk-ingest points; commit=false dry-runs, true inserts (E4). */
export async function importFeatures(
  layerId: string,
  payload: FeatureImportPayload,
  commit: boolean,
  accessToken: string,
): Promise<FeatureImportResult> {
  const res = await fetch(`${API_BASE_URL}/layers/${encodeURIComponent(layerId)}/features/import`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify({ ...payload, commit }),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as FeatureImportResult
}

/** GET /layers/{id}/extent — the layer's WGS-84 bbox [minx, miny, maxx, maxy], or null when empty/non-spatial.
 *  The add-from-file post-create zoom (a fresh layer has no rendered tiles for querySourceFeatures).
 *  Optional `featureIds` (repeatable `id` — decision-zoom-to-selected.md F1) bounds the bbox to just
 *  those rows; all ids missing → bbox null (callers no-op). */
export async function fetchLayerExtent(
  layerId: string,
  accessToken: string,
  featureIds?: string[],
): Promise<{ layerId: string; bbox: [number, number, number, number] | null }> {
  const q = new URLSearchParams()
  for (const fid of featureIds ?? []) q.append('id', fid)
  const qs = q.toString()
  const res = await fetch(
    `${API_BASE_URL}/layers/${encodeURIComponent(layerId)}/extent${qs ? `?${qs}` : ''}`,
    { headers: { Authorization: `Bearer ${accessToken}` } },
  )
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as { layerId: string; bbox: [number, number, number, number] | null }
}

/** Geometry calculation + bulk set field value (decision-geometry-calc.md). */
export type CalcOp = 'x' | 'y' | 'length' | 'perimeter' | 'area' | 'set'

/** POST /layers/{id}/features/calc body: op + target field + op params, over a scope of the grid's
 *  own predicates (filter strings / selected ids / bbox). commit=false dry-runs the matched count. */
export interface FeatureCalcPayload {
  op: CalcOp
  target_field: string
  unit?: string // length/perimeter: ft|m|mi|km · area: acres|sqft|sqm|sqkm|hectares
  srid?: number // x/y only; omitted = the layer's working CRS
  value?: unknown // set only; null clears the field
  scope: { filters?: string[]; ids?: string[]; bbox?: string }
  commit: boolean
}

export interface FeatureCalcResult {
  matched: number
  committed: boolean
  updated?: number // present after an apply
}

/** POST /layers/{id}/features/calc — dry-run (commit=false) then apply (editor only; 50k row cap). */
export async function calcFeatures(
  layerId: string,
  payload: FeatureCalcPayload,
  accessToken: string,
): Promise<FeatureCalcResult> {
  const res = await fetch(`${API_BASE_URL}/layers/${encodeURIComponent(layerId)}/features/calc`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify(payload),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as FeatureCalcResult
}

/** GET /transform-point — reproject the point (lon,lat in `fromSrid`, default WGS-84) into `srid` (X/Y).
 *  Used by the "What's here?" readout (E2) and working-CRS coordinate entry (E3). */
export async function transformPoint(
  lon: number,
  lat: number,
  srid: number,
  accessToken: string,
  fromSrid?: number,
): Promise<{ x: number; y: number; srid: number }> {
  const fs = fromSrid != null ? `&from_srid=${fromSrid}` : ''
  const res = await fetch(
    `${API_BASE_URL}/transform-point?lon=${lon}&lat=${lat}&srid=${srid}${fs}`,
    { headers: { Authorization: `Bearer ${accessToken}` } },
  )
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as { x: number; y: number; srid: number }
}

/**
 * Claim a publisher slot (Talus 10). The api picks a free slot, names/colours it, and
 * grants the caller owner access. 409 "no free layer slots" when the pool is exhausted;
 * 403 if the caller lacks talus-publisher/admin — both surface via ApiError.message.
 */
export async function createLayer(
  payload: CreateLayerPayload,
  accessToken: string,
): Promise<CreateLayerResponse> {
  const res = await fetch(`${API_BASE_URL}/layers`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify(payload),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as CreateLayerResponse
}

/**
 * DELETE /layers/{id} — release a claimed slot back to the pool (owner or admin). This WIPES the
 * slot's features. 404 for an unknown id; 409 for a built-in (non-slot) layer or a delete-protected
 * one (Talus 24); 403 if not owner/admin — all surface via ApiError.message for the catalog notice.
 */
export async function deleteLayer(layerId: string, accessToken: string): Promise<void> {
  const res = await fetch(`${API_BASE_URL}/layers/${encodeURIComponent(layerId)}`, {
    method: 'DELETE',
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
}

// --- view layers (Talus 38) ----------------------------------------------------

/** GET /layer-views/sources/{id}/columns — a source layer's columns for the view allow-list picker
 *  (admin or source owner; 403 otherwise). */
export async function fetchViewSourceColumns(
  sourceId: string,
  accessToken: string,
): Promise<ViewSourceColumnsResponse> {
  const res = await fetch(
    `${API_BASE_URL}/layer-views/sources/${encodeURIComponent(sourceId)}/columns`,
    { headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' } },
  )
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as ViewSourceColumnsResponse
}

/** GET /layer-views/join-tables/{id}/columns — a candidate join table's columns + field keys for
 *  the join picker (admin or table owner; 403 otherwise, 422 if not a standalone table). */
export async function fetchJoinTableColumns(
  tableId: string,
  accessToken: string,
): Promise<JoinTableColumnsResponse> {
  const res = await fetch(
    `${API_BASE_URL}/layer-views/join-tables/${encodeURIComponent(tableId)}/columns`,
    { headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' } },
  )
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as JoinTableColumnsResponse
}

/** POST /layer-views — author a sanitized view of a source layer (admin or source owner). */
export async function createLayerView(
  payload: CreateViewPayload,
  accessToken: string,
): Promise<CreateViewResponse> {
  const res = await fetch(`${API_BASE_URL}/layer-views`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify(payload),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as CreateViewResponse
}

/** DELETE /layer-views/{id} — remove a view layer (admin, author, or source owner). The view's own
 *  delete path; a view's vw_ id is NOT releasable via DELETE /layers/{id} (that 409s "built-in"). */
export async function deleteLayerView(viewId: string, accessToken: string): Promise<void> {
  const res = await fetch(`${API_BASE_URL}/layer-views/${encodeURIComponent(viewId)}`, {
    method: 'DELETE',
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
}

// --- owner-defined custom fields (data-owner-independence arc) ------------------

/** GET /layers/{id}/custom-fields — list a slot layer's owner-defined field defs (view-or-manage). */
export async function listCustomFields(
  layerId: string,
  accessToken: string,
): Promise<CustomFieldsResponse> {
  const res = await fetch(`${API_BASE_URL}/layers/${encodeURIComponent(layerId)}/custom-fields`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as CustomFieldsResponse
}

/** POST /layers/{id}/custom-fields — define a field (admin or layer owner). */
export async function createCustomField(
  layerId: string,
  payload: CustomFieldCreate,
  accessToken: string,
): Promise<CustomField> {
  const res = await fetch(`${API_BASE_URL}/layers/${encodeURIComponent(layerId)}/custom-fields`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify(payload),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as CustomField
}

/** PATCH /layers/{id}/custom-fields/{fieldId} — edit label/required/default/order/active
 *  (key + type are immutable; admin or layer owner). */
export async function updateCustomField(
  layerId: string,
  fieldId: string,
  patch: CustomFieldUpdate,
  accessToken: string,
): Promise<CustomField> {
  const res = await fetch(
    `${API_BASE_URL}/layers/${encodeURIComponent(layerId)}/custom-fields/${encodeURIComponent(fieldId)}`,
    {
      method: 'PATCH',
      headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
      body: JSON.stringify(patch),
    },
  )
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as CustomField
}

/** POST /layers/{id}/custom-fields/{fieldId}/violations — author-time impact preview (fork 7): how many
 *  existing rows already break a PROPOSED constraint. Read-only; admin or layer owner. */
export async function countFieldViolations(
  layerId: string,
  fieldId: string,
  constraints: FieldConstraints,
  accessToken: string,
): Promise<ConstraintViolationCount> {
  const res = await fetch(
    `${API_BASE_URL}/layers/${encodeURIComponent(layerId)}/custom-fields/${encodeURIComponent(fieldId)}/violations`,
    {
      method: 'POST',
      headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
      body: JSON.stringify({ constraints }),
    },
  )
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as ConstraintViolationCount
}

/** POST /layers/{id}/custom-fields/{fieldId}/contingency-violations — impact preview (V077): how many
 *  existing rows already hold a parent/child pair the PROPOSED mapping would forbid. Read-only. */
export async function countContingencyViolations(
  layerId: string,
  fieldId: string,
  parentFieldKey: string,
  contingentValues: ContingentValues,
  accessToken: string,
): Promise<ConstraintViolationCount> {
  const res = await fetch(
    `${API_BASE_URL}/layers/${encodeURIComponent(layerId)}/custom-fields/${encodeURIComponent(fieldId)}/contingency-violations`,
    {
      method: 'POST',
      headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
      body: JSON.stringify({ parentFieldKey, contingentValues }),
    },
  )
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as ConstraintViolationCount
}

/** POST /layers/{id}/custom-fields/import — bulk-create fields from a CSV schema. commit=false previews
 *  (per-row verdicts, no writes); commit=true creates the valid rows. Admin or layer owner. */
export async function importCustomFields(
  layerId: string,
  csvText: string,
  commit: boolean,
  accessToken: string,
): Promise<SchemaImportResult> {
  const res = await fetch(`${API_BASE_URL}/layers/${encodeURIComponent(layerId)}/custom-fields/import`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify({ csv_text: csvText, commit }),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as SchemaImportResult
}

/** POST /layers/{id}/custom-fields/import-geopackage — bulk-create fields from a GeoPackage schema. The
 *  .gpkg bytes are base64-encoded (JSON transport); same dry-run/commit semantics as the CSV import. */
export async function importGeopackageFields(
  layerId: string,
  gpkgBase64: string,
  commit: boolean,
  accessToken: string,
): Promise<SchemaImportResult> {
  const res = await fetch(`${API_BASE_URL}/layers/${encodeURIComponent(layerId)}/custom-fields/import-geopackage`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify({ gpkg_base64: gpkgBase64, commit }),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as SchemaImportResult
}

/** POST /layers/{id}/custom-fields/import-esri-xml — bulk-create fields from an Esri XML Workspace. A GDB is
 *  multi-table: with no `table` and multiple feature classes the response is the picker list (needsTable);
 *  pass `table` to preview/commit that one. */
export async function importEsriXmlFields(
  layerId: string,
  xmlBase64: string,
  table: string | null,
  commit: boolean,
  accessToken: string,
): Promise<EsriXmlImportResult> {
  const res = await fetch(`${API_BASE_URL}/layers/${encodeURIComponent(layerId)}/custom-fields/import-esri-xml`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify({ xml_base64: xmlBase64, table, commit }),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as EsriXmlImportResult
}

/** POST /custom-fields/preview-import — parse a schema OR data file into draft field definitions WITHOUT a
 *  layer (the create wizard's deferred-commit flow). Returns verdicts + the field-create payloads for 'ok'
 *  rows; multi-table Esri returns needsTable + tables. The data-file formats (shapefile/geojson/csv-data —
 *  add-from-file) also return seed metadata (geometry/CRS/counts). Publisher only. */
export async function previewImportFields(
  body: {
    format: 'csv' | 'geopackage' | 'esri-xml' | 'shapefile' | 'geojson' | 'csv-data'
    csv_text?: string
    payload_base64?: string
    table?: string | null
  },
  accessToken: string,
): Promise<PreviewImportResult> {
  const res = await fetch(`${API_BASE_URL}/custom-fields/preview-import`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as PreviewImportResult
}

/** GET /layers/{id}/custom-fields/export.csv — the layer's active field schema as CSV text (re-importable). */
export async function exportCustomFieldsCsv(layerId: string, accessToken: string): Promise<string> {
  const res = await fetch(`${API_BASE_URL}/layers/${encodeURIComponent(layerId)}/custom-fields/export.csv`, {
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return await res.text()
}

/** GET /layers/{id}/custom-fields/export.gpkg — the layer's active schema as an empty OGC GeoPackage (Blob;
 *  re-importable). Lossier than CSV: carries field_key/type/label/required + number range; not defaults,
 *  text length/pattern, or date min/max (CSV is the lossless schema export). */
export async function exportCustomFieldsGpkg(layerId: string, accessToken: string): Promise<Blob> {
  const res = await fetch(`${API_BASE_URL}/layers/${encodeURIComponent(layerId)}/custom-fields/export.gpkg`, {
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return await res.blob()
}

/** DELETE /layers/{id}/custom-fields/{fieldId} — hard-delete; refused 409 if the field has data
 *  (retire via { active: false } instead). Admin or layer owner. */
export async function deleteCustomField(
  layerId: string,
  fieldId: string,
  accessToken: string,
): Promise<void> {
  const res = await fetch(
    `${API_BASE_URL}/layers/${encodeURIComponent(layerId)}/custom-fields/${encodeURIComponent(fieldId)}`,
    { method: 'DELETE', headers: { Authorization: `Bearer ${accessToken}` } },
  )
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
}

// --- reusable coded-value domains (slice 2) ------------------------------------

/** GET /custom-domains — all domains (org-wide; the field-designer picker). */
export async function listCustomDomains(accessToken: string): Promise<CustomDomain[]> {
  const res = await fetch(`${API_BASE_URL}/custom-domains`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return ((await res.json()) as { domains: CustomDomain[] }).domains
}

/** GET /custom-domains/{id} — a domain + its values (active and retired). */
export async function getCustomDomain(
  domainId: string,
  accessToken: string,
): Promise<CustomDomainDetail> {
  const res = await fetch(`${API_BASE_URL}/custom-domains/${encodeURIComponent(domainId)}`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as CustomDomainDetail
}

/** POST /custom-domains — create a domain (creator = owner; optional initial values). */
export async function createCustomDomain(
  payload: DomainCreate,
  accessToken: string,
): Promise<CustomDomainDetail> {
  const res = await fetch(`${API_BASE_URL}/custom-domains`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify(payload),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as CustomDomainDetail
}

/** PATCH /custom-domains/{id} — edit name/description (owner or admin). */
export async function updateCustomDomain(
  domainId: string,
  patch: DomainUpdate,
  accessToken: string,
): Promise<CustomDomainDetail> {
  const res = await fetch(`${API_BASE_URL}/custom-domains/${encodeURIComponent(domainId)}`, {
    method: 'PATCH',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify(patch),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as CustomDomainDetail
}

/** DELETE /custom-domains/{id} — refused 409 if still bound to a field (owner or admin). */
export async function deleteCustomDomain(domainId: string, accessToken: string): Promise<void> {
  const res = await fetch(`${API_BASE_URL}/custom-domains/${encodeURIComponent(domainId)}`, {
    method: 'DELETE',
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
}

/** GET /custom-domains/{id}/usage — the fields/layers bound to a domain. */
export async function getDomainUsage(
  domainId: string,
  accessToken: string,
): Promise<DomainBinding[]> {
  const res = await fetch(`${API_BASE_URL}/custom-domains/${encodeURIComponent(domainId)}/usage`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return ((await res.json()) as { bindings: DomainBinding[] }).bindings
}

/** POST /custom-domains/{id}/values — add a value (owner or admin). */
export async function addDomainValue(
  domainId: string,
  value: DomainValueSeed,
  accessToken: string,
): Promise<DomainValue> {
  const res = await fetch(`${API_BASE_URL}/custom-domains/${encodeURIComponent(domainId)}/values`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify(value),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as DomainValue
}

/** PATCH /custom-domains/{id}/values/{valueId} — relabel/retire/reorder (code immutable; owner or admin). */
export async function updateDomainValue(
  domainId: string,
  valueId: string,
  patch: DomainValuePatch,
  accessToken: string,
): Promise<DomainValue> {
  const res = await fetch(
    `${API_BASE_URL}/custom-domains/${encodeURIComponent(domainId)}/values/${encodeURIComponent(valueId)}`,
    { method: 'PATCH', headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS }, body: JSON.stringify(patch) },
  )
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as DomainValue
}

/** DELETE /custom-domains/{id}/values/{valueId} — refused 409 if a feature still uses it (owner or admin). */
export async function deleteDomainValue(
  domainId: string,
  valueId: string,
  accessToken: string,
): Promise<void> {
  const res = await fetch(
    `${API_BASE_URL}/custom-domains/${encodeURIComponent(domainId)}/values/${encodeURIComponent(valueId)}`,
    { method: 'DELETE', headers: { Authorization: `Bearer ${accessToken}` } },
  )
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
}

// --- table↔layer relates (decision-relates-and-join-views.md PR A) --------------

/** GET /layers/{id}/relates — the layer's relates, ALREADY viewer-filtered server-side (a relate
 * to a table this caller can't view is omitted entirely; no count, no name). View-gated. */
export async function fetchLayerRelates(
  layerId: string,
  accessToken: string,
  signal?: AbortSignal,
): Promise<LayerRelate[]> {
  const res = await fetch(`${API_BASE_URL}/layers/${encodeURIComponent(layerId)}/relates`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
    signal,
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return ((await res.json()) as { relates: LayerRelate[] }).relates
}

/** POST /layers/{id}/relates — author a relate to a table (admin or layer owner). */
export async function createLayerRelate(
  layerId: string,
  seed: RelateSeed,
  accessToken: string,
): Promise<LayerRelate> {
  const res = await fetch(`${API_BASE_URL}/layers/${encodeURIComponent(layerId)}/relates`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify(seed),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as LayerRelate
}

/** PATCH /layers/{id}/relates/{relateId} — display name/cardinality/order (keys immutable). */
export async function updateLayerRelate(
  layerId: string,
  relateId: string,
  patch: RelatePatch,
  accessToken: string,
): Promise<LayerRelate> {
  const res = await fetch(
    `${API_BASE_URL}/layers/${encodeURIComponent(layerId)}/relates/${encodeURIComponent(relateId)}`,
    { method: 'PATCH', headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS }, body: JSON.stringify(patch) },
  )
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as LayerRelate
}

/** DELETE /layers/{id}/relates/{relateId} — removes navigation metadata only; no data is touched. */
export async function deleteLayerRelate(
  layerId: string,
  relateId: string,
  accessToken: string,
): Promise<void> {
  const res = await fetch(
    `${API_BASE_URL}/layers/${encodeURIComponent(layerId)}/relates/${encodeURIComponent(relateId)}`,
    { method: 'DELETE', headers: { Authorization: `Bearer ${accessToken}` } },
  )
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
}

// --- catalog Data table (Talus 15) ---------------------------------------------

/** GET /layers/{id}/columns — the FULL column schema (name/type/coded-domain) that drives
 * the Data table + Fields view. View-gated; token-OPTIONAL (Talus 23): an anonymous caller on a
 * public layer gets the anon-scrubbed schema — which is exactly what the dynamic-tile `fields=`
 * builder needs, so an anon map never requests a scrubbed column (decision-dynamic-mvt.md §4). */
export async function fetchLayerColumns(
  layerId: string,
  accessToken?: string | null,
): Promise<LayerColumnsResponse> {
  const res = await fetch(`${API_BASE_URL}/layers/${encodeURIComponent(layerId)}/columns`, {
    headers: {
      ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
      Accept: 'application/json',
    },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as LayerColumnsResponse
}

function layerQueryParams(params: LayerQuery): URLSearchParams {
  const q = new URLSearchParams()
  if (params.bbox) q.set('bbox', params.bbox)
  for (const f of params.filters ?? []) q.append('filter', f)
  for (const i of params.ids ?? []) q.append('id', i)
  if (params.sort) q.set('sort', params.sort)
  if (params.limit !== undefined) q.set('limit', String(params.limit))
  if (params.offset !== undefined) q.set('offset', String(params.offset))
  return q
}

/** One feature of a publicly shared layer, for a SIGNED-OUT viewer's popup — read through the list
 *  door (`GET /layers/{id}/features?id=<fid>&limit=1`), sent with no Authorization header.
 *
 *  The single-feature door is signed-in only; the list door has answered an anonymous caller since
 *  public sharing shipped, gated to the layer's own public share (404 otherwise) and already
 *  stripped of editor names and AI provenance. So this adds no anonymous surface: it asks an
 *  existing one for one row. A layer that is not public, or a feature that is not in it, throws
 *  ApiError(404). */
export async function fetchPublicSlotFeature(
  layerId: string,
  fid: string,
  signal?: AbortSignal,
): Promise<PublicSlotFeature> {
  const q = layerQueryParams({ ids: [fid], limit: 1 })
  const res = await fetch(`${API_BASE_URL}/layers/${encodeURIComponent(layerId)}/features?${q}`, {
    headers: { Accept: 'application/json' },
    signal,
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  const feature = slotFeatureFromList(await res.json(), fid)
  if (feature === null) throw new ApiError(404, 'feature not found')
  return feature
}

/** GET /layers/{id}/features — server-side filter/sort/paginate; OGC numberMatched/Returned. */
export async function queryLayerFeatures(
  layerId: string,
  params: LayerQuery,
  accessToken: string,
  signal?: AbortSignal,
): Promise<LayerFeatureCollection> {
  const q = layerQueryParams(params)
  const res = await fetch(`${API_BASE_URL}/layers/${encodeURIComponent(layerId)}/features?${q}`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
    signal,
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as LayerFeatureCollection
}

/**
 * Download a layer export (CSV/GeoJSON) honoring the active filters/sort + optional selected
 * `ids`. Fetches with the bearer token (a bare <a href> can't carry it), then saves the blob.
 */
export async function downloadLayerExport(
  layerId: string,
  params: LayerQuery & { ids?: string[]; srid?: number | null },
  format: 'csv' | 'geojson' | 'gpkg',
  accessToken: string,
): Promise<{ truncated: boolean; returned: number; cap: number }> {
  const q = new URLSearchParams()
  q.set('format', format)
  if (params.bbox) q.set('bbox', params.bbox)
  for (const f of params.filters ?? []) q.append('filter', f)
  if (params.sort) q.set('sort', params.sort)
  for (const id of params.ids ?? []) q.append('id', id)
  if (params.srid != null) q.set('srid', String(params.srid)) // output-CRS reproject (E1)
  const res = await fetch(`${API_BASE_URL}/layers/${encodeURIComponent(layerId)}/export?${q}`, {
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  // Truncation signal (decision-export-truncation.md §2): the server caps an export and says so
  // in headers + the filename. We set our own `a.download`, so we must MIRROR the marker —
  // otherwise the SPA is the one surface that hides it.
  const truncated = res.headers.get('X-Talus-Export-Truncated') === 'true'
  const returned = Number(res.headers.get('X-Talus-Export-Returned') ?? '0')
  const cap = Number(res.headers.get('X-Talus-Export-Cap') ?? '0')
  const blob = await res.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  const ext = format === 'csv' ? 'csv' : format === 'gpkg' ? 'gpkg' : 'geojson'
  const stem = `${layerId}-${new Date().toISOString().slice(0, 10)}`
  a.href = url
  a.download = truncated ? `${stem}-TRUNCATED-at-${cap}.${ext}` : `${stem}.${ext}`
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
  return { truncated, returned, cap }
}

// --- media / photos (Talus 16) -------------------------------------------------

// The generic, per-layer-gated attachment surface (any registered layer). Read is view-gated
// (anon-when-public); upload/delete are edit-gated + attachments_enabled. Bytes are fetched to a Blob the
// caller turns into an object URL it owns (a bearer-gated <img src> can't carry Authorization).

function _featureAttachmentsUrl(layerId: string, featureId: string): string {
  return `${API_BASE_URL}/layers/${encodeURIComponent(layerId)}/features/${encodeURIComponent(featureId)}/attachments`
}

function _attachmentUrl(layerId: string, attachmentId: string): string {
  return `${API_BASE_URL}/layers/${encodeURIComponent(layerId)}/attachments/${encodeURIComponent(attachmentId)}`
}

// Attachment reads are view-gated, anon-when-public (decision-attachments-a4.md §3.6): the map popup runs
// signed-out on a public map, so the strip's fetches omit Authorization when there's no token (a present
// but empty Bearer would 401; a missing header is the anon path _gate_read expects).
function _maybeAuth(token?: string): Record<string, string> {
  return token ? { Authorization: `Bearer ${token}` } : {}
}

/** GET /layers/{id}/has-attachments — the A-4 per-layer probe (§3.1). One EXISTS; view-gated, anon-when-public
 * (anon on a private layer → 404 → treated as false by the caller's catch). Token-optional. */
export async function fetchHasAttachments(
  layerId: string,
  accessToken?: string,
  signal?: AbortSignal,
): Promise<boolean> {
  const res = await fetch(`${API_BASE_URL}/layers/${encodeURIComponent(layerId)}/has-attachments`, {
    headers: { ..._maybeAuth(accessToken), Accept: 'application/json' },
    signal,
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return Boolean(((await res.json()) as { hasAttachments?: boolean }).hasAttachments)
}

/** GET an attachment's ≤256px JPEG thumbnail as a Blob (A-4 §3.3). Image content-types only server-side;
 * a non-image or undecodable image → 404 (caller falls back to a file chip). Token-optional. */
export async function fetchAttachmentThumbnailBlob(
  layerId: string,
  attachmentId: string,
  accessToken?: string,
  signal?: AbortSignal,
): Promise<Blob> {
  const res = await fetch(`${_attachmentUrl(layerId, attachmentId)}/thumbnail`, {
    headers: _maybeAuth(accessToken),
    signal,
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return await res.blob()
}

/** GET a feature's attachments (metadata, creation order). View-gated (anon-when-public). */
export async function fetchFeatureAttachments(
  layerId: string,
  featureId: string,
  accessToken?: string,
  signal?: AbortSignal,
): Promise<FeatureAttachmentsResponse> {
  const res = await fetch(_featureAttachmentsUrl(layerId, featureId), {
    headers: { ..._maybeAuth(accessToken), Accept: 'application/json' },
    signal,
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as FeatureAttachmentsResponse
}

/** GET one page of a feature's edit history (decision-edit-history-ui.md). View-gated and, unlike
 * attachments, NEVER anonymous — history is identity-bearing, so a token is required (an anon call
 * 401s). `cursor` = the prior page's next_cursor (keyset paging, newest-first). */
export async function fetchFeatureHistory(
  layerId: string,
  featureId: string,
  opts: { limit?: number; cursor?: string | null } = {},
  accessToken?: string,
  signal?: AbortSignal,
): Promise<FeatureHistoryResponse> {
  const q = new URLSearchParams()
  if (opts.limit != null) q.set('limit', String(opts.limit))
  if (opts.cursor != null) q.set('cursor', opts.cursor)
  const qs = q.toString()
  const res = await fetch(
    `${API_BASE_URL}/layers/${encodeURIComponent(layerId)}/features/${encodeURIComponent(featureId)}/history${qs ? `?${qs}` : ''}`,
    { headers: { ..._maybeAuth(accessToken), Accept: 'application/json' }, signal },
  )
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as FeatureHistoryResponse
}

/** GET an attachment's bytes as a Blob (bearer-gated; caller owns the object URL). */
export async function fetchAttachmentBlob(
  layerId: string,
  attachmentId: string,
  accessToken?: string,
  signal?: AbortSignal,
): Promise<Blob> {
  const res = await fetch(_attachmentUrl(layerId, attachmentId), {
    headers: _maybeAuth(accessToken),
    signal,
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return await res.blob()
}

/** POST an attachment (multipart, field `file`). Editor + editing_enabled + attachments_enabled; 413/415/409 on caps.
 *  Content-Type is intentionally left unset so the browser stamps the multipart boundary. */
export async function uploadFeatureAttachment(
  layerId: string,
  featureId: string,
  file: File,
  accessToken: string,
): Promise<FeatureAttachment> {
  const form = new FormData()
  form.append('file', file)
  const res = await fetch(_featureAttachmentsUrl(layerId, featureId), {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}` },
    body: form,
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as FeatureAttachment
}

/** DELETE an attachment. Editor + editing_enabled + attachments_enabled. */
export async function deleteFeatureAttachment(
  layerId: string,
  attachmentId: string,
  accessToken: string,
): Promise<void> {
  const res = await fetch(_attachmentUrl(layerId, attachmentId), {
    method: 'DELETE',
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
}

// --- geocoding / address search (Talus 18c) ------------------------------------

/**
 * GET /geocode/suggest — address autocomplete proxied from a configured external locator.
 * `opts.locator` scopes to one configured locator (the "Search in" picker); `opts.bbox`
 * ('xmin,ymin,xmax,ymax', EPSG:4326) restricts results to the map view. Returns [] when no
 * geocoder is configured or the service is down (fail-soft). Pass an AbortSignal to cancel
 * the in-flight request on the next keystroke.
 */
export async function fetchGeocodeSuggest(
  q: string,
  accessToken: string,
  opts?: { locator?: string; bbox?: string },
  signal?: AbortSignal,
): Promise<{ suggestions: GeoSuggestion[]; unavailable: boolean }> {
  const qs = new URLSearchParams({ q })
  if (opts?.locator) qs.set('locator', opts.locator)
  if (opts?.bbox) qs.set('bbox', opts.bbox)
  const res = await fetch(`${API_BASE_URL}/geocode/suggest?${qs.toString()}`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
    signal,
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  const body = (await res.json()) as { suggestions: GeoSuggestion[]; unavailable?: boolean }
  return { suggestions: body.suggestions, unavailable: body.unavailable === true }
}

/**
 * GET /geocode/search — resolve an address to EPSG:4326 candidates. Pass {source, magicKey}
 * from a picked suggestion for an exact resolve, else just {q} for a free-text search.
 * {locator} scopes to one configured locator; {bbox} restricts to the map view. Returns []
 * on no match / fail-soft.
 */
export async function fetchGeocodeSearch(
  params: { q?: string; source?: string; magicKey?: string; locator?: string; bbox?: string },
  accessToken: string,
  signal?: AbortSignal,
): Promise<GeoCandidate[]> {
  const qs = new URLSearchParams()
  if (params.q) qs.set('q', params.q)
  if (params.source) qs.set('source', params.source)
  if (params.magicKey) qs.set('magic_key', params.magicKey)
  if (params.locator) qs.set('locator', params.locator)
  if (params.bbox) qs.set('bbox', params.bbox)
  const res = await fetch(`${API_BASE_URL}/geocode/search?${qs.toString()}`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
    signal,
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return ((await res.json()) as { candidates: GeoCandidate[] }).candidates
}

/** GET /geocode/reverse — resolve a point (EPSG:4326) to an address for "What's here?" (fail-soft → null).
 *  `locator` scopes to the "Search in" picker's selection so reverse uses the same service as forward search. */
export async function reverseGeocode(
  lon: number,
  lat: number,
  accessToken: string,
  locator?: string | null,
): Promise<string | null> {
  const loc = locator ? `&locator=${encodeURIComponent(locator)}` : ''
  const res = await fetch(`${API_BASE_URL}/geocode/reverse?lon=${lon}&lat=${lat}${loc}`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  const d = (await res.json()) as { available: boolean; address: string | null }
  return d.available ? d.address : null
}

/** GET /geocode/locators — the configured locator names, for the "Search in" picker. */
export async function fetchGeocodeLocators(accessToken: string): Promise<string[]> {
  const res = await fetch(`${API_BASE_URL}/geocode/locators`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return ((await res.json()) as { locators: string[] }).locators
}

// --- batch (bulk) geocoding (decision-batch-geocoding.md) -----------------------
//
// Async: run() returns a jobId + the output layer id; poll the job for progress. The output is a normal
// hosted point layer carrying the match metadata + a geocode_input field. run() is publisher-gated.

export interface BatchPreflight {
  type?: string // 'internal' | 'arcgis' | 'nominatim' — the wizard shows the country step only for external
  batchCapable: boolean
  batchSize: number | null
  creditConsuming: boolean
  needsToken: boolean
  hasToken: boolean
  reason: string | null
  locator: string
  rowCap: number
  estimatedCredits: number | null
  creditRatePer1000: number
  maxBatchSize?: number
}

/** POST /geocode/batch/preflight — the three-layer check for a chosen locator (+ credit estimate). */
export async function fetchBatchPreflight(
  body: { locator: string; rowCount?: number },
  accessToken: string,
): Promise<BatchPreflight> {
  const res = await fetch(`${API_BASE_URL}/geocode/batch/preflight`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as BatchPreflight
}

export interface BatchRunPayload {
  locator: string
  columns: string[]
  csvText?: string
  sourceLayerId?: string
  /** Rows already in the (headers, rows) shape — what `fetchKmlGeocodeRows` returns for an
   *  address-only KML/KMZ (decision-kml-address-geocode.md F1). Same caps and gates as csvText. */
  rows?: Record<string, string>[]
  displayName?: string
  threshold?: number
  chargesAcknowledged?: boolean
  country?: string[] // optional ISO alpha-2 narrow (external locators); omit/[] = global
}

export interface BatchRunResult {
  jobId: string
  outputLayerId: string
  totalRows: number
}

/** POST /geocode/batch/run — start an async run. 402 when the locator consumes credits and
 *  chargesAcknowledged is not set; 422 for a non-batch locator / bad input; 413 over the row cap. */
/** The rows the batch geocoder can take from an address-only KML/KMZ, plus what the file held. */
export interface KmlGeocodeRows {
  headers: string[]
  rows: Record<string, string>[]
  placemarks: number
  withAddress: number
  withGeometry: number
  skipped: number
}

/** POST /geocode/batch/kml/rows — the file streams as the raw body (publisher-gated server-side).
 *  Nothing is stored; the rows come straight back for the run. */
export async function fetchKmlGeocodeRows(file: File, accessToken: string): Promise<KmlGeocodeRows> {
  const res = await fetch(`${API_BASE_URL}/geocode/batch/kml/rows`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, 'Content-Type': 'application/octet-stream' },
    body: file,
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as KmlGeocodeRows
}

export async function runBatchGeocode(
  payload: BatchRunPayload,
  accessToken: string,
): Promise<BatchRunResult> {
  const res = await fetch(`${API_BASE_URL}/geocode/batch/run`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify(payload),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as BatchRunResult
}

export type BatchJobStatus = 'pending' | 'running' | 'completed' | 'failed' | 'canceled'

export interface BatchJob {
  jobId: string
  status: BatchJobStatus
  locator: string
  outputLayerId: string
  threshold: number
  creditConsuming: boolean
  totalRows: number
  processedRows: number
  matchedCount: number
  error: string | null
  createdBy: string
  createdAt: string
  startedAt: string | null
  finishedAt: string | null
}

/** GET /geocode/batch/jobs/{id} — poll progress (owner or admin). */
export async function fetchBatchJob(jobId: string, accessToken: string): Promise<BatchJob> {
  const res = await fetch(`${API_BASE_URL}/geocode/batch/jobs/${encodeURIComponent(jobId)}`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as BatchJob
}

/** POST /geocode/batch/jobs/{id}/cancel — stop a running job (owner or admin). */
export async function cancelBatchJob(jobId: string, accessToken: string): Promise<BatchJobStatus> {
  const res = await fetch(`${API_BASE_URL}/geocode/batch/jobs/${encodeURIComponent(jobId)}/cancel`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return ((await res.json()) as { status: BatchJobStatus }).status
}

/** POST /geocode/batch/{layerId}/rematch — re-run misses (or given rows) as a new async job. */
export async function rematchBatch(
  layerId: string,
  body: { featureIds?: string[]; chargesAcknowledged?: boolean },
  accessToken: string,
): Promise<{ jobId: string; outputLayerId: string; requeued: number }> {
  const res = await fetch(`${API_BASE_URL}/geocode/batch/${encodeURIComponent(layerId)}/rematch`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as { jobId: string; outputLayerId: string; requeued: number }
}

// --- geocoder management (Talus 37 — Admin → Geocoders) -------------------------
//
// The admin-gated CRUD surface that replaces editing the GEOCODERS env var. All of these 403 for
// non-admins server-side; the Admin nav gating is cosmetic. The map's "Search in" picker still reads
// the public /geocode/locators name list (above) — these manage what feeds it.

/** GET /geocoders — the full org locator list (admin only). */
export async function fetchGeocoders(accessToken: string): Promise<Geocoder[]> {
  const res = await fetch(`${API_BASE_URL}/geocoders`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as Geocoder[]
}

/** POST /geocoders — add a locator (admin only). A duplicate name → 409. */
export async function createGeocoder(
  body: GeocoderWrite,
  accessToken: string,
): Promise<Geocoder> {
  const res = await fetch(`${API_BASE_URL}/geocoders`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as Geocoder
}

/** PUT /geocoders/{id} — edit a locator, full replace (admin only). */
export async function updateGeocoder(
  id: string,
  body: GeocoderWrite,
  accessToken: string,
): Promise<Geocoder> {
  const res = await fetch(`${API_BASE_URL}/geocoders/${id}`, {
    method: 'PUT',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as Geocoder
}

/** DELETE /geocoders/{id} — remove a locator (admin only). */
export async function deleteGeocoder(id: string, accessToken: string): Promise<void> {
  const res = await fetch(`${API_BASE_URL}/geocoders/${id}`, {
    method: 'DELETE',
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
}

/** PUT /geocoders/reorder — set the fallback order from an ordered id list (admin only). */
export async function reorderGeocoders(
  ids: string[],
  accessToken: string,
): Promise<Geocoder[]> {
  const res = await fetch(`${API_BASE_URL}/geocoders/reorder`, {
    method: 'PUT',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify({ ids }),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as Geocoder[]
}

/** GET /geocoders/eligible-layers — point layers an internal locator may search (admin only). */
export async function fetchEligibleLocatorLayers(
  accessToken: string,
): Promise<EligibleLocatorLayer[]> {
  const res = await fetch(`${API_BASE_URL}/geocoders/eligible-layers`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as EligibleLocatorLayer[]
}

/** GET /geocoders/eligible-layers/{id}/fields — address-field choices for one layer (admin only). */
export async function fetchMappableFields(
  layerId: string,
  accessToken: string,
): Promise<MappableField[]> {
  const res = await fetch(`${API_BASE_URL}/geocoders/eligible-layers/${layerId}/fields`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as MappableField[]
}

/**
 * POST /geocoders/test — probe a locator before relying on it (admin only). Pass either an existing
 * `id`, an ad-hoc `{ type, url, outSR, extent }`, or an ad-hoc internal def
 * `{ type: 'internal', layerId, addressFields }` so a not-yet-saved locator can be tested.
 * Non-blocking: the result is advisory, never a save gate.
 */
export async function testGeocoder(
  body:
    | { id: string; token?: string; q?: string }
    | { type: GeocoderType; url: string; outSR?: number; extent?: number[] | null; token?: string; q?: string }
    | { type: 'internal'; layerId: string; addressFields: string[]; localityFields?: string[]; q?: string },
  accessToken: string,
): Promise<GeocoderTestResult> {
  const res = await fetch(`${API_BASE_URL}/geocoders/test`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as GeocoderTestResult
}

// --- service accounts (Talus 19 c4) --------------------------------------------
//
// The admin-gated provisioning surface. The portal orchestrates + records; Keycloak issues
// the secret, which the API returns exactly ONCE (on create / rotate) and never stores. All
// of these 403 for non-admins server-side — the Admin nav gating is cosmetic.

export type SaAccessMode = 'read-only' | 'read-write'

/** One scope group from the registry (GET /groups) and in an SA's grants. */
export interface SaGroup {
  group_id: string
  display_name: string
}

/** A service account as listed (GET /service-accounts). `enabled` is null when the identity
 * provider couldn't be reached for that row (the list degrades rather than failing). */
export interface ServiceAccount {
  client_id: string
  name: string
  created_at: string
  access_mode: SaAccessMode
  enabled: boolean | null
  groups: SaGroup[]
}

/** POST /service-accounts response — carries the one-time secret (never retrievable again). */
export interface ServiceAccountCreated {
  client_id: string
  secret: string
  sa_sub: string
  name: string
  access_mode: SaAccessMode
  groups: string[]
}

/** GET /groups — the registry the scope picker reads. */
export async function fetchGroups(accessToken: string): Promise<SaGroup[]> {
  const res = await fetch(`${API_BASE_URL}/groups`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return ((await res.json()) as { groups: SaGroup[] }).groups
}

/** GET /service-accounts — list with grants. */
export async function fetchServiceAccounts(accessToken: string): Promise<ServiceAccount[]> {
  const res = await fetch(`${API_BASE_URL}/service-accounts`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return ((await res.json()) as { service_accounts: ServiceAccount[] }).service_accounts
}

/** POST /service-accounts — create + scope; the response's `secret` is shown ONCE. */
export async function createServiceAccount(
  payload: { name: string; access_mode: SaAccessMode; groups: string[] },
  accessToken: string,
): Promise<ServiceAccountCreated> {
  const res = await fetch(`${API_BASE_URL}/service-accounts`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify(payload),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as ServiceAccountCreated
}

/** POST /service-accounts/{id}/rotate-secret — returns the NEW secret, shown once. */
export async function rotateServiceAccountSecret(
  clientId: string,
  accessToken: string,
): Promise<{ client_id: string; secret: string }> {
  const res = await fetch(
    `${API_BASE_URL}/service-accounts/${encodeURIComponent(clientId)}/rotate-secret`,
    { method: 'POST', headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' } },
  )
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as { client_id: string; secret: string }
}

/** POST /service-accounts/{id}/disable — revoke (no new tokens issue). */
export async function disableServiceAccount(clientId: string, accessToken: string): Promise<void> {
  const res = await fetch(
    `${API_BASE_URL}/service-accounts/${encodeURIComponent(clientId)}/disable`,
    { method: 'POST', headers: { Authorization: `Bearer ${accessToken}` } },
  )
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
}

// --- web maps (Talus 20) -------------------------------------------------------
//
// A web map is a SAVED VIEW over layers: layer refs + order + visibility, the basemap, and the
// viewport. Create is publisher-gated; open is can_view-gated; update/delete is owner-or-admin
// (all enforced server-side). The map REFERENCES layers — it never copies their data.

/** One layer reference in a saved map; array ORDER is draw order (front = top, AGO order). */
/** One attribute-filter condition on an operational layer (decision-feature-filter.md). Mirrors the
 * injection-safe `/features?filter=field:op:value` primitives; `value` is a string for every op
 * (`in` is comma-joined; "is between" is stored as two conditions, gte + lte). Re-validated server-side
 * at render time (field ∈ real columns, op ∈ whitelist, value bound). */
export interface FilterCondition {
  field: string
  op: 'eq' | 'ne' | 'ilike' | 'gte' | 'lte' | 'lt' | 'in'  // 'lt': exclusive upper bound — the dashboards date-range's inclusive-end-day emission (lt next-day)
  value: string
}

/** Per-layer map labels (decision-map-labels.md): ONE label class — text = an attribute field only
 * (the standing no-expression-engine fence; no Arcade, no concatenation), styled by a fixed knob set.
 * Map-scoped presentation persisted with the map; absent = labels off. Optional knobs fall back to
 * the shared defaults (LABEL_DEFAULTS client-side; the server model fills the same values). */
export interface WebMapLabelConfig {
  field: string
  size?: number
  color?: string
  haloColor?: string
  bold?: boolean
  minZoom?: number | null
}

/** Per-layer, per-map popup config (decision-map-popups.md): the STRUCTURAL subset of Esri's popupInfo —
 * on/off + title field + ordered visible-field list. NO templates/expressions/media (the standing
 * no-expression-engine fence). Absent = default popup behavior (built-ins curated rows; slots
 * notes+owner-defined fields; views all). */
export interface WebMapPopupConfig {
  /** false = this layer is skipped by map identify on THIS map. Default true. */
  enabled?: boolean
  /** Field whose VALUE titles the popup page (and relabels Locate results). Absent/null = layer label. */
  titleField?: string | null
  /** Ordered VISIBLE list — array order IS display order; anything not listed is hidden.
   *  Absent/null = default rows; [] = title-only popup. Object-per-field mirrors Esri fieldInfos. */
  fields?: { name: string }[] | null
}

export interface WebMapOperationalLayer {
  // An op is a vector layer (kind 'layer'/absent, layerId), a raster (kind 'raster', rasterId,
  // opacity), or an EXTERNAL reference (kind 'external' — decision-add-from-url.md). kind defaults
  // to 'layer' so pre-raster saved maps stay valid.
  kind?: 'layer' | 'raster' | 'external'
  layerId?: string
  rasterId?: string
  visible: boolean
  opacity?: number
  /** external-only: a remote public service the viewer's browser renders directly (no ingest, no
   *  catalog item, no proxy). url is the normalized service/layer/template URL; `where` is an
   *  arcgis-feature filter carried from a pasted /query URL; geometryType/color style vector kinds. */
  externalId?: string
  externalKind?: 'arcgis-feature' | 'geojson' | 'xyz' | 'arcgis-image'
  url?: string
  geometryType?: 'point' | 'line' | 'polygon'
  color?: string
  where?: string
  /** external-only: link to a shared catalog reference item (decision-external-catalog-items.md).
   *  When set, the spec fields above are the SNAPSHOT; map open resolves the item and its
   *  connection spec (url/where/geometryType) wins while readable. Absent on map-scoped externals. */
  externalItemId?: string
  /** raster-only: named colormap for a single-band raster (absent = default render). */
  colormap?: string
  /** Per-layer attribute filter (decision-feature-filter.md): AND-composed simple predicates that
   *  narrow the layer to matching features. Absent/empty = unfiltered (backward-compatible). */
  filters?: FilterCondition[]
  /** Per-layer labels (decision-map-labels.md): absent = off (backward-compatible). Vector + view
   *  layers only — rasters and non-spatial tables are never labelable. */
  labels?: WebMapLabelConfig
  /** Per-layer popup config (decision-map-popups.md): absent = default popups (backward-compatible).
   *  Same vector+view scoping as labels. */
  popup?: WebMapPopupConfig
  /** Map-only display-name override (decision-map-rename.md): renames the layer ON THIS MAP only —
   *  never the layer item. Absent = the layer's own name. */
  title?: string
  /** This map's OWN visible range for the layer (decision-layer-zoom-range.md §3.1): when either end
   *  is set the pair replaces the layer's default as a whole; both absent = use the layer's default.
   *  Any kind. Written only when set, so pre-range maps stay byte-identical; the server echoes an
   *  unset end as null. */
  visibleMinZoom?: number | null
  visibleMaxZoom?: number | null
}

/** The saved viewport — center [lon, lat] + zoom (what MapLibre exposes natively). */
export interface WebMapInitialView {
  center: [number, number]
  zoom: number
}

/** A saved named viewpoint (map bookmark) — a named InitialView. Extent-only: it never snapshots
 *  layer visibility/symbology. Persisted inside the definition; managed by whoever can edit the map. */
export interface WebMapBookmark {
  name: string
  center: [number, number]
  zoom: number
}

/** One map note (decision-sketch-layer.md): an ad-hoc drawn point/line/polygon with a per-feature
 *  color and an optional static text label. Map-scoped only — never a catalog item, never served.
 *  Coordinates are GeoJSON for the type (point [lon,lat]; line positions; polygon rings). */
export interface SketchFeature {
  id: string // skt_<8hex> — the select/edit key (string id, the identify lesson)
  geometryType: 'point' | 'line' | 'polygon'
  coordinates: number[] | number[][] | number[][][]
  color: string
  label?: string | null
}

/** The versioned web-map document stored in `definition`. */
export interface WebMapDefinition {
  version: number
  basemap: string
  /** Talus Field (decision-talus-field-f2.md §4.4): the "Field-ready" switch — crews see this map in the
   *  field app only when `field.enabled` is true. Optional so every pre-f2 map still parses (absent = not
   *  a field map: opt-in, fork 3). A map property, authored where the map is authored. */
  field?: { enabled: boolean }
  operationalLayers: WebMapOperationalLayer[]
  initialView: WebMapInitialView
  /** Saved named viewpoints. Optional so pre-bookmarks maps still parse (default to [] on read). */
  bookmarks?: WebMapBookmark[]
  /** Map notes (decision-sketch-layer.md). Optional so pre-sketch maps still parse ([] on read). */
  sketch?: SketchFeature[]
  /** Tables shown on this map (decision-map-tables.md): refs to geometry-less table layers, listed in the
   *  Tables section + opened in the docked grid — nothing drawn. Optional so pre-tables maps still parse. */
  tables?: { layerId: string }[]
}

/** List row (GET /me/web-maps) — no definition. */
export interface WebMapSummary {
  id: string
  title: string
  description: string | null
  created_at: string
  updated_at: string
  /** Set for owned maps; absent for shared-with-me maps (and on OpenFGA hiccups). */
  shareLevel?: ShareLevel | null
  /** Has the caller starred this map? (Talus 24 — favorites parity with layers.) */
  favorite?: boolean
  /** Is this shared-bucket map a GENUINE share (not owned)? true for a regular caller; for an ADMIN the shared
   *  bucket also holds admin-override-only maps with sharedWithMe:false (→ "All" only). Drives "Shared with me". */
  sharedWithMe?: boolean
  /** Talus 24 c4: delete-protection flag — drives the at-a-glance padlock on the row's delete action. */
  deleteProtected?: boolean
  /** Talus 26: the map owner's id + display name (mutable; transfer changes them). Names the
   *  "ask the owner" permission notice and the catalog owner column. */
  ownerId?: string | null
  ownerName?: string | null
}

/** Full web map (GET/POST/PATCH) — includes the definition. `canManage` (owner or admin)
 * gates the Save/Delete/Share affordances so a non-owner opens a shared map read-only. */
export interface WebMapDetail extends WebMapSummary {
  definition: WebMapDefinition
  canManage: boolean
  // ANON ONLY (decision-map-embed.md F0): server-computed MyLayer-shaped metadata for the PUBLIC
  // subset of this map's own vector layers — the anon restore path's substitute for /me/layers.
  // Absent (null) for authenticated callers.
  layersMeta?: MyLayer[] | null
}

export interface WebMapSavePayload {
  title: string
  description?: string | null
  definition: WebMapDefinition
}

/** GET /me/web-maps — the caller's maps in two buckets: owned by them, and shared with them
 * (Talus 21). The Catalog's Owned | Shared scope reads these directly. */
export interface MyWebMaps {
  owned: WebMapSummary[]
  shared: WebMapSummary[]
}

export async function fetchMyWebMaps(accessToken: string): Promise<MyWebMaps> {
  const res = await fetch(`${API_BASE_URL}/me/web-maps`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as MyWebMaps
}

/** GET /web-maps/{id} — open a saved map (can_view gated). */
/** GET /layers/{id}/form — the layer's form document (view-gated). Null = the default form. */
export async function fetchLayerForm(layerId: string, accessToken: string): Promise<FormConfig | null> {
  const res = await fetch(`${API_BASE_URL}/layers/${encodeURIComponent(layerId)}/form`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  const body = (await res.json()) as { formConfig: FormConfig | null }
  return body.formConfig ?? null
}

/** PUT /layers/{id}/form — replace the document (owner or admin). A 400 carries every problem found. */
export async function putLayerForm(layerId: string, config: FormConfig, accessToken: string): Promise<FormConfig> {
  const res = await fetch(`${API_BASE_URL}/layers/${encodeURIComponent(layerId)}/form`, {
    method: 'PUT',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify(config),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  const body = (await res.json()) as { formConfig: FormConfig }
  return body.formConfig
}

/** DELETE /layers/{id}/form — back to the default form (owner or admin). */
export async function clearLayerForm(layerId: string, accessToken: string): Promise<void> {
  const res = await fetch(`${API_BASE_URL}/layers/${encodeURIComponent(layerId)}/form`, {
    method: 'DELETE',
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
}

export async function fetchWebMap(id: string, accessToken?: string): Promise<WebMapDetail> {
  // Tokenless = the anon public-map path (decision-map-embed.md F0): the server gates on the
  // map's public state and answers 404 otherwise — never send an empty Authorization header.
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`
  const res = await fetch(`${API_BASE_URL}/web-maps/${encodeURIComponent(id)}`, { headers })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as WebMapDetail
}

/** POST /web-maps — save a new map (publisher only). */
export async function createWebMap(
  payload: WebMapSavePayload,
  accessToken: string,
): Promise<WebMapDetail> {
  const res = await fetch(`${API_BASE_URL}/web-maps`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify(payload),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as WebMapDetail
}

/** PATCH /web-maps/{id} — update a map in place (owner or admin). */
export async function updateWebMap(
  id: string,
  payload: WebMapSavePayload,
  accessToken: string,
): Promise<WebMapDetail> {
  const res = await fetch(`${API_BASE_URL}/web-maps/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify(payload),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as WebMapDetail
}

/** DELETE /web-maps/{id} — delete a map (owner or admin). */
export async function deleteWebMap(id: string, accessToken: string): Promise<void> {
  const res = await fetch(`${API_BASE_URL}/web-maps/${encodeURIComponent(id)}`, {
    method: 'DELETE',
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
}

// --- rasters (store & serve, V045+) --------------------------------------------
//
// A raster is an uploaded Cloud-Optimized GeoTIFF served as dynamic tiles. Catalog summary shape
// mirrors WebMapSummary so it reuses the shared catalog filter/sort + CatalogRow.

/** One raster in the catalog list. snake_case timestamps match WebMapSummary (shared filter/sort). */
export interface RasterSummary {
  id: string
  title: string
  description: string | null
  created_at: string
  updated_at: string
  srid: number | null
  bandCount: number | null
  hasThumbnail: boolean
  shareLevel?: ShareLevel | null
  favorite?: boolean
  ownerId?: string | null
  ownerName?: string | null
  /** V050: governance flags. false downloadable = view-only (no /export). */
  downloadable?: boolean
  deleteProtected?: boolean
}

export interface MyRasters {
  owned: RasterSummary[]
  shared: RasterSummary[]
}

/** GET /me/rasters — the caller's rasters (owned + shared-with-them). */
export async function fetchMyRasters(accessToken: string): Promise<MyRasters> {
  const res = await fetch(`${API_BASE_URL}/me/rasters`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as MyRasters
}

/** POST /rasters — upload a COG. The bytes ARE the request body (the store is never browser-exposed, so
 * there is no presigned direct upload); title/description ride as query params. Publisher/admin only. */
export async function uploadRaster(
  file: File,
  title: string,
  description: string,
  accessToken: string,
): Promise<{ id: string; title: string; status: string }> {
  const qs = new URLSearchParams({ title })
  if (description) qs.set('description', description)
  const res = await fetch(`${API_BASE_URL}/rasters?${qs.toString()}`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, 'Content-Type': file.type || 'image/tiff' },
    body: file,
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as { id: string; title: string; status: string }
}

// --- 9d bulk-import jobs (decision-9d-bulk-import.md) -------------------------------------------

export interface ImportJob {
  id: string
  status: 'pending' | 'running' | 'completed' | 'failed' | 'canceled'
  format: string
  original_name: string
  staged_bytes: number
  target_layer_id: string
  created_new: boolean
  source_srid: number | null
  total_rows: number | null
  processed_rows: number
  inserted_rows: number
  failed_rows: number
  error_sample: { row: number; reason: string }[]
  error: string | null
}

/** POST /import-jobs — stream the file as the raw body (never base64, never browser-parsed).
 * Exactly one of `name` (new layer) or `layerId` (append) per the door's contract. */
export async function createImportJob(
  file: File,
  opts: { format: 'shp-zip' | 'gpkg' | 'geojson' | 'csv' | 'kml'; name?: string; layerId?: string; sourceSrid?: number },
  accessToken: string,
): Promise<ImportJob> {
  const qs = new URLSearchParams({ format: opts.format, filename: file.name })
  if (opts.name !== undefined) qs.set('name', opts.name)
  if (opts.layerId !== undefined) qs.set('layer_id', opts.layerId)
  if (opts.sourceSrid !== undefined) qs.set('source_srid', String(opts.sourceSrid))
  const res = await fetch(`${API_BASE_URL}/import-jobs?${qs.toString()}`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, 'Content-Type': 'application/octet-stream' },
    body: file,
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as ImportJob
}

/** GET /import-jobs/{id} — owner-scoped job status for progress polling. */
export async function fetchImportJob(id: string, accessToken: string): Promise<ImportJob> {
  const res = await fetch(`${API_BASE_URL}/import-jobs/${id}`, {
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as ImportJob
}

/** POST /import-jobs/{id}/cancel — pending dies now; running dies at the next batch boundary. */
export async function cancelImportJob(id: string, accessToken: string): Promise<ImportJob> {
  const res = await fetch(`${API_BASE_URL}/import-jobs/${id}/cancel`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as ImportJob
}

/** DELETE /rasters/{id} — owner/admin; the server purges the stored objects. */
export async function deleteRaster(id: string, accessToken: string): Promise<void> {
  const res = await fetch(`${API_BASE_URL}/rasters/${encodeURIComponent(id)}`, {
    method: 'DELETE',
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
}

/** Raster bounds + zoom range (EPSG:4326), for the map's "zoom to" + extent fit. */
export interface RasterInfo {
  bounds: [number, number, number, number] // [west, south, east, north]
  center: [number, number, number]
  minzoom: number
  maxzoom: number
  bandCount: number
  defaultRender: Record<string, unknown>
}

/** GET /rasters/{id}/info — bounds + zoom range (can_view gated). */
export async function fetchRasterInfo(id: string, accessToken: string): Promise<RasterInfo> {
  const res = await fetch(`${API_BASE_URL}/rasters/${encodeURIComponent(id)}/info`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as RasterInfo
}

/** GET /rasters/{id}/point — sampled band value(s) at a lon/lat, for the multi-layer identify popup. */
export interface RasterPointResult {
  values: (number | null)[] // one per band; null = masked/nodata
  bandCount: number
  outOfBounds: boolean // the click fell outside the raster's footprint
}

/** Sample a raster's band value(s) at a WGS-84 lon/lat (can_view gated). */
export async function identifyRasterPoint(
  id: string,
  lon: number,
  lat: number,
  accessToken: string,
  signal?: AbortSignal,
): Promise<RasterPointResult> {
  const res = await fetch(
    `${API_BASE_URL}/rasters/${encodeURIComponent(id)}/point?lon=${lon}&lat=${lat}`,
    { headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' }, signal },
  )
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as RasterPointResult
}

/** GET /rasters/{id}/thumbnail.png — catalog thumbnail bytes (can_view gated). The endpoint needs a
 * bearer token, so callers fetch the Blob here and render it via an object URL (a plain <img src>
 * can't carry the auth header). */
export async function fetchRasterThumbnail(id: string, accessToken: string): Promise<Blob> {
  const res = await fetch(`${API_BASE_URL}/rasters/${encodeURIComponent(id)}/thumbnail.png`, {
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return res.blob()
}

/** A raster's current share state (org + groups + the anonymous Public Access tier —
 *  raster-public slice). */
export interface RasterShares {
  organizationOnly: boolean
  groups: ShareGroup[]
  publicAccess: boolean
}

/** GET /rasters/{id}/shares — current org/group share state (owner or admin). */
export async function fetchRasterShares(id: string, accessToken: string): Promise<RasterShares> {
  const res = await fetch(`${API_BASE_URL}/rasters/${encodeURIComponent(id)}/shares`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as RasterShares
}

/** PUT /rasters/{id}/shares — set/replace org + group + public sharing (owner or admin). */
export async function setRasterShares(
  id: string,
  payload: { organizationOnly: boolean; groups: string[]; publicAccess: boolean },
  accessToken: string,
): Promise<RasterShares> {
  const res = await fetch(`${API_BASE_URL}/rasters/${encodeURIComponent(id)}/shares`, {
    method: 'PUT',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify(payload),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as RasterShares
}

/** PUT /rasters/{id}/public-share — set/clear a raster's Public Access (owner or admin) — the
 *  single-tier path the web-map make-public cascade drives per referenced raster. */
export async function setRasterPublicShare(
  rasterId: string,
  publicAccess: boolean,
  accessToken: string,
): Promise<{ publicAccess: boolean; canManage: boolean }> {
  const res = await fetch(`${API_BASE_URL}/rasters/${encodeURIComponent(rasterId)}/public-share`, {
    method: 'PUT',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify({ publicAccess }),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as { publicAccess: boolean; canManage: boolean }
}

// --- raster export / governance (V050+, decision-raster-export.md) --------------

/** Raster-specific download governance (delete-protection is the shared cross-item subsystem). */
export interface RasterSettings {
  downloadable: boolean
}

/** GET /rasters/{id}/settings — current download flag (owner or admin). */
export async function fetchRasterSettings(id: string, accessToken: string): Promise<RasterSettings> {
  const res = await fetch(`${API_BASE_URL}/rasters/${encodeURIComponent(id)}/settings`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as RasterSettings
}

/** PUT /rasters/{id}/settings — set the downloadable flag (owner or admin). */
export async function setRasterSettings(
  id: string,
  payload: RasterSettings,
  accessToken: string,
): Promise<RasterSettings> {
  const res = await fetch(`${API_BASE_URL}/rasters/${encodeURIComponent(id)}/settings`, {
    method: 'PUT',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify(payload),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as RasterSettings
}

/** One export format the running GDAL can write (probed server-side). */
export interface RasterExportFormat {
  key: string
  label: string
  ext: string
}

/** GET /rasters/export/formats — the writable export formats (drives the format dropdown). */
export async function fetchRasterExportFormats(accessToken: string): Promise<RasterExportFormat[]> {
  const res = await fetch(`${API_BASE_URL}/rasters/export/formats`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return ((await res.json()) as { formats: RasterExportFormat[] }).formats
}

/**
 * GET /rasters/{id}/export — download a clip (bbox in EPSG:4326) or the whole raster, in `format`.
 * Streams the file as a Blob; the caller saves it. Throws ApiError on 4xx (e.g. 413 oversize, 403
 * view-only). On 413 the detail explains the cap + the maxDim downsample opt-in.
 */
export async function exportRaster(
  id: string,
  opts: { format: string; bbox?: [number, number, number, number]; maxDim?: number },
  accessToken?: string,
): Promise<{ blob: Blob; filename: string }> {
  const qs = new URLSearchParams({ format: opts.format })
  if (opts.bbox) qs.set('bbox', opts.bbox.join(','))
  if (opts.maxDim) qs.set('maxDim', String(opts.maxDim))
  // Token optional (raster-public slice): anon downloads work for public+downloadable rasters.
  const headers: Record<string, string> = {}
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`
  const res = await fetch(`${API_BASE_URL}/rasters/${encodeURIComponent(id)}/export?${qs.toString()}`, {
    headers,
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  // Pull the server's Content-Disposition filename if present (friendly, format-correct extension).
  const cd = res.headers.get('Content-Disposition') || ''
  const m = /filename="?([^"]+)"?/.exec(cd)
  return { blob: await res.blob(), filename: m ? m[1] : `raster_${id}.${opts.format}` }
}

// --- web-map sharing (Talus 21) ------------------------------------------------
//
// Share a saved map to one or more groups and/or the whole organization. "Public Access"
// (anonymous) is a later slice and is not represented here. All share calls are owner-or-admin
// server-side; the group picker is scoped to the caller's own groups (also enforced server-side).

/** A group offered in the share picker / present in a map's shares. */
export interface ShareGroup {
  group_id: string
  display_name: string
  /** V026 system group (home / open_data) — some item kinds hide these (Talus+ reports). */
  system?: boolean
}

/** A web map's share state. `organizationOnly` = shared to the whole org; `groups` = group shares;
 *  `publicAccess` = the anonymous Public Access tier (Talus 23). */
export interface WebMapShares {
  organizationOnly: boolean
  groups: ShareGroup[]
  publicAccess: boolean
}

/** GET /me/shareable-groups — the groups the caller may share to (member-scoped; admin sees all). */
export async function fetchShareableGroups(accessToken: string): Promise<ShareGroup[]> {
  const res = await fetch(`${API_BASE_URL}/me/shareable-groups`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return ((await res.json()) as { groups: ShareGroup[] }).groups
}

/** GET /web-maps/{id}/shares — current share state (owner or admin). */
export async function fetchWebMapShares(id: string, accessToken: string): Promise<WebMapShares> {
  const res = await fetch(`${API_BASE_URL}/web-maps/${encodeURIComponent(id)}/shares`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as WebMapShares
}

/** PUT /web-maps/{id}/shares — set/replace share state (owner or admin). Empty clears all sharing. */
export async function setWebMapShares(
  id: string,
  payload: { organizationOnly: boolean; groups: string[]; publicAccess: boolean },
  accessToken: string,
): Promise<WebMapShares> {
  const res = await fetch(`${API_BASE_URL}/web-maps/${encodeURIComponent(id)}/shares`, {
    method: 'PUT',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify(payload),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as WebMapShares
}

/** One referenced layer in the make-public cascade: current public state + whether the caller
 *  may change it (Talus 23 c3b). */
export interface PublicLayerReviewItem {
  layerId: string
  title: string
  isPublic: boolean
  canManage: boolean
}

/** One referenced RASTER in the make-public cascade (raster-public slice) — same shape, raster id. */
export interface PublicRasterReviewItem {
  rasterId: string
  title: string
  isPublic: boolean
  canManage: boolean
}

/** GET /web-maps/{id}/public-layer-review — per-item public status for the cascade (owner or
 *  admin): the map's vector layers AND its rasters (raster-public slice). */
export async function fetchWebMapPublicLayerReview(
  id: string,
  accessToken: string,
): Promise<{ layers: PublicLayerReviewItem[]; rasters: PublicRasterReviewItem[] }> {
  const res = await fetch(
    `${API_BASE_URL}/web-maps/${encodeURIComponent(id)}/public-layer-review`,
    { headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' } },
  )
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  const body = (await res.json()) as {
    layers: PublicLayerReviewItem[]
    rasters?: PublicRasterReviewItem[]
  }
  return { layers: body.layers, rasters: body.rasters ?? [] }
}

/** PUT /layers/{id}/public-share — set/clear a layer's Public Access (owner or admin). */
export async function setLayerPublicShare(
  layerId: string,
  publicAccess: boolean,
  accessToken: string,
): Promise<{ publicAccess: boolean; canManage: boolean }> {
  const res = await fetch(
    `${API_BASE_URL}/layers/${encodeURIComponent(layerId)}/public-share`,
    {
      method: 'PUT',
      headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
      body: JSON.stringify({ publicAccess }),
    },
  )
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as { publicAccess: boolean; canManage: boolean }
}

/** A layer's full share state — org/group/public (Talus 25). Mirrors WebMapShares so the Sharing
 *  section is identical across catalog item types. */
export interface LayerShares {
  organizationOnly: boolean
  groups: ShareGroup[]
  publicAccess: boolean
}

/** GET /layers/{id}/shares — current share state (owner or admin). */
export async function fetchLayerShares(id: string, accessToken: string): Promise<LayerShares> {
  const res = await fetch(`${API_BASE_URL}/layers/${encodeURIComponent(id)}/shares`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as LayerShares
}

/** PUT /layers/{id}/shares — set/replace share state (owner or admin). Empty clears all sharing. */
export async function setLayerShares(
  id: string,
  payload: { organizationOnly: boolean; groups: string[]; publicAccess: boolean },
  accessToken: string,
): Promise<LayerShares> {
  const res = await fetch(`${API_BASE_URL}/layers/${encodeURIComponent(id)}/shares`, {
    method: 'PUT',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify(payload),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as LayerShares
}

// --- org settings (Talus 22) ----------------------------------------------------
//
// Org-wide governance settings (Admin → Settings). Tenants: the information / maintenance banner
// and the `nonadminCanSharePublic` governance policy (Talus 23). GET is open to any authenticated
// caller (the shell reads the banner; a non-admin owner's share dialog reads the policy to decide
// whether to offer the Public tier); PUT is admin-only server-side.

export type BannerLevel = 'info' | 'warning' | 'critical'

/** The client-facing org settings (the curated GET /org-settings projection): banner fields plus
 *  the `nonadminCanSharePublic` capability hint. */
export interface OrgSettings {
  bannerEnabled: boolean
  bannerMessage: string | null
  bannerLevel: BannerLevel
  /** May a non-admin owner share content to Public Access? Server is the authority; this drives
   *  the share dialog's Public tier visibility. Default restrictive (false). */
  nonadminCanSharePublic: boolean
  /** Does this deployment offer public/anonymous access at all (the F6 master switch)? When false,
   *  the share dialog hides the Public tier entirely. Since V092 this is the RESOLVED switch —
   *  the org setting an admin can reach in Admin → Settings, falling back to the deployment's
   *  PUBLIC_ANON_ACCESS_ENABLED when no admin has decided it. Same meaning as before; the source
   *  moved from an env var nobody in the product could reach to a setting they can. */
  publicAnonAccessEnabled: boolean
  /** Talus 26: may a non-admin owner transfer their own items to a colleague without an admin?
   *  Server is the authority; drives the Manage dialog's Transfer control. Default ON. */
  nonadminCanTransferOwnership: boolean
  /** Talus 30: the admin-configurable home-page text (also served to anon via /public/config). */
  home: OrgHome
  /** Interop I-1 (V063): the externally-reachable deployment root shown in connection recipes
   *  (the "Use in…" wizard). Null = unset; consumers fall back to the build-time API base. */
  advertisedBaseUrl: string | null
}

/** PUT body. Banner fields are replaced; the governance policies are OPTIONAL — omit to leave them
 *  unchanged (so a banner-only save never clobbers them). */
export interface OrgSettingsUpdate {
  bannerEnabled: boolean
  bannerMessage: string | null
  bannerLevel: BannerLevel
  nonadminCanSharePublic?: boolean
  nonadminCanTransferOwnership?: boolean
}

/** GET /org-settings — banner state for the shell + the sharing-policy hint. Any authenticated caller. */
export async function fetchOrgSettings(accessToken: string): Promise<OrgSettings> {
  const res = await fetch(`${API_BASE_URL}/org-settings`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as OrgSettings
}

/** PUT /org-settings — update the banner and/or the sharing policy (admin-only server-side). */
export async function updateOrgSettings(
  payload: OrgSettingsUpdate,
  accessToken: string,
): Promise<OrgSettings> {
  const res = await fetch(`${API_BASE_URL}/org-settings`, {
    method: 'PUT',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify(payload),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as OrgSettings
}

// --- the public-sharing master switch (decision-public-sharing-reachable.md) --------------------
//
// Public map sharing, the public map embed and the Open Data / DCAT-US feed all hang off one
// boolean that used to be reachable ONLY by editing a file on the server and restarting a
// container. Its own endpoint (not part of the batched settings save) so enabling internet-facing
// publication is a deliberate, separately-confirmed act with its own audit record.

/** GET/PUT /org-settings/public-sharing response. */
export interface PublicSharingState {
  enabled: boolean
  /** 'override' = an admin decided it in the portal; 'default' = still following the deployment. */
  source: 'override' | 'default'
  /** How many items are shared publicly right now — i.e. how many STOP being served the moment
   *  this is turned off. Null when the count is unavailable (authorization service unreachable):
   *  warn without a number rather than under-report public exposure. */
  publicItemCount: number | null
}

/** GET /org-settings/public-sharing — the master switch + the live public-item count (admin only). */
export async function fetchPublicSharing(accessToken: string): Promise<PublicSharingState> {
  const res = await fetch(`${API_BASE_URL}/org-settings/public-sharing`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as PublicSharingState
}

/** PUT /org-settings/public-sharing — turn public sharing on or off (admin only). Takes effect
 *  immediately, with no restart: turning it OFF stops serving already-public items at once. */
export async function updatePublicSharing(
  enabled: boolean,
  accessToken: string,
): Promise<PublicSharingState> {
  const res = await fetch(`${API_BASE_URL}/org-settings/public-sharing`, {
    method: 'PUT',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify({ enabled }),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as PublicSharingState
}

/** PUT /org-settings/home — update the home-page text (admin-only server-side). All three fields are
 *  sent together; a blank/null clears that field (reverts to the built-in default). */
export async function updateOrgHome(payload: OrgHome, accessToken: string): Promise<OrgHome> {
  const res = await fetch(`${API_BASE_URL}/org-settings/home`, {
    method: 'PUT',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify(payload),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as OrgHome
}

/** PUT /org-settings/advertised-base-url — set/clear the externally-reachable deployment root
 *  (admin-only server-side). Direct write: null/blank clears back to unset. */
export async function updateAdvertisedBaseUrl(
  advertisedBaseUrl: string | null,
  accessToken: string,
): Promise<OrgSettings> {
  const res = await fetch(`${API_BASE_URL}/org-settings/advertised-base-url`, {
    method: 'PUT',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify({ advertisedBaseUrl }),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as OrgSettings
}

// --- basemap provisioning (decision-basemap-provisioning.md) ----------------------

/** The archive's serving URL — the SPA joins its own API base (the server never guesses its
 *  public hostname; the multi-homed-rig lesson). Consumed as `pmtiles://<this>` by MapLibre.
 *  A function, not a module-scope const: API_BASE_URL is a live binding that runtime config
 *  rewrites at boot (9a), so a top-level template literal would freeze the pre-config value. */
export const basemapArchiveUrl = (): string => `${API_BASE_URL}/basemap/archive`

/** A STREAMED basemap (V115 / decision-streamed-basemap.md §14): a public map each viewer's browser
 *  fetches straight from its publisher — OpenStreetMap's standard tiles, USGS Topo, USGS imagery.
 *  A NAME, never an address: the tile addresses are constants in `basemapRuntime.ts`, and nothing
 *  the api returns or a saved map stores can become one. */
export type StreamedBasemap = 'osm' | 'usgstopo' | 'imagery'

/** A streamed LOOK (V116 / decision-streamed-basemap.md §16): what a map names. Each belongs to one
 *  publisher above. 'osm-gray' and 'osm-dark' are the standard OpenStreetMap tiles drawn gray or
 *  dark by the viewer's browser — the same publisher and the same tile address as 'osm', so they
 *  are offered exactly when 'osm' is. A publisher's own name is also its standard look. */
export type StreamedLook = StreamedBasemap | 'osm-gray' | 'osm-dark'

/** The basemaps the switcher can offer, and the closed set the org default may name (V093 /
 *  decision-default-basemap.md fork A1; widened by V115). Declared HERE, in the lib, so the DB
 *  CHECK, the API Literal, and the one TS union stay a single set — `BasemapSwitcher`'s `Basemap`
 *  aliases this rather than restating it. Light / Dark / Gray draw from the organization's
 *  downloaded area; the streamed three are offered only when the organization says so. There is
 *  deliberately no "match the user's theme" value: an org that wants a dark basemap picks 'dark'. */
export type DefaultBasemap = 'light' | 'dark' | 'grayscale' | StreamedLook

/** V094 / decision-org-default-extent.md fork C — units for the EMBED scale bar (its only
 *  consumer; the main map shows a unitless ratio). Two values on purpose: 'nautical' is fenced. */
export type MapUnits = 'metric' | 'imperial'

/** GET /basemap — ANON runtime discovery (F2): is a jurisdiction basemap provisioned? */
export interface BasemapStatus {
  installed: boolean
  sourceDate: string | null
  fetchedAt: string | null
  /** V093 — the basemap a NEW map opens on, org-wide. Anonymous by design: a public map and
   *  /embed/map/:id both construct a basemap without a token. */
  defaultBasemap: DefaultBasemap
  /** V094 — WGS84 [w, s, e, n] a NEW map opens on; null = unset (the coverage fit stands). Rides
   *  this anon payload for ORDERING (map construction gates on this fetch) — it only ever fires on
   *  the authenticated /map route, since every saved map owns its view (decision doc §2.3). */
  defaultExtent: [number, number, number, number] | null
  /** V094 — units for the embed scale bar, which renders logged-out (the genuinely-anon consumer). */
  defaultUnits: MapUnits
  /** V115 — the streamed maps this portal OFFERS. Anonymous by design: a signed-out viewer of a
   *  public map draws a basemap too, and must not be handed one the organization has withdrawn.
   *  Optional only so a bundle talking to an api that predates V115 still type-checks its own
   *  fall-back; read it through `offeredStreamed()`, never directly. */
  streamed?: StreamedBasemap[]
  /** V116 — whether this portal LISTS its downloaded looks (Light, Dark, Gray). With `streamed` it
   *  gives three states: not listed + something streamed = Streamed; listed + nothing streamed =
   *  Downloaded (no external map request); both = what every install had before V116. Optional so
   *  a bundle talking to an older api type-checks; read it through `downloadedListed()`. */
  downloadedListed?: boolean
}

export interface BasemapJob {
  state: 'idle' | 'running' | 'done' | 'failed'
  kind: 'fetch' | 'upload' | null
  message: string | null
  startedAt: number | null
  finishedAt: number | null
}

/** GET /admin/basemap — the full metadata + provisioning-job progress. */
export interface BasemapAdminStatus extends BasemapStatus {
  bbox: [number, number, number, number] | null
  maxzoom: number | null
  sizeBytes: number | null
  origin: 'fetched' | 'uploaded' | null
  job: BasemapJob
}

export async function fetchBasemapStatus(): Promise<BasemapStatus> {
  const res = await fetch(`${API_BASE_URL}/basemap`, { headers: { Accept: 'application/json' } })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as BasemapStatus
}

export async function fetchBasemapAdminStatus(accessToken: string): Promise<BasemapAdminStatus> {
  const res = await fetch(`${API_BASE_URL}/admin/basemap`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as BasemapAdminStatus
}

/** PUT /admin/basemap/default — set the org-wide default basemap (V093). Seeds NEW maps only:
 *  a saved map restores its own basemap, and an unprovisioned box still cold-starts on imagery. */
export async function setDefaultBasemap(
  value: DefaultBasemap,
  accessToken: string,
): Promise<{ defaultBasemap: DefaultBasemap }> {
  const res = await fetch(`${API_BASE_URL}/admin/basemap/default`, {
    method: 'PUT',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify({ value }),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as { defaultBasemap: DefaultBasemap }
}

/** What PUT /admin/basemap/streamed answers with: everything the write decided. */
export interface BasemapListing {
  streamed: StreamedBasemap[]
  downloadedListed: boolean
  defaultBasemap: DefaultBasemap
}

/** PUT /admin/basemap/streamed — set what this portal LISTS: which streamed publishers it offers
 *  (V115) and whether it lists its downloaded looks (V116), in ONE write. Admin-only and audited
 *  by the api. NAMES from the closed list, never an address. Offering nothing while listing the
 *  downloaded looks is the setting for no external map-background request; listing nothing at all
 *  is refused (422). The answer carries the default too, because when the map new maps open on
 *  stops being listed the api moves that default in the same write — the caller must show that it
 *  happened, not just adopt it. */
export async function setStreamedBasemaps(
  value: StreamedBasemap[],
  downloaded: boolean,
  accessToken: string,
): Promise<BasemapListing> {
  const res = await fetch(`${API_BASE_URL}/admin/basemap/streamed`, {
    method: 'PUT',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify({ value, downloaded }),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as BasemapListing
}

/** PUT /org-settings/default-extent — set (or, with null, clear) the org default map extent
 *  (V094). Admin-only; the read side rides GET /basemap. */
export async function setDefaultExtent(
  extent: [number, number, number, number] | null,
  accessToken: string,
): Promise<{ defaultExtent: [number, number, number, number] | null }> {
  const res = await fetch(`${API_BASE_URL}/org-settings/default-extent`, {
    method: 'PUT',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify({ extent }),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as { defaultExtent: [number, number, number, number] | null }
}

/** PUT /org-settings/default-units — set the org units default (V094). Admin-only. */
export async function setDefaultUnits(
  units: MapUnits,
  accessToken: string,
): Promise<{ defaultUnits: MapUnits }> {
  const res = await fetch(`${API_BASE_URL}/org-settings/default-units`, {
    method: 'PUT',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify({ units }),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as { defaultUnits: MapUnits }
}

/** POST /admin/basemap/fetch — start a server-side extract (202; poll fetchBasemapAdminStatus). */
export async function startBasemapFetch(
  payload: { bbox: [number, number, number, number]; maxzoom?: number; date?: string },
  accessToken: string,
): Promise<{ started: boolean; sourceDate: string }> {
  const res = await fetch(`${API_BASE_URL}/admin/basemap/fetch`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify(payload),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as { started: boolean; sourceDate: string }
}

/** POST /admin/basemap/upload — the air-gapped walk-in: the .pmtiles bytes as the RAW request
 *  body (python-multipart-free server, the rasters-ingest pattern). The answer is the installed
 *  archive's metadata WITHOUT the job block (only GET /admin/basemap adds it), so it is not a full
 *  admin status: re-read that after an upload rather than storing this. */
export async function uploadBasemapArchive(
  file: File,
  accessToken: string,
): Promise<Omit<BasemapAdminStatus, 'job'>> {
  const res = await fetch(`${API_BASE_URL}/admin/basemap/upload`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, 'Content-Type': 'application/vnd.pmtiles' },
    body: file,
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as Omit<BasemapAdminStatus, 'job'>
}

// --- personal access tokens (Interop I-1) ----------------------------------------

/** A personal access token's metadata (GET /me/tokens). The secret is unrecoverable by design —
 *  it appears exactly once, in the mint response. */
export interface PatSummary {
  id: string
  label: string
  scope: 'read_only' | 'read_write'
  created_at: string
  expires_at: string | null
  last_used_at: string | null
  revoked_at: string | null
}

/** The mint response — the ONLY place the plaintext `tpat_…` ever appears. Never persist it. */
export interface PatMinted extends PatSummary {
  token: string
}

/** GET /me/tokens — the caller's tokens, metadata only, newest first. */
export async function fetchMyTokens(accessToken: string): Promise<PatSummary[]> {
  const res = await fetch(`${API_BASE_URL}/me/tokens`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return ((await res.json()) as { tokens: PatSummary[] }).tokens
}

/** POST /me/tokens — mint a token (scope defaults to read_only server-side; read_write unlocks
 *  desktop editing through the FeatureServer façade — I-3). 409 = the 25-live cap. */
export async function createMyToken(
  payload: { label: string; expiresInDays?: number; scope?: 'read_only' | 'read_write' },
  accessToken: string,
): Promise<PatMinted> {
  const res = await fetch(`${API_BASE_URL}/me/tokens`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify(payload),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as PatMinted
}

/** DELETE /me/tokens/{id} — revoke (immediate; idempotent; owner-scoped server-side). */
export async function revokeMyToken(tokenId: string, accessToken: string): Promise<void> {
  const res = await fetch(`${API_BASE_URL}/me/tokens/${encodeURIComponent(tokenId)}`, {
    method: 'DELETE',
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  if (!res.ok && res.status !== 204) throw new ApiError(res.status, await errorDetail(res))
}

/** DELETE /me/tokens/{id}?purge=true — permanently remove an ALREADY-REVOKED token record (list
 *  cleanup). 409 if the token is still live (revoke it first). */
export async function purgeMyToken(tokenId: string, accessToken: string): Promise<void> {
  const res = await fetch(`${API_BASE_URL}/me/tokens/${encodeURIComponent(tokenId)}?purge=true`, {
    method: 'DELETE',
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  if (!res.ok && res.status !== 204) throw new ApiError(res.status, await errorDetail(res))
}

// --- ownership transfer (Talus 26) ----------------------------------------------

/** A user an item may be transferred to (GET /me/transfer-targets). */
export interface TransferTarget {
  id: string
  display_name: string
}

/** GET /me/transfer-targets — the human users an item may be transferred to. Open to any
 *  authenticated caller; the real gate is the transfer POST. */
export async function fetchTransferTargets(accessToken: string): Promise<TransferTarget[]> {
  const res = await fetch(`${API_BASE_URL}/me/transfer-targets`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return ((await res.json()) as { users: TransferTarget[] }).users
}

// --- Groups (Groups management UI, decision-groups-ui.md) -----------------------

export type GroupRole = 'owner' | 'member'
export type ShareableItemType = 'layer' | 'web_map' | 'raster'

/** A group card on the browse page. `role` is the caller's relationship (null = org-discoverable). */
export interface GroupCard {
  groupId: string
  displayName: string
  summary: string | null
  memberCount: number
  system: boolean
  role: GroupRole | null
}

/** GET /me/groups — `mine` = groups I own/belong to; `org` = other org-discoverable groups. */
export interface MyGroups {
  mine: GroupCard[]
  org: GroupCard[]
}

export interface GroupMember {
  userId: string
  displayName: string
  username: string | null
  disabled: boolean
  isOwner: boolean
}

/** An item shared to a group. For layers, `editable`/`canEdit` carry the three-key edit legibility
 *  (the viewing member's actual edit capability); null for maps/rasters (read-only to recipients). */
export interface GroupContentItem {
  id: string
  title: string
  editable?: boolean | null
  canEdit?: boolean | null
}

export interface GroupContent {
  layers: GroupContentItem[]
  maps: GroupContentItem[]
  rasters: GroupContentItem[]
}

export interface GroupDetail {
  groupId: string
  displayName: string
  summary: string | null
  system: boolean
  /** Absolute deletion guard — when true, delete is refused for EVERYONE (owner and admin). */
  deleteProtected: boolean
  owner: GroupMember | null
  myRole: 'owner' | 'member' | 'admin' | null
  members: GroupMember[]
  content: GroupContent
}

export async function fetchMyGroups(accessToken: string): Promise<MyGroups> {
  const res = await fetch(`${API_BASE_URL}/me/groups`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as MyGroups
}

export async function fetchGroupDetail(groupId: string, accessToken: string): Promise<GroupDetail> {
  const res = await fetch(`${API_BASE_URL}/groups/${encodeURIComponent(groupId)}`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as GroupDetail
}

/** POST /groups — create a group (publisher/admin). Returns the new group's id (409 on slug clash). */
export async function createGroup(
  payload: { name: string; summary?: string | null },
  accessToken: string,
): Promise<{ group: string; groupId: string; displayName: string }> {
  const res = await fetch(`${API_BASE_URL}/groups`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify(payload),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as { group: string; groupId: string; displayName: string }
}

/** PATCH /groups/{id} — edit displayName / summary (owner or admin). The slug is immutable. */
export async function updateGroup(
  groupId: string,
  patch: { displayName?: string; summary?: string | null; deleteProtected?: boolean },
  accessToken: string,
): Promise<void> {
  const res = await fetch(`${API_BASE_URL}/groups/${encodeURIComponent(groupId)}`, {
    method: 'PATCH',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify(patch),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
}

/** DELETE /groups/{id} — owner/admin; refused on system groups; cascades shares + membership. */
export async function deleteGroup(groupId: string, accessToken: string): Promise<void> {
  const res = await fetch(`${API_BASE_URL}/groups/${encodeURIComponent(groupId)}`, {
    method: 'DELETE',
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
}

export async function addGroupMembers(
  groupId: string,
  userIds: string[],
  accessToken: string,
): Promise<void> {
  const res = await fetch(`${API_BASE_URL}/groups/${encodeURIComponent(groupId)}/members`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify({ userIds }),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
}

export async function removeGroupMember(
  groupId: string,
  userId: string,
  accessToken: string,
): Promise<void> {
  const res = await fetch(
    `${API_BASE_URL}/groups/${encodeURIComponent(groupId)}/members/${encodeURIComponent(userId)}`,
    { method: 'DELETE', headers: { Authorization: `Bearer ${accessToken}` } },
  )
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
}

export async function transferGroup(
  groupId: string,
  userId: string,
  accessToken: string,
): Promise<void> {
  const res = await fetch(`${API_BASE_URL}/groups/${encodeURIComponent(groupId)}/transfer`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify({ userId }),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
}

export async function addGroupItems(
  groupId: string,
  items: { itemType: ShareableItemType; id: string }[],
  accessToken: string,
): Promise<void> {
  const res = await fetch(`${API_BASE_URL}/groups/${encodeURIComponent(groupId)}/items`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify({ items }),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
}

export async function removeGroupItem(
  groupId: string,
  itemType: ShareableItemType,
  itemId: string,
  accessToken: string,
): Promise<void> {
  const res = await fetch(
    `${API_BASE_URL}/groups/${encodeURIComponent(groupId)}/items/${itemType}/${encodeURIComponent(itemId)}`,
    { method: 'DELETE', headers: { Authorization: `Bearer ${accessToken}` } },
  )
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
}

/** POST /catalog/{item_type}/{item_id}/transfer — reassign ownership (admin always; a non-admin
 *  owner when the org policy allows). The server is the authority. */
export async function transferOwnership(
  itemType: FavoriteItemType,
  itemId: string,
  newOwnerId: string,
  accessToken: string,
): Promise<void> {
  const res = await fetch(
    `${API_BASE_URL}/catalog/${itemType}/${encodeURIComponent(itemId)}/transfer`,
    {
      method: 'POST',
      headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
      body: JSON.stringify({ newOwnerId }),
    },
  )
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
}

// --- user administration (Talus 27) ---------------------------------------------
//
// The Admin → Users surface: onboard/offboard human users without the Keycloak console. Every
// endpoint is admin-gated server-side (this client is cosmetic; the server is the authority).

/** The assignable realm-role tier (single per user). Display labels live in the view. */
export type UserRole = 'talus-viewer' | 'talus-data-editor' | 'talus-publisher' | 'talus-admin'

/** A group an admin user belongs to (same shape as the SA registry). */
export interface AdminUserGroup {
  group_id: string
  display_name: string
}

/** A human user as listed (GET /admin/users). `role` is null when the identity provider couldn't
 * be reached to resolve it (the list degrades rather than failing). `active` mirrors disabled_at. */
export interface AdminUser {
  id: string
  username: string
  email: string
  display_name: string
  created_at: string
  disabled_at: string | null
  active: boolean
  // null => provisioned but never signed in (the "Pending" summary metric).
  last_seen_at: string | null
  role: string | null
  groups: AdminUserGroup[]
  // false => an ATTRIBUTION-ONLY principal: a platform.users row with no identity-provider account,
  // synthesised by bulk import so imported features have a valid created_by. They can never sign
  // in, so password and role actions are not offered on them (decision-user-lifecycle.md F1).
  // Defaults true when the identity provider is unreachable — a transient outage must not hide
  // real actions from an admin.
  can_sign_in: boolean
}

/** POST /admin/users response — carries the one-time temporary password (never retrievable again). */
export interface AdminUserCreated {
  id: string
  username: string
  email: string
  display_name: string
  role: string
  groups: string[]
  // null for a federated/SSO user (Talus 48) — they have no local password to show.
  temp_password: string | null
}

/** GET /admin/users — list humans with role, groups, status. */
export async function fetchUsers(accessToken: string): Promise<AdminUser[]> {
  const res = await fetch(`${API_BASE_URL}/admin/users`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return ((await res.json()) as { users: AdminUser[] }).users
}

/** POST /admin/users — create; the response's `temp_password` is shown ONCE. */
export async function createUser(
  payload: {
    username: string
    email: string
    display_name: string
    role: string
    groups: string[]
    // Talus 48: a federated/SSO user — created with no local password (authenticates via a linked IdP).
    federated?: boolean
  },
  accessToken: string,
): Promise<AdminUserCreated> {
  const res = await fetch(`${API_BASE_URL}/admin/users`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify(payload),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as AdminUserCreated
}

/** POST /admin/users/{id}/disable — offboard (Keycloak enabled=false + disabled_at). */
export async function disableUser(userId: string, accessToken: string): Promise<void> {
  const res = await fetch(`${API_BASE_URL}/admin/users/${encodeURIComponent(userId)}/disable`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
}

/** POST /admin/users/{id}/enable — re-enable a disabled user. */
export async function enableUser(userId: string, accessToken: string): Promise<void> {
  const res = await fetch(`${API_BASE_URL}/admin/users/${encodeURIComponent(userId)}/enable`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
}

/** POST /admin/users/{id}/reset-password — returns a NEW temp password, shown once. */
export async function resetUserPassword(
  userId: string,
  accessToken: string,
): Promise<{ id: string; temp_password: string }> {
  const res = await fetch(
    `${API_BASE_URL}/admin/users/${encodeURIComponent(userId)}/reset-password`,
    { method: 'POST', headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' } },
  )
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as { id: string; temp_password: string }
}

/** POST /me/password — change YOUR OWN password (decision-first-ten-minutes.md F3).
 *
 * Takes the current password as well as the new one: the server re-authenticates before it
 * writes, so a lifted access token alone cannot rotate the account credential. There is no user
 * id in the body by design — this endpoint can only ever change the caller's own password.
 * 204 on success; 403 = wrong current password, 400 = policy rejection (the detail names the
 * rule), 409 = an SSO account with no local password, 429 = brute-force lockout. */
export async function changeMyPassword(
  currentPassword: string,
  newPassword: string,
  accessToken: string,
): Promise<void> {
  const res = await fetch(`${API_BASE_URL}/me/password`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify({ currentPassword, newPassword }),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
}

/** The two honest outcomes of deleting a user (decision-user-lifecycle.md F4/F5).
 *  "deleted" — nothing referenced them; the row is gone.
 *  "removed" — their identity-provider account was deleted (irreversibly), but the record survives
 *              so historical edits still resolve to their name; it leaves the roster for good. */
export interface DeleteUserResult {
  id: string
  outcome: 'deleted' | 'removed'
  identity_deleted: boolean
}

/** DELETE /admin/users/{id} — remove a departed user. 409 while they still own content (the detail
 *  names what they own) or while the account is still enabled. The caller MUST report which
 *  outcome came back: never imply an erase that did not happen. */
export async function deleteUser(
  userId: string,
  accessToken: string,
): Promise<DeleteUserResult> {
  const res = await fetch(`${API_BASE_URL}/admin/users/${encodeURIComponent(userId)}`, {
    method: 'DELETE',
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as DeleteUserResult
}

/** PUT /admin/users/{id}/role — set the single role tier. */
export async function updateUserRole(
  userId: string,
  role: string,
  accessToken: string,
): Promise<void> {
  const res = await fetch(`${API_BASE_URL}/admin/users/${encodeURIComponent(userId)}/role`, {
    method: 'PUT',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify({ role }),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
}

/** PUT /admin/users/{id}/groups — set group memberships (reconciled server-side). */
export async function updateUserGroups(
  userId: string,
  groups: string[],
  accessToken: string,
): Promise<void> {
  const res = await fetch(`${API_BASE_URL}/admin/users/${encodeURIComponent(userId)}/groups`, {
    method: 'PUT',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify({ groups }),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
}

/** GET /admin/users/{id}/owned-content — counts for the reassign dialog. */
export async function fetchUserOwnedContent(
  userId: string,
  accessToken: string,
): Promise<{ maps: number; layers: number }> {
  const res = await fetch(
    `${API_BASE_URL}/admin/users/${encodeURIComponent(userId)}/owned-content`,
    { headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' } },
  )
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as { maps: number; layers: number }
}

/** Result of an all-to-one offboard reassign (POST /admin/users/{id}/reassign-content). */
export interface ReassignResult {
  from_user: string
  to_user: string
  moved_maps: string[]
  moved_layers: string[]
  failed: { type: string; id: string; error: string }[]
}

/** POST /admin/users/{id}/reassign-content — move ALL of a user's owned content to one successor. */
export async function reassignUserContent(
  userId: string,
  successorId: string,
  accessToken: string,
): Promise<ReassignResult> {
  const res = await fetch(
    `${API_BASE_URL}/admin/users/${encodeURIComponent(userId)}/reassign-content`,
    {
      method: 'POST',
      headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
      body: JSON.stringify({ successor_id: successorId }),
    },
  )
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as ReassignResult
}

// --- bulk user import (decision-bulk-user-import.md) ----------------------------
//
// The at-scale onboarding sibling of createUser: POST a CSV; the server validates every row and
// (on commit) creates the valid ones. Called twice — commit:false to preview, commit:true to
// create — with the server authoritative both times.

/** One CSV row's verdict. validate: ok | skip_exists | error; commit: created | failed (| skip_exists). */
export type ImportRowStatus = 'ok' | 'skip_exists' | 'error' | 'created' | 'failed'

export interface ImportRowResult {
  line: number
  username: string | null
  email: string | null
  display_name: string | null
  role: string | null
  groups: string[]
  status: ImportRowStatus
  message: string | null
}

/** A created user's one-time temp password — for the credentials-CSV download; never re-retrievable. */
export interface ImportCredential {
  username: string
  temp_password: string
}

export interface ImportSummary {
  total: number
  to_create: number
  to_skip: number
  errors: number
  created: number
  failed: number
}

export interface ImportResult {
  committed: boolean
  summary: ImportSummary
  rows: ImportRowResult[]
  credentials: ImportCredential[]
}

/** POST /admin/users/import — commit:false validates (dry run, no writes); commit:true validates
 * THEN creates the valid rows. On commit, `credentials` carries each created user's temp password. */
export async function bulkImportUsers(
  csvText: string,
  commit: boolean,
  accessToken: string,
): Promise<ImportResult> {
  const res = await fetch(`${API_BASE_URL}/admin/users/import`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify({ csv_text: csvText, commit }),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as ImportResult
}

// --- admin reports (Talus 29) ---------------------------------------------------
//
// Operational reports (admin-gated): content inventory + sharing breakdown, and the member roster.
// Each is viewable as JSON (table) or downloadable as CSV. Counts + tables only — no BI.

export interface ContentReportRow {
  itemType: string
  itemId: string
  title: string
  ownerId: string | null
  ownerName: string | null
  shareLevel: string | null
  groupsShared: number
  deleteProtected: boolean
  created_at: string
  updated_at: string
  lastAccessed: string | null
}

export interface ContentReport {
  summary: {
    total: number
    byType: Record<string, number>
    byShareLevel: Record<string, number>
    ownerless: number
    staleCount: number
  }
  rows: ContentReportRow[]
}

export interface MemberReport {
  summary: { total: number; pending: number; disabled: number; byRole: Record<string, number> }
  rows: AdminUser[]
}

export interface PublicItemRow {
  itemType: string // 'layer' | 'web_map'
  itemId: string
  title: string
  ownerName: string | null
  inOpenData: boolean // curated into the Open Data catalog (vs public-but-not-curated)
  created_at: string
  updated_at: string
}

export interface PublicContentReport {
  summary: { total: number; byType: Record<string, number>; notCurated: number }
  rows: PublicItemRow[]
}

/** GET /admin/reports/content — content inventory + sharing breakdown. */
export async function fetchContentReport(accessToken: string): Promise<ContentReport> {
  const res = await fetch(`${API_BASE_URL}/admin/reports/content`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as ContentReport
}

/** GET /admin/reports/members — member roster. */
export async function fetchMemberReport(accessToken: string): Promise<MemberReport> {
  const res = await fetch(`${API_BASE_URL}/admin/reports/members`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as MemberReport
}

/** GET /admin/reports/public-content — every PUBLIC item (governance lens, Talus 33). */
export async function fetchPublicContentReport(accessToken: string): Promise<PublicContentReport> {
  const res = await fetch(`${API_BASE_URL}/admin/reports/public-content`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as PublicContentReport
}

/** One governance action from the admin-audit log (decision-admin-reports-round2 §4.3). */
export interface ActivityRow {
  at: string
  actorId: string
  actorName: string | null
  actorType: string
  action: string
  targetType: string | null
  targetId: string | null
  targetName: string | null
  detail: Record<string, unknown>
}

export interface ActivityReport {
  summary: { total: number; byAction: Record<string, number>; byActorType: Record<string, number> }
  rows: ActivityRow[]
}

/** GET /admin/reports/activity — the governance-action log (admin; newest-first, windowed). */
export async function fetchActivityReport(accessToken: string): Promise<ActivityReport> {
  const res = await fetch(`${API_BASE_URL}/admin/reports/activity`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as ActivityReport
}

/** Storage / capacity report (admin-reports-round2 §5.1). */
export interface StorageReport {
  summary: {
    databaseBytes: number
    objectBuckets: Record<string, number | null>
    totalObjectBytes: number
    layerCount: number
    totalFeatures: number
  }
  rows: { layerId: string; title: string; featureCount: number | null }[]
}

export async function fetchStorageReport(accessToken: string): Promise<StorageReport> {
  const res = await fetch(`${API_BASE_URL}/admin/reports/storage`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as StorageReport
}

/** Groups inventory report (admin-reports-round2 §5.2). */
export interface GroupsReport {
  summary: { total: number; degraded: boolean }
  rows: {
    groupId: string
    displayName: string
    memberCount: number | null
    contentCount: number | null
    deleteProtected: boolean
    created_at: string
  }[]
}

export async function fetchGroupsReport(accessToken: string): Promise<GroupsReport> {
  const res = await fetch(`${API_BASE_URL}/admin/reports/groups`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as GroupsReport
}

/** Service-account inventory report (admin-reports-round2 §5.3). */
export interface ServiceAccountReport {
  summary: { total: number; enabled: number; disabled: number }
  rows: {
    id: string
    clientId: string | null
    displayName: string
    enabled: boolean | null
    accessLevel: string
    last_seen_at: string | null
    created_at: string
  }[]
}

export async function fetchServiceAccountReport(accessToken: string): Promise<ServiceAccountReport> {
  const res = await fetch(`${API_BASE_URL}/admin/reports/service-accounts`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as ServiceAccountReport
}

/** Download a report as CSV. Fetches with the bearer (the endpoint is admin-gated, so a plain
 *  <a href> won't carry auth), then saves the returned blob via a temporary link. */
export async function downloadReportCsv(
  kind: 'content' | 'members' | 'public-content' | 'usage' | 'activity' | 'storage' | 'groups' | 'service-accounts',
  accessToken: string,
): Promise<void> {
  const res = await fetch(`${API_BASE_URL}/admin/reports/${kind}?format=csv`, {
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  const blob = await res.blob()
  const cd = res.headers.get('Content-Disposition') ?? ''
  const match = /filename="?([^"]+)"?/.exec(cd)
  const filename = match ? match[1] : `talus-${kind}.csv`
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

// --- where-used (decision-where-used.md) -----------------------------------------
// Per-item cross-references, viewer-filtered SERVER-SIDE (§3.1: referencing items the caller can't
// view are omitted entirely — no titles, no "+N hidden" counts). Every surface that renders these
// lists carries USAGE_DISCLAIMER verbatim (the fixed copy), which is what makes the omission
// honest. The org-wide roll-up is admin-only (T29 Reports).

export const USAGE_DISCLAIMER =
  'This list includes all items you have access to. For a complete list, contact a Talus administrator.'

export interface UsageRef {
  id: string
  title: string
}

export interface UsageGeocoderRef {
  id: string
  name: string
  enabled: boolean
}

/** GET /layers/{id}/usage — where a layer/view/table is used. Requires can_view on the subject. */
export interface LayerUsage {
  item: { id: string; type: string; title: string }
  webMaps: UsageRef[]
  viewLayers: UsageRef[]
  geocoders: UsageGeocoderRef[]
  openData: boolean
  homeFeatured: boolean
}

/** Raster / external-ref usage — web maps are their only reference kind. */
export interface ItemUsage {
  item: { id: string; type: string; title: string }
  webMaps: UsageRef[]
}

export async function fetchLayerUsage(layerId: string, accessToken: string): Promise<LayerUsage> {
  const res = await fetch(`${API_BASE_URL}/layers/${encodeURIComponent(layerId)}/usage`, {
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as LayerUsage
}

export async function fetchRasterUsage(rasterId: string, accessToken: string): Promise<ItemUsage> {
  const res = await fetch(`${API_BASE_URL}/rasters/${encodeURIComponent(rasterId)}/usage`, {
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as ItemUsage
}

export async function fetchExternalRefUsage(refId: string, accessToken: string): Promise<ItemUsage> {
  const res = await fetch(`${API_BASE_URL}/external-refs/${encodeURIComponent(refId)}/usage`, {
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as ItemUsage
}

/** One row of the admin cross-reference report (every item, referenced or not). */
export interface UsageReportRow {
  itemType: string // 'layer' | 'view' | 'table' | 'raster' | 'external' | 'web_map'
  itemId: string
  title: string
  webMaps: number
  viewLayers: number
  geocoders: number
  openData: boolean
  homeFeatured: boolean
  total: number
}

export interface UsageReport {
  summary: { items: number; itemsReferenced: number; totalReferences: number; degraded: boolean }
  rows: UsageReportRow[]
}

/** GET /admin/reports/usage — the org-wide cross-reference roll-up (admin). */
export async function fetchUsageReport(accessToken: string): Promise<UsageReport> {
  const res = await fetch(`${API_BASE_URL}/admin/reports/usage`, {
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as UsageReport
}

// --- maintenance / housekeeping (Talus 39) --------------------------------------
//
// Read-only view of the self-maintenance scheduler: per-job last-run summary + recent run history.
// Cadence/retention are config-driven (MAINTENANCE_* env), so there is no write surface here.

/** Latest run for one registered cleanup job (nulls if it has never run yet). */
export interface MaintenanceJobSummary {
  jobName: string
  lastRunAt: string | null
  lastStatus: string | null
  lastItemsAffected: number | null
  lastBytesFreed: number | null
  lastDetail: string | null
}

/** One historical run row (newest-first list). */
export interface MaintenanceRunRow {
  id: number
  jobName: string
  startedAt: string
  finishedAt: string
  status: string
  itemsAffected: number
  bytesFreed: number
  detail: string | null
}

export interface MaintenanceRuns {
  jobs: MaintenanceJobSummary[]
  recent: MaintenanceRunRow[]
}

// --- system health (decision-system-health.md) ----------------------------------
//
// The Admin → System one-glance page: live service probes + already-recorded operational state.
// STATUS, not a control plane — read-only, admin-gated server-side.

export interface SystemProbe {
  name: string
  status: 'up' | 'slow' | 'down'
  latencyMs: number | null
  error: string | null
  detail?: string | null
}

export interface SystemHealth {
  generatedAt: string
  probes: SystemProbe[]
  backup: { lastOkAt: string | null; target: string | null; scheduledEnabled: boolean | null }
  maintenance: { job: string; lastRunAt: string | null; lastStatus: string | null }[]
  storage: {
    dbBytes: number | null
    mediaBytes: number | null
    mediaObjects: number | null
    mediaAsOf: string | null
  }
  version: { service: string; migrationHead: string | null }
  restoreDrillLastOkAt: string | null
  /**
   * What is wrong with the SYSTEM, as opposed to which processes are down. Server-derived policy
   * (decision-system-health-issue-escalation.md) — the client renders the verdict, it does not
   * re-derive one. Empty ⇒ nothing operational needs attention.
   */
  issues: SystemIssue[]
}

export interface SystemIssue {
  key: string
  severity: 'warn' | 'critical'
  summary: string
}

/** GET /admin/system-health — one-glance portal health (admin-only, read-only). */
export async function fetchSystemHealth(accessToken: string): Promise<SystemHealth> {
  const res = await fetch(`${API_BASE_URL}/admin/system-health`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as SystemHealth
}

/** GET /admin/maintenance/runs — housekeeping scheduler run history (admin-only, read-only). */
export async function fetchMaintenanceRuns(accessToken: string): Promise<MaintenanceRuns> {
  const res = await fetch(`${API_BASE_URL}/admin/maintenance/runs`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as MaintenanceRuns
}

// --- disaster recovery / backups (Talus 40) -------------------------------------
//
// The DR catalog: one row per backup artifact (pg_dump + incremental media archive), plus the effective
// (config-driven) cadence/retention/target. Two safe actions — "Back up now" + Download. Restore is a
// documented operator runbook, never a button.

/** One backup-catalog entry (newest-first). */
export interface BackupRow {
  id: number
  createdAt: string
  kind: string                 // 'scheduled' | 'manual'
  status: string               // 'ok' | 'error'
  dbObjectKey: string | null
  dbSizeBytes: number
  dbSha256: string | null
  keycloakSizeBytes: number
  openfgaSizeBytes: number
  envInBackup: boolean         // Talus 45: the `.env` lifeboat is in this set (always age-encrypted)
  mediaObjectsCopied: number
  mediaBytesCopied: number
  target: string
  pgVersion: string | null
  detail: string | null
  encrypted: boolean
}

/** Which artifact in a backup set to download (Talus 42 full-system + Talus 45 `.env` lifeboat). */
export type BackupArtifact = 'db' | 'keycloak' | 'openfga' | 'env'

/** Which editable knobs are DB overrides (true) vs the env default (false) — Talus 43, drives default/custom. */
export interface BackupOverrides {
  intervalSeconds: boolean
  retentionDays: boolean
  retentionMinKeep: boolean
  scheduledEnabled: boolean
  ageRecipients: boolean
  includeEnv: boolean
}

/** The env (deployment) defaults each editable knob reverts to when its override is cleared — Talus 43. */
export interface BackupEnvDefaults {
  intervalSeconds: number
  retentionDays: number
  retentionMinKeep: number
  scheduledEnabled: boolean
  ageRecipients: string[]
  includeEnv: boolean
  destinationTarget: string    // the env-side destination LABEL "reset to default" reverts to (no creds)
}

/** The effective backup destination (decision-backup-destination.md). The secret key is NEVER present in
 * any form — secretKeySet is the only signal; accessKey is returned only for the DB override. */
export interface BackupDestination {
  source: 'override' | 'env'
  kind: 's3' | 'local'
  target: string               // the honest-DR label (same string as config.target / catalog rows)
  endpoint: string             // '' for the local kind
  bucket: string
  accessKey: string            // override only; '' otherwise
  secretKeySet: boolean
}

/** Effective (override-or-env) DR settings + the override/env detail the editable form needs (Talus 43). */
export interface BackupConfig {
  enabled: boolean
  intervalSeconds: number
  retentionDays: number
  retentionMinKeep: number
  ageRecipients: string[]      // effective recipients (PUBLIC age1… keys; editable)
  target: string               // the effective destination's honest-DR label
  offHost: boolean
  destination: BackupDestination  // the editable destination detail (secret-free)
  objectBuckets: string[]
  encryptionEnabled: boolean
  encryptionRecipients: number
  includesKeycloak: boolean
  includesOpenfga: boolean
  // Talus 45 — the `.env` lifeboat. includeEnv = the setting; includeEnvEffective = the runtime truth
  // (toggle AND encryption-on AND the api can read .env); includeEnvFileReadable drives the precise hint.
  includeEnv: boolean
  includeEnvEffective: boolean
  includeEnvFileReadable: boolean
  overridden: BackupOverrides
  envDefaults: BackupEnvDefaults
}

export interface BackupsResponse {
  config: BackupConfig
  backups: BackupRow[]
}

/** PUT body for the editable backup settings. null = "use the deployment (env) default" (clears the
 * override). ageRecipients are PUBLIC age1… keys; null/[] clears the override (uses env). */
export interface BackupSettingsUpdate {
  intervalSeconds: number | null
  retentionDays: number | null
  retentionMinKeep: number | null
  ageRecipients: string[] | null
  scheduledEnabled: boolean | null
  // Talus 45: include the `.env` lifeboat. null = use env default. Enabling 422s if the api can't read .env;
  // at runtime it's still ANDed with encryption-on (no recipients ⇒ no .env).
  includeEnv: boolean | null
}

/** GET /admin/backups — the DR backup catalog + effective config (admin-only, read-only). */
export async function fetchBackups(accessToken: string): Promise<BackupsResponse> {
  const res = await fetch(`${API_BASE_URL}/admin/backups`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as BackupsResponse
}

/** PUT /admin/backups/settings — update the editable backup knobs (admin-only). Returns the post-update
 * catalog + effective config (so the caller can update in one round-trip). A null field clears that
 * override (falls back to the deployment/env default). */
export async function updateBackupSettings(
  accessToken: string,
  body: BackupSettingsUpdate,
): Promise<BackupsResponse> {
  const res = await fetch(`${API_BASE_URL}/admin/backups/settings`, {
    method: 'PUT',
    headers: {
      Authorization: `Bearer ${accessToken}`,
      'Content-Type': 'application/json',
      Accept: 'application/json',
    },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as BackupsResponse
}

/** Body for setting/testing the off-host destination override (decision-backup-destination.md).
 * secretKey null/omitted = KEEP the stored secret (PUT) / test with the stored secret (test) — the
 * opposite of the knobs' null-clears convention, which is why the destination is its own endpoint.
 * bucket null/blank = use the deployment's default bucket name. */
export interface BackupDestinationUpdate {
  endpoint: string
  bucket: string | null
  accessKey: string
  secretKey: string | null
}

export interface BackupDestinationTestResult {
  ok: boolean
  detail: string
}

/** PUT /admin/backups/destination — set/update the off-host destination override (admin-only). */
export async function updateBackupDestination(
  accessToken: string,
  body: BackupDestinationUpdate,
): Promise<BackupsResponse> {
  const res = await fetch(`${API_BASE_URL}/admin/backups/destination`, {
    method: 'PUT',
    headers: {
      Authorization: `Bearer ${accessToken}`,
      'Content-Type': 'application/json',
      Accept: 'application/json',
    },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as BackupsResponse
}

/** DELETE /admin/backups/destination — clear the override; the destination reverts to the deployment
 * (env) default. Existing backups at the old destination are not touched. */
export async function clearBackupDestination(accessToken: string): Promise<BackupsResponse> {
  const res = await fetch(`${API_BASE_URL}/admin/backups/destination`, {
    method: 'DELETE',
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as BackupsResponse
}

/** POST /admin/backups/destination/test — probe the CANDIDATE destination (write + stat + delete a tiny
 * object; nothing persisted). Always 200 with ok/detail; throws only on auth/validation errors. */
export async function testBackupDestination(
  accessToken: string,
  body: BackupDestinationUpdate,
): Promise<BackupDestinationTestResult> {
  const res = await fetch(`${API_BASE_URL}/admin/backups/destination/test`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${accessToken}`,
      'Content-Type': 'application/json',
      Accept: 'application/json',
    },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as BackupDestinationTestResult
}

/** POST /admin/backups/run — take a backup now (admin-only). Resolves once the run is accepted (202). */
export async function runBackupNow(accessToken: string): Promise<void> {
  const res = await fetch(`${API_BASE_URL}/admin/backups/run`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
}

/** GET /admin/backups/{id}/download — fetch one artifact (db/keycloak/openfga/env) and trigger a save. The
 * `env` artifact (Talus 45 lifeboat) is always age-encrypted ciphertext — decrypt off-host with the private
 * key. */
export async function downloadBackup(
  accessToken: string,
  id: number,
  artifact: BackupArtifact = 'db',
): Promise<void> {
  const res = await fetch(`${API_BASE_URL}/admin/backups/${id}/download?artifact=${artifact}`, {
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  const blob = await res.blob()
  const cd = res.headers.get('Content-Disposition') ?? ''
  const m = /filename="?([^"]+)"?/.exec(cd)
  const fallback = artifact === 'env' ? `talus-env-${id}.age` : `talus-${artifact}-${id}.dump`
  const name = m ? m[1] : fallback
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = name
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

// --- public home (Talus 30) -----------------------------------------------------
//
// The public-facing landing: an anonymous bootstrap (is there a public face + the org home text) and
// the featured gallery (items shared to the reserved 'home' group, filtered to the caller's access).

export interface OrgHome {
  heading: string | null
  intro: string | null
  heroImageUrl: string | null
}

export interface PublicConfig {
  publicAnonAccessEnabled: boolean
  home: OrgHome
}

export interface FeaturedItem {
  itemType: string // 'web_map' | 'layer'
  itemId: string
  title: string
  description: string | null
  ownerName: string | null
}

/** GET /public/config — the anonymous bootstrap (NO auth): public-access flag + home text. */
export async function fetchPublicConfig(): Promise<PublicConfig> {
  const res = await fetch(`${API_BASE_URL}/public/config`, { headers: { Accept: 'application/json' } })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as PublicConfig
}

/** GET /home/featured — featured items the caller may see (optional-auth: pass a token if signed in;
 *  anon callers get the public subset). */
export async function fetchFeatured(accessToken?: string): Promise<FeaturedItem[]> {
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`
  const res = await fetch(`${API_BASE_URL}/home/featured`, { headers })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return ((await res.json()) as { items: FeaturedItem[] }).items
}

// --- Open Data portal (Talus 33) ------------------------------------------------
//
// The public, anonymous-friendly catalog of curated public datasets. All reads are optional-auth
// (pass a token when signed in; anon callers get the public subset, F6-gated). Downloads ride the
// same anon /export surface (CSV/GeoJSON/Shapefile), which the server scrubs + rate-limits for anon.

export interface OpenDataset {
  layerId: string
  title: string
  abstract: string | null
  keywords: string[]
  topicCategory: string | null
  license: string | null
  ownerName: string | null
  featureCount: number
  updatedAt: string | null
  hasGeometry: boolean
  /** PostGIS's own spelling — 'Point', 'MultiPolygon', 'MultiLineString'. Null for a hosted table,
   *  and for a geom column with no typmod constraint (omitted rather than guessed). Collapsing
   *  Multi* to a UI label is the client's job. */
  geometryType: string | null
  /** Storage CRS. `/export?srid=` reprojects at the edge on request, but this is the layer's own
   *  projection and the honest default to advertise. Null when unconstrained. */
  srid: number | null
  formats: string[] // 'csv' | 'geojson' | 'shapefile'
  apiUrl: string
  /** Interop I-1 tail: the dataset's OGC API — Features collection on the PUBLIC door. ABSOLUTE
   *  (server-built from the advertised base) — meant for desktop GIS / GDAL / any OGC client.
   *  Null for geometry-less tables. */
  ogcUrl: string | null
}

/** One curated public RASTER (raster-public slice): imagery/elevation on the Open Data portal.
 *  `formats` is ['geotiff','cog'] when downloadable, [] for view/stream-only rasters. */
export interface OpenRasterDataset {
  rasterId: string
  title: string
  abstract: string | null
  keywords: string[]
  topicCategory: string | null
  license: string | null
  ownerName: string | null
  updatedAt: string | null
  bandCount: number | null
  downloadable: boolean
  formats: string[]
  thumbnailUrl: string
  tilejsonUrl: string
  wmtsUrl: string
  wcsUrl: string | null
}

/** GET /open-data/datasets — the public catalog the caller may see (optional-auth, anon → public):
 *  vector datasets + curated public rasters (raster-public slice). */
export async function fetchOpenDatasets(
  accessToken?: string,
): Promise<{ datasets: OpenDataset[]; rasters: OpenRasterDataset[] }> {
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`
  const res = await fetch(`${API_BASE_URL}/open-data/datasets`, { headers })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  const body = (await res.json()) as { datasets: OpenDataset[]; rasters?: OpenRasterDataset[] }
  return { datasets: body.datasets, rasters: body.rasters ?? [] }
}

/** GET /open-data/rasters/{id} — one curated public raster's detail (optional-auth; 404 if not
 *  curated-and-visible to the caller). */
export async function fetchOpenRasterDataset(
  rasterId: string,
  accessToken?: string,
): Promise<OpenRasterDataset> {
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`
  const res = await fetch(`${API_BASE_URL}/open-data/rasters/${encodeURIComponent(rasterId)}`, { headers })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as OpenRasterDataset
}

/** GET /open-data/datasets/{id} — one curated public dataset's detail (optional-auth; 404 if not
 *  curated-and-visible to the caller). */
export async function fetchOpenDataset(layerId: string, accessToken?: string): Promise<OpenDataset> {
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`
  const res = await fetch(`${API_BASE_URL}/open-data/datasets/${encodeURIComponent(layerId)}`, { headers })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as OpenDataset
}

/** GET /layers/{id}/features (limit N) — a small anon-capable preview for the dataset page. */
export async function fetchOpenDataPreview(
  layerId: string,
  limit = 10,
  accessToken?: string,
): Promise<LayerFeatureCollection> {
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`
  const res = await fetch(
    `${API_BASE_URL}/layers/${encodeURIComponent(layerId)}/features?limit=${limit}`,
    { headers },
  )
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as LayerFeatureCollection
}

export type OpenDataFormat = 'csv' | 'geojson' | 'shapefile' | 'gpkg'
const _OPEN_DATA_EXT: Record<OpenDataFormat, string> = { csv: 'csv', geojson: 'geojson', shapefile: 'zip', gpkg: 'gpkg' }

/** Download a public dataset in `format` (optional-auth; anon for a public dataset). Shapefile comes
 *  back zipped. Fetches the blob (so the bearer can ride along when signed in) then saves it. */
export async function downloadOpenDataExport(
  layerId: string,
  format: OpenDataFormat,
  accessToken?: string,
): Promise<void> {
  const headers: Record<string, string> = {}
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`
  const res = await fetch(
    `${API_BASE_URL}/layers/${encodeURIComponent(layerId)}/export?format=${format}`,
    { headers },
  )
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  const blob = await res.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${layerId}-${new Date().toISOString().slice(0, 10)}.${_OPEN_DATA_EXT[format]}`
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

// Open Data CURATION (Talus 33 c1) — the staff-facing publish surface, owner-or-admin gated and
// (for non-admins) Open Data membership gated; surfaced in the catalog Manage dialog's Open Data tab.

export interface CurationStatus {
  curated: boolean
  public: boolean
  metadataComplete: boolean
  missing: string[]
  canManage: boolean
  canCurate: boolean
  /** Whether the caller may turn the layer public (admin, or org policy permits non-admins). A
   *  member who lacks this can't publish even with complete metadata — the org gates it. */
  publicSharePermitted: boolean
}

/** GET /open-data/layers/{id}/curation — curation state + publish-readiness (owner-or-admin). */
export async function fetchCuration(layerId: string, accessToken: string): Promise<CurationStatus> {
  const res = await fetch(`${API_BASE_URL}/open-data/layers/${encodeURIComponent(layerId)}/curation`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as CurationStatus
}

/** POST /open-data/layers/{id}/curate — the governed publish: metadata gate -> make public
 *  (audited) -> share to group:open_data. Returns the updated status. */
export async function curateLayer(layerId: string, accessToken: string): Promise<CurationStatus> {
  const res = await fetch(`${API_BASE_URL}/open-data/layers/${encodeURIComponent(layerId)}/curate`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as CurationStatus
}

/** DELETE /open-data/layers/{id}/curate — un-curate (drops from the catalog; does NOT un-publish). */
export async function uncurateLayer(layerId: string, accessToken: string): Promise<CurationStatus> {
  const res = await fetch(`${API_BASE_URL}/open-data/layers/${encodeURIComponent(layerId)}/curate`, {
    method: 'DELETE',
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as CurationStatus
}

/** GET /open-data/rasters/{id}/curation — a raster's curation state (raster-public slice). */
export async function fetchRasterCuration(rasterId: string, accessToken: string): Promise<CurationStatus> {
  const res = await fetch(`${API_BASE_URL}/open-data/rasters/${encodeURIComponent(rasterId)}/curation`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as CurationStatus
}

/** POST /open-data/rasters/{id}/curate — the governed raster publish (metadata gate -> public ->
 *  group:open_data). */
export async function curateRaster(rasterId: string, accessToken: string): Promise<CurationStatus> {
  const res = await fetch(`${API_BASE_URL}/open-data/rasters/${encodeURIComponent(rasterId)}/curate`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as CurationStatus
}

/** DELETE /open-data/rasters/{id}/curate — un-curate a raster (does NOT un-publish). */
export async function uncurateRaster(rasterId: string, accessToken: string): Promise<CurationStatus> {
  const res = await fetch(`${API_BASE_URL}/open-data/rasters/${encodeURIComponent(rasterId)}/curate`, {
    method: 'DELETE',
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as CurationStatus
}

// Metadata export (Talus 33 c7) — the ISO-19115 record -> open standards (QGIS .qmd / ISO 19139 XML).
// Optional-auth: a PUBLIC dataset's metadata is anon-downloadable; staff export any item they may view.

export type MetadataFormat = 'qmd' | 'iso19139'
const _META_EXT: Record<MetadataFormat, string> = { qmd: 'qmd', iso19139: 'iso19139.xml' }

/** Download an item's metadata as QGIS `.qmd` or ISO 19139 XML. Fetches the blob (so a bearer can
 *  ride along when signed in; omitted for the public Open Data page) and saves it. */
export async function downloadMetadata(
  itemType: FavoriteItemType,
  itemId: string,
  format: MetadataFormat,
  accessToken?: string,
): Promise<void> {
  const headers: Record<string, string> = {}
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`
  const res = await fetch(
    `${API_BASE_URL}/items/${itemType}/${encodeURIComponent(itemId)}/metadata/download?format=${format}`,
    { headers },
  )
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  const blob = await res.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${itemId}.${_META_EXT[format]}`
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

// --- delete protection (Talus 24 c4) --------------------------------------------
//
// An owner-settable guard-rail blocking accidental deletion of a catalog item. Read/write is
// owner-or-admin (server-enforced); surfaced in the catalog "Manage" dialog. Generic over the two
// item types, mapping to /web-maps/{id}/delete-protection and /layers/{id}/delete-protection. A
// protected item's delete (web-map delete / layer release) is rejected server-side for a non-admin.

// --- item metadata (Talus 31, ISO-19115-cored) ----------------------------------

export interface MetadataContact {
  name?: string | null
  organization?: string | null
  email?: string | null
  role?: string | null
}

/** EX_GeographicBoundingBox (EPSG:4326). Server-derived from geometry — read-only in the UI. */
export interface GeoBoundingBox {
  west: number
  south: number
  east: number
  north: number
}

export interface MetadataDoc {
  title?: string | null
  abstract?: string | null
  keywords: string[]
  topicCategory?: string | null
  license?: string | null
  pointOfContact?: MetadataContact | null
  language: string
  spatialExtent?: GeoBoundingBox | null
}

export interface ItemMetadata {
  itemType: string
  itemId: string
  doc: MetadataDoc
  /** Whether the required-core is filled (the Open Data publish gate). */
  complete: boolean
  /** Required-core keys still blank, when not complete. */
  missing: string[]
  canManage: boolean
  updatedAt?: string | null
}

function _metadataUrl(itemType: FavoriteItemType, itemId: string): string {
  return `${API_BASE_URL}/items/${itemType}/${encodeURIComponent(itemId)}/metadata`
}

/** GET …/metadata — the item's record + completeness (anyone who can view OR manage it). */
export async function fetchItemMetadata(
  itemType: FavoriteItemType,
  itemId: string,
  accessToken: string,
): Promise<ItemMetadata> {
  const res = await fetch(_metadataUrl(itemType, itemId), {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as ItemMetadata
}

/** PUT …/metadata — write the record (owner or admin). Spatial extent is server-derived (any value
 * sent is ignored). Returns the saved record + recomputed completeness. */
export async function saveItemMetadata(
  itemType: FavoriteItemType,
  itemId: string,
  doc: MetadataDoc,
  accessToken: string,
): Promise<ItemMetadata> {
  const res = await fetch(_metadataUrl(itemType, itemId), {
    method: 'PUT',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify(doc),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as ItemMetadata
}

function _deleteProtectionUrl(itemType: FavoriteItemType, itemId: string): string {
  const base =
    itemType === 'web_map'
      ? 'web-maps'
      : itemType === 'raster'
        ? 'rasters'
        : itemType === 'field_project'
          ? 'field-projects'
          : 'layers'
  return `${API_BASE_URL}/${base}/${encodeURIComponent(itemId)}/delete-protection`
}

/** GET …/delete-protection — the current flag (owner or admin). */
export async function fetchDeleteProtection(
  itemType: FavoriteItemType,
  itemId: string,
  accessToken: string,
): Promise<boolean> {
  const res = await fetch(_deleteProtectionUrl(itemType, itemId), {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return ((await res.json()) as { deleteProtected: boolean }).deleteProtected
}

/** PUT …/delete-protection — set/clear the flag (owner or admin). Returns the saved value. */
export async function setDeleteProtection(
  itemType: FavoriteItemType,
  itemId: string,
  deleteProtected: boolean,
  accessToken: string,
): Promise<boolean> {
  const res = await fetch(_deleteProtectionUrl(itemType, itemId), {
    method: 'PUT',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify({ deleteProtected }),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return ((await res.json()) as { deleteProtected: boolean }).deleteProtected
}

// --- field projects (.qgz intake & Import-URL door, V085) -----------------------
//
// A Field project is an uploaded QField/QGIS project (.qgz) stored as an OPAQUE artifact and
// served to QField's Import URL through a credentialed door (decision-field-projects.md). The
// portal never opens or edits the content — qgsTitle/layerCount are a best-effort catalog sniff.

/** One field project in the catalog list. snake_case timestamps ride the shared filter/sort. */
export interface FieldProjectSummary {
  id: string
  title: string
  description: string | null
  created_at: string
  updated_at: string
  ownerId?: string | null
  ownerName?: string | null
  sizeBytes: number
  qgsTitle?: string | null
  layerCount?: number | null
  /** Upload-lip hazards (F5): 'multiple_qgs' | 'os_junk' | 'foreign_datasources' |
   * 'unreadable_metadata' — plus, on GENERATED items, the generation notes
   * (decision-generate-field-project.md F2–F4): 'skipped_layers' | 'view_layer_not_served' |
   * 'filters_not_applied' | 'renderer_flattened' | 'point_icon_not_carried' |
   * 'photo_naming_wall_clock' |
   * 'token_scope_read_only' | 'layer_token_denied'. */
  warnings?: string[]
  shareLevel?: ShareLevel | null
  favorite?: boolean
  deleteProtected?: boolean
  /** Set when the item was GENERATED from a web map (null after that map is deleted —
   * Regenerate then disappears; the item lives on as a plain upload-like artifact). */
  sourceWebMapId?: string | null
}

export interface MyFieldProjects {
  owned: FieldProjectSummary[]
  shared: FieldProjectSummary[]
}

/** GET /me/field-projects — the caller's field projects (owned + shared-with-them). */
export async function fetchMyFieldProjects(accessToken: string): Promise<MyFieldProjects> {
  const res = await fetch(`${API_BASE_URL}/me/field-projects`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as MyFieldProjects
}

/** POST /field-projects — upload a .qgz. The bytes ARE the request body (the rasters-ingest
 * pattern); title/description ride as query params. Publisher/admin only. */
export async function uploadFieldProject(
  file: File,
  title: string,
  description: string,
  accessToken: string,
): Promise<{ id: string; title: string; warnings: string[] }> {
  const qs = new URLSearchParams({ title })
  if (description) qs.set('description', description)
  const res = await fetch(`${API_BASE_URL}/field-projects?${qs.toString()}`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, 'Content-Type': 'application/octet-stream' },
    body: file,
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as { id: string; title: string; warnings: string[] }
}

/** PUT /field-projects/{id}/archive — replace the artifact in place (owner or admin). The next
 * crew re-import pulls the new bytes (the ETag rolls with the content). */
export async function replaceFieldProject(
  id: string,
  file: File,
  accessToken: string,
): Promise<{ id: string; warnings: string[] }> {
  const res = await fetch(`${API_BASE_URL}/field-projects/${encodeURIComponent(id)}/archive`, {
    method: 'PUT',
    headers: { Authorization: `Bearer ${accessToken}`, 'Content-Type': 'application/octet-stream' },
    body: file,
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as { id: string; warnings: string[] }
}

/** POST /web-maps/{mapId}/field-project — "Make field project": the server compiles the map
 * into a .qgz, mints a dedicated embedded PAT (owned by the caller, labeled
 * `field-project: <title>`, visible/revocable in their token list), and stores the result as
 * a normal Field project item. Publisher/admin only; 409 when the advertised base URL is
 * unset or the caller is at the token cap (decision-generate-field-project.md). */
export async function generateFieldProjectFromMap(
  mapId: string,
  payload: { title?: string; description?: string },
  accessToken: string,
): Promise<FieldProjectSummary & { warnings: string[]; pat: { label: string; scope: string } }> {
  const res = await fetch(`${API_BASE_URL}/web-maps/${encodeURIComponent(mapId)}/field-project`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify(payload),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as FieldProjectSummary & {
    warnings: string[]
    pat: { label: string; scope: string }
  }
}

/** POST /field-projects/{id}/regenerate — recompile from the CURRENT source web map with the
 * SAME embedded token (crews' imported copies keep live-layer access; they still delete-old +
 * re-import for the new compile). Owner/admin, and the caller must be able to view the source
 * map; 409 when the source map is gone or the token is revoked/unrecoverable. */
export async function regenerateFieldProject(
  id: string,
  accessToken: string,
): Promise<FieldProjectSummary & { warnings: string[] }> {
  const res = await fetch(`${API_BASE_URL}/field-projects/${encodeURIComponent(id)}/regenerate`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as FieldProjectSummary & { warnings: string[] }
}

/** PATCH /field-projects/{id} — rename / re-describe (owner or admin). Deliberately does NOT
 * bump the artifact's Last-Modified (a rename must not defeat crews' 304s). */
export async function patchFieldProject(
  id: string,
  payload: { title?: string; description?: string },
  accessToken: string,
): Promise<void> {
  const res = await fetch(`${API_BASE_URL}/field-projects/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify(payload),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
}

/** DELETE /field-projects/{id} — owner/admin; the server purges the stored artifact. */
export async function deleteFieldProject(id: string, accessToken: string): Promise<void> {
  const res = await fetch(`${API_BASE_URL}/field-projects/${encodeURIComponent(id)}`, {
    method: 'DELETE',
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
}

/** GET /field-projects/{id}/archive — the artifact bytes for the portal download button (the
 * endpoint needs a bearer header, so callers fetch the Blob and save via an object URL). */
export async function downloadFieldProject(id: string, accessToken: string): Promise<Blob> {
  const res = await fetch(`${API_BASE_URL}/field-projects/${encodeURIComponent(id)}/archive`, {
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return res.blob()
}

/** A field project's current share state. publicAccess is always false — the server refuses a
 * public tier outright (the artifact may embed access credentials; decision F3). */
export async function fetchFieldProjectShares(id: string, accessToken: string): Promise<RasterShares> {
  const res = await fetch(`${API_BASE_URL}/field-projects/${encodeURIComponent(id)}/shares`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as RasterShares
}

/** PUT /field-projects/{id}/shares — set/replace org + group sharing (owner or admin). */
export async function setFieldProjectShares(
  id: string,
  payload: { organizationOnly: boolean; groups: string[] },
  accessToken: string,
): Promise<RasterShares> {
  const res = await fetch(`${API_BASE_URL}/field-projects/${encodeURIComponent(id)}/shares`, {
    method: 'PUT',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify({ ...payload, publicAccess: false }),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as RasterShares
}

// --- editing toggle (decision-editing-toggle.md) --------------------------------
// The owner-controlled SOFT read-only switch (layer-only — no web-map/raster analog), enforced server-side in
// edit_allowed. When off, regular editors lose `canEdit` (affordances + writes 403); a talus-admin still edits
// via the existing editViaAdmin notice. Read/write is owner-or-admin, mirroring delete-protection.

function _layerEditingUrl(layerId: string): string {
  return `${API_BASE_URL}/layers/${encodeURIComponent(layerId)}/editing`
}

/** GET /layers/{id}/editing — whether editing is enabled on the layer (owner or admin). */
export async function fetchLayerEditing(layerId: string, accessToken: string): Promise<LayerEditing> {
  const res = await fetch(_layerEditingUrl(layerId), {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as LayerEditing
}

/** PUT /layers/{id}/editing — the master switch + the four capability flags (owner or admin; V109). Returns
 *  the saved state. `editOps` may be omitted to write the switch alone (the server leaves the flags as they are). */
export async function setLayerEditing(
  layerId: string,
  body: { editingEnabled: boolean; editOps?: EditOps },
  accessToken: string,
): Promise<LayerEditing> {
  const res = await fetch(_layerEditingUrl(layerId), {
    method: 'PUT',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as LayerEditing
}

// --- attachments toggle (decision-attachments.md A-1) ---------------------------
// The owner-controlled per-layer "allow attachments" switch, sibling to editing in Manage → Settings.
// DEFAULT off (opt-in). Enforced server-side (HARD) in the attachment write gate. Read/write is owner-or-admin.

function _layerAttachmentsUrl(layerId: string): string {
  return `${API_BASE_URL}/layers/${encodeURIComponent(layerId)}/attachments-enabled`
}

/** GET /layers/{id}/attachments-enabled — whether attachments are enabled on the layer (owner or admin). */
export async function fetchLayerAttachmentsEnabled(layerId: string, accessToken: string): Promise<boolean> {
  const res = await fetch(_layerAttachmentsUrl(layerId), {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return ((await res.json()) as { attachmentsEnabled: boolean }).attachmentsEnabled
}

/** PUT /layers/{id}/attachments-enabled — allow/disallow attachments on the layer (owner or admin). */
export async function setLayerAttachmentsEnabled(
  layerId: string,
  attachmentsEnabled: boolean,
  accessToken: string,
): Promise<boolean> {
  const res = await fetch(_layerAttachmentsUrl(layerId), {
    method: 'PUT',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify({ attachmentsEnabled }),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return ((await res.json()) as { attachmentsEnabled: boolean }).attachmentsEnabled
}

// --- display field (decision-layer-display-field.md) ----------------------------
// The layer's "which attribute names a feature" setting, set by starring an attribute-table column. The GET
// returns the RAW value (null = unset; the map uses the EFFECTIVE value from /me/layers) + canManage so the
// star affordance renders honestly for viewers. The PUT is manage-gated (owner/admin); null clears the star.

/** RAW display field of a layer + whether this caller may change it (GET /layers/{id}/display-field). */
export interface LayerDisplayField {
  displayField: string | null
  canManage: boolean
}

function _layerDisplayFieldUrl(layerId: string): string {
  return `${API_BASE_URL}/layers/${encodeURIComponent(layerId)}/display-field`
}

/** GET /layers/{id}/display-field — the layer's raw starred field + this caller's manage right. */
export async function fetchLayerDisplayField(
  layerId: string,
  accessToken: string,
): Promise<LayerDisplayField> {
  const res = await fetch(_layerDisplayFieldUrl(layerId), {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as LayerDisplayField
}

/** PUT /layers/{id}/display-field — star `displayField` (a real field of the layer) or clear it with null.
 *  Owner/admin only (403 otherwise); a non-existent field is a 400. Returns the saved value. */
export async function setLayerDisplayField(
  layerId: string,
  displayField: string | null,
  accessToken: string,
): Promise<LayerDisplayField> {
  const res = await fetch(_layerDisplayFieldUrl(layerId), {
    method: 'PUT',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify({ displayField }),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as LayerDisplayField
}

/**
 * In-map feature search (decision-feature-search.md) — GET /search fans out over the given layer
 * ids (the map's visible layers), each view-gated server-side (fail-closed). `q` uses websearch
 * syntax (phrase "...", space = AND, OR, -NOT). Returns hits grouped-by-layer via `counts`; the
 * caller aborts an in-flight request on each keystroke (last-write-wins), exactly like geocode.
 */
export async function searchFeatures(
  q: string,
  layerIds: string[],
  accessToken: string,
  signal?: AbortSignal,
): Promise<FeatureSearchResponse> {
  const qs = new URLSearchParams({ q, layers: layerIds.join(',') })
  const res = await fetch(`${API_BASE_URL}/search?${qs.toString()}`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
    signal,
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as FeatureSearchResponse
}

// --- external catalog items (decision-external-catalog-items.md, V060) ----------
//
// A referenced external service promoted to a shareable catalog item. Read-only pointer: the spec
// mirrors ExternalLayerSpec (addFromUrl.ts); the VIEWER'S browser fetches the url directly. Maps
// store the item id + a snapshot (hybrid resolution) — see fetchExternalRef.

export type ExternalRefKind = 'arcgis-feature' | 'geojson' | 'xyz' | 'arcgis-image'

/** One reference item, full spec — the resolution payload AND the picker-add seed. */
export interface ExternalRef {
  id: string
  title: string
  description: string | null
  externalKind: ExternalRefKind
  url: string
  geometryType?: 'point' | 'line' | 'polygon' | null
  color?: string | null
  where?: string | null
  updated_at: string
}

/** A /me/external-refs listing row: the full spec + the caller's capabilities (the reference
 * analog of MyLayer — own shape, fork §3.7, so /me/layers stays clean of inapplicable fields). */
export interface ExternalRefSummary extends ExternalRef {
  owner: boolean
  sharedWithMe: boolean
  favorite: boolean
  canManage: boolean
  shareLevel?: ShareLevel | null
  ownerId?: string | null
  ownerName?: string | null
}

/** GET /me/external-refs — the references the caller may view, with capabilities. */
export async function fetchMyExternalRefs(accessToken: string): Promise<ExternalRefSummary[]> {
  const res = await fetch(`${API_BASE_URL}/me/external-refs`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return ((await res.json()) as { refs: ExternalRefSummary[] }).refs
}

export interface ExternalRefPayload {
  title: string
  description?: string | null
  url: string
  geometryType?: 'point' | 'line' | 'polygon' | null
  color?: string | null
  where?: string | null
}

/** POST /external-refs — create a reference item (publisher; the browser probe already validated
 * the service — the server only shape-validates, per the no-proxy fence). */
export async function createExternalRef(
  payload: ExternalRefPayload & { externalKind: ExternalRefKind },
  accessToken: string,
): Promise<ExternalRef> {
  const res = await fetch(`${API_BASE_URL}/external-refs`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify(payload),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as ExternalRef
}

/** GET /external-refs/{id} — resolve one reference to its CURRENT spec (view-gated, 404-on-deny).
 * The map-open resolution call: a live item's spec wins over the op's snapshot; a 404 (deleted OR
 * unshared — indistinguishable by design) means "fall back to the snapshot". */
export async function fetchExternalRef(id: string, accessToken: string): Promise<ExternalRef> {
  const res = await fetch(`${API_BASE_URL}/external-refs/${encodeURIComponent(id)}`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as ExternalRef
}

/** PUT /external-refs/{id} — update display fields / url / where (owner or admin). Kind is
 * immutable (a different protocol is a different reference). */
export async function updateExternalRef(
  id: string,
  payload: ExternalRefPayload,
  accessToken: string,
): Promise<ExternalRef> {
  const res = await fetch(`${API_BASE_URL}/external-refs/${encodeURIComponent(id)}`, {
    method: 'PUT',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify(payload),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as ExternalRef
}

/** DELETE /external-refs/{id} — plain delete (owner or admin); referencing maps heal via their
 * op snapshot, so this is harmless to saved maps. */
export async function deleteExternalRef(id: string, accessToken: string): Promise<void> {
  const res = await fetch(`${API_BASE_URL}/external-refs/${encodeURIComponent(id)}`, {
    method: 'DELETE',
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
}

/** A reference's share state — the LayerShares wire shape (org/groups/public). */
export interface ExternalRefShares {
  organizationOnly: boolean
  groups: ShareGroup[]
  publicAccess: boolean
}

/** GET /external-refs/{id}/shares — current share state (owner or admin). */
export async function fetchExternalRefShares(
  id: string,
  accessToken: string,
): Promise<ExternalRefShares> {
  const res = await fetch(`${API_BASE_URL}/external-refs/${encodeURIComponent(id)}/shares`, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as ExternalRefShares
}

/** PUT /external-refs/{id}/shares — set/replace org/group/public sharing (owner or admin). */
export async function setExternalRefShares(
  id: string,
  payload: { organizationOnly: boolean; groups: string[]; publicAccess: boolean },
  accessToken: string,
): Promise<ExternalRefShares> {
  const res = await fetch(`${API_BASE_URL}/external-refs/${encodeURIComponent(id)}/shares`, {
    method: 'PUT',
    headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS },
    body: JSON.stringify(payload),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as ExternalRefShares
}

/** Pull FastAPI's `{ "detail": ... }` message out of an error body when present. */
/**
 * The message the user sees, plus — on a server fault — the error code they can paste into the Helper:
 * the first 8 characters of the api's request id,
 * which every log record of that request carries. 4xx answers are the user's own input; no code there.
 */
async function errorDetail(res: Response): Promise<string> {
  const text = await errorDetailText(res)
  const rid = res.status >= 500 ? res.headers.get('x-request-id') : null
  return rid ? `${text} (error code ${rid.slice(0, 8)})` : text
}

async function errorDetailText(res: Response): Promise<string> {
  try {
    const body: unknown = await res.json()
    if (body !== null && typeof body === 'object' && 'detail' in body) {
      const detail = (body as { detail: unknown }).detail
      if (typeof detail === 'string') return detail
      // A structured refusal ({ reason, message } — the SQL-source doors): show the sentence.
      if (detail !== null && typeof detail === 'object' && 'message' in detail) {
        const m = (detail as { message: unknown }).message
        if (typeof m === 'string') return m
      }
    }
  } catch {
    // non-JSON body — fall through to the generic message
  }
  return `Request failed with status ${res.status}`
}

// --- SQL-source pull (decision-sql-source-pull.md s1 — Admin → Database sources) --------------
//
// Read-only copies of tables that OTHER systems own, inside Talus. Every door is admin-only
// server-side; the password is write-only (never echoed — `hasPassword` is all that comes back).

export type SqlDialect = 'postgresql' | 'sqlserver' | 'mysql' | 'oracle'
export type SqlTlsMode = 'verify-full' | 'require' | 'disable'

export interface SqlDialectInfo {
  name: SqlDialect
  defaultPort: number
  available: boolean
  unavailableReason: string | null
}

export interface SqlConnection {
  id: string
  name: string
  ownerId: string
  ownerName?: string
  dialect: SqlDialect
  dialectAvailable: boolean
  host: string
  port: number
  database: string
  username: string
  tlsMode: SqlTlsMode
  hasCaBundle: boolean
  timezone: string
  hasPassword: boolean
  passwordUnavailable: boolean
  lastTestedAt: string | null
  lastTestOk: boolean | null
  lastTestError: string | null
  createdAt: string | null
  updatedAt: string | null
}

export interface SqlConnectionWrite {
  name: string
  dialect: SqlDialect
  host: string
  port: number
  database: string
  username: string
  /** A new password replaces the stored one; omit to keep it; `clearPassword` removes it. */
  password?: string
  clearPassword?: boolean
  tlsMode: SqlTlsMode
  caPem?: string
  clearCaBundle?: boolean
  timezone: string
}

export interface SqlTestResult {
  ok: boolean
  reason: string | null
  message: string | null
  tables: number | null
}

export interface SqlCatalogTable {
  schema: string
  name: string
  kind: 'table' | 'view'
  keyColumns: string[]
}

export interface SqlCatalogColumn {
  name: string
  sourceType: string
  dataType: 'text' | 'number' | 'date' | 'boolean' | null
  numericSubtype: string | null
  temporal: boolean
  unsupportedReason: string | null
}

export interface SqlPullRun {
  id: string
  pullId: string
  status: 'pending' | 'running' | 'completed' | 'failed' | 'canceled'
  trigger: 'manual' | 'schedule'
  totalRows: number | null
  processedRows: number
  insertedRows: number
  failedRows: number
  updatedRows: number
  deletedRows: number
  unchangedRows: number
  errorSample: { row: number; reason: string }[]
  error: string | null
  columnStats: Record<string, { source: number; target: number }> | null
  createdAt: string | null
  startedAt: string | null
  finishedAt: string | null
}

export interface SqlPullColumnPlan {
  name: string
  sourceType: string
  dataType: 'text' | 'number' | 'date' | 'boolean'
  numericSubtype: string | null
  temporal: boolean
  utc: boolean
  fieldKey: string
}

export interface SqlPullGeometry {
  kind: 'xy'
  x: string
  y: string
  srid: number | null
}

export interface SqlPullSchedule {
  kind: 'manual' | 'nightly' | 'interval'
  hour: number | null
  minutes: number | null
}

export interface SqlPull {
  id: string
  ownerId: string
  ownerName?: string
  connectionId: string
  connectionName: string
  targetLayerId: string
  targetName: string
  schema: string
  table: string
  columns: SqlPullColumnPlan[]
  keyColumn: string | null
  /** null = a non-spatial table; xy = a point layer built from two coordinate columns (s2). */
  geometry: SqlPullGeometry | null
  targetGeomType: 'none' | 'point' | 'line' | 'polygon'
  /** s3: manual, nightly at an hour in the connection's time zone, or every N minutes (≥ 60). */
  schedule: SqlPullSchedule
  nextRunAt: string | null
  /** s3: set by a failed run (the copy is untouched), cleared by the next success. */
  staleSince: string | null
  lastSuccessAt: string | null
  lastError: string | null
  createdAt: string | null
  lastRun: SqlPullRun | null
}

export interface SqlPullCreate {
  connectionId: string
  schema: string
  table: string
  columns: { name: string; utc?: boolean; toDate?: boolean }[]
  keyColumn?: string | null
  name: string
  geometry?: SqlPullGeometry | null
  schedule?: SqlPullSchedule
}

function _auth(accessToken: string): Record<string, string> {
  return { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' }
}

export async function fetchSqlDialects(accessToken: string): Promise<SqlDialectInfo[]> {
  const res = await fetch(`${API_BASE_URL}/sql-connections/dialects`, { headers: _auth(accessToken) })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as SqlDialectInfo[]
}

/** GET /sql-connections — MY connections (publisher). With `all`, every connection in the org, read-only
 *  (admin only — host and owner, never a secret). */
export async function fetchSqlConnections(accessToken: string, all = false): Promise<SqlConnection[]> {
  const res = await fetch(`${API_BASE_URL}/sql-connections${all ? '/all' : ''}`, { headers: _auth(accessToken) })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as SqlConnection[]
}

export async function createSqlConnection(body: SqlConnectionWrite, accessToken: string): Promise<SqlConnection> {
  const res = await fetch(`${API_BASE_URL}/sql-connections`, {
    method: 'POST', headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS }, body: JSON.stringify(body),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as SqlConnection
}

export async function updateSqlConnection(id: string, body: SqlConnectionWrite, accessToken: string): Promise<SqlConnection> {
  const res = await fetch(`${API_BASE_URL}/sql-connections/${id}`, {
    method: 'PUT', headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS }, body: JSON.stringify(body),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as SqlConnection
}

export async function deleteSqlConnection(id: string, accessToken: string): Promise<void> {
  const res = await fetch(`${API_BASE_URL}/sql-connections/${id}`, { method: 'DELETE', headers: _auth(accessToken) })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
}

export async function testSqlConnection(id: string, accessToken: string): Promise<SqlTestResult> {
  const res = await fetch(`${API_BASE_URL}/sql-connections/${id}/test`, { method: 'POST', headers: _auth(accessToken) })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as SqlTestResult
}

export async function fetchSqlCatalog(id: string, accessToken: string): Promise<SqlCatalogTable[]> {
  const res = await fetch(`${API_BASE_URL}/sql-connections/${id}/catalog`, { headers: _auth(accessToken) })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as SqlCatalogTable[]
}

export async function fetchSqlCatalogColumns(
  id: string, schema: string, table: string, accessToken: string,
): Promise<{ table: SqlCatalogTable; columns: SqlCatalogColumn[]; xyCandidates: SqlPullGeometry | null }> {
  const res = await fetch(
    `${API_BASE_URL}/sql-connections/${id}/catalog/${encodeURIComponent(schema)}/${encodeURIComponent(table)}`,
    { headers: _auth(accessToken) },
  )
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as { table: SqlCatalogTable; columns: SqlCatalogColumn[]; xyCandidates: SqlPullGeometry | null }
}

export async function fetchSqlPulls(accessToken: string, all = false): Promise<SqlPull[]> {
  const res = await fetch(`${API_BASE_URL}/sql-pulls${all ? '/all' : ''}`, { headers: _auth(accessToken) })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as SqlPull[]
}

export async function createSqlPull(body: SqlPullCreate, accessToken: string): Promise<SqlPull> {
  const res = await fetch(`${API_BASE_URL}/sql-pulls`, {
    method: 'POST', headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS }, body: JSON.stringify(body),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as SqlPull
}

export async function deleteSqlPull(id: string, accessToken: string): Promise<void> {
  const res = await fetch(`${API_BASE_URL}/sql-pulls/${id}`, { method: 'DELETE', headers: _auth(accessToken) })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
}

export async function setSqlPullSchedule(id: string, schedule: SqlPullSchedule, accessToken: string): Promise<SqlPull> {
  const res = await fetch(`${API_BASE_URL}/sql-pulls/${id}/schedule`, {
    method: 'PUT', headers: { Authorization: `Bearer ${accessToken}`, ..._JSON_HEADERS }, body: JSON.stringify(schedule),
  })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as SqlPull
}

export async function runSqlPull(id: string, accessToken: string): Promise<SqlPullRun> {
  const res = await fetch(`${API_BASE_URL}/sql-pulls/${id}/run`, { method: 'POST', headers: _auth(accessToken) })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as SqlPullRun
}

export async function fetchSqlPullRun(id: string, accessToken: string): Promise<SqlPullRun> {
  const res = await fetch(`${API_BASE_URL}/sql-pull-runs/${id}`, { headers: _auth(accessToken) })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as SqlPullRun
}

export async function cancelSqlPullRun(id: string, accessToken: string): Promise<SqlPullRun> {
  const res = await fetch(`${API_BASE_URL}/sql-pull-runs/${id}/cancel`, { method: 'POST', headers: _auth(accessToken) })
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res))
  return (await res.json()) as SqlPullRun
}
