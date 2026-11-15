import { useEffect, useState } from 'react'
import { useParams } from 'react-router'
import { useAuth } from 'react-oidc-context'
import { Download } from 'lucide-react'

import PanelSection from '@/components/PanelSection'
import {
  cell as cellValue,
  colLabel,
  columnKind,
  HIDDEN_COLUMNS,
  isBlank,
} from '@/features/catalog/format'
import {
  API_BASE_URL,
  apiOrigin,
  ApiError,
  downloadOpenDataExport,
  fetchLayerColumns,
  fetchOpenDataPreview,
  fetchOpenDataset,
  type LayerColumn,
  type LayerFeatureCollection,
  type OpenDataFormat,
  type OpenDataset,
} from '@/lib/api'
import {
  ApiSection,
  cell as rawCell,
  DetailHero,
  inferKind,
  downloadErrorMessage,
  FactStrip,
  formatLabel,
  geometryLabel,
  MetadataDownload,
  NotFoundPage,
} from './parts'

/**
 * A single curated public dataset's page (Talus 33) — the municipal-transparency detail view:
 * published T31 metadata, per-format downloads (gated to what the geometry supports), the
 * programmatic API URL, and a small table preview off the anon read path. Anon-capable; a dataset
 * that isn't curated-and-visible to the caller 404s server-side (existence-hiding) and renders as
 * "not found" here.
 *
 * Rebuilt to handoff-auth-opendata-4 §3: the list had the dark branded hero while this page kept a
 * centred reading column, so the flow changed design language mid-click.
 */
export default function OpenDataDetail() {
  const { layerId = '' } = useParams<{ layerId: string }>()
  const auth = useAuth()
  // Token ONLY when truly signed in — a lingering expired token would 401 the public dataset fetch
  // (auth.user persists with isAuthenticated false). The public page must degrade to anon, not error.
  const token = auth.isAuthenticated ? auth.user?.access_token : undefined

  const [ds, setDs] = useState<OpenDataset | null>(null)
  const [notFound, setNotFound] = useState(false)
  const [preview, setPreview] = useState<LayerFeatureCollection | null>(null)
  // §3.5's typed cells need the column schema. `/layers/{id}/columns` is optional-auth — anon reads
  // a public layer's schema (canEdit false, editor identity scrubbed), which is what makes the typed
  // preview work for the visitors this page exists for. It briefly was NOT: a `_validated_claims`
  // dependency added in passing 401'd every anon caller until decision-columns-anon.md restored it.
  // Still best-effort — a failure falls back to `inferKind`, which reads face/alignment off the
  // value rather than the schema.
  const [cols, setCols] = useState<LayerColumn[] | null>(null)

  useEffect(() => {
    let active = true
    setDs(null)
    setNotFound(false)
    setPreview(null)
    setCols(null)
    fetchOpenDataset(layerId, token)
      .then((d) => active && setDs(d))
      .catch((e) => active && setNotFound(e instanceof ApiError && e.status === 404))
    fetchOpenDataPreview(layerId, 10, token)
      .then((fc) => active && setPreview(fc))
      .catch(() => active && setPreview(null)) // preview is best-effort; never blocks the page
    fetchLayerColumns(layerId, token)
      .then((r) => active && setCols(r.columns))
      .catch(() => active && setCols(null))
    return () => {
      active = false
    }
  }, [layerId, token])

  if (notFound) return <NotFoundPage />
  if (!ds) {
    return (
      <div className="opendata opendata--detail">
        <div className="opendata__body">
          <p className="opendata__muted">Loading…</p>
        </div>
      </div>
    )
  }

  return (
    <div className="opendata opendata--detail">
      <JsonLd dataset={ds} />
      <DetailHero title={ds.title} lede={ds.abstract} />

      <FactStrip
        cells={[
          { label: 'Features', value: ds.featureCount.toLocaleString(), mono: true },
          {
            label: 'Geometry',
            value: ds.geometryType ? geometryLabel(ds.geometryType) : '',
            geometry: true,
          },
          { label: 'CRS', value: ds.srid ? `EPSG:${ds.srid}` : '', mono: true },
          {
            label: 'Updated',
            value: ds.updatedAt ? new Date(ds.updatedAt).toISOString().slice(0, 10) : '',
            mono: true,
          },
          { label: 'License', value: ds.license ?? '' },
        ]}
      />

      <div className="opendata__body opendata__split">
        <div className="opendata__main">
          <Preview preview={preview} cols={cols} />

          <section className="opendata__tools">
            <h2 className="opendata__section-title">Use it from your own tools</h2>
            {/* Interop I-1 tail: the standard OGC endpoint FIRST — the URL desktop GIS and any OGC
                client consume (already absolute, built server-side from the advertised base). It is
                what this audience actually needs, so it is the one that should be found first. */}
            {ds.ogcUrl && (
              <ApiSection
                title="OGC API — Features"
                note="Standard endpoint — add it to QGIS, ArcGIS Pro, GDAL, or any OGC client."
                url={ds.ogcUrl}
                standard
              />
            )}
            {/* Developer/harvester surfaces — neither is the point of the page, so they fold away. */}
            <PanelSection title="REST API & metadata">
              <ApiSection url={`${API_BASE_URL}${ds.apiUrl}`} heading={false} />
              <MetadataDownload kind="layer" id={ds.layerId} token={token} />
            </PanelSection>
          </section>
        </div>

        <aside className="opendata__rail">
          <Downloads dataset={ds} token={token} />
          <About dataset={ds} />
        </aside>
      </div>
    </div>
  )
}

