import { type ChangeEvent, useEffect, useRef, useState } from 'react'

import { ApiError, cancelImportJob, createImportJob, fetchImportJob, type ImportJob, type SpatialRef } from '@/lib/api'
import { estimateExternalCopyBytes, fetchExternalAllFeatures, probeExternalUrl, type ExternalProbe } from '@/features/map/addFromUrl'

// The browser-side copy's size tiers. The ceiling is the import door's own GeoJSON parse guard
// (import_jobs_service._SNIFF_GEOJSON_MAX = 512 MiB): past it the upload would be refused anyway, so
// refuse here with the alternative named. Above the caution line the copy still works but is heavy
// for a tab (the whole collection is held in memory, then serialised once more for upload).
const COPY_CAUTION_BYTES = 100 * 1024 * 1024
const COPY_MAX_BYTES = 512 * 1024 * 1024
const fmtMB = (b: number): string => (b >= 1024 * 1024 * 1024 ? `${(b / (1024 * 1024 * 1024)).toFixed(1)} GB` : `${Math.max(1, Math.round(b / (1024 * 1024)))} MB`)
/** A picked file's size. fmtMB floors at 1 MB (right for a copy estimate); a 70-byte CSV is not 1 MB. */
const fmtFileSize = (b: number): string => (b < 1024 * 1024 ? `${Math.max(1, Math.round(b / 1024))} KB` : fmtMB(b))
import CrsSearchField from './CrsSearchField'
import CloseButton from '@/components/CloseButton'

interface AppendTarget {
  id: string
  name: string
}

interface ImportDataDialogProps {
  open: boolean
  onClose: () => void
  onImported: (layerId: string) => void // parent reloads layers / navigates
  appendTargets: AppendTarget[] // vector layers the caller can offer as append targets (server re-checks)
  accessToken: string
  /** decision-kml-address-geocode.md F5: the door's address-only KML refusal offers a one-click
   *  handoff to Batch geocode addresses with the same file. Absent = no button. */
  onGeocodeInstead?: (file: File) => void
}

type ImportFormat = 'shp-zip' | 'gpkg' | 'geojson' | 'csv' | 'kml'

const FORMAT_LABEL: Record<ImportFormat, string> = {
  'shp-zip': 'zipped Shapefile',
  gpkg: 'GeoPackage',
  geojson: 'GeoJSON',
  csv: 'CSV',
  kml: 'KML / KMZ',
}

const STEPS = ['Source', 'Target', 'Options'] as const

// Mirrors the api's DEFAULT upload ceiling (config.py import_max_upload_mb = 2048). An operator who
// raises it gets a larger real limit than this line says; the server's own 413 names the true number.
const IMPORT_MAX_LABEL = '2 GB'

/** Extension → the door's format. null = unsupported. */
function detectFormat(name: string): ImportFormat | null {
  const n = name.toLowerCase()
  if (n.endsWith('.zip')) return 'shp-zip'
  if (n.endsWith('.gpkg')) return 'gpkg'
  if (n.endsWith('.geojson') || n.endsWith('.json')) return 'geojson'
  if (n.endsWith('.csv')) return 'csv'
  // KML and KMZ share one door format; the server tells them apart by zip magic (G3).
  if (n.endsWith('.kml') || n.endsWith('.kmz')) return 'kml'
  return null
}

/**
 * Catalog → Import data — the 9d bulk-import door (decision-9d-bulk-import.md §7).
 *
 * County-scale files stream to the server as the raw request body (never parsed in the
 * browser — the fence-3 lesson) and land via an async job: pick file → target (new layer
 * or append) → optional CRS override → upload → poll progress → done. The CRS field is
 * required only when the file declares none (the server's honest 422 says so — common for
 * Shapefiles whose .prj has no EPSG AUTHORITY, e.g. county HARN State Plane).
 * Laid out as three short steps — Source, Target, Options.
 * Publisher/admin only (enforced server-side; inv24).
 */
