/**
 * Popup row/title resolution (decision-map-popups.md). Pure helpers shared by the identify path
 * (`loadFeaturePage` in AssetMap): given a layer's already-fetched feature props, the map's per-layer
 * popup config (if any), and — for slots — the layer's owner-defined field definitions, produce the
 * ordered [label, value] rows and the page title. STRUCTURAL only: names pick keys off props; there is
 * no template/expression surface (the standing fence).
 *
 * Value-resolution rules:
 *  - Slots: a name resolves against the flat feature first (notes, created_at, …), then the owner-defined
 *    `props` object; owner fields use their `platform.layer_fields` label, and coded values resolve to
 *    their domain label when the defs are in hand (raw code fallback — resilient, never blocking).
 *  - Built-ins: `<stem>_code` names prefer the server-joined `<stem>_label` sibling value (the curated
 *    rows' own behavior); the row label comes from the shared fieldLabel prettifier.
 *  - Booleans render Yes/No; empty/null renders '—' (the popupRows conventions).
 */
import { fieldLabel } from '@/features/map/fields'
import { isSlotId } from '@/features/map/layers'
import type { LayerConfig } from '@/features/map/types'
import type { CustomField, DomainValue, WebMapPopupConfig } from '@/lib/api'

/** What the popup reads off one field definition. Narrow on purpose: a signed-out viewer's
 *  definitions come from the public columns door, which carries exactly this much. */
export type SlotFieldDef = Pick<CustomField, 'fieldKey' | 'label' | 'active' | 'domainId'>

/** A slot layer's owner-defined field definitions + the values of every domain those fields bind. */
export interface SlotFieldDefs {
  fields: SlotFieldDef[]
  domainValues: Record<string, DomainValue[]> // keyed by domainId
}

const dash = (v: unknown): string => (v === null || v === undefined || v === '' ? '—' : String(v))

function display(v: unknown): string {
  if (typeof v === 'boolean') return v ? 'Yes' : 'No'
  return dash(v)
}

function asRecord(v: unknown): Record<string, unknown> {
  return v && typeof v === 'object' ? (v as Record<string, unknown>) : {}
}

/** Resolve one field name to its display value off the fetched props (rules above). */
function valueOf(
  name: string,
  props: unknown,
  slot: boolean,
  defs: SlotFieldDefs | null,
): string {
  const flat = asRecord(props)
  if (slot) {
    if (name in flat) return display(flat[name])
    const custom = asRecord(flat.props)[name]
    const def = defs?.fields.find((f) => f.fieldKey === name)
    if (def?.domainId && custom !== null && custom !== undefined) {
      const match = defs?.domainValues[def.domainId]?.find((v) => v.code === String(custom))
      if (match) return match.label
    }
    return display(custom)
  }
  // Built-ins/views: prefer the server-joined label sibling for coded columns (curated-row parity).
  if (name.endsWith('_code')) {
    const sibling = flat[`${name.slice(0, -5)}_label`]
    if (sibling !== null && sibling !== undefined && sibling !== '') return display(sibling)
  }
  return display(flat[name])
}

/** Row label for a configured field: the owner-defined label when the slot defines one, else prettified. */
function labelOf(name: string, slot: boolean, defs: SlotFieldDefs | null): string {
  if (slot) {
    const def = defs?.fields.find((f) => f.fieldKey === name)
    if (def) return def.label
    if (name === 'notes') return 'Notes'
  }
  return fieldLabel(name)
}

/** The slot DEFAULT rows (fork §4.1 — the deferred auto-fields): Notes + every ACTIVE owner-defined
 * field in sortOrder (labels + coded→domain-label). Falls back to prettified raw `props` keys when the
 * defs fetch failed (never blocks the popup). */
function slotAutoRows(props: unknown, defs: SlotFieldDefs | null): Array<[string, string]> {
  const flat = asRecord(props)
  const rows: Array<[string, string]> = [['Notes', dash(flat.notes)]]
  if (defs) {
    for (const f of defs.fields) {
      if (!f.active) continue
      rows.push([f.label, valueOf(f.fieldKey, props, true, defs)])
    }
  } else {
    for (const key of Object.keys(asRecord(flat.props))) {
      rows.push([fieldLabel(key), display(asRecord(flat.props)[key])])
    }
  }
  return rows
}

/** The popup rows for a page: configured list when present (order = array order), else the defaults —
 * built-ins/views keep their curated/auto popupRows; slots get the auto-fields default. */
export function popupRowsFor(
  config: LayerConfig<unknown>,
  props: unknown,
  cfg: WebMapPopupConfig | undefined,
  defs: SlotFieldDefs | null,
): Array<[string, string]> {
  const slot = isSlotId(config.id)
  if (cfg?.fields != null) {
    return cfg.fields.map((f) => [labelOf(f.name, slot, defs), valueOf(f.name, props, slot, defs)])
  }
  if (slot) return slotAutoRows(props, defs)
  return config.popupRows(props)
}

/** The popup page title + eyebrow (fork §4.2): the title FIELD's value names the page, with the layer label
 * as the eyebrow. Resolution order (decision-layer-display-field.md §2): the map's per-map `titleField`
 * override → the layer's effective `display_field` (which config.popupTitleField carries — the starred column
 * or a server-derived first-text default) → the layer label alone. An empty value at any rung falls through,
 * so a feature missing its title attribute still titles by the layer name rather than showing a dash. */
export function popupTitleFor(
  config: LayerConfig<unknown>,
  props: unknown,
  cfg: WebMapPopupConfig | undefined,
  defs: SlotFieldDefs | null,
): { title: string; eyebrow?: string } {
  const field = cfg?.titleField ?? config.popupTitleField
  if (field) {
    const v = valueOf(field, props, isSlotId(config.id), defs)
    if (v !== '—') return { title: v, eyebrow: config.label }
  }
  return { title: config.label }
}
