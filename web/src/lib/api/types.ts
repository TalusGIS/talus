/**
 * Types mirroring the FastAPI feature responses.
 * Source of truth: api/app/models/geojson.py.
 *
 * Note: dates/datetimes arrive as JSON strings over the wire (Python `date` ->
 * "YYYY-MM-DD", `datetime` -> ISO-8601). The response is a plain GeoJSON
 * FeatureCollection — there is no `bbox` on the response; bbox is a request param.
 */

export interface PointGeometry {
  type: 'Point'
  coordinates: [number, number] | [number, number, number]
}

/** GeoJSON Feature, generic over its properties (defaults to slot props). */
export interface Feature<P = SlotProperties> {
  type: 'Feature'
  id: string | null
  geometry: PointGeometry
  properties: P
}

/** GeoJSON FeatureCollection, generic over feature properties (defaults to slot props). */
export interface FeatureCollection<P = SlotProperties> {
  type: 'FeatureCollection'
  features: Feature<P>[]
}

/**
 * Publisher slot feature properties (assets.tal_<8hex>, V019). The slot schema is lean
 * — id/geom/edit-tracking/notes only, NO attribute picklists — so the SPA reads just
 * these. The generic slot CRUD lives at /layers/{id}/features[/{fid}] (Talus 10).
 */
export interface SlotProperties {
  id: string
  geometry?: unknown
  notes: string | null
  // Owner-defined custom field values (V044), keyed by fieldKey. Present on the GET-one read.
  props?: Record<string, unknown>
  created_at?: string // ISO datetime
  updated_at?: string // ISO datetime
  ai_provenance?: unknown
  version?: string // opaque optimistic-concurrency token (row_version); echoed via If-Match (T34)
}

/** POST /layers/{id}/features body — geometry + optional notes + owner-defined field values. Default layers
 *  send {lon,lat} (WGS-84); native-storage layers send {x,y} in their CRS (E3, decision-coord-entry.md). */
export interface SlotCreate {
  lon?: number
  lat?: number
  x?: number
  y?: number
  // Line/polygon layers (decision-line-polygon-layers.md c4): a GeoJSON LineString/Polygon geometry instead of
  // lon/lat·x/y. Coords are in the layer's storage CRS (4326 for a default layer; terra-draw draws in WGS-84).
  geometry?: unknown
  notes: string | null
  props?: Record<string, unknown>
}

/** PATCH /layers/{id}/features/{fid} body — MOVE sends {lon,lat} (default) or {x,y} (native); edit sends
 *  {notes} and/or {props} (a partial props patch is merged onto the row's existing props server-side). */
export interface SlotUpdate {
  lon?: number
  lat?: number
  x?: number
  y?: number
  // Line/polygon RESHAPE (decision-line-polygon-layers.md c6b): a replacement GeoJSON geometry, WGS-84
  // as terra-draw emits it. The PATCH door has accepted it since c6b (SlotFeatureUpdate.geometry); the
  // portal passed it through an untyped write payload, so the type only caught up when the Talus+
  // field app called `update()` with it directly.
  geometry?: unknown
  notes?: string | null
  props?: Record<string, unknown>
}



export interface RendererStop {
  value: string
  color: string
  label?: string | null
}

/** The portable layer-default symbology spec (Talus 13, persisted as platform.layers.renderer).
 * `simple` carries one `symbol.color`; `uniqueValue` carries `field` + `stops` (+ a `default`
 * «No value»/other bucket). Both the map paint and the read-only legend resolve from this; the
 * future per-user saved-map override stores the SAME shape, resolved ahead of it. */
export interface Renderer {
  type: 'simple' | 'uniqueValue'
  symbol?: { color: string } | null
  field?: string | null
  stops?: RendererStop[] | null
  default?: { color: string; label?: string | null } | null
  minZoom?: number | null // forward hook; no behavior yet
  // c7 (decision-line-polygon-layers.md §8) — LAYER-WIDE geometry style (color varies by category, these
  // don't). Applied per geometry; absent => today's defaults. Line: width + dash. Polygon: fill + outline.
  lineWidth?: number | null
  lineDash?: number[] | null // MapLibre line-dasharray (e.g. [2, 2]); null/absent = solid
  fillOpacity?: number | null
  outlineColor?: string | null
  outlineWidth?: number | null
  // POINT size, stored as the rendered DIAMETER in px — not MapLibre's radius. ArcGIS and QGIS both
  // mean diameter by "size", and the QField generator's marker `size` is a diameter too, so storing
  // a radius here would put a silent x2 in the field-project export. Halved at the one paint site.
  pointSize?: number | null
  // Bundled marker name (features/map/point-symbols). Absent => the plain circle. Server-validated
  // against the generated allowlist, so an unknown name never reaches a saved renderer.
  pointIcon?: string | null
}

