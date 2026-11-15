import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router'
import { useAuth } from 'react-oidc-context'

import { API_BASE_URL, fetchOpenDatasets, type OpenDataset, type OpenRasterDataset } from '@/lib/api'
import HeroMotif from '@/features/shell/HeroMotif'

function SearchGlyph() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <circle cx="11" cy="11" r="8" />
      <path d="m21 21-4.3-4.3" />
    </svg>
  )
}

/**
 * The PUBLIC Open Data catalog (Talus 33) — a searchable list of the org's curated public datasets,
 * for anonymous visitors AND signed-in staff. Deliberately its OWN lightweight surface, not the
 * authenticated CatalogView (which carries favorites / manage / scopes that have no place on a public
 * page). Title/keyword/abstract search only — no taxonomy (drift fence; a faceted filter is a later
 * add now that T31 keywords exist). Reads are optional-auth: anon sees the public subset.
 */
export default function OpenDataView() {
  const auth = useAuth()
  // Send a token ONLY when truly signed in. A lingering EXPIRED token (auth.user is still populated
  // when isAuthenticated is false) would otherwise be sent and rejected (401), breaking the public
  // catalog for a stale session — the public surface must always degrade to the anon view, not error.
  const token = auth.isAuthenticated ? auth.user?.access_token : undefined
  const [datasets, setDatasets] = useState<OpenDataset[] | null>(null)
  const [rasters, setRasters] = useState<OpenRasterDataset[]>([])
  const [error, setError] = useState(false)
  const [q, setQ] = useState('')

  useEffect(() => {
    let active = true
    fetchOpenDatasets(token)
      .then(({ datasets: d, rasters: r }) => {
        if (!active) return
        setDatasets(d)
        setRasters(r)
      })
      .catch(() => active && setError(true))
    return () => {
      active = false
    }
  }, [token])

  const visible = useMemo(() => {
    if (!datasets) return []
    const needle = q.trim().toLowerCase()
    if (!needle) return datasets
    return datasets.filter(
      (d) =>
        d.title.toLowerCase().includes(needle) ||
        (d.abstract ?? '').toLowerCase().includes(needle) ||
        d.keywords.some((k) => k.toLowerCase().includes(needle)),
    )
  }, [datasets, q])

  const visibleRasters = useMemo(() => {
    const needle = q.trim().toLowerCase()
    if (!needle) return rasters
    return rasters.filter(
      (r) =>
        r.title.toLowerCase().includes(needle) ||
        (r.abstract ?? '').toLowerCase().includes(needle) ||
        r.keywords.some((k) => k.toLowerCase().includes(needle)),
    )
  }, [rasters, q])

  return (
    <div className="opendata">
      <div className="hero hero--compact">
        <HeroMotif />
        <div className="hero__scrim" />
        <div className="hero__inner">
          <p className="hero__eyebrow">Talus GIS</p>
          <h1 className="hero__title">Open Data</h1>
          <p className="hero__lede">
            Browse, preview, and download our organization’s public datasets — free to use.
          </p>
        </div>
      </div>

      <div className="opendata__body">
        <div className="opendata__toolbar">
          <div className="opendata__search">
            <span className="opendata__search-icon">
              <SearchGlyph />
            </span>
            <input
              className="field"
              type="search"
              placeholder="Search datasets…"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              aria-label="Search datasets"
            />
          </div>
          {datasets && (
            <span className="opendata__count">
              {visible.length + visibleRasters.length} dataset
              {visible.length + visibleRasters.length === 1 ? '' : 's'}
            </span>
          )}
          <a
            className="opendata__dcat"
            href={`${API_BASE_URL}/open-data/dcat`}
            target="_blank"
            rel="noreferrer"
            title="DCAT-US 1.1 catalog feed for data.gov / open-data aggregators"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
              <path d="M4 11a9 9 0 0 1 9 9M4 4a16 16 0 0 1 16 16" />
              <circle cx="5" cy="19" r="1.5" fill="currentColor" />
            </svg>
            DCAT feed
          </a>
        </div>

        {error ? (
          <p className="opendata__muted">Couldn’t load the catalog. Please try again later.</p>
        ) : datasets === null ? (
          <p className="opendata__muted">Loading…</p>
        ) : visible.length === 0 && visibleRasters.length === 0 ? (
          <p className="opendata__muted">
            {datasets.length === 0 && rasters.length === 0
              ? 'No public datasets have been published yet.'
              : 'No datasets match your search.'}
          </p>
        ) : (
          <div className="opendata__grid">
            {visible.map((d) => (
              <DatasetCard key={d.layerId} d={d} />
            ))}
            {visibleRasters.map((r) => (
              <RasterCard key={r.rasterId} r={r} />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function RasterCard({ r }: { r: OpenRasterDataset }) {
  return (
    <Link to={`/open-data/rasters/${encodeURIComponent(r.rasterId)}`} className="opendata__card">
      <span className="opendata__card-title">{r.title}</span>
      {r.abstract && <span className="opendata__card-abstract">{r.abstract}</span>}
      <span className="opendata__card-meta">
        Imagery / raster
        {r.topicCategory ? ` · ${r.topicCategory}` : ''}
        {r.ownerName ? ` · ${r.ownerName}` : ''}
      </span>
      <span className="opendata__formats">
        {r.formats.map((f) => (
          <span key={f} className="chip">
            {f.toUpperCase()}
          </span>
        ))}
        {!r.downloadable && <span className="chip">VIEW-ONLY</span>}
      </span>
    </Link>
  )
}

function DatasetCard({ d }: { d: OpenDataset }) {
  return (
    <Link to={`/open-data/${encodeURIComponent(d.layerId)}`} className="opendata__card">
      <span className="opendata__card-title">{d.title}</span>
      {d.abstract && <span className="opendata__card-abstract">{d.abstract}</span>}
      <span className="opendata__card-meta">
        {d.featureCount.toLocaleString()} feature{d.featureCount === 1 ? '' : 's'}
        {d.topicCategory ? ` · ${d.topicCategory}` : ''}
        {d.ownerName ? ` · ${d.ownerName}` : ''}
      </span>
      <span className="opendata__formats">
        {d.formats.map((f) => (
          <span key={f} className="chip">
            {f.toUpperCase()}
          </span>
        ))}
      </span>
    </Link>
  )
}
