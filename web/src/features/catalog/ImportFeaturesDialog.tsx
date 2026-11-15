import { useState } from 'react'

import { ApiError, importFeatures, searchSpatialRefs } from '@/lib/api'
import type { FeatureImportPayload, FeatureImportResult, MyLayer, SpatialRef } from '@/lib/api'
import CloseButton from '@/components/CloseButton'

/** Read a binary file as base64 (GeoPackage / zipped Shapefile). Bounded by the server's feature cap. */
async function fileToBase64(file: File): Promise<string> {
  const bytes = new Uint8Array(await file.arrayBuffer())
  let bin = ''
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i])
  return btoa(bin)
}

/** Merge the chosen source CRS into the payload (Shapefile + CSV can carry an override). */
function withSrid(base: FeatureImportPayload, srid: number | null): FeatureImportPayload {
  return (base.format === 'shapefile' || base.format === 'csv') && srid != null
    ? { ...base, source_srid: srid }
    : base
}

/**
 * Bulk feature ingest (E4, decision-bulk-feature-ingest.md): upload a GeoJSON / GeoPackage / zipped Shapefile of
 * points → server validate-first DRY RUN (per-row verdicts) → commit inserts the valid rows. Geometry is
 * reprojected to the layer's storage CRS server-side; attributes auto-map to the layer's fields by name. When a
 * Shapefile's .prj carries no EPSG code the server asks for the CRS — we surface a picker and re-run. v1:
 * point-only, insert-only.
 */