/** One layer's per-user capabilities (GET /me/layers). Layer 7b: the access key.
 * displayName/displayColor (Talus 10) are always present in the response but null for
 * the 3 real layers and unclaimed slots (their color comes from the static LayerDef).
 * renderer (Talus 13) is the layer-default symbology, null when none is set/seeded. */
/** Current sharing level for the catalog share-status badge ('owner' = private). Shared by
 *  MyLayer and WebMapSummary so the badge is identical across catalog item types (Talus 24). */
export type ShareLevel = 'owner' | 'organization' | 'groups' | 'public'

/** A layer's geometry family (decision-line-polygon-layers.md, V055). The friendly enum — storage may be
 *  Multi* for line/polygon — that the map branches on (circle / line / fill+outline) and the wizard offers. */
export type GeometryType = 'point' | 'line' | 'polygon'

/** The four per-layer edit capabilities (V109, decision-layer-edit-capabilities.md): what anyone with edit
 *  access may do on the layer. The owner sets them in Manage ▸ Settings ▸ Editing; every write door enforces
 *  them (a flag can only refuse), so the UI HIDES the affordance and never pre-empts the server. */
export interface EditOps {
  add: boolean
  attributes: boolean
  geometry: boolean
  delete: boolean
}

export const ALL_EDIT_OPS: EditOps = { add: true, attributes: true, geometry: true, delete: true }

/** A layer's flags, defaulting to all-on for a row that carries none (an older cached read). */
export function editOpsOf(layer: { editOps?: EditOps | null } | undefined | null): EditOps {
  return layer?.editOps ?? ALL_EDIT_OPS
}

/** GET/PUT /layers/{id}/editing — the master switch + the four flags (one door, one settings screen). */
export interface LayerEditing {
  editingEnabled: boolean
  editOps: EditOps
}

