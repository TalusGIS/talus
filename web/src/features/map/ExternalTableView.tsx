import { useEffect, useState } from 'react'
import { ChevronLeft, ChevronRight } from 'lucide-react'

import {
  fetchExternalColumns,
  fetchExternalRowCount,
  fetchExternalRowGeometry,
  fetchExternalRows,
} from '@/features/map/addFromUrl'
import type { ExternalLayerSpec, ExternalTableColumn } from '@/features/map/addFromUrl'
import type { FilterCondition } from '@/lib/api/client'
import { formatTemporal } from '@/lib/formatTemporal'

/**
 * Read-only attribute table for an EXTERNAL layer (decision-add-from-url.md)
 * — the AGO-style grid over a remote service, rendered in the same docked panel as
 * the Talus TableView but deliberately a SEPARATE lean component: an external reference has no
 * field defs, no editing, no filter-sync, no calc, so fusing it into the shipped grid would trade
 * regression risk for cosmetic parity. arcgis-feature pages/sorts SERVER-side (308k-parcel
 * services cost one ~50-row query per page); geojson sorts/pages its already-cached document.
 * Row click → the caller highlights + zooms to that feature (one geometry query).
 */

const PAGE = 50

export default function ExternalTableView({
  spec,
  onLocate,
  conds = [],
  onOpenFilters,
}: {
  spec: ExternalLayerSpec
  onLocate: (geometry: GeoJSON.Geometry) => void
  /** The layer's ONE shared map filter (decision-feature-filter.md) — the grid shows the filtered
   *  rowset and its Filters button opens the same rail FilterPanel (one filter, two entry points). */
  conds?: FilterCondition[]
  onOpenFilters?: () => void
}) {
  const [columns, setColumns] = useState<ExternalTableColumn[]>([])
  const [objectIdField, setObjectIdField] = useState<string | null>(null)
  const [total, setTotal] = useState<number | null>(null)
  const [rows, setRows] = useState<Record<string, unknown>[]>([])
  const [offset, setOffset] = useState(0)
  const [orderBy, setOrderBy] = useState<string | null>(null)
  const [orderDir, setOrderDir] = useState<'asc' | 'desc'>('asc')
  const [sortable, setSortable] = useState(true) // flips off when the service refuses orderByFields
  const [busy, setBusy] = useState(true)
  const [error, setError] = useState<string | null>(null)
  // The geojson-kind document cache is per-table-instance: the map render path keeps its own copy,
  // and a modest duplicate beats threading a shared ref through the dock chrome.
  const [cache] = useState<Record<string, GeoJSON.FeatureCollection>>({})
  // Stable-by-value dep for the shared map filter (the filter-sync condsKey pattern) — a funnel
  // Apply re-queries an OPEN grid live without effect-identity churn.
  const condsKey = JSON.stringify(conds)

  // Columns + count once per layer.
  useEffect(() => {
    let cancelled = false
    setColumns([])
    setTotal(null)
    setOffset(0)
    setOrderBy(null)
    setSortable(true)
    setError(null)
    Promise.all([fetchExternalColumns(spec, cache), fetchExternalRowCount(spec, cache, conds)])
      .then(([cols, count]) => {
        if (cancelled) return
        setColumns(cols.columns)
        setObjectIdField(cols.objectIdField)
        setTotal(count)
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Could not read the service')
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [spec.externalId, condsKey])

  // A page of rows whenever the page/sort changes.
  useEffect(() => {
    let cancelled = false
    setBusy(true)
    fetchExternalRows(spec, { offset, limit: PAGE, orderBy: orderBy ?? undefined, orderDir }, cache, conds)
      .then((r) => {
        if (!cancelled) {
          setRows(r)
          setError(null)
        }
      })
      .catch(() => {
        if (cancelled) return
        if (orderBy !== null) {
          // The service refused orderByFields — degrade to unsorted rather than an empty grid.
          setSortable(false)
          setOrderBy(null)
        } else {
          setError('Could not load rows from the service')
        }
      })
      .finally(() => {
        if (!cancelled) setBusy(false)
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [spec.externalId, offset, orderBy, orderDir, condsKey])

  const sortBy = (name: string): void => {
    if (!sortable) return
    setOffset(0)
    if (orderBy === name) setOrderDir((d) => (d === 'asc' ? 'desc' : 'asc'))
    else {
      setOrderBy(name)
      setOrderDir('asc')
    }
  }

  const locate = (row: Record<string, unknown>): void => {
    if (!objectIdField) return
    const id = row[objectIdField]
    if (id == null) return
    void fetchExternalRowGeometry(spec, objectIdField, id, cache).then((geom) => {
      if (geom) onLocate(geom)
    })
  }

  const from = total === 0 ? 0 : offset + 1
  const to = Math.min(offset + rows.length, offset + PAGE)

  return (
    <div className="exttable">
      <div className="exttable__hdr">
        <span>
          {total !== null ? `${total.toLocaleString()} records` : busy ? 'Loading…' : 'Records'}
          {spec.where && (
            <span className="modal__muted"> · filtered: <code>{spec.where}</code></span>
          )}
        </span>
        <span className="exttable__pager">
          {onOpenFilters && (
            <button
              type="button"
              className={`btn btn--xs${conds.length ? ' exttable__filters--on' : ''}`}
              title="Edit this layer's map filter"
              onClick={onOpenFilters}
            >
              Filters{conds.length ? ` (${conds.length})` : ''}
            </button>
          )}
          <span className="modal__muted">
            {rows.length > 0 ? `${from.toLocaleString()}–${to.toLocaleString()}` : '—'}
          </span>
          <button type="button" className="btn btn--xs" disabled={busy || offset === 0} onClick={() => setOffset((o) => Math.max(0, o - PAGE))}>
            <ChevronLeft size={13} aria-hidden="true" /> Prev
          </button>
          <button
            type="button"
            className="btn btn--xs"
            disabled={busy || rows.length < PAGE || (total !== null && offset + PAGE >= total)}
            onClick={() => setOffset((o) => o + PAGE)}
          >
            Next <ChevronRight size={13} aria-hidden="true" />
          </button>
        </span>
      </div>
      {error !== null ? (
        <p className="sign-form__error">{error}</p>
      ) : (
        <div className="exttable__scroll">
          <table className="data-table exttable__table">
            <thead>
              <tr>
                {columns.map((c) => (
                  <th key={c.name}>
                    <button
                      type="button"
                      className="exttable__sort"
                      disabled={!sortable}
                      title={sortable ? `Sort by ${c.alias}` : 'This service doesn’t support sorting'}
                      onClick={() => sortBy(c.name)}
                    >
                      {c.alias}
                      {orderBy === c.name && <span aria-hidden> {orderDir === 'asc' ? '▲' : '▼'}</span>}
                    </button>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr
                  key={objectIdField && r[objectIdField] != null ? String(r[objectIdField]) : `${offset}-${i}`}
                  className={objectIdField ? 'exttable__row--locatable' : undefined}
                  title={objectIdField ? 'Click to zoom to this feature' : undefined}
                  onClick={() => locate(r)}
                >
                  {columns.map((c) => (
                    <td key={c.name} title={formatTemporal(r[c.name])?.raw}>
                      {r[c.name] == null ? '—' : (formatTemporal(r[c.name])?.text ?? String(r[c.name]))}
                    </td>
                  ))}
                </tr>
              ))}
              {rows.length === 0 && !busy && (
                <tr>
                  <td colSpan={Math.max(columns.length, 1)} className="modal__muted">
                    No rows.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
