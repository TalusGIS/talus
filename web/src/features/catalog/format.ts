import type { LayerColumn } from '@/lib/api'
import { formatTemporal } from '@/lib/formatTemporal'

/** information_schema data_type -> a friendly type label (mirrors AGE's Type column). */
const TYPE_LABEL: Record<string, string> = {
  text: 'String',
  'character varying': 'String',
  uuid: 'ID',
  integer: 'Number',
  bigint: 'Number',
  smallint: 'Number',
  'double precision': 'Number',
  numeric: 'Number',
  real: 'Number',
  boolean: 'Boolean',
  date: 'Date',
  'timestamp with time zone': 'Date',
  'timestamp without time zone': 'Date',
  jsonb: 'JSON',
  json: 'JSON',
}

export function friendlyType(dataType: string): string {
  return TYPE_LABEL[dataType] ?? dataType
}

/** Derive a Display Name from the column name (Talus has no field-alias storage yet —
 * alias editing rides the schema-edit slice). 'condition_code' -> 'Condition Code'. */
export function displayName(field: string): string {
  return field
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase())
}

/** A column's header/row label: the owner-authored label for a virtual props column, else the
 * displayName prettifier. Use wherever a LayerColumn (not just a name) is in hand. */
/**
 * Internal / redundant columns hidden from every attribute surface — the data grid's default
 * column set and the record drawer's attribute list.
 *
 * `props` is the jsonb bag behind the owner-defined virtual columns. Those columns are ALREADY
 * listed individually (each `LayerColumn` with `props: true`), so rendering the raw bag as well
 * shows every value twice — once as unformatted JSON, once as its own labelled row. The grid had
 * always hidden it; the drawer's copy of the set was missing it while its comment claimed to
 * "mirror the Data table's default-hidden set". One set now, so the claim can't drift again.
 */
export const HIDDEN_COLUMNS = new Set(['ai_provenance', 'cover_photo_id', 'props'])

export function colLabel(col: LayerColumn): string {
  return col.label ?? displayName(col.name)
}

/** The filter-control kind a column gets in the Table's filter panel. uuid/json columns get
 * no control (id-by-uuid / blob filtering isn't useful); text columns get a contains box. */
export type FilterKind = 'coded' | 'date' | 'boolean' | 'number' | 'text' | 'none'

export function filterKind(col: LayerColumn): FilterKind {
  if (col.domain || col.customDomain) return 'coded'
  const t = col.type
  if (t.startsWith('timestamp') || t === 'date') return 'date'
  if (t === 'boolean') return 'boolean'
  if (['integer', 'bigint', 'smallint', 'double precision', 'numeric', 'real'].includes(t)) {
    return 'number'
  }
  if (t === 'text' || t === 'character varying') return 'text'
  return 'none'
}

/** Is this column's value empty for this row? The SAME test `cell()` applies before it returns
 * '—', exposed because presentation differs: §4.1 sets every null in the mono face at
 * `--text-subtle` whatever the column's own kind is, so a missing date and a missing name look
 * alike instead of the em-dash changing width from column to column. */
export function isBlank(col: LayerColumn, props: Record<string, unknown>): boolean {
  if (col.name === 'created_by' || col.name === 'updated_by') {
    return props[`${col.name}_name`] === null || props[`${col.name}_name`] === undefined
  }
  const val = col.props
    ? (props.props as Record<string, unknown> | null | undefined)?.[col.name]
    : props[col.name]
  return val === null || val === undefined || val === ''
}

/** How a column's VALUE should be PRESENTED (handoff-record-table-3 §4.1) — a different question
 * from `filterKind`, which picks a filter CONTROL. Here the question is face and alignment: values
 * that get compared down a column (numbers, dates, ids) set in the mono face with tabular figures
 * so digits line up; prose stays sans. `LayerColumn.type` is the raw information_schema data_type,
 * so this is where that vocabulary is translated once for both the grid and the drawer.
 *
 * Consumers render `data-kind={columnKind(col)}` and let CSS carry the presentation — one
 * attribute, no per-surface class juggling. */
export type ColumnKind = 'coded' | 'number' | 'date' | 'boolean' | 'id' | 'text'

/** Columns that are identifiers whatever their storage type — a bigint objectid is an id, not a
 * measurement, and right-aligning it next to real numbers would invite comparing the two. */
const ID_COLUMNS = new Set(['id', 'row_version', 'globalid', 'objectid', 'cover_photo_id'])

export function columnKind(col: LayerColumn): ColumnKind {
  if (col.domain || col.customDomain) return 'coded'
  // uuid columns, but `cell()` resolves both to a person's NAME — prose, not an identifier.
  if (col.name === 'created_by' || col.name === 'updated_by') return 'text'
  if (ID_COLUMNS.has(col.name) || col.type === 'uuid') return 'id'
  const t = col.type
  if (t.startsWith('timestamp') || t === 'date') return 'date'
  if (t === 'boolean') return 'boolean'
  if (['integer', 'bigint', 'smallint', 'double precision', 'numeric', 'real'].includes(t)) {
    return 'number'
  }
  return 'text'
}

/** Render one column's value for display: resolved editor name for created_by/updated_by, the
 * domain label for a coded field (built-in table OR custom domain — codeLabels is keyed by
 * both), compact JSON for a blob, '—' for empty. An owner-defined VIRTUAL column reads its
 * value from the nested props object. The single source of truth for cell formatting across
 * the Data table and the record-detail drawer. */
export function cell(
  col: LayerColumn,
  props: Record<string, unknown>,
  codeLabels: Record<string, Record<string, string>>,
): string {
  if (col.name === 'created_by' || col.name === 'updated_by') {
    return String(props[`${col.name}_name`] ?? '—')
  }
  const val = col.props
    ? (props.props as Record<string, unknown> | null | undefined)?.[col.name]
    : props[col.name]
  if (val === null || val === undefined || val === '') return '—'
  const dom = col.domain ?? col.customDomain
  if (dom) return codeLabels[dom]?.[String(val)] ?? String(val)
  if (typeof val === 'object') return JSON.stringify(val)
  // A date or timestamp reads as one ("Sep 25, 2024, 8:52 PM"), whatever the column's type — an
  // imported timestamp is a text column. cellTitle() below hands back the stored value for a tooltip.
  return formatTemporal(val)?.text ?? String(val)
}

/** The stored value behind a cell that `cell()` reformatted as a date or time — for the element's
 * `title`, so the exact value is one hover away. undefined when the cell shows the value as stored
 * (most cells), so callers can spread it straight into `title`. Same lookup order as `cell()`:
 * resolved names and coded values are never dates. */
export function cellTitle(col: LayerColumn, props: Record<string, unknown>): string | undefined {
  if (col.name === 'created_by' || col.name === 'updated_by') return undefined
  if (col.domain ?? col.customDomain) return undefined
  const val = col.props
    ? (props.props as Record<string, unknown> | null | undefined)?.[col.name]
    : props[col.name]
  return formatTemporal(val)?.raw
}