export interface MyLayer {
  id: string
  canView: boolean
  canEdit: boolean
  // editViaAdmin: canEdit holds ONLY because the caller is a talus-admin (no genuine edit access). The edit
  // form shows a "editing is restricted but you have admin privileges" notice when true (admin-edit-all).
  editViaAdmin?: boolean
  // V109: the owner's four capability flags (the same set for everyone with edit access). Describe the LAYER,
  // not the caller — present even when canEdit is false. Absent on an older read → treat as all-on (editOpsOf).
  editOps?: EditOps
  canManage: boolean // Talus 13: may set/clear the layer-default symbology (owner/admin)
  // Talus 18: does the caller OWN this layer (OpenFGA owner relation)? Drives the "Owned by me" scope.
  // Distinct from canManage (owner OR admin).
  owner: boolean
  // Is this layer viewable via a GENUINE share (group/org/public/direct), not owned? Drives "Shared with me".
  // For a regular caller == !owner; for an ADMIN (who sees ALL layers) it's false for admin-override-only
  // layers, so those land in "All" only (admin-sees-all, 2026-06-30). Fall back to !owner if absent.
  sharedWithMe?: boolean
  // Talus 18: has the caller starred this layer? Per-caller (platform.user_favorites).
  // Drives the ★ Favorites filter and the per-row star toggle.
  favorite: boolean
  // Talus 24: the layer's sharing level for the catalog share-status badge (parity with maps).
  shareLevel?: ShareLevel | null
  // Talus 24: last-modified timestamp (ISO-8601) — backs the catalog modified-date column and
  // the Modified sort (parity with WebMapSummary.updated_at).
  updated_at: string
  // Talus 24 c4: delete-protection flag — drives the at-a-glance padlock on the row's delete action.
  deleteProtected: boolean
  // Talus 26: the layer owner's id + display name (null for ownerless system layers). Names the
  // "ask the owner" permission notice and the catalog owner column.
  ownerId?: string | null
  ownerName?: string | null
  // Talus 36: live feature count of the layer's backing assets table — null when the layer has no
  // backing table (unclaimed slot / not yet provisioned). Drives the zero-features health color
  // (count === 0 → orange layer icon); the number itself is not shown.
  featureCount?: number | null
  editable: boolean
  displayName: string | null
  displayColor: string | null
  renderer: Renderer | null
  // Talus 38: how the SPA fetches this layer's geometry — 'tiles' (MVT, the default for every
  // pre-V038 layer) or 'features' (a view layer, rendered as a GeoJSON source off
  // /layers/{id}/features). Absent/'tiles' for all non-view layers.
  renderMode?: 'tiles' | 'features'
  // Talus 38: for a view layer, the source it filters + the source's display name (the catalog
  // "view of <source>" lineage). BOTH null unless the caller can also view the source — the api
  // never leaks a private source's identity to a holder of just the view.
  sourceLayerId?: string | null
  sourceName?: string | null
  // Native-CRS storage (decision-crs-native-slice1.md): true = geometry stored byte-exact in workingSrid
  // (survey/high-accuracy). Drives the Manage "native storage" indicator. Default layers: false / 4326.
  nativeStorage?: boolean
  workingSrid?: number
  // Geometry family (decision-line-polygon-layers.md, V055): 'point' | 'line' | 'polygon'. Drives the map
  // layer type (circle / line / fill+outline) + the catalog/wizard geometry display. Default 'point'.
  // 'none' = a standalone non-spatial TABLE (decision-nonspatial-tables.md): surfaced under the Catalog
  // Tables tab, excluded from the map; rows are edited via the Data grid, not a map click.
  geomType?: GeometryType | 'none'
  // The layer's EFFECTIVE display field (decision-layer-display-field.md, V072): the attribute that names a
  // feature in the popup title + Locate result labels. The starred column when set, else a server-derived
  // first-text-field default (never 'id'). Feeds buildSlotLayerDef.popupTitleField. Distinct from the RAW
  // value the attribute-table star reflects (GET /layers/{id}/display-field) — that shows a filled star only
  // on an explicit choice. null only when the layer has no text field at all.
  displayField?: string | null
  // Whether the layer accepts feature attachments (V068 toggle; decision-attachments-in-edit-form.md).
  // Gates the edit form's attachment-staging control. Absent/false = attachments off (or a view).
  attachmentsEnabled?: boolean
  // formConfig (V104, decision-talus-field-f2.md): the layer's declarative form document. Null/absent = the
  // default form (every active field, one group, no rules). Rendered by slotFormModel.applyFormRules for
  // both the desk form and the Talus+ field app; authored on the map's Form tab via PUT /layers/{id}/form.
  formConfig?: FormConfig | null
}

// --- the layer form document (decision-talus-field-f2.md §4.1) — a CLOSED vocabulary, data not code ----

/** ONE sibling field, ONE operator. `eq` is the code / string / boolean; `in` is a code set. */
export type FormCondition =
  | { field: string; eq: string | boolean }
  | { field: string; in: string[] }
  | { field: string; isEmpty: true }
  | { field: string; notEmpty: true }

export interface FormFieldRules {
  visibleIf?: FormCondition
  requiredIf?: FormCondition
  readOnly?: true
}

export interface FormGroup {
  title: string
  fields: string[]
  collapsed?: boolean
}

export interface FormTemplate {
  id: string
  name: string
  defaults: Record<string, unknown>
}

export interface FormConfig {
  version?: 1
  groups?: FormGroup[]
  fields?: Record<string, FormFieldRules>
  templates?: FormTemplate[]
  photo?: { prompt: boolean }
}

/** GET /me/layers — only layers the caller can view are present. */
export interface MyLayersResponse {
  layers: MyLayer[]
}

// --- view layers (Talus 38) ----------------------------------------------------

/** One column of a view's SOURCE (GET /layer-views/sources/{id}/columns). `auto` columns (id, geom)
 *  are always included in the view regardless of the allow-list; `geometry` flags the geom column. */
export interface ViewSourceColumn {
  name: string
  type: string
  auto: boolean
  geometry: boolean
  /** An owner-defined field of the source — picked, it becomes a typed computed column of the view. */
  field?: boolean
  /** The owner's label; present on field entries. */
  label?: string
  /** A coded field's V046 custom-domain id; present on coded field entries. */
  customDomain?: string
}

export interface ViewSourceColumnsResponse {
  sourceLayerId: string
  columns: ViewSourceColumn[]
}