/** Browse metadata — who published it and how it is classified. Deliberately NOT in the fact strip:
 *  that strip answers "should I download this", and owner/topic/keywords do not (§3.2). */
function About({ dataset }: { dataset: OpenDataset }) {
  const { ownerName, topicCategory, keywords } = dataset
  if (!ownerName && !topicCategory && keywords.length === 0) return null
  return (
    <div className="opendata__about">
      {ownerName && (
        <div className="opendata__about-row">
          <span className="opendata__about-label">Published by</span>
          <span className="opendata__about-value">{ownerName}</span>
        </div>
      )}
      {topicCategory && (
        <div className="opendata__about-row">
          <span className="opendata__about-label">Topic</span>
          <span className="opendata__about-value">{topicCategory}</span>
        </div>
      )}
      {keywords.length > 0 && (
        <div className="opendata__about-row">
          <span className="opendata__about-label">Keywords</span>
          <span className="opendata__formats">
            {keywords.map((k) => (
              <span key={k} className="chip">
                {k}
              </span>
            ))}
          </span>
        </div>
      )}
    </div>
  )
}

/**
 * The download card (§3.3). Every format used to be `className="btn"` — four formats plus two
 * metadata formats plus two Copy buttons was eight primary-weight buttons on one page, and when
 * everything is primary nothing is. Now exactly ONE brand fill; the rest are rows.
 *
 * The primary is the FIRST format the server offers, not a hardcoded GeoJSON — the server already
 * gates `formats` by geometry (GeoPackage/Shapefile 422 for a table), so hardcoding would offer a
 * format that 422s.
 *
 * NO per-format sizes, contrary to §3.3's preference: the server has no cheap size estimate for an
 * export it has not run, and §3.3 itself says show them only where the server can estimate and
 * "do not print a guess". Left as the follow-up the handoff already flags.
 */
function Downloads({ dataset, token }: { dataset: OpenDataset; token?: string }) {
  const [busy, setBusy] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  async function run(format: OpenDataFormat) {
    setErr(null)
    setBusy(format)
    try {
      await downloadOpenDataExport(dataset.layerId, format, token)
    } catch (e) {
      setErr(downloadErrorMessage(e))
    } finally {
      setBusy(null)
    }
  }

  const [primary, ...rest] = dataset.formats
  if (!primary) return null
  return (
    <div className="opendata__dlcard">
      <h2 className="opendata__dlcard-title">Download</h2>
      <button
        type="button"
        className="opendata__dl opendata__dl--primary"
        disabled={busy !== null}
        onClick={() => void run(primary as OpenDataFormat)}
      >
        <Download size={15} aria-hidden="true" />
        <span>{busy === primary ? 'Preparing…' : formatLabel(primary)}</span>
      </button>
      {rest.map((f) => (
        <button
          key={f}
          type="button"
          className="opendata__dl"
          disabled={busy !== null}
          onClick={() => void run(f as OpenDataFormat)}
        >
          <Download size={14} aria-hidden="true" />
          <span>{busy === f ? 'Preparing…' : formatLabel(f)}</span>
        </button>
      ))}
      {err && <p className="opendata__error">{err}</p>}
    </div>
  )
}

