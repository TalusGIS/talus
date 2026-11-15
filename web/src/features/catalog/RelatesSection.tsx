import { useEffect, useMemo, useState } from 'react'
import { X } from 'lucide-react'

import {
  ApiError,
  createLayerRelate,
  deleteLayerRelate,
  fetchLayerColumns,
  fetchLayerRelates,
  fetchMyLayers,
} from '@/lib/api'
import type { LayerColumn, LayerRelate, MyLayer } from '@/lib/api'
import { colLabel } from './format'
import useConfirm from '@/components/useConfirm'

/**
 * Relates authoring (decision-relates-and-join-views.md PR A) — the **Relates** tab of a spatial
 * layer's Manage dialog. A relate is pure navigation metadata: "this layer's <key> matches that
 * table's <key>" (sign_id ↔ inspections.sign_id), surfaced as a related-records section in the
 * record-detail drawer. No FK is created, nothing cascades, rows are never edited through it —
 * the documented-FK fence from the nonspatial-tables arc.
 *
 * The table picker lists only tables THIS author can view (fetchMyLayers is already
 * viewer-scoped), and the server re-checks visibility + both keys at create time. Viewers of the
 * layer never see a relate to a table they can't view (server-side fail-closed omission).
 */

// Columns that can't meaningfully key a relate — mirrors the server's _UNRELATABLE_COLUMNS plus
// the bookkeeping columns the drawer hides. The server is the authority; this just keeps the
// pickers sensible.
const UNKEYABLE = new Set([
  'geom', 'props', 'row_version', 'ai_provenance', 'cover_photo_id',
  'created_at', 'created_by', 'updated_at', 'updated_by',
])

function keyable(columns: LayerColumn[]): LayerColumn[] {
  return columns.filter((c) => !UNKEYABLE.has(c.name))
}

