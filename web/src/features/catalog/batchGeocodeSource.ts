/** Batch geocoding from a table already in Talus (decision-batch-geocoding.md, rider 2026-11-06):
 *  which catalog items can be a source, and which of a layer's columns can hold an address.
 *
 *  The run door (`POST /geocode/batch/run` with `sourceLayerId`) takes a HOSTED layer or table the
 *  caller can view, and reads its owner-defined fields. These helpers mirror those two limits so the
 *  dialog offers only what the door will accept; the door stays the authority.
 *
 *  No imports: this file is also loaded directly by `node --test`. */

/** The part of a catalog layer row (`GET /me/layers`) these helpers read. */
export interface SourceLayerRow {
  id: string
  canView: boolean
  displayName: string | null
  featureCount?: number | null
  geomType?: string
  renderMode?: string
  sourceLayerId?: string | null
  ownerName?: string | null
}

/** One choice in the dialog's source list. */
export interface GeocodeSourceChoice {
  id: string
  name: string
  /** What the list shows: the name, with the owner added when two choices share a name. */
  label: string
  /** 'table' for a non-spatial table, else 'layer'. */
  kind: 'table' | 'layer'
  /** Rows in the source, when the catalog knows it. */
  rowCount: number | null
}

/** The run door's own id shape for a hosted layer or table. */
const HOSTED_ID = /^tal_[0-9a-f]{8}$/

/** The catalog rows that can be a geocoding source: hosted, viewable, not a view layer, with a
 *  name. Tables first (the common case: a table of addresses with no geometry), then by name. */
export function geocodeSourceChoices(layers: readonly SourceLayerRow[]): GeocodeSourceChoice[] {
  const out: (GeocodeSourceChoice & { owner: string })[] = []
  for (const l of layers) {
    if (!l.canView || !HOSTED_ID.test(l.id)) continue
    if (l.renderMode === 'features' || l.sourceLayerId) continue // a view layer — not a hosted table
    const name = (l.displayName ?? '').trim()
    if (!name) continue // an unclaimed slot
    out.push({
      id: l.id,
      name,
      label: name,
      owner: (l.ownerName ?? '').trim(),
      kind: l.geomType === 'none' ? 'table' : 'layer',
      rowCount: typeof l.featureCount === 'number' ? l.featureCount : null,
    })
  }
  // Two items may share a name (a copy, or two owners' layers): the owner tells them apart.
  const seen = new Map<string, number>() // a Map: a layer may be named "constructor"
  for (const c of out) seen.set(c.name, (seen.get(c.name) ?? 0) + 1)
  return out
    .map(({ owner, ...c }) => ((seen.get(c.name) ?? 0) > 1 && owner ? { ...c, label: `${c.name} — ${owner}` } : c))
    .sort((a, b) =>
      a.kind === b.kind ? a.label.localeCompare(b.label) : a.kind === 'table' ? -1 : 1)
}

/** The part of a column (`GET /layers/{id}/columns`) these helpers read. */
export interface SourceColumn {
  name: string
  props?: boolean
  label?: string
}

/** The columns the run door can geocode from: the layer's owner-defined fields, by key, each with
 *  the label to show for it. System columns (id, audit stamps) are not offered — the door's reader
 *  does not return them. */
export function addressFieldChoices(columns: readonly SourceColumn[]): { keys: string[]; labels: Record<string, string> } {
  const keys: string[] = []
  const labels: Record<string, string> = {}
  for (const c of columns) {
    if (!c.props || !c.name || keys.includes(c.name)) continue
    keys.push(c.name)
    const label = (c.label ?? '').trim()
    if (label && label !== c.name) labels[c.name] = label
  }
  return { keys, labels }
}

/** The text to show for a column: its label when it has one, else its own name. Reads OWN keys
 *  only — a CSV header or field key may be "constructor" or "toString", and a bare `labels[key]`
 *  would then return a function from Object.prototype (a blank chip, and a sort that throws). */
export function fieldLabel(labels: Readonly<Record<string, string>>, key: string): string {
  return Object.prototype.hasOwnProperty.call(labels, key) ? labels[key] : key
}
