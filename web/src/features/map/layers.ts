import { makeSlotApi } from '@/lib/api'
import type { MyLayer, SlotCreate, SlotProperties, SlotUpdate } from '@/lib/api'
import SlotForm from '@/features/map/SlotForm'
import type { LayerDef } from '@/features/map/types'

// Every layer the map renders is manufactured at runtime from GET /me/layers — a claimed
// publisher slot (tal_<8hex>) or a view layer (vw_<hex>). The three hardcoded LayerDefs that
// used to live here (signs / street_lights / county_boundary) were the pilot built-ins, retired
// in decision-retire-builtins.md phase 2: the demo now runs on hosted layers built through the
// product's own doors, so there is no longer a static list to merge.

// --- publisher slots (Talus 10): LayerDefs built per-user from GET /me/layers --------
// Unlike the three static layers above, a claimed slot (tal_<8hex>) is NOT hardcoded —
// AssetMap manufactures its LayerDef from the MyLayer row so it renders + edits exactly
// exactly like any other hosted layer. teal default; overridden by the stored displayColor.
/** Popup cell formatter shared by the manufactured LayerDefs. */
const dash = (v: unknown): string => (v === null || v === undefined || v === '' ? '—' : String(v))

/** A never-rendered placeholder Form for view-only layers — the map hides every
 *  create/edit affordance for them, so this component is never mounted. */
const NoForm = (): null => null

export const DEFAULT_SLOT_COLOR = '#2a9d8f'

export const isSlotId = (id: string): boolean => /^tal_[0-9a-f]{8}$/.test(id)

export function buildSlotLayerDef(my: MyLayer): LayerDef<SlotProperties, SlotCreate, SlotUpdate> {
  // V055: line/polygon slots store Multi* but render branch by family. 'none' (a non-spatial table)
  // never reaches here — every caller filters `geomType !== 'none'` (tables aren't map layers) — so
  // narrow it out explicitly rather than widening LayerDef (which is spatial by definition).
  const geometryType = !my.geomType || my.geomType === 'none' ? 'point' : my.geomType
  return {
    id: my.id,
    label: my.displayName ?? my.id,
    noun: geometryType, // 'point' | 'line' | 'polygon' — confirm dialogs / create-button text
    sourceLayer: my.id, // MVT layer name = slot token (matches V019 ST_AsMVT name)
    geometryType,
    color: my.displayColor ?? DEFAULT_SLOT_COLOR,
    strokeColor: '#1b1b1b',
    fillOpacity: geometryType === 'polygon' ? 0.3 : undefined, // readable default fill for polygon layers
    popupTitleField: 'id',
    api: makeSlotApi(my.id),
    popupRows: (p) => [['Notes', p.notes ?? '—']],
    buildCreate: (lon, lat, attrs) => ({ lon, lat, notes: attrs.notes ?? null, props: attrs.props }),
    validateCreate: () => null,
    Form: SlotForm,
    nativeStorage: my.nativeStorage, // E3: storage-CRS context for coordinate entry (decision-coord-entry.md)
    workingSrid: my.workingSrid,
  }
}

// --- view layers (Talus 38): GeoJSON-rendered sanitized lenses -----------------------
// A view layer (vw_<hex>) is NOT a slot and is NOT hardcoded — like a slot, AssetMap manufactures
// its LayerDef from the MyLayer row, but it carries isView:true so the map renders it from a GeoJSON
// source (/layers/{id}/features) instead of MVT tiles. Views are READ-ONLY (the sanitization case),
// so create/edit affordances are never shown; the popup renders the allow-listed columns generically.
export const DEFAULT_VIEW_COLOR = '#6a4c93'

export const isViewId = (id: string): boolean => /^vw_[0-9a-f]{12}$/.test(id)

// --- external layers (decision-add-from-url.md): remote-service references ----------------------
// An external layer (ext_<8hex>) is a REFERENCE to a remote public service — like a view it renders
// from a GeoJSON source and is read-only, but its features come from the REMOTE server (the viewer's
// browser fetches directly; see addFromUrl.fetchExternalViewport). isView:true routes it onto the
// existing GeoJSON render path + generic read-only popup; the api/Form stubs are never invoked.
export function buildExternalLayerDef(spec: {
  externalId: string
  title: string
  geometryType?: 'point' | 'line' | 'polygon'
  color?: string
}): LayerDef {
  return {
    id: spec.externalId,
    label: spec.title,
    noun: 'feature',
    sourceLayer: spec.externalId, // unused for a GeoJSON source, but LayerDef requires it
    geometryType: spec.geometryType ?? 'point',
    color: spec.color ?? DEFAULT_VIEW_COLOR,
    strokeColor: '#1b1b1b',
    fillOpacity: 0.15,
    popupTitleField: 'id',
    api: makeSlotApi(spec.externalId), // never invoked — externals are read-only references
    popupRows: (p: Record<string, unknown>) =>
      Object.entries(p)
        .filter(([k]) => k !== 'id')
        .map(([k, v]) => [k, dash(v)] as [string, string]),
    buildCreate: () => {
      throw new Error('external layers are read-only')
    },
    validateCreate: () => 'External layers are read-only',
    Form: NoForm,
    isView: true,
  } as unknown as LayerDef
}

export function buildViewLayerDef(my: MyLayer): LayerDef {
  return {
    id: my.id,
    label: my.displayName ?? my.id,
    noun: 'feature',
    sourceLayer: my.id, // unused for a GeoJSON source, but LayerDef requires it
    geometryType: 'point', // nominal — the GeoJSON branch adds fill+line+circle, one per geometry class
    color: my.displayColor ?? DEFAULT_VIEW_COLOR,
    strokeColor: '#1b1b1b',
    fillOpacity: 0.15,
    popupTitleField: 'id',
    // Never invoked: a view is read-only so the map shows no create/edit/move, and the popup renders
    // straight from the GeoJSON feature's properties (no per-feature fetch). Provide safe stubs.
    api: makeSlotApi(my.id),
    popupRows: (p: Record<string, unknown>) =>
      Object.entries(p)
        .filter(([k]) => k !== 'id')
        .map(([k, v]) => [k, dash(v)] as [string, string]),
    buildCreate: () => {
      throw new Error('view layers are read-only')
    },
    validateCreate: () => 'View layers are read-only',
    Form: NoForm,
    isView: true,
  } as unknown as LayerDef
}