/** The view filter op set — the SAME validated grammar the feature endpoint filters with. */
export type ViewFilterOp = 'in' | 'eq' | 'gte' | 'lte' | 'ilike'

/** One baked filter condition (field:op:value) for POST /layer-views. */
export interface ViewFilterInput {
  field: string
  op: ViewFilterOp
  value: string
}

/** V080 join recipe (decision-relates-and-join-views.md PR B): one LEFT JOIN of a standalone
 *  table on an equality key pair, 1:first by `sortKey`/`sortDir`. Every joined output column is
 *  prefixed `<alias>_<key>` server-side (collision-proof). Deep validation is server-side. */
export interface ViewJoinInput {
  joinTableId: string
  layerKey: string
  tableKey: string
  alias: string
  columns: string[]
  sortKey: string
  sortDir: 'asc' | 'desc'
}

/** GET /layer-views/join-tables/{id}/columns — what a join may key/sort/expose on the table side
 *  (real scalar columns + active owner-defined field keys). Admin-or-table-owner gated. */
export interface JoinTableColumnsResponse {
  tableId: string
  columns: { name: string; type: string }[]
  fields: { key: string; type: string; label: string }[]
}

/** POST /layer-views body — author a sanitized view of `sourceLayerId`. */
export interface CreateViewPayload {
  sourceLayerId: string
  displayName: string
  displayColor?: string | null
  allowedColumns: string[]
  filters: ViewFilterInput[]
  join?: ViewJoinInput | null
}

/** POST /layer-views response — the new view layer's summary. */
export interface CreateViewResponse {
  layer_id: string
  source_layer_id: string
  display_name: string
  display_color: string | null
  allowed_columns: string[]
  join_table_id?: string | null
  joined_columns?: string[]
  renderMode: string
  status: string
}

/** One symbolizable field (GET /layers/{id}/fields). `customDomain` is the bound V046 domain
 * id when the field is coded, else null. (A second marker, `domain`, once named a BESPOKE
 * layer's FK-target code table; those layers retired in decision-retire-builtins.md phase 2.) */
export interface SymbolizableField {
  field: string
  type: string
  customDomain?: string | null
}

export interface SymbolizableFieldsResponse {
  layer_id: string
  fields: SymbolizableField[]
}

// --- table↔layer relates (decision-relates-and-join-views.md PR A; /layers/{id}/relates) ----------

/** One relate: navigation metadata connecting an origin (spatial) layer to a standalone table on a
 * key pair. The server lists a relate ONLY when the caller can view the related table (fail-closed
 * omission) — a relate never widens access; related ROWS are read via the table's own gated
 * features endpoint filtered on the key. */
export interface LayerRelate {
  id: string
  originLayerId: string
  originFieldKey: string
  relatedTableId: string
  relatedTableName: string
  relatedFieldKey: string
  displayName: string
  cardinality: '1:1' | '1:M'
  sortOrder: number
}

export interface RelateSeed {
  relatedTableId: string
  originFieldKey: string
  relatedFieldKey: string
  displayName: string
  cardinality?: '1:1' | '1:M'
  sortOrder?: number
}

/** Display metadata only — the ends and key pair are immutable (delete + re-add to rekey). */
export interface RelatePatch {
  displayName?: string
  cardinality?: '1:1' | '1:M'
  sortOrder?: number
}

// --- owner-defined custom fields (data-owner-independence arc; /layers/{id}/custom-fields) --------

export type FieldDataType = 'text' | 'number' | 'date' | 'boolean' | 'coded'

/** Refines a `number` field (decision-create-wizard.md §4): integer kinds enforce whole-number + bit-range;
 * float/double differ only in export column type. Null/absent = a generic number. */
export type NumericSubtype = 'short_integer' | 'long_integer' | 'float' | 'double'

/** 'today' resolves to the current date on write (date fields only); 'constant' uses defaultValue. */
/** 'now' (ISO-8601 UTC timestamp) and 'current_user' (the acting user's display name) are text-only tokens
 *  (V104, decision-talus-field-f2.md fork 2); like 'today' they resolve on write, server-side. */
export type DefaultKind = 'constant' | 'today' | 'now' | 'current_user'