/**
 * The preview table (§3.5) — the most public table in the product, and until now the only one
 * running its own style (`.opendata__table`) with `String(v)` for every value.
 *
 * Renders through `.data-table` with the SAME `data-kind` discipline as the grid and the record
 * drawer, sourced from format.ts's `columnKind()` — one authority, three surfaces.
 *
 * NO badges for coded columns, unlike the grid (ruling §6.7). `/domains` requires auth and the
 * `<col>_label` join exists only on the CSV export path, so an anon caller has no labels: a badge
 * here would dress a raw code (`R8-3`) as a resolved label. Passing `{}` as codeLabels makes
 * `cell()` fall through to the raw code, which is the honest rendering.
 *
 * Untyped fallback when the columns fetch fails — the preview must never block the page.
 */
function Preview({
  preview,
  cols,
}: {
  preview: LayerFeatureCollection | null
  cols: LayerColumn[] | null
}) {
  if (!preview || preview.features.length === 0) return null
  // HIDDEN_COLUMNS applies on BOTH paths. It did not, and the anon page rendered `props` — a raw
  // jsonb blob — as a table column, the same bug the record drawer had.
  const present = Object.keys(preview.features[0].properties).filter((c) => !HIDDEN_COLUMNS.has(c))
  if (present.length === 0) return null
  const typed = cols?.filter((c) => present.includes(c.name) && !HIDDEN_COLUMNS.has(c.name)) ?? null
  const shown = typed && typed.length > 0 ? typed : null

  return (
    <section className="opendata__preview-wrap">
      <div className="opendata__preview-head">
        <h2 className="opendata__section-title">Preview</h2>
        <span className="opendata__preview-count">
          {preview.features.length} of {preview.numberMatched.toLocaleString()}
        </span>
      </div>
      <div className="opendata__preview">
        <table className="data-table">
          <thead>
            <tr>
              {shown
                ? shown.map((c) => <th key={c.name}>{colLabel(c)}</th>)
                : present.map((c) => <th key={c}>{c}</th>)}
            </tr>
          </thead>
          <tbody>
            {preview.features.map((f, i) => (
              <tr key={f.id ?? i}>
                {shown
                  ? shown.map((c) => (
                      <td
                        key={c.name}
                        data-kind={columnKind(c)}
                        data-null={isBlank(c, f.properties) ? '' : undefined}
                      >
                        {cellValue(c, f.properties, {})}
                      </td>
                    ))
                  : present.map((c) => {
                      const v = f.properties[c]
                      const blank = v === null || v === undefined || v === ''
                      return (
                        <td key={c} data-kind={inferKind(v)} data-null={blank ? '' : undefined}>
                          {blank ? '—' : rawCell(v)}
                        </td>
                      )
                    })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

const _MEDIA: Record<string, string> = {
  csv: 'text/csv',
  geojson: 'application/geo+json',
  shapefile: 'application/zip',
  gpkg: 'application/geopackage+sqlite3',
}

/**
 * schema.org Dataset JSON-LD for the detail page (Talus 33 c5) — search-engine + dataset-aggregator
 * discoverability (Google Dataset Search reads this). Built client-side from the OpenDataset; the
 * authoritative machine feed for harvesters is the server's DCAT-US endpoint.
 */
function JsonLd({ dataset }: { dataset: OpenDataset }) {
  const ld: Record<string, unknown> = {
    '@context': 'https://schema.org',
    '@type': 'Dataset',
    name: dataset.title,
    url: typeof window !== 'undefined' ? window.location.href : undefined,
    isAccessibleForFree: true,
    distribution: dataset.formats.map((f) => ({
      '@type': 'DataDownload',
      encodingFormat: _MEDIA[f] ?? f,
      contentUrl: `${apiOrigin()}/api/v1/layers/${dataset.layerId}/export?format=${f}`,
    })),
  }
  if (dataset.abstract) ld.description = dataset.abstract
  if (dataset.keywords.length) ld.keywords = dataset.keywords
  if (dataset.license) ld.license = dataset.license
  if (dataset.updatedAt) ld.dateModified = dataset.updatedAt
  if (dataset.ownerName) ld.creator = { '@type': 'Organization', name: dataset.ownerName }
  return <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(ld) }} />
}
