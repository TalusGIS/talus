/**
 * Per-layer interactive attribute filter (decision-feature-filter.md). Opened from the layer's TOC row
 * (the funnel action, mirroring the Styles panel's swatch), it builds "Show features where <field>
 * <operator> <value>" conditions, AND-composed. On Apply the parent (AssetMap) stores the conditions on
 * the layer, swaps it to the GeoJSON/View render path with the predicate applied, and persists it with the
 * map. It's the ephemeral, unshared cousin of a View layer — same render path, no new catalog item.
 *
 * v1 fence (§0/§4a): simple predicates only — field·op·value rows, AND-only, no expression engine. Ops map
 * onto the injection-safe `/features?filter=` grammar (is=eq, is not=ne, contains=ilike, ≥=gte, ≤=lte,
 * any-of=in); "is between" is a UI convenience that emits a gte+lte pair (no new backend op). Value input:
 * a coded/domain dropdown for coded fields, a capped unique-values datalist for other fields (reusing the
 * shipped /field-values endpoint), free-typing always allowed.
 */
import { useEffect, useMemo, useRef, useState } from 'react'

import { HIDDEN_FIELDS, fieldLabel } from '@/features/map/fields'
import { ApiError, fetchFieldValues, fetchLayerColumns, getCustomDomain } from '@/lib/api'
import type { FilterCondition, LayerColumn } from '@/lib/api'
import CloseButton from '@/components/CloseButton'

// Backend op count cap — mirrors OperationalLayer.filters (web_maps.py _MAX_FILTERS). "is between" expands
// to two conditions, so a between row counts as two toward this ceiling.
const MAX_CONDITIONS = 10

type FieldKind = 'coded' | 'boolean' | 'number' | 'date' | 'text'
// The UI op set — a superset of the backend ops with 'between' (which expands to gte+lte on apply).
type UiOp = 'eq' | 'ne' | 'ilike' | 'between' | 'gte' | 'lte' | 'in'

/** Classify a column for its operator menu + value control, from the information_schema data_type
 * (and coded when it carries a domain). */
function kindOf(col: LayerColumn): FieldKind {
  if (col.domain || col.customDomain) return 'coded' // built-in FK domain OR an owner custom domain
  const t = col.type.toLowerCase()
  if (t === 'boolean') return 'boolean'
  if (/(int|numeric|double|real|decimal|money|serial)/.test(t)) return 'number'
  if (/(date|time)/.test(t)) return 'date' // 'date' | 'time…' | 'timestamp with time zone'
  return 'text'
}

const OPS_BY_KIND: Record<FieldKind, { op: UiOp; label: string }[]> = {
  coded: [{ op: 'eq', label: 'is' }, { op: 'ne', label: 'is not' }, { op: 'in', label: 'is any of' }],
  boolean: [{ op: 'eq', label: 'is' }, { op: 'ne', label: 'is not' }],
  number: [
    { op: 'eq', label: 'is' }, { op: 'ne', label: 'is not' }, { op: 'between', label: 'is between' },
    { op: 'gte', label: 'is at least' }, { op: 'lte', label: 'is at most' },
  ],
  date: [
    { op: 'eq', label: 'is' }, { op: 'ne', label: 'is not' }, { op: 'between', label: 'is between' },
    { op: 'gte', label: 'is on or after' }, { op: 'lte', label: 'is on or before' },
  ],
  text: [
    { op: 'eq', label: 'is' }, { op: 'ne', label: 'is not' },
    { op: 'ilike', label: 'contains' }, { op: 'in', label: 'is any of' },
  ],
}

// One editing row. `value2` is the upper bound for 'between' (unused otherwise). `key` keeps React list
// identity stable across edits/removals.
interface Row {
  key: number
  field: string
  op: UiOp
  value: string
  value2: string
}

/** Collapse the persisted conditions into editing rows. Each backend condition becomes its own row (a
 * saved gte+lte pair shows as two rows, not recombined into a "between" — functionally identical, simpler
 * and unambiguous). */
function rowsFromConditions(conds: FilterCondition[]): Row[] {
  return conds.map((c, i) => ({
    key: i,
    field: c.field,
    op: c.op as UiOp,
    value: c.value,
    value2: '',
  }))
}