export default function RelatesSection({
  layer,
  accessToken,
}: {
  layer: MyLayer
  accessToken: string
}) {
  const layerName = layer.displayName ?? layer.id

  const { confirm, confirmDialog } = useConfirm()
  const [relates, setRelates] = useState<LayerRelate[] | null>(null)
  const [tables, setTables] = useState<MyLayer[] | null>(null)
  const [originCols, setOriginCols] = useState<LayerColumn[] | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)

  // Create-form state.
  const [tableId, setTableId] = useState('')
  const [tableCols, setTableCols] = useState<LayerColumn[] | null>(null)
  const [originKey, setOriginKey] = useState('')
  const [relatedKey, setRelatedKey] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [cardinality, setCardinality] = useState<'1:1' | '1:M'>('1:M')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    Promise.all([
      fetchLayerRelates(layer.id, accessToken),
      fetchMyLayers(accessToken),
      fetchLayerColumns(layer.id, accessToken),
    ])
      .then(([rel, mine, cols]) => {
        if (!active) return
        setRelates(rel)
        setTables(mine.layers.filter((l) => l.geomType === 'none'))
        setOriginCols(keyable(cols.columns))
      })
      .catch((e) => {
        if (active) setLoadError(e instanceof ApiError ? e.message : String(e))
      })
    return () => {
      active = false
    }
  }, [layer.id, accessToken])

  // Load the picked table's columns for the related-key picker (reset the key on table change).
  useEffect(() => {
    setTableCols(null)
    setRelatedKey('')
    if (!tableId) return
    let active = true
    fetchLayerColumns(tableId, accessToken)
      .then((cols) => {
        if (active) setTableCols(keyable(cols.columns))
      })
      .catch((e) => {
        if (active) setError(e instanceof ApiError ? e.message : String(e))
      })
    return () => {
      active = false
    }
  }, [tableId, accessToken])

  // Default the display name to the picked table's name (editable; the drawer section header).
  const pickedTable = useMemo(() => tables?.find((t) => t.id === tableId), [tables, tableId])
  useEffect(() => {
    if (pickedTable) setDisplayName(pickedTable.displayName ?? pickedTable.id)
  }, [pickedTable])

  const incomplete = !tableId || !originKey || !relatedKey || displayName.trim() === ''

  async function submit(): Promise<void> {
    if (incomplete) return
    setBusy(true)
    setError(null)
    try {
      const created = await createLayerRelate(
        layer.id,
        {
          relatedTableId: tableId,
          originFieldKey: originKey,
          relatedFieldKey: relatedKey,
          displayName: displayName.trim(),
          cardinality,
        },
        accessToken,
      )
      setRelates((prev) => [...(prev ?? []), created])
      setTableId('')
      setOriginKey('')
      setRelatedKey('')
      setDisplayName('')
      setCardinality('1:M')
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  function remove(r: LayerRelate): void {
    confirm({
      title: `Delete the relate “${r.displayName}”?`,
      consequence:
        'No data is touched. A relate is navigation metadata, so this only removes the connection — both layers and every row in them stay as they are.',
      confirmLabel: 'Delete relate',
      onConfirm: async () => {
        setBusy(true)
        setError(null)
        try {
          await deleteLayerRelate(layer.id, r.id, accessToken)
          setRelates((prev) => (prev ?? []).filter((x) => x.id !== r.id))
        } catch (e) {
          setError(e instanceof ApiError ? e.message : String(e))
        } finally {
          setBusy(false)
        }
      },
    })
  }

  if (loadError !== null) return <p className="sign-form__error">{loadError}</p>
  if (relates === null || tables === null || originCols === null)
    return <p className="modal__muted">Loading relates…</p>

  return (
    <>
      <section className="modal__section">
        <p className="modal__muted">
          Relate “{layerName}” to a table on a matching key — clicking a feature then shows its
          related rows in the record detail. Navigation only: no constraint is created and related
          rows stay read-through-the-table.
        </p>
      </section>

      {/* Existing relates. */}
      <section className="modal__section">
        <h3 className="modal__section-title">Relates</h3>
        {relates.length === 0 && <p className="modal__muted">No relates yet.</p>}
        {relates.map((r) => (
          <div key={r.id} className="viewbuilder__filter">
            <span>
              <strong>{r.displayName}</strong>
              <span className="modal__muted">
                {' '}
                — {r.originFieldKey} ↔ {r.relatedTableName}.{r.relatedFieldKey} ({r.cardinality})
              </span>
            </span>
            <button
              type="button"
              className="btn btn--sm"
              aria-label={`Delete relate ${r.displayName}`}
              onClick={() => remove(r)}
              disabled={busy}
            >
              <X size={13} aria-hidden="true" />
            </button>
          </div>
        ))}
      </section>

      {/* New relate. */}
      <section className="modal__section">
        <h3 className="modal__section-title">Add a relate</h3>
        {tables.length === 0 ? (
          <p className="modal__muted">
            No tables available — create a standalone table in the catalog first (Catalog → Tables).
          </p>
        ) : (
          <>
            <label className="sign-form__field">
              <span>Table *</span>
              <select value={tableId} onChange={(e) => setTableId(e.target.value)}>
                <option value="">Choose a table…</option>
                {tables.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.displayName ?? t.id}
                  </option>
                ))}
              </select>
            </label>
            <label className="sign-form__field">
              <span>This layer’s key *</span>
              <select value={originKey} onChange={(e) => setOriginKey(e.target.value)}>
                <option value="">Choose a field…</option>
                {originCols.map((c) => (
                  <option key={c.name} value={c.name}>
                    {colLabel(c)}
                  </option>
                ))}
              </select>
            </label>
            <label className="sign-form__field">
              <span>Table’s key *</span>
              <select
                value={relatedKey}
                onChange={(e) => setRelatedKey(e.target.value)}
                disabled={!tableId || tableCols === null}
              >
                <option value="">{tableId ? (tableCols === null ? 'Loading…' : 'Choose a field…') : 'Pick a table first'}</option>
                {(tableCols ?? []).map((c) => (
                  <option key={c.name} value={c.name}>
                    {colLabel(c)}
                  </option>
                ))}
              </select>
            </label>
            <label className="sign-form__field">
              <span>Display name *</span>
              <input
                type="text"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                placeholder="e.g. Inspections"
              />
            </label>
            <label className="sign-form__field">
              <span>Cardinality</span>
              <select value={cardinality} onChange={(e) => setCardinality(e.target.value as '1:1' | '1:M')}>
                <option value="1:M">One feature → many rows (1:M)</option>
                <option value="1:1">One feature → one row (1:1)</option>
              </select>
            </label>

            {error !== null && <p className="sign-form__error">{error}</p>}

            <div className="sign-form__actions">
              <button
                type="button"
                className="btn btn--sm"
                onClick={() => void submit()}
                disabled={busy || incomplete}
              >
                {busy ? 'Saving…' : 'Add relate'}
              </button>
            </div>
          </>
        )}
      </section>
      {confirmDialog}
    </>
  )
}
