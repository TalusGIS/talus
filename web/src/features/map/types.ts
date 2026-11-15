import type { ComponentType, ReactNode } from 'react'

import type { AssetApi, Renderer, EditOps } from '@/lib/api'

/** Props every per-asset form takes. SlotForm implements this. */
export interface AssetFormProps<P = unknown, U = unknown> {
  mode: 'create' | 'edit'
  initial: P | null
  busy: boolean
  error: string | null
  // onSubmit carries the attributes and, for an attachments-enabled layer, the files staged in the
  // form's AttachmentStage (decision-attachments-in-edit-form.md). The caller (AssetMap /
  // RowFormDialog) writes the feature first, then uploads the staged files to its id.
  onSubmit: (attrs: U, attachments?: File[]) => void
  onCancel: () => void
  // A row-scoped action that belongs IN the action row rather than in a second actions div below it
  // (handoff-record-table-3 §4.5) — today, RowFormDialog's "Delete row". Rendered leading, away
  // from Save. Optional; the map's create/edit forms pass nothing.
  extraAction?: ReactNode
  // Whether this layer accepts attachments (from /me/layers). When true (and the caller can edit),
  // the form renders the AttachmentStage control. Absent/false → no picker.
  attachmentsEnabled?: boolean
  // The active layer's id + token — supplied to every form, used only by SlotForm to fetch this
  // slot's owner-defined custom fields (V044).
  layerId?: string
  accessToken?: string
  // Working-CRS coordinate entry (E3, decision-coord-entry.md): on CREATE, the clicked point (lon/lat 4326)
  // + the layer's storage CRS. SlotForm shows editable coordinate fields in `srid` and emits backend-ready
  // geometry ({x,y} for native, reprojected {lon,lat} otherwise). Absent on edit / for the bespoke forms.
  coord?: { lon: number; lat: number; srid: number; native: boolean }
  // The layer's geometry noun ('point' | 'line' | 'polygon'), for the form title (line/polygon slice c6a) —
  // a drawn line/polygon must not read "New point". Optional; SlotForm falls back to 'feature'.
  noun?: string
  // True when the caller can edit this layer ONLY via talus-admin override (no genuine edit access) — the form
  // shows a "editing is restricted but you have admin privileges" notice (admin-edit-all). Optional.
  editViaAdmin?: boolean
}

/**
 * The STATIC description of an asset layer — everything that doesn't depend on
 * who is logged in. Today this is a hardcoded list (see layers.ts); in Layer 7b
 * the same shape is produced per-user from GET /me/layers. Map source wiring, the
 * toggle, the popup, and the form all consume LayerDefs, so adding a layer is a
 * data change, not a component change.
 */
export interface LayerDef<P = unknown, C = unknown, U = unknown> {
  id: string // the layer id (tal_<8hex> / vw_<hex>) — also the API path + tile-route segment
  label: string // toggle + create-button text
  noun: string // 'sign' | 'street light' — confirm dialogs / messages
  sourceLayer: string // MVT internal layer name
  geometryType: 'point' | 'line' | 'polygon' // render branch: circle / line / fill+line (decision-line-polygon-layers.md)
  color: string // point: circle fill. line: stroke. polygon: fill-color AND line-color.
  strokeColor: string // point: circle outline. line/polygon: outline (line uses `color` for the stroke).
  fillOpacity?: number // polygon fill opacity (default ~0.12 if absent); ignored for points.
  popupTitleField: string // the lean tile attribute that titles the popup
  api: AssetApi<P, C, U>
  popupRows: (props: P) => Array<[string, string]>
  buildCreate: (lon: number, lat: number, attrs: U) => C
  validateCreate: (attrs: U) => string | null // null = ok; message = block create
  Form: ComponentType<AssetFormProps<P, U>>
  // Talus 38: a VIEW layer (sanitized lens) renders from a GeoJSON source (/layers/{id}/features),
  // not MVT tiles — read-only, geometry-agnostic (fill+line+circle on one source). The map branches
  // on this in registerLayerOnMap; geometryType is nominal for views (all three render layers added).
  isView?: boolean
  // Working-CRS coordinate entry (E3, decision-coord-entry.md): the layer's storage-CRS context carried from
  // MyLayer, so the create/move boundary places geometry in the right CRS and the form labels its fields.
  // Absent when the caps entry is missing. nativeStorage ⇒ store {x,y} in workingSrid (exact).
  nativeStorage?: boolean
  workingSrid?: number
}

/**
 * A LayerDef plus the CURRENT user's capabilities on it. Today the map fills these
 * statically (canView = true, canEdit = isEditor); Layer 7b swaps the source to
 * GET /me/layers (per-layer view/edit) without touching the consumers.
 */
export interface LayerConfig<P = unknown, C = unknown, U = unknown> extends LayerDef<P, C, U> {
  canView: boolean
  canEdit: boolean
  editViaAdmin: boolean // canEdit holds only via talus-admin override → the form shows a restricted-edit notice
  editOps: EditOps // V109: the owner's four capability flags — the affordances hide where a flag is off
  canManage: boolean // Talus 13: may set/clear the layer-default symbology (owner/admin)
  editable: boolean // platform.layers.editable — same for all users (read-only-badge hook)
  renderer: Renderer | null // layer-default symbology (Talus 13); null = single-symbol fallback
}