/** Expand editing rows to backend conditions: 'between' → gte + lte (whichever bounds are filled, so an
 * open-ended range is allowed); 'in' → a comma-joined value (blanks trimmed); others → one condition. Rows
 * missing their value are dropped as incomplete. */
function conditionsFromRows(rows: Row[]): FilterCondition[] {
  const out: FilterCondition[] = []
  for (const r of rows) {
    if (!r.field) continue
    if (r.op === 'between') {
      if (r.value.trim() !== '') out.push({ field: r.field, op: 'gte', value: r.value.trim() })
      if (r.value2.trim() !== '') out.push({ field: r.field, op: 'lte', value: r.value2.trim() })
    } else if (r.op === 'in') {
      const vals = r.value.split(',').map((s) => s.trim()).filter(Boolean)
      if (vals.length) out.push({ field: r.field, op: 'in', value: vals.join(',') })
    } else if (r.value.trim() !== '') {
      out.push({ field: r.field, op: r.op, value: r.value.trim() })
    }
  }
  return out
}

export interface FilterPanelProps {
  layerId: string
  layerLabel: string
  accessToken: string
  initial: FilterCondition[] // the layer's current conditions
  onApply: (conds: FilterCondition[]) => void // apply + persist-in-state; [] clears the filter
  onClose: () => void
  /** EXTERNAL layers (decision-add-from-url.md rail parity): the caller supplies the column schema
   *  (from the remote service's metadata) — no Talus /columns fetch, no distinct-values datalist. */
  columnsOverride?: LayerColumn[]
}