export default function ImportDataDialog({ open, onClose, onImported, appendTargets, accessToken, onGeocodeInstead }: ImportDataDialogProps) {
  const [file, setFile] = useState<File | null>(null)
  const [format, setFormat] = useState<ImportFormat | null>(null)
  const [mode, setMode] = useState<'new' | 'append'>('new')
  const [name, setName] = useState('')
  const [layerId, setLayerId] = useState('')
  const [crs, setCrs] = useState<SpatialRef | null>(null)
  const [sridNeeded, setSridNeeded] = useState(false)
  const [busy, setBusy] = useState(false)
  const [job, setJob] = useState<ImportJob | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [geocodeOffer, setGeocodeOffer] = useState(false)
  const pollRef = useRef<number | null>(null)
  // Source: a file (the original door) or a URL (an ArcGIS feature layer or a GeoJSON document,
  // fetched by the BROWSER page by page and handed to the same door as a GeoJSON upload; the Talus
  // server never calls the other server — decision-external-catalog-items.md's no-proxy fence).
  const [source, setSource] = useState<'file' | 'url'>('file')
  const [urlText, setUrlText] = useState('')
  const [probe, setProbe] = useState<ExternalProbe | null>(null)
  const [probing, setProbing] = useState(false)
  const [estBytes, setEstBytes] = useState<number | null>(null) // the copy's estimated size (url source)
  const [fetched, setFetched] = useState<number | null>(null) // features pulled so far (url source)
  const fetchAbortRef = useRef<AbortController | null>(null)
  const [step, setStep] = useState<0 | 1 | 2>(0)
  const [crsOpen, setCrsOpen] = useState(false) // the coordinate-system picker, opened by hand
  const fileInputRef = useRef<HTMLInputElement | null>(null)

  // Clean form per open.
  useEffect(() => {
    if (open) {
      setFile(null); setFormat(null); setMode('new'); setName(''); setLayerId('')
      setCrs(null); setSridNeeded(false); setBusy(false); setJob(null); setError(null)
      setSource('file'); setUrlText(''); setProbe(null); setProbing(false); setFetched(null); setEstBytes(null)
      setStep(0); setCrsOpen(false); setGeocodeOffer(false)
    }
    return () => fetchAbortRef.current?.abort()
  }, [open])

  // Poll the job while it runs; stop on terminal states and on unmount/close.
  useEffect(() => {
    if (!open || job === null || (job.status !== 'pending' && job.status !== 'running')) return
    pollRef.current = window.setInterval(async () => {
      try { setJob(await fetchImportJob(job.id, accessToken)) } catch { /* transient — keep polling */ }
    }, 2000)
    return () => { if (pollRef.current !== null) window.clearInterval(pollRef.current) }
  }, [open, job, accessToken])

  if (!open) return null

  const terminal = job !== null && job.status !== 'pending' && job.status !== 'running'

  function onPickFile(e: ChangeEvent<HTMLInputElement>): void {
    const picked = e.target.files?.[0] ?? null
    setFile(picked)
    setError(null)
    setSridNeeded(false)
    const f = picked ? detectFormat(picked.name) : null
    setFormat(f)
    if (picked && f === null) {
      setError('Unsupported file type — use a zipped Shapefile (.zip), GeoPackage (.gpkg), GeoJSON, CSV, or KML/KMZ')
    }
    if (picked && name.trim() === '') {
      setName(picked.name.replace(/\.(zip|gpkg|geojson|json|csv|kml|kmz)$/i, ''))
    }
  }

  /** The new layer's name: what was typed, else the source's own name (a file's without its
   *  extension, a service's title). ONE answer for the Options step's sentence and for the request,
   *  so the dialog cannot promise one name and create another. */
  function newLayerName(): string {
    const typed = name.trim()
    if (typed !== '') return typed
    if (source === 'url') return probe?.title ?? ''
    return (file?.name ?? '').replace(/\.(zip|gpkg|geojson|json|csv|kml|kmz)$/i, '')
  }

  async function check(): Promise<void> {
    setProbing(true)
    setError(null)
    setProbe(null)
    setEstBytes(null)
    try {
      const p = await probeExternalUrl(urlText)
      if (p.externalKind !== 'arcgis-feature' && p.externalKind !== 'geojson') {
        throw new Error('That is an image or tile service — only a feature layer or a GeoJSON document can be imported. Add it as a Reference instead.')
      }
      setProbe(p)
      setEstBytes(await estimateExternalCopyBytes(p))
      if (name.trim() === '') setName(p.title)
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Could not read that URL'
      // An ArcGIS "Token Required" is the sign-in case the instructions name — say it in those words.
      setError(/token required|invalid token|499|498/i.test(msg)
        ? 'This service needs a sign-in. Talus holds no ArcGIS token, so only public services can be copied — ask its owner to share the layer publicly, or export it as a Shapefile or GeoPackage and import the file.'
        : msg)
    } finally {
      setProbing(false)
    }
  }

  async function submit(): Promise<void> {
    if (source === 'file' && (file === null || format === null)) return
    if (source === 'url' && probe === null) return
    setBusy(true)
    setError(null)
    try {
      const sridNum = crs?.srid
      let upload: File
      let fmt: ImportFormat
      if (source === 'url' && probe !== null) {
        // Pull every feature through the browser, then upload the collection as a GeoJSON file.
        const ctrl = new AbortController()
        fetchAbortRef.current = ctrl
        setFetched(0)
        const fc = await fetchExternalAllFeatures(probe, setFetched, ctrl.signal)
        if (fc.features.length === 0) throw new Error('The service returned no features')
        const base = (name.trim() || probe.title).replace(/[\\/:*?"<>|]+/g, '_')
        upload = new File([JSON.stringify(fc)], `${base}.geojson`, { type: 'application/geo+json' })
        fmt = 'geojson'
      } else {
        upload = file as File
        fmt = format as ImportFormat
      }
      const created = await createImportJob(
        upload,
        mode === 'new'
          ? { format: fmt, name: newLayerName(), sourceSrid: sridNum }
          : { format: fmt, layerId, sourceSrid: sridNum },
        accessToken,
      )
      setJob(created)
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') return
      const msg = err instanceof ApiError ? err.message : err instanceof Error ? err.message : 'Import failed to start'
      // The door's honest no-CRS refusal → surface the CRS field instead of a dead end.
      const needsCrs = msg.includes('no usable CRS') || msg.includes('source_srid')
      if (needsCrs) setSridNeeded(true)
      // The door's address-only KML refusal (a Google My Maps export) → offer the geocoding handoff.
      setGeocodeOffer(format === 'kml' && msg.includes('Batch geocode addresses'))
      // With no coordinate system chosen yet, the Options step says what to do in plain words; the
      // server's own sentence would only repeat it in door vocabulary.
      setError(needsCrs && crs === null ? null : msg)
    } finally {
      setBusy(false)
      setFetched(null)
      fetchAbortRef.current = null
    }
  }

  async function cancel(): Promise<void> {
    if (job === null) return
    try { setJob(await cancelImportJob(job.id, accessToken)) } catch { /* the poll will catch up */ }
  }

  function finish(): void {
    if (job !== null && job.status === 'completed') onImported(job.target_layer_id)
    onClose()
  }

  const progressPct =
    job?.total_rows != null && job.total_rows > 0
      ? Math.min(100, Math.round((job.processed_rows / job.total_rows) * 100))
      : null

  // The three steps. One decision per step;
  // the coordinate system sits on the last one, out of the way unless the file needs it.
  const sourceReady =
    source === 'file'
      ? file !== null && format !== null
      : probe !== null && !(estBytes !== null && estBytes >= COPY_MAX_BYTES)
  const targetReady = mode === 'new' || layerId !== ''
  const appendName = appendTargets.find((t) => t.id === layerId)?.name ?? ''
  const sourceLabel = source === 'file' ? (file?.name ?? '') : (probe?.title ?? '')
  const showCrs = crsOpen || sridNeeded || crs !== null

  function goTo(next: 0 | 1 | 2): void {
    setError(null)
    setGeocodeOffer(false)
    setStep(next)
  }

  return (
    <div className="modal__backdrop" role="presentation" onClick={terminal || job === null ? onClose : undefined}>
      <div className="modal import-dlg" role="dialog" aria-modal="true" aria-label="Import data" onClick={(e) => e.stopPropagation()}>
        <div className="modal__header">
          <h2 className="modal__title">Import data</h2>
          <CloseButton onClick={onClose} />
        </div>

        {job === null ? (
          <>
            <ol className="import-dlg__steps" aria-label="Steps">
              {STEPS.map((label, i) => (
                <li key={label} aria-current={step === i ? 'step' : undefined}>
                  {i + 1} {label}
                </li>
              ))}
            </ol>

            {/* ① Source — a file (streamed to the server; nothing is parsed in the browser) or a URL
                (the browser pulls the features, then uploads them as GeoJSON). */}
            {step === 0 && (
              <section className="modal__section">
                <div className="catalog__segmented" role="group" aria-label="Import source">
                  <button
                    type="button"
                    className={`catalog__seg${source === 'file' ? ' catalog__seg--active' : ''}`}
                    aria-pressed={source === 'file'}
                    onClick={() => { setSource('file'); setError(null); setSridNeeded(false) }}
                  >
                    File
                  </button>
                  <button
                    type="button"
                    className={`catalog__seg${source === 'url' ? ' catalog__seg--active' : ''}`}
                    aria-pressed={source === 'url'}
                    onClick={() => { setSource('url'); setError(null); setSridNeeded(false) }}
                  >
                    ArcGIS feature layer URL
                  </button>
                </div>
                {source === 'file' ? (
                  <>
                    {/* The native picker draws in the browser's own face and wording; a button in
                        the app's style drives it instead. */}
                    <input
                      ref={fileInputRef}
                      type="file"
                      hidden
                      accept=".zip,.gpkg,.geojson,.json,.csv,.kml,.kmz"
                      onChange={onPickFile}
                    />
                    <div className="import-dlg__file">
                      <button type="button" className="btn btn--sm btn--secondary" onClick={() => fileInputRef.current?.click()}>
                        Choose file
                      </button>
                      <span className={`import-dlg__file-name${file ? ' import-dlg__file-name--picked' : ''}`} title={file?.name}>
                        {file ? file.name : 'No file chosen'}
                      </span>
                    </div>
                    {file && format && (
                      <p className="import-dlg__hint">
                        {fmtFileSize(file.size)} · {FORMAT_LABEL[format]}
                      </p>
                    )}
                    <p className="import-dlg__hint">
                      Shapefile (.zip), GeoPackage, GeoJSON, KML/KMZ or CSV, up to {IMPORT_MAX_LABEL}.
                    </p>
                    <details className="import-dlg__more">
                      <summary>More</summary>
                      <p>
                        A CSV needs coordinate columns (lon/lat or x/y). GeoJSON is limited to 512 MB. After the
                        upload finishes the import runs on the server; closing this window does not stop it.
                      </p>
                    </details>
                  </>
                ) : (
                  <>
                    <label className="sign-form__field import-dlg__field">
                      <span>Layer URL</span>
                      <span className="dash-picker__search">
                        <input
                          type="url"
                          placeholder="https://…/FeatureServer/0"
                          value={urlText}
                          onChange={(e) => { setUrlText(e.target.value); setProbe(null) }}
                        />
                        <button type="button" className="btn btn--sm" disabled={probing || urlText.trim() === ''} onClick={() => void check()}>
                          {probing ? 'Checking…' : 'Check'}
                        </button>
                      </span>
                    </label>
                    {probe && (
                      <p className="import-dlg__hint">
                        {probe.title}
                        {probe.geometryType && <> · {probe.geometryType}</>}
                        {probe.featureCount !== null && <> · {probe.featureCount.toLocaleString()} features</>}
                        {estBytes !== null && <> · about {fmtMB(estBytes)} to copy</>}
                        {probe.where && <> · where {probe.where}</>}
                      </p>
                    )}
                    {probe?.dateFields && probe.dateFields.length > 0 && (
                      <p className="import-dlg__hint">
                        Date fields are copied as dates. The time of day is not kept.
                      </p>
                    )}
                    {/* The size cue, tiered: nothing under the caution line; a one-line caution; a refusal
                        with the alternative named at the door's ceiling. */}
                    {estBytes !== null && estBytes >= COPY_MAX_BYTES && (
                      <p className="sign-form__error">
                        Too large to copy through the browser (about {fmtMB(estBytes)}; the limit is 512 MB). Export a
                        Shapefile or GeoPackage from the source and import the file instead.
                      </p>
                    )}
                    {estBytes !== null && estBytes >= COPY_CAUTION_BYTES && estBytes < COPY_MAX_BYTES && (
                      <p className="import-dlg__hint">
                        <strong>Large copy (about {fmtMB(estBytes)}).</strong> Keep this tab open and expect a wait. A
                        Shapefile or GeoPackage export from the source imports faster.
                      </p>
                    )}
                    <p className="import-dlg__hint">
                      A public ArcGIS feature layer or GeoJSON address. The features are copied once; nothing stays
                      linked.
                    </p>
                    <details className="import-dlg__more">
                      <summary>More about limits</summary>
                      <ul>
                        <li>
                          <strong>Public services only.</strong> Talus holds no ArcGIS token, so a service that needs a
                          sign-in cannot be read — ask its owner to share the layer publicly, or export it as a Shapefile
                          or GeoPackage and import the file.
                        </li>
                        <li>
                          <strong>Address.</strong> A layer address ends in <code>/FeatureServer/0</code> or{' '}
                          <code>/MapServer/0</code>.
                        </li>
                        <li>
                          <strong>Size.</strong> The copy is built in this tab's memory before upload. After <em>Check</em>{' '}
                          the estimate shows here; up to about 100 MB is routine, larger copies need the tab kept open,
                          and past 512 MB the import refuses — export a file instead.
                        </li>
                        <li>
                          <strong>Browser access.</strong> The service must allow cross-origin requests (CORS); ArcGIS
                          Server and ArcGIS Online do by default.
                        </li>
                        <li>
                          <strong>A live link instead of a copy:</strong> add the service as a <strong>Reference</strong>{' '}
                          — it draws on maps straight from the source but cannot feed dashboards or reports.
                        </li>
                      </ul>
                    </details>
                  </>
                )}
              </section>
            )}

            {/* ② Target — a new layer (fields from the file) or more rows for an existing one. */}
            {step === 1 && (
              <section className="modal__section">
                <div className="catalog__segmented" role="group" aria-label="Import into">
                  <button
                    type="button"
                    className={`catalog__seg${mode === 'new' ? ' catalog__seg--active' : ''}`}
                    aria-pressed={mode === 'new'}
                    onClick={() => setMode('new')}
                  >
                    New layer
                  </button>
                  <button
                    type="button"
                    className={`catalog__seg${mode === 'append' ? ' catalog__seg--active' : ''}`}
                    aria-pressed={mode === 'append'}
                    disabled={appendTargets.length === 0}
                    title={appendTargets.length === 0 ? 'You have no layers to add to yet' : undefined}
                    onClick={() => setMode('append')}
                  >
                    Existing layer
                  </button>
                </div>
                {mode === 'new' ? (
                  <label className="sign-form__field import-dlg__field">
                    <span>Layer name</span>
                    <input type="text" value={name} onChange={(e) => setName(e.target.value)} />
                  </label>
                ) : (
                  <label className="sign-form__field import-dlg__field">
                    <span>Add the features to</span>
                    <select value={layerId} onChange={(e) => setLayerId(e.target.value)}>
                      <option value="">Choose a layer…</option>
                      {appendTargets.map((t) => (
                        <option key={t.id} value={t.id}>{t.name}</option>
                      ))}
                    </select>
                  </label>
                )}
              </section>
            )}

            {/* ③ Options — what is about to happen, and the coordinate system: read from the file,
                asked for only when the server says the file declares none. */}
            {step === 2 && (
              <section className="modal__section">
                <p className="import-dlg__summary">
                  <strong>{sourceLabel}</strong>{' '}
                  {mode === 'new' ? <>becomes the new layer <strong>{newLayerName()}</strong>.</> : <>is added to <strong>{appendName}</strong>.</>}
                </p>
                {showCrs ? (
                  <div className="import-dlg__field">
                    {/* Once one is chosen the line below it says so; the prompt has done its job. */}
                    {crs === null && (
                      <p className={sridNeeded ? 'sign-form__error import-dlg__crs-need' : 'import-dlg__hint'}>
                        {sridNeeded
                          ? 'This file doesn’t say which coordinate system it uses. Choose it below — search by name or EPSG number, for example “State Plane” or “3857”.'
                          : 'Choose a coordinate system only if the file doesn’t say which one it uses.'}
                      </p>
                    )}
                    <CrsSearchField accessToken={accessToken} value={crs} onPick={setCrs} onClear={() => setCrs(null)} />
                  </div>
                ) : (
                  <div className="import-dlg__crs-row">
                    <span>
                      Coordinate system: <span className="modal__muted">read from the file</span>
                    </span>
                    <button type="button" className="import-dlg__link" onClick={() => setCrsOpen(true)}>
                      Change
                    </button>
                  </div>
                )}
              </section>
            )}

            {error !== null && <p className="sign-form__error">{error}</p>}
            {geocodeOffer && onGeocodeInstead && file && (
              <p>
                <button type="button" className="btn btn--sm btn--accent" onClick={() => onGeocodeInstead(file)}>
                  Geocode these addresses instead
                </button>
              </p>
            )}

            <div className="sign-form__actions">
              {step === 0 ? (
                // `detail > 1` = the second click of a double-click. Back (at Target) and Cancel are drawn
                // in the same place, as are Next and Import below: without this, a double-click on Back
                // closed the dialog and a double-click on Next RAN THE IMPORT without Options being seen
                // (once-over C4). A keyboard activation has detail 0 and is unaffected.
                <button type="button" className="btn btn--sm btn--ghost" onClick={(e) => { if (e.detail > 1) return; onClose() }}>Cancel</button>
              ) : (
                <button type="button" className="btn btn--sm btn--secondary" onClick={() => goTo((step - 1) as 0 | 1)} disabled={busy}>
                  Back
                </button>
              )}
              {busy && fetched !== null && (
                <button type="button" className="btn btn--sm btn--secondary" onClick={() => fetchAbortRef.current?.abort()}>
                  Stop
                </button>
              )}
              {step < 2 ? (
                <button
                  type="button"
                  className="btn btn--sm"
                  onClick={() => goTo((step + 1) as 1 | 2)}
                  disabled={step === 0 ? !sourceReady : !targetReady}
                >
                  Next
                </button>
              ) : (
                <button
                  type="button"
                  className="btn btn--sm"
                  onClick={(e) => { if (e.detail > 1) return; void submit() }}
                  disabled={busy || !sourceReady || !targetReady || (sridNeeded && crs === null)}
                >
                  {busy
                    ? fetched !== null
                      ? `Fetching… ${fetched.toLocaleString()}${probe?.featureCount != null ? ` of ${probe.featureCount.toLocaleString()}` : ''}`
                      : 'Uploading…'
                    : 'Import'}
                </button>
              )}
            </div>
          </>
        ) : (
          <>
            {/* Progress — the job runs server-side; closing this dialog does NOT stop it. */}
            <section className="modal__section">
              <h3 className="modal__section-title">
                {job.status === 'completed' ? 'Import complete' : job.status === 'failed' ? 'Import failed' : job.status === 'canceled' ? 'Import canceled' : 'Importing…'}
              </h3>
              <p className="modal__muted">
                {job.original_name} · {job.processed_rows.toLocaleString()} processed
                {job.total_rows != null && <> of {job.total_rows.toLocaleString()}</>} ·{' '}
                {job.inserted_rows.toLocaleString()} inserted
                {job.failed_rows > 0 && <> · {job.failed_rows.toLocaleString()} refused</>}
                {progressPct !== null && <> · {progressPct}%</>}
              </p>
              {job.error !== null && <p className="sign-form__error">{job.error}</p>}
              {job.failed_rows > 0 && job.error_sample.length > 0 && (
                <ul className="modal__muted">
                  {job.error_sample.slice(0, 5).map((s) => (
                    <li key={s.row}>row {s.row}: {s.reason}</li>
                  ))}
                  {job.failed_rows > job.error_sample.length && (
                    <li>… and {(job.failed_rows - job.error_sample.length).toLocaleString()} more (counts are exact; the sample is bounded)</li>
                  )}
                </ul>
              )}
            </section>

            <div className="sign-form__actions">
              {!terminal && (
                <button type="button" className="btn btn--sm" onClick={() => void cancel()}>Cancel import</button>
              )}
              <button type="button" className="btn btn--sm" onClick={finish}>
                {job.status === 'completed' ? 'Open catalog' : 'Close'}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