export default function ImportFeaturesDialog({
  layer,
  token,
  onClose,
  onImported,
}: {
  layer: MyLayer
  token?: string
  onClose: () => void
  onImported: () => void
}) {
  const [fileName, setFileName] = useState('')
  const [base, setBase] = useState<FeatureImportPayload | null>(null)
  const [sourceSrid, setSourceSrid] = useState<number | null>(null)
  const [sourceSridName, setSourceSridName] = useState('')
  const [needsCrs, setNeedsCrs] = useState(false)
  const [crsQuery, setCrsQuery] = useState('')
  const [crsResults, setCrsResults] = useState<SpatialRef[]>([])
  const [preview, setPreview] = useState<FeatureImportResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<number | null>(null)

  async function dryRun(b: FeatureImportPayload, srid: number | null): Promise<void> {
    if (!token) return
    setBusy(true)
    try {
      const r = await importFeatures(layer.id, withSrid(b, srid), false, token)
      setPreview(r)
      setNeedsCrs(false)
      setError(null)
    } catch (e) {
      setPreview(null)
      if (e instanceof ApiError && /coordinate system/i.test(e.message)) {
        setNeedsCrs(true) // the Shapefile's .prj didn't declare a CRS — ask the user
      } else {
        setError(e instanceof ApiError ? e.message : 'Could not read the file')
      }
    } finally {
      setBusy(false)
    }
  }

  async function onFile(file: File): Promise<void> {
    setError(null)
    setPreview(null)
    setDone(null)
    setNeedsCrs(false)
    setSourceSrid(null)
    setSourceSridName('')
    setCrsQuery('')
    setCrsResults([])
    setFileName(file.name)
    const lower = file.name.toLowerCase()
    const b: FeatureImportPayload = lower.endsWith('.gpkg')
      ? { format: 'geopackage', gpkg_base64: await fileToBase64(file) }
      : lower.endsWith('.zip') || lower.endsWith('.shp')
        ? { format: 'shapefile', shp_base64: await fileToBase64(file) }
        : lower.endsWith('.parquet') || lower.endsWith('.geoparquet')
          ? { format: 'geoparquet', parquet_base64: await fileToBase64(file) }
          : lower.endsWith('.csv')
            ? { format: 'csv', csv_text: await file.text() }
            : { format: 'geojson', geojson_text: await file.text() }
    setBase(b)
    void dryRun(b, null)
  }

  async function runCrsSearch(q: string): Promise<void> {
    setCrsQuery(q)
    if (!token) return
    try {
      setCrsResults(await searchSpatialRefs(q, token))
    } catch {
      setCrsResults([])
    }
  }
  function pickCrs(r: SpatialRef): void {
    setSourceSrid(r.srid)
    setSourceSridName(r.name)
    setCrsResults([])
    setCrsQuery('')
    if (base) void dryRun(base, r.srid)
  }

  async function commit(): Promise<void> {
    if (!token || !base || !preview || preview.valid === 0) return
    setBusy(true)
    setError(null)
    try {
      const r = await importFeatures(layer.id, withSrid(base, sourceSrid), true, token)
      setDone(r.committed)
      onImported()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Import failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal__backdrop" role="presentation" onClick={onClose}>
      <div className="modal" role="dialog" aria-modal="true" aria-label="Import features" onClick={(e) => e.stopPropagation()}>
        <div className="modal__header">
          <h2 className="modal__title">Import features</h2>
          <CloseButton onClick={onClose} />
        </div>

        <p className="modal__muted" style={{ marginTop: 0 }}>
          Upload a <strong>GeoJSON</strong>, <strong>GeoPackage</strong>, zipped <strong>Shapefile</strong>, or
          <strong> CSV</strong> (lon/lat or x/y columns) of point features. Geometry is reprojected to this layer’s
          CRS (EPSG:{layer.nativeStorage ? layer.workingSrid : 4326}); attributes match your fields by name.
        </p>

        <label className="sign-form__field">
          <span>GeoJSON / GeoPackage / Shapefile (.zip) / GeoParquet / CSV</span>
          <input
            type="file"
            accept=".geojson,.json,.gpkg,.zip,.parquet,.geoparquet,.csv,application/geo+json,application/json,application/geopackage+sqlite3,application/zip,text/csv"
            onChange={(e) => {
              const f = e.target.files?.[0]
              if (f) void onFile(f)
            }}
          />
        </label>

        {needsCrs && (
          <div className="modal__section">
            <p className="modal__muted" style={{ marginTop: 0 }}>
              This file didn’t declare a coordinate system. Choose the CRS its coordinates are in:
            </p>
            {sourceSrid != null && (
              <p>
                Source CRS: <strong>{sourceSridName}</strong> <span className="modal__muted">(EPSG:{sourceSrid})</span>
              </p>
            )}
            <input
              type="text"
              style={{ width: '100%' }}
              placeholder="Search CRS (e.g. State Plane, 3857)"
              value={crsQuery}
              onChange={(e) => void runCrsSearch(e.target.value)}
            />
            {crsResults.length > 0 && (
              <ul style={{ listStyle: 'none', margin: '0.25rem 0 0', padding: 0, maxHeight: '8rem', overflow: 'auto' }}>
                {crsResults.map((r) => (
                  <li key={r.srid}>
                    <button type="button" style={{ width: '100%', textAlign: 'left' }} onClick={() => pickCrs(r)}>
                      {r.name} <span className="modal__muted">(EPSG:{r.srid})</span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}

        {busy && <p className="modal__muted">Working…</p>}
        {error && <p className="sign-form__error">{error}</p>}

        {done !== null ? (
          <p style={{ color: 'var(--success-600)', fontWeight: 600 }}>
            Imported {done.toLocaleString()} feature{done === 1 ? '' : 's'}.
          </p>
        ) : (
          preview && (
            <div className="modal__section">
              <p>
                <strong>{preview.valid.toLocaleString()}</strong> of {preview.total.toLocaleString()} feature
                {preview.total === 1 ? '' : 's'} ready to import
                {preview.error_count > 0 && <> · <span className="modal__muted">{preview.error_count} skipped</span></>}
                {sourceSrid != null && <> · <span className="modal__muted">from EPSG:{sourceSrid}</span></>}
              </p>
              {preview.unmatched_columns.length > 0 && (
                <p className="modal__muted" style={{ fontSize: '0.85em' }}>
                  Ignored columns (no matching field): {preview.unmatched_columns.join(', ')}
                </p>
              )}
              {preview.errors.length > 0 && (
                <ul className="modal__muted" style={{ fontSize: '0.85em', maxHeight: '8rem', overflow: 'auto' }}>
                  {preview.errors.slice(0, 20).map((er) => (
                    <li key={er.index}>
                      Row {er.index + 1}: {er.reason}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )
        )}

        <div className="sign-form__actions">
          <button type="button" className="btn btn--sm" onClick={onClose} disabled={busy}>
            {done !== null ? 'Close' : 'Cancel'}
          </button>
          {done === null && (
            <button
              type="button"
              className="btn btn--sm"
              onClick={() => void commit()}
              disabled={busy || !preview || preview.valid === 0}
              title={fileName}
            >
              {busy ? 'Importing…' : `Import ${preview ? preview.valid.toLocaleString() : ''} features`}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