export default function FilterPanel({
  layerId,
  layerLabel,
  accessToken,
  initial,
  onApply,
  onClose,
  columnsOverride,
}: FilterPanelProps) {
  const [columns, setColumns] = useState<LayerColumn[] | null>(null)
  const [columnsError, setColumnsError] = useState<string | null>(null)
  const [rows, setRows] = useState<Row[]>(() => rowsFromConditions(initial))
  const [error, setError] = useState<string | null>(null)
  // Lazily-fetched capped distinct values per non-coded field → a datalist of suggestions (free-type stays).
  const [uniqueValues, setUniqueValues] = useState<Record<string, string[]>>({})
  const keySeq = useRef(initial.length)

  // Load the layer's full column schema (name/type/coded-domain) once — the field dropdown + op/value gating.
  // An external layer's schema comes from the caller (the remote service's metadata), not Talus.
  useEffect(() => {
    if (columnsOverride) {
      setColumns(columnsOverride)
      return
    }
    let cancelled = false
    fetchLayerColumns(layerId, accessToken)
      .then((res) => {
        if (!cancelled) setColumns(res.columns.filter((c) => !HIDDEN_FIELDS.has(c.name)))
      })
      .catch((err) => {
        if (!cancelled) setColumnsError(err instanceof ApiError ? `HTTP ${err.status}` : 'request failed')
      })
    return () => {
      cancelled = true
    }
  }, [layerId, accessToken, columnsOverride])

  const colByName = useMemo(() => {
    const m = new Map<string, LayerColumn>()
    for (const c of columns ?? []) m.set(c.name, c)
    return m
  }, [columns])

  // Fetch capped distinct values for a non-coded field the first time it's used, to power its datalist.
  // Ask for the server's ?limit ceiling (2000) — a datalist filters as you type, so a long list is fine,
  // and the symbology default (200) truncated real fields (e.g. Signs' 252 sign_text values stopped in the R's).
  function ensureUniqueValues(field: string): void {
    if (columnsOverride) return // external layer — no Talus distinct-values endpoint; free-typing only
    if (field in uniqueValues) return
    setUniqueValues((prev) => ({ ...prev, [field]: [] })) // mark in-flight so we don't refetch
    fetchFieldValues(layerId, field, accessToken, 2000)
      .then((res) => setUniqueValues((prev) => ({ ...prev, [field]: res.values })))
      .catch(() => {
        /* leave empty — free-typing still works */
      })
  }

  // Owner custom-domain values (coded props fields), fetched lazily per domain — the map-panel
  // sibling of the grid's coded cards (decision-geometry-calc.md flagged item 2). Active values
  // only, in the author's sortOrder; empty-while-loading just renders the free input briefly.
  const [customDomainItems, setCustomDomainItems] = useState<Record<string, { code: string; label: string }[]>>({})
  function ensureCustomDomain(domainId: string): void {
    if (domainId in customDomainItems) return
    setCustomDomainItems((prev) => ({ ...prev, [domainId]: [] })) // mark in-flight
    getCustomDomain(domainId, accessToken)
      .then((d) =>
        setCustomDomainItems((prev) => ({
          ...prev,
          [domainId]: d.values
            .filter((v) => v.active)
            .sort((a, b) => a.sortOrder - b.sortOrder)
            .map(({ code, label }) => ({ code, label })),
        })),
      )
      .catch(() => {
        /* free-typing still works */
      })
  }

  function domainItems(col: LayerColumn | undefined): { code: string; label: string }[] | null {
    if (col?.customDomain) {
      const items = customDomainItems[col.customDomain]
      return items?.length ? items : null
    }
    return null
  }

  function addRow(): void {
    keySeq.current += 1
    setRows((prev) => [...prev, { key: keySeq.current, field: '', op: 'eq', value: '', value2: '' }])
  }

  function removeRow(key: number): void {
    setRows((prev) => prev.filter((r) => r.key !== key))
  }

  function patchRow(key: number, patch: Partial<Row>): void {
    setRows((prev) => prev.map((r) => (r.key === key ? { ...r, ...patch } : r)))
  }

  // Field picked on a row: reset op to the first valid one for the new field's kind + clear the values, and
  // warm the unique-values datalist for a non-coded field.
  function onPickField(key: number, field: string): void {
    const col = colByName.get(field)
    const kind = col ? kindOf(col) : 'text'
    patchRow(key, { field, op: OPS_BY_KIND[kind][0].op, value: '', value2: '' })
    if (col?.customDomain) ensureCustomDomain(col.customDomain)
    else if (col && !col.customDomain && field) ensureUniqueValues(field)
  }

  function apply(): void {
    const conds = conditionsFromRows(rows)
    if (conds.length > MAX_CONDITIONS) {
      setError(`Too many conditions — up to ${MAX_CONDITIONS} (a "between" counts as two).`)
      return
    }
    setError(null)
    onApply(conds)
  }

  function clearAll(): void {
    setRows([])
    setError(null)
    onApply([])
  }

  const listId = (field: string): string => `filter-vals-${layerId}-${field}`

  // The value control for a row, chosen by the field's kind and the selected operator.
  function valueControl(r: Row): React.ReactNode {
    const col = colByName.get(r.field)
    if (!col) return null
    const kind = kindOf(col)
    const items = domainItems(col)

    if (kind === 'coded' && r.op === 'in') {
      // "is any of" over a coded field — checkboxes; the row value is the comma-joined selected codes.
      const selected = new Set(r.value.split(',').map((s) => s.trim()).filter(Boolean))
      const toggle = (code: string): void => {
        const next = new Set(selected)
        if (next.has(code)) next.delete(code)
        else next.add(code)
        patchRow(r.key, { value: [...next].join(',') })
      }
      return items ? (
        <div className="filter-panel__checks">
          {items.map((it) => (
            <label key={it.code} className="filter-panel__check">
              <input type="checkbox" checked={selected.has(it.code)} onChange={() => toggle(it.code)} />
              {it.label}
            </label>
          ))}
        </div>
      ) : (
        <input
          className="filter-panel__value"
          type="text"
          value={r.value}
          placeholder="code1, code2"
          onChange={(e) => patchRow(r.key, { value: e.target.value })}
        />
      )
    }

    if (kind === 'coded' && items) {
      return (
        <select
          className="filter-panel__value"
          value={r.value}
          onChange={(e) => patchRow(r.key, { value: e.target.value })}
        >
          <option value="">Choose…</option>
          {items.map((it) => (
            <option key={it.code} value={it.code}>
              {it.label}
            </option>
          ))}
        </select>
      )
    }

    if (kind === 'boolean') {
      return (
        <select
          className="filter-panel__value"
          value={r.value}
          onChange={(e) => patchRow(r.key, { value: e.target.value })}
        >
          <option value="">Choose…</option>
          <option value="true">true</option>
          <option value="false">false</option>
        </select>
      )
    }

    const inputType = kind === 'number' ? 'number' : kind === 'date' ? 'date' : 'text'
    if (r.op === 'between') {
      return (
        <span className="filter-panel__between">
          <input
            className="filter-panel__value"
            type={inputType}
            value={r.value}
            aria-label="From"
            onChange={(e) => patchRow(r.key, { value: e.target.value })}
          />
          <span className="filter-panel__and">and</span>
          <input
            className="filter-panel__value"
            type={inputType}
            value={r.value2}
            aria-label="To"
            onChange={(e) => patchRow(r.key, { value2: e.target.value })}
          />
        </span>
      )
    }

    // text/number/date single value. Text fields get a datalist of capped distinct values (free-type stays).
    const showList = kind === 'text' && (uniqueValues[r.field]?.length ?? 0) > 0
    return (
      <>
        <input
          className="filter-panel__value"
          type={inputType}
          value={r.value}
          placeholder={r.op === 'in' ? 'value1, value2' : r.op === 'ilike' ? 'contains…' : 'value'}
          list={showList ? listId(r.field) : undefined}
          onChange={(e) => patchRow(r.key, { value: e.target.value })}
        />
        {showList && (
          <datalist id={listId(r.field)}>
            {uniqueValues[r.field].map((v) => (
              <option key={v} value={v} />
            ))}
          </datalist>
        )}
      </>
    )
  }

  return (
    <div className="styles-panel filter-panel">
      <div className="styles-panel__header">
        <h3 className="styles-panel__title">Filter · {layerLabel}</h3>
        <CloseButton onClick={onClose} />
      </div>

      <p className="styles-panel__muted">Show features where all of these are true:</p>

      {columnsError ? (
        <p className="sign-form__error">Could not load fields ({columnsError})</p>
      ) : !columns ? (
        <p className="styles-panel__muted">Loading fields…</p>
      ) : (
        <>
          {rows.length === 0 && <p className="styles-panel__muted">No conditions — the whole layer shows.</p>}
          <ul className="filter-panel__rows">
            {rows.map((r) => {
              const col = colByName.get(r.field)
              const kind = col ? kindOf(col) : 'text'
              return (
                <li className="filter-panel__row" key={r.key}>
                  <div className="filter-panel__row-main">
                    <select
                      className="filter-panel__field"
                      value={r.field}
                      aria-label="Field"
                      onChange={(e) => onPickField(r.key, e.target.value)}
                    >
                      <option value="">Field…</option>
                      {columns.map((c) => (
                        <option key={c.name} value={c.name}>
                          {c.label ?? fieldLabel(c.name)}
                        </option>
                      ))}
                    </select>
                    <select
                      className="filter-panel__op"
                      value={r.op}
                      aria-label="Operator"
                      disabled={!r.field}
                      onChange={(e) => patchRow(r.key, { op: e.target.value as UiOp, value: '', value2: '' })}
                    >
                      {OPS_BY_KIND[kind].map((o) => (
                        <option key={o.op} value={o.op}>
                          {o.label}
                        </option>
                      ))}
                    </select>
                    <CloseButton
                      onClick={() => removeRow(r.key)}
                      label="Remove condition"
                      className="filter-panel__remove"
                      title="Remove condition"
                    />
                  </div>
                  {r.field && <div className="filter-panel__row-value">{valueControl(r)}</div>}
                </li>
              )
            })}
          </ul>

          <button type="button" className="btn btn--sm filter-panel__add" onClick={addRow}>
            + Add condition
          </button>
        </>
      )}

      {error && <p className="sign-form__error">{error}</p>}

      <div className="sign-form__actions">
        <button type="button" className="btn btn--sm" onClick={clearAll} disabled={!columns}>
          Clear all
        </button>
        <button type="button" className="btn btn--sm" onClick={onClose}>
          Cancel
        </button>
        <button type="button" className="btn btn--sm" onClick={apply} disabled={!columns}>
          Apply
        </button>
      </div>
    </div>
  )
}
