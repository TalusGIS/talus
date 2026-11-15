import { useEffect, useMemo, useState } from 'react'
import { X } from 'lucide-react'

import {
  ApiError,
  createLayerView,
  fetchJoinTableColumns,
  fetchMyLayers,
  fetchViewSourceColumns,
  listCustomFields,
} from '@/lib/api'
import type {
  CustomField,
  JoinTableColumnsResponse,
  MyLayer,
  ViewFilterInput,
  ViewFilterOp,
  ViewJoinInput,
  ViewSourceColumn,
} from '@/lib/api'

/**
 * Talus 38 — the "Create view" builder, mounted as the **Views** tab of a layer's Manage dialog
 * (next to Metadata). Authors a sanitized, read-only lens of THIS layer: pick the columns to expose
 * (an allow-list) + attribute filter conditions (the SAME field:op:value grammar the feature endpoint
 * filters with) → name → create. The result is its own catalog item with its own sharing (share it
 * via that item's ⚙ Manage afterwards). Sanitization is DB-enforced server-side (the PG view only
 * contains the allow-listed columns + filter-passing rows). The Manage dialog only opens for an
 * owner/admin, which is exactly the author gate (admin OR source owner) — the API re-enforces.
 */

const PALETTE = ['#6a4c93', '#2a9d8f', '#c0392b', '#1f4d7a', '#d35400', '#16a085']

const OPS: { op: ViewFilterOp; label: string }[] = [
  { op: 'eq', label: 'equals' },
  { op: 'in', label: 'is any of (comma-sep)' },
  { op: 'gte', label: '≥' },
  { op: 'lte', label: '≤' },
  { op: 'ilike', label: 'contains' },
]

interface DraftFilter {
  field: string
  op: ViewFilterOp
  value: string
}

const ALIAS_RE = /^[a-z][a-z0-9_]{0,20}$/
// PG identifier cap — the server 422s past it; mirror client-side so the preview warns first.
const MAX_IDENT = 63

/** Default join alias from the table's display name: lowercased, non-alphanumerics collapsed to
 *  `_`, forced to start with a letter, capped at 21 chars (the server's alias grammar). */
function defaultAlias(name: string): string {
  const slug = name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .slice(0, 21)
  return ALIAS_RE.test(slug) ? slug : 't'
}

