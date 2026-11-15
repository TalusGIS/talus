import { useEffect, useState } from 'react'
import { useAuth } from 'react-oidc-context'

import { ApiError, fetchLayerColumns } from '@/lib/api'
import type { LayerColumn, MyLayer } from '@/lib/api'

import { displayName, friendlyType } from './format'

/**
 * The Fields sub-view: a read-only table of the layer's schema (Display Name / Field Name /
 * Type), mirroring the ArcGIS Enterprise Data > Fields page. Source is GET /layers/{id}/columns
 * (the FULL schema). Add-field / alias editing are deferred (the schema-edit slice).
 */
export default function FieldsView({ layer }: { layer: MyLayer }) {
  const auth = useAuth()
  const [columns, setColumns] = useState<LayerColumn[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [query, setQuery] = useState('')

  useEffect(() => {
    const token = auth.user?.access_token
    if (!token) return
    let active = true
    setColumns(null)
    setError(null)
    fetchLayerColumns(layer.id, token)
      .then((r) => active && setColumns(r.columns))
      .catch((e) => active && setError(e instanceof ApiError ? e.message : String(e)))
    return () => {
      active = false
    }
  }, [layer.id, auth.user?.access_token])

  if (error) return <p className="signs__error">{error}</p>
  if (!columns) return <p className="signs__empty">Loading fields…</p>

  const q = query.trim().toLowerCase()
  const shown = q
    ? columns.filter((c) => c.name.toLowerCase().includes(q) || displayName(c.name).toLowerCase().includes(q))
    : columns

  return (
    <div className="dview__body">
      <div className="dtable__toolbar">
        <input
          className="dtable__search"
          placeholder="Search fields"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <span className="signs__count">
          {shown.length} of {columns.length} fields
        </span>
      </div>
      <table className="signs__table">
        <thead>
          <tr>
            <th>Display Name</th>
            <th>Field Name</th>
            <th>Type</th>
          </tr>
        </thead>
        <tbody>
          {shown.map((c) => (
            <tr key={c.name}>
              <td>{displayName(c.name)}</td>
              <td>
                <code>{c.name}</code>
              </td>
              <td>
                {friendlyType(c.type)}
                {c.customDomain ? <span className="dtable__coded"> · coded</span> : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
