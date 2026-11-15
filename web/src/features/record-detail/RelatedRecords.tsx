import { useEffect, useMemo, useState } from 'react'
import { useAuth } from 'react-oidc-context'

import { ApiError, fetchLayerColumns, fetchLayerRelates, queryLayerFeatures } from '@/lib/api'
import type { LayerColumn, LayerFeature, LayerRelate } from '@/lib/api'
import { cell, cellTitle, colLabel, columnKind, isBlank } from '@/features/catalog/format'
import PanelSection from '@/components/PanelSection'

/**
 * Related records in the record-detail drawer (decision-relates-and-join-views.md PR A): one
 * PanelSection per relate on this layer — "Inspections (3)" — listing the table rows whose related
 * key matches this feature's key value. It used to be "collapsible-FEELING" (its own words): a
 * fixed-open block wearing a section header. It collapses through the same control as everything
 * else in the drawer now (handoff-record-table-3 §2.2).
 *
 * Security shape: the relate list arrives ALREADY viewer-filtered (the server omits relates to
 * tables this caller can't view — no count, no name), and each row read goes through the table's
 * own gated features endpoint. If a read still fails (sharing changed mid-session), the section
 * degrades to a quiet note — never a leak, never a broken drawer.
 */

// How many related rows the drawer shows inline; the rest is a "+N more" note. The drawer is a
// glance surface — the full grid lives in the table's own catalog Data view.
const ROW_LIMIT = 5
// Attribute columns per row line: the first few meaningful table columns, id excluded.
const COLS_PER_ROW = 3

const SKIP_ROW_COLS = new Set([
  'id', 'props', 'row_version', 'ai_provenance', 'cover_photo_id',
  'created_at', 'created_by', 'updated_at', 'updated_by',
])

export default function RelatedRecords({
  layerId,
  recordId,
  properties,
}: {
  layerId: string
  recordId: string
  properties: Record<string, unknown>
}) {
  const auth = useAuth()
  const token = auth.user?.access_token
  const [relates, setRelates] = useState<LayerRelate[] | null>(null)

  useEffect(() => {
    setRelates(null)
    if (!token) return
    let active = true
    const ctrl = new AbortController()
    fetchLayerRelates(layerId, token, ctrl.signal)
      .then((r) => {
        if (active) setRelates(r)
      })
      .catch(() => {
        if (active) setRelates([]) // no access / no relates → no section, quietly
      })
    return () => {
      active = false
      ctrl.abort()
    }
  }, [layerId, token])

  if (!relates || relates.length === 0) return null

  return (
    <>
      {relates.map((r) => (
        <RelateBlock
          key={r.id}
          relate={r}
          recordId={recordId}
          properties={properties}
          token={token}
        />
      ))}
    </>
  )
}

/** This feature's value for the relate's origin key: a real column reads from properties, an
 * owner-defined field from the nested props object; `id` comes from the feature envelope. */
function originValue(
  key: string,
  recordId: string,
  properties: Record<string, unknown>,
): unknown {
  if (key === 'id') return recordId
  if (key in properties) return properties[key]
  return (properties.props as Record<string, unknown> | null | undefined)?.[key]
}

function RelateBlock({
  relate,
  recordId,
  properties,
  token,
}: {
  relate: LayerRelate
  recordId: string
  properties: Record<string, unknown>
  token: string | undefined
}) {
  const value = originValue(relate.originFieldKey, recordId, properties)
  const hasKey = value !== null && value !== undefined && value !== ''

  const [rows, setRows] = useState<LayerFeature[] | null>(null)
  const [total, setTotal] = useState(0)
  const [columns, setColumns] = useState<LayerColumn[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setRows(null)
    setError(null)
    if (!token || !hasKey) return
    let active = true
    const ctrl = new AbortController()
    const limit = relate.cardinality === '1:1' ? 1 : ROW_LIMIT
    Promise.all([
      fetchLayerColumns(relate.relatedTableId, token),
      queryLayerFeatures(
        relate.relatedTableId,
        { filters: [`${relate.relatedFieldKey}:eq:${String(value)}`], limit },
        token,
        ctrl.signal,
      ),
    ])
      .then(([cols, fc]) => {
        if (!active) return
        setColumns(cols.columns)
        setRows(fc.features)
        setTotal(fc.numberMatched)
      })
      .catch((e) => {
        if (!active || e?.name === 'AbortError') return
        setError(e instanceof ApiError ? e.message : String(e))
      })
    return () => {
      active = false
      ctrl.abort()
    }
  }, [relate, value, hasKey, token])

  const codeLabels: Record<string, Record<string, string>> = {}
  // The first few meaningful table columns for the one-line row summary. The relate's own key
  // column is excluded — its value is identical on every listed row (it's what matched).
  const rowCols = useMemo(
    () =>
      (columns ?? [])
        .filter((c) => !SKIP_ROW_COLS.has(c.name) && c.name !== relate.relatedFieldKey)
        .slice(0, COLS_PER_ROW),
    [columns, relate.relatedFieldKey],
  )

  return (
    <PanelSection title={relate.displayName} count={rows !== null ? total : undefined}>
      {!hasKey && <div className="panel-section__note">No {relate.originFieldKey} on this record.</div>}
      {hasKey && rows === null && !error && <div className="panel-section__note">Loading…</div>}
      {error && <div className="panel-section__note">Couldn’t load related rows ({error})</div>}

      {rows !== null && rows.length === 0 && (
        <div className="panel-section__note">No related rows.</div>
      )}

      {rows !== null && rows.length > 0 && (
        <ul className="panel-section__list">
          {rows.map((f) => (
            <li className="panel-section__row" key={f.id ?? JSON.stringify(f.properties)}>
              {/* §3.4 — a related row is DATA, so it renders as labelled attribute pairs on the
                  drawer's own grid. It used to reuse the attachment metadata line, which stacked
                  "Label: value" spans and read as file metadata rather than a record. */}
              <dl className="panel-section__attrs">
                {rowCols.map((c) => (
                  <div className="panel-section__attr" key={c.name}>
                    <dt className="panel-section__attr-k">{colLabel(c)}</dt>
                    <dd
                      className="panel-section__attr-v"
                      data-kind={columnKind(c)}
                      data-null={isBlank(c, f.properties) || undefined}
                      title={cellTitle(c, f.properties)}
                    >
                      {cell(c, f.properties, codeLabels)}
                    </dd>
                  </div>
                ))}
              </dl>
            </li>
          ))}
        </ul>
      )}

      {rows !== null && total > rows.length && (
        <div className="panel-section__note">
          …and {total - rows.length} more — open the table in the catalog to see all.
        </div>
      )}
    </PanelSection>
  )
}