/**
 * Declarative validation rules beyond type + domain (slice 3, decision-constraint-rules.md). Only the
 * keys relevant to the field's data_type are present: range (min/max) on number+date, length
 * (minLength/maxLength) + pattern (an anchored regex) on text. `pattern` is match/reject only.
 */
export interface FieldConstraints {
  min?: number | string // number value, or ISO date string for date fields
  max?: number | string
  minLength?: number
  maxLength?: number
  pattern?: string
}

/**
 * Contingent values (V077, decision-contingent-values.md): a coded child field's picklist depends on ONE
 * coded sibling parent field — parentCode -> the child codes allowed under it. A parent value with no
 * entry (and an empty/absent parent) is unrestricted; the pair is trigger-enforced server-side.
 */
export type ContingentValues = Record<string, string[]>

/** One owner-defined custom field on a slot layer. Values live in each feature's `props`. */
export interface CustomField {
  id: string
  fieldKey: string
  label: string
  dataType: FieldDataType
  required: boolean
  defaultValue: unknown | null
  sortOrder: number
  active: boolean // false = soft-retired (hidden from forms; existing values preserved)
  domainId: string | null // the bound coded-value domain (slice 2); null unless dataType === 'coded'
  constraints: FieldConstraints | null // slice 3; null = no extra rules
  defaultKind: DefaultKind
  numericSubtype: NumericSubtype | null // refines a number field; null otherwise
  parentFieldKey: string | null // contingency (V077): the coded sibling this field depends on
  contingentValues: ContingentValues | null // set iff parentFieldKey is
  createdAt: string
  updatedAt: string
}

export interface CustomFieldsResponse {
  layerId: string
  fields: CustomField[]
}

/** POST body — define a new field (fieldKey + dataType are immutable after create). */
export interface CustomFieldCreate {
  fieldKey: string
  label: string
  dataType: FieldDataType
  required?: boolean
  defaultValue?: unknown | null
  sortOrder?: number
  domainId?: string // required iff dataType === 'coded' (the bound domain, slice 2)
  constraints?: FieldConstraints | null // slice 3; validated server-side against dataType
  defaultKind?: DefaultKind // 'today' = date fields only; default 'constant'
  numericSubtype?: NumericSubtype // only valid when dataType === 'number'
  parentFieldKey?: string | null // contingency (V077); set with contingentValues, coded fields only
  contingentValues?: ContingentValues | null
}

// --- reusable coded-value domains (slice 2; /custom-domains) ----------------------------------------

export interface DomainValue {
  id: string
  code: string
  label: string
  active: boolean
  sortOrder: number
}

/** GET /custom-domains list item (org-wide; for the field-designer picker + the Domains page). */
export interface CustomDomain {
  id: string
  name: string
  description: string | null
  ownerId: string
  ownerName: string | null
  valueCount: number
  boundFieldCount?: number // how many fields bind this domain (Domains page; absent from older callers)
  canEdit?: boolean // server-authoritative: is the caller the owner or an admin?
}

/** PATCH /custom-domains/{id} body. */
export interface DomainUpdate {
  name?: string
  description?: string | null
}

/** PATCH /custom-domains/{id}/values/{valueId} body (code is immutable). */
export interface DomainValuePatch {
  label?: string
  active?: boolean
  sortOrder?: number
}

/** One field/layer a domain is bound to (GET /custom-domains/{id}/usage). */
export interface DomainBinding {
  layerId: string
  layerName: string
  fieldKey: string
  fieldLabel: string
}

/** GET /custom-domains/{id} — a domain plus its values (active and retired). */
export interface CustomDomainDetail {
  id: string
  name: string
  description: string | null
  ownerId: string
  values: DomainValue[]
}

export interface DomainValueSeed {
  code: string
  label: string
  sortOrder?: number
}

/** POST /custom-domains — create a domain (optionally with initial values). */
export interface DomainCreate {
  name: string
  description?: string | null
  values?: DomainValueSeed[]
}

/** PATCH body — edit label/required/default/order/active (key + type stay immutable). */
export interface CustomFieldUpdate {
  label?: string
  required?: boolean
  defaultValue?: unknown | null
  sortOrder?: number
  active?: boolean
  constraints?: FieldConstraints | null // slice 3
  defaultKind?: DefaultKind
  // Contingency (V077): PATCH BOTH together — both values to set/replace, both nulls to clear.
  parentFieldKey?: string | null
  contingentValues?: ContingentValues | null
}

