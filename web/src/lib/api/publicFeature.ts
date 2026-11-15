/** A signed-out viewer's popup on a public map: shapes for the two reads it makes.
 *
 *  The single-feature door (`GET /layers/{id}/features/{fid}`) is signed-in only. A signed-out
 *  viewer reads the clicked feature through the LIST door instead (`?id=<fid>&limit=1`) and the
 *  layer's field labels through the COLUMNS door. Both already answer an anonymous caller for a
 *  layer that is itself shared publicly, and both already leave out what the public must not see.
 *  These helpers only reshape those answers into what the popup reads; they add nothing.
 *
 *  No imports: this file is also loaded directly by `node --test`. */

/** The flat feature the popup reads (the single-feature door's shape, minus what is never public). */
export interface PublicSlotFeature {
  id: string
  geometry?: unknown
  notes: string | null
  props: Record<string, unknown>
  created_at?: string
  updated_at?: string
}

/** The four things the popup reads off a field definition. */
export interface PublicFieldDef {
  fieldKey: string
  label: string
  active: boolean
  domainId: string | null
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

/** The one feature out of a list-door answer, or null when the answer does not hold exactly the
 *  feature that was asked for. Only named keys are copied, so a key the list door might one day
 *  add does not reach the popup by accident. */
export function slotFeatureFromList(collection: unknown, fid: string): PublicSlotFeature | null {
  const features = isRecord(collection) && Array.isArray(collection.features) ? collection.features : []
  const f: unknown = features.find((x) => isRecord(x) && x.id === fid)
  if (!isRecord(f)) return null
  const p = isRecord(f.properties) ? f.properties : {}
  const out: PublicSlotFeature = {
    id: fid,
    notes: typeof p.notes === 'string' ? p.notes : null,
    props: isRecord(p.props) ? p.props : {},
  }
  if (f.geometry !== null && f.geometry !== undefined) out.geometry = f.geometry
  if (typeof p.created_at === 'string') out.created_at = p.created_at
  if (typeof p.updated_at === 'string') out.updated_at = p.updated_at
  return out
}

/** Field definitions from a columns-door answer: the owner-defined fields, which that door lists
 *  active only and in the owner's order, each with its label. A picklist field keeps its raw code
 *  (`domainId` null): the picklist's values are not public. */
export function fieldDefsFromColumns(response: unknown): PublicFieldDef[] {
  const columns = isRecord(response) && Array.isArray(response.columns) ? response.columns : []
  const out: PublicFieldDef[] = []
  for (const c of columns as unknown[]) {
    if (!isRecord(c) || c.props !== true || typeof c.name !== 'string' || c.name === '') continue
    out.push({
      fieldKey: c.name,
      label: typeof c.label === 'string' && c.label !== '' ? c.label : c.name,
      active: true,
      domainId: null,
    })
  }
  return out
}
