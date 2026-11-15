/** The searchable CRS picker (GET /spatial-refs), extracted from ImportFeaturesDialog so the
 *  9d Import-data dialog shares ONE implementation. Search by name or EPSG number; focusing the
 *  empty field loads the curated common set (WGS 84, the State Plane zones…) so nobody needs an
 *  EPSG number off the top of their head. */
import { useState } from 'react'

import { searchSpatialRefs, type SpatialRef } from '@/lib/api'

export default function CrsSearchField({
  accessToken,
  value,
  onPick,
  onClear,
}: {
  accessToken: string
  value: SpatialRef | null
  onPick: (r: SpatialRef) => void
  onClear: () => void
}) {
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<SpatialRef[]>([])

  async function search(q: string): Promise<void> {
    setQuery(q)
    try {
      setResults(await searchSpatialRefs(q, accessToken))
    } catch {
      setResults([])
    }
  }

  if (value !== null) {
    return (
      <p className="crs-field__picked">
        Coordinate system: <strong>{value.name}</strong>{' '}
        <span className="modal__muted">(EPSG:{value.srid})</span>{' '}
        <button type="button" className="import-dlg__link" onClick={onClear}>
          Change
        </button>
      </p>
    )
  }
  return (
    <div className="crs-field">
      <input
        type="text"
        className="crs-field__input"
        aria-label="Coordinate system"
        placeholder="Search by name or EPSG number"
        value={query}
        onChange={(e) => void search(e.target.value)}
        onFocus={() => { if (query === '' && results.length === 0) void search('') }}
      />
      {results.length > 0 && (
        <ul className="crs-field__list">
          {results.map((r) => (
            <li key={r.srid}>
              <button type="button" className="crs-field__opt" onClick={() => { setResults([]); setQuery(''); onPick(r) }}>
                {r.name} <span className="modal__muted">(EPSG:{r.srid})</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