/** POST /layers/{id}/custom-fields/{fieldId}/violations — author-time impact preview (fork 7). */
export interface ConstraintViolationCount {
  layerId: string
  fieldId: string
  violating: number // existing rows whose value already breaks the proposed rule
  total: number
}

// --- bulk schema import (decision-schema-import.md; /custom-fields/import) -------------------------

export type SchemaImportStatus = 'ok' | 'renamed' | 'error' | 'coded' | 'duplicate' | 'created'

/** One row's verdict from a schema-import dry-run (ok/renamed/error/coded/duplicate) or commit (created).
 * `renamed` is an OK row that carries an explanation: the source column's name is reserved on the ESRI
 * door, so it imported under a `*_src` key (decision-esri-field-name-clash.md §3.6 Q8). It counts toward
 * the importable total everywhere `ok` does — it differs only in that the preview must SAY so. */
export interface SchemaImportRow {
  fieldKey: string
  status: SchemaImportStatus
  reason: string | null
}

/** POST /layers/{id}/custom-fields/import {csv_text, commit} response. */
export interface SchemaImportResult {
  layerId: string
  committed: boolean
  rows: SchemaImportRow[]
  summary: Record<string, number>
  table?: string // the chosen feature class (Esri XML imports only)
}

/** POST /custom-fields/preview-import — the layer-less wizard preview: per-row verdicts PLUS the field-create
 *  payloads for the 'ok' rows (the wizard holds these in memory and creates them at Finish). Multi-table Esri
 *  returns `needsTable` + `tables` (the picker). The DATA-file formats (shapefile/geojson/csv-data — the
 *  add-from-file flow, decision-add-from-file.md) plus geopackage additionally return seed metadata:
 *  geometry family + per-family counts, the file's declared CRS (null ⇒ the user must pick), the feature
 *  count, the per-request ingest cap, and — csv-data only — the columns + detected coordinate candidates. */
export interface PreviewImportResult {
  rows: SchemaImportRow[]
  fields: CustomFieldCreate[]
  summary: Record<string, number>
  needsTable?: boolean
  tables?: EsriXmlTable[]
  table?: string
  // Seed metadata (absent on schema-csv / esri-xml previews).
  geometry?: 'point' | 'line' | 'polygon' | null
  geometryCounts?: Record<string, number>
  featureCount?: number | null
  detectedSrid?: number | null
  featureCap?: number
  // csv-data only:
  columns?: string[]
  xColumn?: string | null
  yColumn?: string | null
}

/** One feature class/table offered by the Esri XML picker (a GDB is multi-table). */
export interface EsriXmlTable {
  name: string
  fieldCount: number
}

/** import-esri-xml returns THIS when no table is chosen yet and the workspace has multiple. */
export interface EsriXmlNeedsTable {
  layerId: string
  needsTable: true
  tables: EsriXmlTable[]
}

/** import-esri-xml response: either the feature-class picker list, or a normal import result. */
export type EsriXmlImportResult = EsriXmlNeedsTable | SchemaImportResult

/** Distinct values of a non-coded field (GET /layers/{id}/field-values/{field}), capped. */
export interface FieldValuesResponse {
  layer_id: string
  field: string
  values: string[]
  truncated: boolean
}

// --- catalog Data table (Talus 15) ---------------------------------------------

/** One column of a layer's FULL schema (GET /layers/{id}/columns). `domain` is the FK
 * domain id when the column is coded, else absent. Unlike
 * SymbolizableField this includes id + audit columns — the table/fields views need them. */
export interface LayerColumn {
  name: string
  type: string // information_schema data_type (text / uuid / timestamp with time zone / …)
  domain: string | null
  /** Owner-defined VIRTUAL column (decision-geometry-calc.md §3.4): the value lives at
   *  properties.props[name], not properties[name]; filter/sort ride the same grammar. */
  props?: boolean
  /** The owner-authored display label (platform.layer_fields.label) — virtual columns only;
   *  real columns keep the client-side displayName prettifier. */
  label?: string
  /** A coded owner field's custom-domain id (platform.domains) — labels/options resolve via
   *  GET /custom-domains/{id}, not the built-in /domains tables. */
  customDomain?: string
}