export default function CreateViewSection({
  source,
  accessToken,
  onCreated,
  onDone,
}: {
  source: MyLayer
  accessToken: string
  /** Refresh the catalog behind the dialog so the new view row appears. */
  onCreated: (layerId: string, name: string) => void
  /** Close the hosting dialog. After a successful create the ONLY offered action is "Done" →
   *  this — re-submitting the pristine reset form would mint a near-duplicate view. */
  onDone: () => void
}) {
  const sourceName = source.displayName ?? source.id

  const [columns, setColumns] = useState<ViewSourceColumn[] | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)

  const [title, setTitle] = useState(`${sourceName} (view)`)
  const [color, setColor] = useState(PALETTE[0])
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [filters, setFilters] = useState<DraftFilter[]>([])
  // Sort the column list by field name (A–Z) vs the database/ordinal order it arrives in. With dozens
  // of fields, name-sort makes a specific column findable; field-order preserves table structure.
  const [sortByName, setSortByName] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [created, setCreated] = useState<string | null>(null)

  // --- join a table (V080, optional). Lazy: nothing fetched until the author opts in. ---
  const [joinEnabled, setJoinEnabled] = useState(false)
  const [joinTables, setJoinTables] = useState<MyLayer[] | null>(null)
  const [sourceFields, setSourceFields] = useState<CustomField[]>([])
  const [joinTableId, setJoinTableId] = useState('')
  const [joinCols, setJoinCols] = useState<JoinTableColumnsResponse | null>(null)
  const [joinError, setJoinError] = useState<string | null>(null)
  const [layerKey, setLayerKey] = useState('')
  const [tableKey, setTableKey] = useState('')
  const [alias, setAlias] = useState('')
  const [sortKey, setSortKey] = useState('')
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc')
  const [joinPicked, setJoinPicked] = useState<Set<string>>(new Set())

  useEffect(() => {
    if (!joinEnabled || joinTables !== null) return
    let cancelled = false
    // Candidate tables = standalone tables the caller can manage (owner; admins via canManage) —
    // the same population the Fork-6 server gate will accept. The server re-enforces regardless.
    fetchMyLayers(accessToken)
      .then((res) => {
        if (cancelled) return
        setJoinTables(res.layers.filter((l) => l.geomType === 'none' && l.canManage))
      })
      .catch(() => {
        if (!cancelled) setJoinError('Couldn’t load your tables')
      })
    // The layer-side key may also be an owner-defined field; built-ins simply have none.
    listCustomFields(source.id, accessToken)
      .then((res) => {
        if (!cancelled) setSourceFields(res.fields.filter((f) => f.active))
      })
      .catch(() => {
        if (!cancelled) setSourceFields([])
      })
    return () => {
      cancelled = true
    }
  }, [joinEnabled, joinTables, source.id, accessToken])

  useEffect(() => {
    if (joinTableId === '') {
      setJoinCols(null)
      return
    }
    let cancelled = false
    setJoinError(null)
    fetchJoinTableColumns(joinTableId, accessToken)
      .then((res) => {
        if (cancelled) return
        setJoinCols(res)
        setJoinPicked(new Set())
        const table = (joinTables ?? []).find((t) => t.id === joinTableId)
        setAlias(defaultAlias(table?.displayName ?? joinTableId))
        // Sensible sort default for "latest wins": updated_at when present, else the first column.
        const names = res.columns.map((c) => c.name)
        setSortKey(names.includes('updated_at') ? 'updated_at' : (names[0] ?? ''))
        setTableKey('')
      })
      .catch((err) => {
        if (!cancelled)
          setJoinError(err instanceof ApiError ? `Couldn’t load table columns (HTTP ${err.status})` : 'Couldn’t load table columns')
      })
    return () => {
      cancelled = true
    }
  }, [joinTableId, accessToken, joinTables])

  // Every join-side option carries (value, label) — physical columns as-is, field keys labeled.
  // The columns list now carries the source's owner-defined fields too (`field: true`); the
  // sourceFields fetch stays the labeled authority here, so skip them in the cols half.
  const layerKeyOptions = useMemo(() => {
    const cols = (columns ?? [])
      .filter((c) => !c.geometry && !c.field)
      .map((c) => ({ value: c.name, label: c.name }))
    const fields = sourceFields.map((f) => ({ value: f.fieldKey, label: `${f.fieldKey} (${f.label})` }))
    return [...cols, ...fields]
  }, [columns, sourceFields])
  const tableKeyOptions = useMemo(() => {
    if (joinCols === null) return []
    return [
      ...joinCols.columns.map((c) => ({ value: c.name, label: c.name })),
      ...joinCols.fields.map((f) => ({ value: f.key, label: `${f.key} (${f.label})` })),
    ]
  }, [joinCols])

  const aliasBad = joinEnabled && alias !== '' && !ALIAS_RE.test(alias)
  const joinNameTooLong = useMemo(
    () => [...joinPicked].some((k) => `${alias}_${k}`.length > MAX_IDENT),
    [joinPicked, alias],
  )
  const joinIncomplete =
    joinEnabled &&
    (joinTableId === '' || layerKey === '' || tableKey === '' || sortKey === '' ||
      alias === '' || aliasBad || joinPicked.size === 0 || joinNameTooLong)

  function toggleJoinColumn(name: string): void {
    setJoinPicked((prev) => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }

  useEffect(() => {
    let cancelled = false
    fetchViewSourceColumns(source.id, accessToken)
      .then((res) => {
        if (!cancelled) setColumns(res.columns)
      })
      .catch((err) => {
        if (!cancelled)
          setLoadError(err instanceof ApiError ? `Couldn’t load columns (HTTP ${err.status})` : 'Couldn’t load columns')
      })
    return () => {
      cancelled = true
    }
  }, [source.id, accessToken])

  const autoColumns = useMemo(() => (columns ?? []).filter((c) => c.auto), [columns])
  // The pickable columns in the chosen order (name A–Z or the source's ordinal order).
  const pickable = useMemo(() => {
    const list = (columns ?? []).filter((c) => !c.auto)
    return sortByName ? [...list].sort((a, b) => a.name.localeCompare(b.name)) : list
  }, [columns, sortByName])
  // Every column is a valid filter FIELD (you can filter on a column you don't expose — it only
  // narrows). Same sort as the checkbox list for consistency.
  const filterFields = useMemo(() => {
    const list = columns ?? []
    return sortByName ? [...list].sort((a, b) => a.name.localeCompare(b.name)) : list
  }, [columns, sortByName])

  const nameMissing = title.trim() === ''
  const incompleteFilter = filters.some((f) => !f.field || f.value.trim() === '')

  function toggleColumn(name: string): void {
    setPicked((prev) => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }
  const selectAll = (): void => setPicked(new Set(pickable.map((c) => c.name)))
  const selectNone = (): void => setPicked(new Set())

  function addFilter(): void {
    const firstField = filterFields[0]?.name ?? ''
    setFilters((prev) => [...prev, { field: firstField, op: 'eq', value: '' }])
  }
  function updateFilter(i: number, patch: Partial<DraftFilter>): void {
    setFilters((prev) => prev.map((f, idx) => (idx === i ? { ...f, ...patch } : f)))
  }
  function removeFilter(i: number): void {
    setFilters((prev) => prev.filter((_, idx) => idx !== i))
  }

  function resetForm(): void {
    setTitle(`${sourceName} (view)`)
    setColor(PALETTE[0])
    setPicked(new Set())
    setFilters([])
    setJoinEnabled(false)
    setJoinTableId('')
    setJoinCols(null)
    setLayerKey('')
    setTableKey('')
    setAlias('')
    setSortKey('')
    setSortDir('desc')
    setJoinPicked(new Set())
  }

  async function submit(): Promise<void> {
    if (nameMissing || incompleteFilter || joinIncomplete) return
    setBusy(true)
    setError(null)
    setCreated(null)
    try {
      const payloadFilters: ViewFilterInput[] = filters.map((f) => ({ field: f.field, op: f.op, value: f.value }))
      const join: ViewJoinInput | null = joinEnabled
        ? {
            joinTableId,
            layerKey,
            tableKey,
            alias,
            columns: [...joinPicked],
            sortKey,
            sortDir,
          }
        : null
      const resp = await createLayerView(
        {
          sourceLayerId: source.id,
          displayName: title.trim(),
          displayColor: color,
          allowedColumns: [...picked],
          filters: payloadFilters,
          join,
        },
        accessToken,
      )
      setCreated(resp.display_name)
      resetForm()
      onCreated(resp.layer_id, resp.display_name)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Create failed')
    } finally {
      setBusy(false)
    }
  }

  if (loadError !== null) return <p className="sign-form__error">{loadError}</p>
  if (columns === null) return <p className="modal__muted">Loading columns…</p>

  return (
    <>
      <section className="modal__section">
        <p className="modal__muted">
          Create a sanitized, read-only view of “{sourceName}” — choose what it exposes, then share it
          separately from its own catalog row.
        </p>
      </section>

      {/* ① Columns — the allow-list. id + geom are always included. */}
      <section className="modal__section">
        <div className="viewbuilder__colhead">
          <h3 className="modal__section-title">Columns to expose</h3>
          <div className="viewbuilder__colactions">
            <button type="button" className="btn btn--xs" onClick={selectAll}>
              Select all
            </button>
            <button type="button" className="btn btn--xs" onClick={selectNone}>
              Select none
            </button>
            <button
              type="button"
              className="btn btn--xs"
              onClick={() => setSortByName((s) => !s)}
              title="Toggle column order"
            >
              {sortByName ? 'Sort: A–Z' : 'Sort: field order'}
            </button>
          </div>
        </div>
        {autoColumns.length > 0 && (
          <p className="modal__muted">Always included: {autoColumns.map((c) => c.name).join(', ')}</p>
        )}
        <div className="viewbuilder__cols">
          {pickable.map((c) => (
            <label key={c.name} className="viewbuilder__col">
              <input type="checkbox" checked={picked.has(c.name)} onChange={() => toggleColumn(c.name)} />
              <span>{c.field && c.label ? `${c.name} (${c.label})` : c.name}</span>
              <span className="modal__muted viewbuilder__coltype">{c.type}</span>
            </label>
          ))}
          {pickable.length === 0 && <p className="modal__muted">No additional columns to expose.</p>}
        </div>
      </section>

      {/* ② Filters — the baked WHERE. */}
      <section className="modal__section">
        <h3 className="modal__section-title">Filter rows (optional)</h3>
        <p className="modal__muted">Only rows matching ALL conditions are exposed by the view.</p>
        {filters.map((f, i) => (
          <div key={i} className="viewbuilder__filter">
            <select aria-label="Field" value={f.field} onChange={(e) => updateFilter(i, { field: e.target.value })}>
              {filterFields.map((c) => (
                <option key={c.name} value={c.name}>
                  {c.field && c.label ? `${c.name} (${c.label})` : c.name}
                </option>
              ))}
            </select>
            <select aria-label="Operator" value={f.op} onChange={(e) => updateFilter(i, { op: e.target.value as ViewFilterOp })}>
              {OPS.map((o) => (
                <option key={o.op} value={o.op}>
                  {o.label}
                </option>
              ))}
            </select>
            <input
              type="text"
              aria-label="Value"
              placeholder="value"
              value={f.value}
              onChange={(e) => updateFilter(i, { value: e.target.value })}
            />
            <button type="button" className="btn btn--sm" aria-label="Remove filter" onClick={() => removeFilter(i)}>
              <X size={13} aria-hidden="true" />
            </button>
          </div>
        ))}
        <button type="button" className="btn btn--sm" onClick={addFilter}>
          + Add filter
        </button>
      </section>

      {/* ②½ Join a table (V080, optional) — one LEFT JOIN, 1:first by the chosen sort. */}
      <section className="modal__section">
        <h3 className="modal__section-title">Join a table (optional)</h3>
        <label className="viewbuilder__col">
          <input
            type="checkbox"
            checked={joinEnabled}
            onChange={(e) => setJoinEnabled(e.target.checked)}
          />
          <span>Add columns from a related table (e.g. each hydrant’s latest inspection)</span>
        </label>
        {joinEnabled && (
          <>
            <p className="modal__muted">
              One table, matched on a key pair. When several rows match, the FIRST by the sort
              below is joined. Joined columns are read-only and prefixed with the alias. You must
              own the table (or be an admin) — joining republishes its columns under this view’s
              own sharing.
            </p>
            <div className="viewbuilder__filter">
              <select
                aria-label="Table to join"
                value={joinTableId}
                onChange={(e) => setJoinTableId(e.target.value)}
              >
                <option value="">— pick a table —</option>
                {(joinTables ?? []).map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.displayName ?? t.id}
                  </option>
                ))}
              </select>
            </div>
            {joinTables !== null && joinTables.length === 0 && (
              <p className="modal__muted">No standalone tables you can manage were found.</p>
            )}
            {joinError !== null && <p className="sign-form__error">{joinError}</p>}
            {joinCols !== null && (
              <>
                <div className="viewbuilder__filter">
                  <select aria-label="Layer key" value={layerKey} onChange={(e) => setLayerKey(e.target.value)}>
                    <option value="">— layer key —</option>
                    {layerKeyOptions.map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </select>
                  <span className="modal__muted">=</span>
                  <select aria-label="Table key" value={tableKey} onChange={(e) => setTableKey(e.target.value)}>
                    <option value="">— table key —</option>
                    {tableKeyOptions.map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="viewbuilder__filter">
                  <span className="modal__muted">First by</span>
                  <select aria-label="Sort field" value={sortKey} onChange={(e) => setSortKey(e.target.value)}>
                    {tableKeyOptions.map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </select>
                  <select
                    aria-label="Sort direction"
                    value={sortDir}
                    onChange={(e) => setSortDir(e.target.value === 'asc' ? 'asc' : 'desc')}
                  >
                    <option value="desc">newest / highest first</option>
                    <option value="asc">oldest / lowest first</option>
                  </select>
                </div>
                <label className="sign-form__field">
                  <span>Column prefix (alias)</span>
                  <input
                    type="text"
                    value={alias}
                    onChange={(e) => setAlias(e.target.value)}
                    aria-invalid={aliasBad}
                  />
                </label>
                {aliasBad && (
                  <p className="sign-form__error">
                    Alias must be 1–21 chars: lowercase letters, digits, underscores; starts with a letter.
                  </p>
                )}
                <p className="modal__muted">Columns to bring in (shown as {alias || 'alias'}_…):</p>
                <div className="viewbuilder__cols">
                  {tableKeyOptions.map((o) => (
                    <label key={o.value} className="viewbuilder__col">
                      <input
                        type="checkbox"
                        checked={joinPicked.has(o.value)}
                        onChange={() => toggleJoinColumn(o.value)}
                      />
                      <span>
                        {alias || 'alias'}_{o.value}
                      </span>
                    </label>
                  ))}
                </div>
                {joinNameTooLong && (
                  <p className="sign-form__error">
                    A joined column name exceeds {MAX_IDENT} characters — shorten the alias.
                  </p>
                )}
              </>
            )}
          </>
        )}
      </section>

      {/* ③ Details. */}
      <section className="modal__section">
        <h3 className="modal__section-title">Details</h3>
        <label className="sign-form__field">
          <span>Title *</span>
          <input type="text" value={title} onChange={(e) => setTitle(e.target.value)} />
        </label>
        <div className="sign-form__field">
          <span>Color</span>
          <div className="modal__swatches">
            {PALETTE.map((c) => (
              <button
                key={c}
                type="button"
                className={`modal__swatch${color === c ? ' is-selected' : ''}`}
                style={{ background: c }}
                aria-label={`Color ${c}`}
                aria-pressed={color === c}
                onClick={() => setColor(c)}
              />
            ))}
          </div>
        </div>
      </section>

      {created && <p className="modal__muted">Created view “{created}”. Find it in the catalog to share it.</p>}
      {error !== null && <p className="sign-form__error">{error}</p>}

      <div className="sign-form__actions">
        {created ? (
          <button type="button" className="btn btn--sm" onClick={onDone}>
            Done
          </button>
        ) : (
          <button
            type="button"
            className="btn btn--sm"
            onClick={() => void submit()}
            disabled={busy || nameMissing || incompleteFilter || joinIncomplete}
          >
            {busy ? 'Creating…' : 'Create view'}
          </button>
        )}
      </div>
    </>
  )
}