export interface LayerColumnsResponse {
  layer_id: string
  columns: LayerColumn[]
  /** This caller's edit decision for the layer (decision-attachments.md A-1) — drives the record-detail
   *  attachments upload/delete affordances. Anon → false. Optional: absent on older/other consumers. */
  canEdit?: boolean
  /** The layer's attachments toggle (V068). Upload is offered only when canEdit && attachmentsEnabled. */
  attachmentsEnabled?: boolean
}

/** One feature from the generic layer query. Properties are RAW columns (coded fields carry
 * codes — labels resolve client-side from /domains; created_by/updated_by also arrive
 * pre-resolved as `<field>_name`). Geometry may be null for a geometry-less hosted table. */
export interface LayerFeature {
  type: 'Feature'
  id: string | null
  geometry: { type: string; coordinates: unknown } | null
  properties: Record<string, unknown>
}

/** GET /layers/{id}/features — OGC API Features paged collection. `numberMatched` is the full
 * filtered count (page-independent); `numberReturned` is this page. */
export interface LayerFeatureCollection {
  type: 'FeatureCollection'
  numberMatched: number
  numberReturned: number
  features: LayerFeature[]
}

/** Query params for the generic layer query / export. `filters` are 'field:op:value'
 * (op in in/eq/gte/lte/ilike); `sort` is 'field' | 'field:asc' | 'field:desc'. */
export interface LayerQuery {
  bbox?: string
  filters?: string[]
  sort?: string
  /** Repeatable selected-row ids — Show-selected-records mode (decision-select-features.md F5):
   *  the server pages ONLY these rows, AND-composed with filters/bbox, so numberMatched is the
   *  selected-set size and paging stays honest. */
  ids?: string[]
  limit?: number
  offset?: number
}

// --- media / photos (Talus 16) -------------------------------------------------

export interface FeatureAttachment {
  id: string
  filename: string
  content_type: string
  byte_size: number
  created_at: string
  captured_at: string | null
  /** Who uploaded it. Sent to a signed-in viewer only; absent for a signed-out viewer of a public layer. */
  created_by_name?: string | null
}

/** GET /layers/{id}/features/{fid}/attachments — a feature's attachments (creation order; empty when none). */
export interface FeatureAttachmentsResponse {
  layer_id: string
  feature_id: string
  attachments: FeatureAttachment[]
}

// --- per-feature edit history (decision-edit-history-ui.md) --------------------

/** One changed field within a history entry. `field` is the RAW key (a props key, a built-in
 * column, or `notes`); the client resolves its label + coded-value labels from the layer's
 * columns/domains. `__geometry__` is the synthetic geometry-change marker (kind:'geometry',
 * with an optional moved_m for point moves). */
export interface FeatureHistoryChange {
  field: string
  old?: unknown
  new?: unknown
  kind?: 'geometry'
  moved_m?: number
}

/** One entry in a feature's edit history (newest-first). `kind` is 'feature' (an attribute/geometry/
 * notes change — see `changes`) or 'attachment' (an add/remove of a file — see `attachment`).
 * feature INSERT/DELETE carry an empty `changes` (the operation is the meaning). `via` is the
 * originating client (portal-api / geoservices-api). */
export interface FeatureHistoryEntry {
  audit_id: number
  kind: 'feature' | 'attachment'
  at: string | null
  operation: 'INSERT' | 'UPDATE' | 'DELETE'
  actor: { id: string | null; name: string | null }
  actor_type: 'human' | 'service_account' | 'ai_inference'
  via: string | null
  changes: FeatureHistoryChange[]
  /** Present only when kind === 'attachment' — the file added (INSERT) or removed (DELETE). */
  attachment?: { filename: string | null }
}

/** GET /layers/{id}/features/{fid}/history — one page of a feature's edit history (view-gated;
 * anonymous denied). The timeline merges the feature's own audit with its attachment add/remove
 * events. `next_cursor` is the ISO changed_at to pass back as `cursor` for the next (older) page,
 * or null when this is the last page. */
export interface FeatureHistoryResponse {
  entries: FeatureHistoryEntry[]
  next_cursor: string | null
}

// --- geocoding / address search (Talus 18c) ------------------------------------

/** One GET /geocode/suggest autocomplete row. `source` is the configured geocoder that
 * produced it; `magic_key` (ArcGIS magicKey) resolves THIS exact suggestion via
 * /geocode/search — it is meaningful only to its issuing source. */
export interface GeoSuggestion {
  text: string
  source: string
  magic_key: string | null
}

/** One GET /geocode/search result — a resolved location in EPSG:4326. */
export interface GeoCandidate {
  label: string
  lon: number
  lat: number
  score: number | null
  source: string
}

/** The geocoder adapter protocols Talus speaks (Talus 18c; `internal` = Mode B, the own-data
 * locator over a hosted address-point layer — decision-own-data-geocoder.md). */
export type GeocoderType = 'arcgis' | 'nominatim' | 'internal'

/** One admin-managed locator (Talus 37 — Admin → Geocoders). The org-wide list the /geocode proxy
 * consumes, managed in-portal instead of via the GEOCODERS env var.
 * `extent` is an optional [xmin, ymin, xmax, ymax] in EPSG:4326 that restricts matches to a boundary.
 * Mode B: `url` is null for type 'internal', which instead carries `layerId` + `addressFields`
 * (the hosted layer searched, and its address fields in match/display order).
 * Token locators (decision-geocoder-token-locators.md): the credential is WRITE-ONLY — the API never
 * returns it. `hasToken` + `tokenHint` (last 4) show that/which one is set; `tokenUnavailable` means
 * ciphertext exists but the server's key can't open it (key loss — re-enter the token). */
export interface Geocoder {
  id: string
  name: string
  type: GeocoderType
  url: string | null
  outSR: number
  extent: number[] | null
  order: number
  enabled: boolean
  layerId: string | null
  addressFields: string[] | null
  /** Mode B: locality fields (city, ZIP …) — tiebreak + label, never scored or indexed. [] for
   * external types (decision-batch-geocoding-county-scale.md F3a, V107). */
  localityFields: string[]
  hasToken: boolean
  tokenHint: string | null
  tokenUnavailable: boolean
}

/** Create/edit body for a geocoder (full replace on PUT — EXCEPT the token, which is write-only and
 * can't round-trip: `token` set → replace; `clearToken` → remove; both omitted → keep what's stored).
 * Mirrors the server's GeocoderWrite: external types require `url`; type 'internal' requires
 * `layerId` + `addressFields` instead; `token` applies only to type 'arcgis'. */
export interface GeocoderWrite {
  name: string
  type: GeocoderType
  url?: string | null
  outSR?: number
  extent?: number[] | null
  order?: number
  enabled?: boolean
  layerId?: string | null
  addressFields?: string[] | null
  localityFields?: string[] | null
  token?: string | null
  clearToken?: boolean
}

/** One layer an internal locator may search (GET /geocoders/eligible-layers — point layers only). */
export interface EligibleLocatorLayer {
  layerId: string
  displayName: string
}

/** One address-field choice for an internal locator's mapping (GET /geocoders/eligible-layers/{id}/fields). */
export interface MappableField {
  key: string
  label: string
}

/** Result of POST /geocoders/test — a non-blocking probe of a locator before relying on it. */
export interface GeocoderTestResult {
  ok: boolean
  sampleCount: number
  error: string | null
}

// --- in-map feature search (decision-feature-search.md) ------------------------

/**
 * One hit from GET /search: a feature matching the attribute query, with a representative point
 * (fly) + a bbox (fitBounds for line/polygon). Full geometry is NOT returned — the popup fetches
 * that itself on click.
 */
/** How a result matched (v2): 'exact' = token match (v1); 'partial' = a `*`/`?` wildcard match;
 * 'similar' = a fuzzy/typo match surfaced only when exact was thin. Drives the panel's badge + ranking tier. */
export type FeatureMatchKind = 'exact' | 'partial' | 'similar'

export interface FeatureSearchResult {
  layer_id: string
  id: string
  label: string // server best-effort fallback label
  // The matched feature's display fields (built-in real columns, or a slot's props + notes) — lets the panel
  // label each result by its layer's title field (popupTitleField) so a hit reads recognizably.
  properties?: Record<string, unknown> | null
  rank: number
  match_kind: FeatureMatchKind
  point: [number, number] | null // [lon, lat]
  bbox: [number, number, number, number] | null // [minx, miny, maxx, maxy] (EPSG:4326)
}

/** GET /search response: hits (rank-ordered within each layer) + per-layer match counts + the layer ids
 * whose results were truncated at the per-layer cap (so the UI can prompt to refine). */
export interface FeatureSearchResponse {
  query: string
  results: FeatureSearchResult[]
  counts: Record<string, number>
  capped: string[]
}
