import { useEffect, useRef, useState } from 'react'

import {
  ApiError,
  cancelBatchJob,
  fetchBatchJob,
  fetchBatchPreflight,
  fetchKmlGeocodeRows,
  type KmlGeocodeRows,
  fetchGeocodeLocators,
  fetchLayerColumns,
  fetchMyLayers,
  rematchBatch,
  runBatchGeocode,
} from '@/lib/api'
import type { BatchJob, BatchPreflight } from '@/lib/api'
import { COUNTRIES } from '@/features/catalog/countries'
import {
  addressFieldChoices,
  fieldLabel,
  geocodeSourceChoices,
  type GeocodeSourceChoice,
} from '@/features/catalog/batchGeocodeSource'
import CloseButton from '@/components/CloseButton'

/** Parse a CSV header row (first line) into column names — a light client-side split for the column
 *  picker; the server re-parses the file authoritatively. Handles simple double-quoted fields. */
function parseHeader(text: string): string[] {
  const line = text.replace(/^\uFEFF/, '').split(/\r?\n/, 1)[0] ?? ''
  const out: string[] = []
  let cur = ''
  let q = false
  for (let i = 0; i < line.length; i++) {
    const c = line[i]
    if (q) {
      if (c === '"' && line[i + 1] === '"') { cur += '"'; i++ } else if (c === '"') { q = false } else { cur += c }
    } else if (c === '"') { q = true } else if (c === ',') { out.push(cur.trim()); cur = '' } else { cur += c }
  }
  out.push(cur.trim())
  return out.filter((h) => h.length > 0)
}

const TERMINAL = ['completed', 'failed', 'canceled']

/** "8s" / "1m 12s" / "1h 03m" — the progress line's elapsed-time figure. */
function formatElapsed(seconds: number): string {
  const s = Math.floor(seconds)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, '0')}s`
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`
}
// The config steps (ESRI "Geocode Table" flow, adapted). Country/Category are external-only follow-ons.
const STEPS = ['Locator', 'Input', 'Fields', 'Output'] as const

/**
 * Batch (bulk) geocoding — a multi-step wizard (decision-batch-geocoding.md, framed after ESRI's
 * "Geocode Table" tool). Locator (three-layer preflight: block non-batch / warn credits / prompt for a
 * token) → Input (a CSV or address-only KML/KMZ, OR a table already in Talus — F1's second source,
 * rider 2026-11-06) → Fields (address column[s], single or ordered-multi) → Output (layer name +
 * match threshold) → Finish. The run is ASYNC: it returns a job + a new point layer; we poll progress,
 * then offer Re-run misses / Open layer. Geocoding is GLOBAL by default (no country lock-in); a
 * per-country narrow is a follow-on step for the external locators.
 */
export default function BatchGeocodeDialog({
  token,
  onClose,
  onOpenLayer,
  initialFile,
}: {
  token?: string
  onClose: () => void
  onOpenLayer?: (layerId: string) => void
  /** The import dialog's "Geocode these addresses instead" handoff: an address-only KML/KMZ it just
   *  refused (decision-kml-address-geocode.md F5). Loaded as if picked here; the wizard opens on Input. */
  initialFile?: File | null
}) {
  const [step, setStep] = useState(0)

  const [csvText, setCsvText] = useState('')
  // An address-only KML/KMZ arrives as rows from the server (F1), never as text; null = CSV mode.
  const [rows, setRows] = useState<Record<string, string>[] | null>(null)
  const [kmlStats, setKmlStats] = useState<KmlGeocodeRows | null>(null)
  const [fileName, setFileName] = useState('')
  // Where the addresses are: an uploaded file, or a hosted table/layer already in the catalog (the
  // run door's `sourceLayerId`). `sourceChoices` is null until the table choice is first picked.
  const [source, setSource] = useState<'file' | 'table'>('file')
  const [sourceChoices, setSourceChoices] = useState<GeocodeSourceChoice[] | null>(null)
  const [sourceId, setSourceId] = useState('')
  const [rowCountKnown, setRowCountKnown] = useState(true)
  // A table's fields are sent by key and shown by label; a file's headers have no labels.
  const [labels, setLabels] = useState<Record<string, string>>({})
  const pickSeq = useRef(0) // drops a slow columns answer for a table the user has moved off
  const [headers, setHeaders] = useState<string[]>([])
  const [rowCount, setRowCount] = useState(0)
  const [columns, setColumns] = useState<string[]>([])
  const [fieldSort, setFieldSort] = useState<'file' | 'az' | 'za'>('file') // how the field chips are ordered
  const [displayName, setDisplayName] = useState('')

  const [locators, setLocators] = useState<string[]>([])
  const [locator, setLocator] = useState('')
  const [preflight, setPreflight] = useState<BatchPreflight | null>(null)
  const [chargesAck, setChargesAck] = useState(false)
  const [threshold, setThreshold] = useState(85)
  const [country, setCountry] = useState<string[]>([]) // ISO alpha-2 narrow (external only); [] = global
  const [countryQuery, setCountryQuery] = useState('')

  const [job, setJob] = useState<BatchJob | null>(null)
  const [outputLayerId, setOutputLayerId] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const pollRef = useRef<number | null>(null)

  useEffect(() => {
    if (!token) return
    fetchGeocodeLocators(token).then(setLocators).catch(() => setLocators([]))
  }, [token])

  // The handoff file loads once, exactly as if it had been picked in step ②.
  useEffect(() => {
    if (initialFile && token) { void onFile(initialFile); setStep(1) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialFile, token])

  // Re-run preflight whenever the chosen locator (or the row count) changes.
  useEffect(() => {
    if (!token || !locator) { setPreflight(null); return }
    setChargesAck(false)
    fetchBatchPreflight({ locator, rowCount }, token).then(setPreflight).catch(() => setPreflight(null))
  }, [token, locator, rowCount])

  // Poll the job while it runs; stop on a terminal state or unmount.
  useEffect(() => {
    if (!token || !job || TERMINAL.includes(job.status)) return
    pollRef.current = window.setInterval(async () => {
      try { setJob(await fetchBatchJob(job.jobId, token)) } catch { /* transient — keep polling */ }
    }, 2000)
    return () => { if (pollRef.current) window.clearInterval(pollRef.current) }
  }, [token, job])

  function clearInput(): void {
    pickSeq.current += 1
    setError(null)
    setRows(null); setKmlStats(null); setCsvText(''); setFileName('')
    setHeaders([]); setLabels({}); setColumns([]); setRowCount(0); setRowCountKnown(true)
    setSourceId('')
  }

  function onSourceMode(next: 'file' | 'table'): void {
    if (next === source) return
    clearInput()
    setSource(next)
    if (next === 'table' && sourceChoices === null && token) {
      fetchMyLayers(token)
        .then((r) => setSourceChoices(geocodeSourceChoices(r.layers)))
        .catch((e) => { setSourceChoices([]); setError(e instanceof ApiError ? e.message : 'Could not list your tables') })
    }
  }

  async function onPickSource(id: string): Promise<void> {
    clearInput()
    setSourceId(id)
    const choice = sourceChoices?.find((c) => c.id === id)
    if (!token || !choice) return
    const seq = pickSeq.current
    try {
      const { keys, labels: found } = addressFieldChoices((await fetchLayerColumns(id, token)).columns)
      if (seq !== pickSeq.current) return
      if (keys.length === 0) { setError(`${choice.name} has no fields to read addresses from.`); return }
      setHeaders(keys)
      setLabels(found)
      setRowCount(choice.rowCount ?? 0)
      setRowCountKnown(choice.rowCount != null)
      setFileName(choice.name)
      setDisplayName(`Geocoded — ${choice.name}`)
    } catch (e) {
      if (seq === pickSeq.current) setError(e instanceof ApiError ? e.message : 'Could not read that table’s fields')
    }
  }

  async function onFile(file: File): Promise<void> {
    setError(null)
    setLabels({})
    setRowCountKnown(true)
    if (/\.(kml|kmz)$/i.test(file.name)) {
      // Google My Maps' address-built layers export with an <address> per placemark and no
      // coordinates; the server turns the file into rows with `address` first (F2).
      if (!token) return
      try {
        const r = await fetchKmlGeocodeRows(file, token)
        setRows(r.rows)
        setKmlStats(r)
        setCsvText('')
        setFileName(file.name)
        setHeaders(r.headers)
        setRowCount(r.rows.length)
        setColumns(r.headers.includes('address') ? ['address'] : [])
        setDisplayName(`Geocoded — ${file.name.replace(/\.(kml|kmz)$/i, '')}`)
      } catch (e) {
        setError(e instanceof ApiError ? e.message : 'Could not read the KML')
      }
      return
    }
    setRows(null)
    setKmlStats(null)
    const text = await file.text()
    const lines = text.replace(/^\uFEFF/, '').split(/\r?\n/).filter((l) => l.trim().length > 0)
    setCsvText(text)
    setFileName(file.name)
    setHeaders(parseHeader(text))
    setRowCount(Math.max(0, lines.length - 1))
    setColumns([])
    setDisplayName(`Geocoded — ${file.name.replace(/\.csv$/i, '')}`)
  }

  function toggleColumn(h: string): void {
    setColumns((cs) => (cs.includes(h) ? cs.filter((c) => c !== h) : [...cs, h]))
  }

  const overCap = preflight != null && rowCount > preflight.rowCap
  const needsTokenMissing = preflight != null && preflight.needsToken && !preflight.hasToken
  const locatorBlocked =
    preflight != null && !preflight.batchCapable ? preflight.reason ?? 'This locator can’t batch geocode.'
    : needsTokenMissing ? 'This locator needs a stored token. Add one in Admin → Geocoders, then try again.'
    : null
  const needsAck = preflight?.creditConsuming === true

  // Per-step gate for Next / Finish.
  function stepValid(s: number): boolean {
    if (s === 0) return !!locator && preflight?.batchCapable === true && !locatorBlocked
    if (s === 1) return headers.length > 0 && !overCap
    if (s === 2) return columns.length > 0
    if (s === 3) return !needsAck || chargesAck
    return true
  }
  const canAdvance = stepValid(step)
  const canFinish = !!token && [0, 1, 2, 3].every(stepValid) && !busy && !job

  // The country-narrow step shows only for the EXTERNAL locators (own-data internal ignores country).
  const isExternal = preflight?.type === 'arcgis' || preflight?.type === 'nominatim'
  const steps: string[] = isExternal ? [...STEPS, 'Country'] : [...STEPS]

  async function onRun(): Promise<void> {
    if (!token) return
    setBusy(true); setError(null)
    try {
      const r = await runBatchGeocode(
        { locator, columns,
          ...(source === 'table' ? { sourceLayerId: sourceId } : rows ? { rows } : { csvText }),
          displayName, threshold,
          chargesAcknowledged: chargesAck,
          country: isExternal && country.length ? country : undefined }, token,
      )
      setOutputLayerId(r.outputLayerId)
      setJob(await fetchBatchJob(r.jobId, token))
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not start the run')
    } finally {
      setBusy(false)
    }
  }

  async function onCancel(): Promise<void> {
    if (!token || !job) return
    try { await cancelBatchJob(job.jobId, token); setJob(await fetchBatchJob(job.jobId, token)) } catch { /* ignore */ }
  }

  async function onRematch(): Promise<void> {
    if (!token || !outputLayerId) return
    setBusy(true); setError(null)
    try {
      const r = await rematchBatch(outputLayerId, { chargesAcknowledged: chargesAck }, token)
      setJob(await fetchBatchJob(r.jobId, token))
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not re-run the misses')
    } finally {
      setBusy(false)
    }
  }

  const pct = job && job.totalRows > 0 ? Math.round((job.processedRows / job.totalRows) * 100) : 0
  const running = job != null && !TERMINAL.includes(job.status)
  // Elapsed wall time since the worker picked the job up (the 2 s poll re-renders this while it runs;
  // a finished job freezes at finishedAt). With it, a run is never a silent "0 / N" — the clock moves
  // even when the count hasn't yet, and the rows/s figure says how long the rest will take.
  const elapsedS = job?.startedAt
    ? Math.max(0, ((job.finishedAt ? Date.parse(job.finishedAt) : Date.now()) - Date.parse(job.startedAt)) / 1000)
    : null
  const rate = elapsedS && elapsedS >= 1 && job && job.processedRows > 0 ? job.processedRows / elapsedS : null

  const countryMatches = countryQuery
    ? COUNTRIES.filter((c) => c.name.toLowerCase().includes(countryQuery.trim().toLowerCase()))
    : COUNTRIES

  // ── After Finish: the async progress / done view (not a wizard step). ──
  if (job) {
    return (
      <div className="modal__backdrop" role="presentation" onClick={onClose}>
        <div className="modal" role="dialog" aria-modal="true" aria-label="Batch geocode addresses" onClick={(e) => e.stopPropagation()}>
          <div className="modal__header">
            <h2 className="modal__title">Batch geocode addresses</h2>
            <CloseButton onClick={onClose} />
          </div>
          <div className="modal__section">
            <p style={{ marginTop: 0 }}><strong>{displayName || 'Batch geocode'}</strong> — {job.status}</p>
            <div style={{ height: 10, borderRadius: 999, background: 'var(--surface-sunken)', overflow: 'hidden' }}>
              <div style={{ width: `${pct}%`, height: '100%', background: 'var(--brand)', transition: 'width .3s' }} />
            </div>
            <p className="modal__muted">
              {job.processedRows.toLocaleString()} / {job.totalRows.toLocaleString()} processed · {job.matchedCount.toLocaleString()} matched
              {elapsedS != null && <> · {formatElapsed(elapsedS)}</>}
              {rate != null && <> · {rate >= 10 ? Math.round(rate) : rate.toFixed(1)} rows/s</>}
              {running && job.processedRows === 0 && elapsedS != null && elapsedS >= 10 && (
                <> · still working — the first rows are being matched</>
              )}
            </p>
            {running && (
              <p className="modal__muted">
                The output layer already exists and fills in as rows are matched; you can open it now and
                the run continues in the background.
              </p>
            )}
            {job.error && <p className="sign-form__error">{job.error}</p>}
            {error && <p className="sign-form__error">{error}</p>}
            <div className="sign-form__actions wizard__actions">
              <span className="wizard__actions-spacer" />
              {running ? (
                <>
                  <button type="button" className="btn btn--sm" onClick={() => void onCancel()}>Cancel run</button>
                  <button type="button" className="btn btn--sm"
                    onClick={() => { if (outputLayerId) onOpenLayer?.(outputLayerId); onClose() }}>
                    Open layer
                  </button>
                </>
              ) : (
                <>
                  {job.status === 'completed' && job.matchedCount < job.totalRows && (
                    <button type="button" className="btn btn--sm" disabled={busy} onClick={() => void onRematch()}>Re-run misses</button>
                  )}
                  <button type="button" className="btn btn--sm" onClick={onClose}>Close</button>
                  <button type="button" className="btn btn--sm"
                    style={{ background: 'var(--brand)', color: 'var(--text-inverse)' }}
                    onClick={() => { if (outputLayerId) onOpenLayer?.(outputLayerId); onClose() }}>
                    Open layer
                  </button>
                </>
              )}
            </div>
          </div>
        </div>
      </div>
    )
  }

  // ── The multi-step config wizard. ──
  return (
    <div className="modal__backdrop" role="presentation" onClick={onClose}>
      <div className="modal modal--wizard" role="dialog" aria-modal="true" aria-label="Batch geocode addresses" onClick={(e) => e.stopPropagation()}>
        <div className="modal__header">
          <h2 className="modal__title">Batch geocode addresses</h2>
          <CloseButton onClick={onClose} />
        </div>

        <div className="wizard__steps" aria-hidden>
          {steps.map((s, i) => (
            <span key={s} className={`wizard__dot${i === step ? ' wizard__dot--active' : ''}${i < step ? ' wizard__dot--done' : ''}`} />
          ))}
        </div>
        <p className="modal__muted" style={{ marginTop: 0 }}>
          Step {step + 1} of {steps.length} — <strong>{steps[step]}</strong>
        </p>

        {/* ── ① Locator ── */}
        {step === 0 && (
          <section className="modal__section">
            <p className="modal__muted" style={{ marginTop: 0 }}>
              Which locator resolves your addresses? Your internal locator is free and instant; a
              self-hosted OpenStreetMap locator is free and global; an external ESRI locator is credit-warned.
            </p>
            <label className="sign-form__field">
              <span>Locator</span>
              <select value={locator} onChange={(e) => setLocator(e.target.value)} autoFocus>
                <option value="">Choose a locator…</option>
                {locators.map((l) => <option key={l} value={l}>{l}</option>)}
              </select>
            </label>
            {preflight && !locatorBlocked && (
              <p className="modal__muted" style={{ marginTop: 0 }}>
                {preflight.creditConsuming ? 'Consumes credits (estimate shown at the Output step).' : 'Free — no credits.'}
              </p>
            )}
            {locatorBlocked && <p className="sign-form__error">{locatorBlocked}</p>}
          </section>
        )}

        {/* ── ② Input ── */}
        {step === 1 && (
          <section className="modal__section">
            <p style={{ margin: '0 0 0.25rem' }}>Where are the addresses?</p>
            <label style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
              <input type="radio" name="geocode-source" checked={source === 'file'} onChange={() => onSourceMode('file')} />
              <span>A file — a CSV, or a KML/KMZ of addresses</span>
            </label>
            <label style={{ display: 'flex', gap: '0.5rem', alignItems: 'center', marginBottom: '0.6rem' }}>
              <input type="radio" name="geocode-source" checked={source === 'table'} onChange={() => onSourceMode('table')} />
              <span>A table or layer already in Talus</span>
            </label>
            {source === 'file' && (
              <>
                <p className="modal__muted" style={{ marginTop: 0 }}>
                  Upload a CSV of address text, or a KML/KMZ whose placemarks carry addresses instead of
                  coordinates (a Google My Maps export).
                </p>
                <label className="sign-form__field">
                  <span>Address CSV or KML/KMZ</span>
                  <input type="file" accept=".csv,text/csv,.kml,.kmz" onChange={(e) => { const f = e.target.files?.[0]; if (f) void onFile(f) }} />
                </label>
                {headers.length > 0 && (
                  <p className="modal__muted">{fileName} — <strong>{rowCount.toLocaleString()}</strong> rows, {headers.length} columns.</p>
                )}
              </>
            )}
            {source === 'table' && (
              <>
                <label className="sign-form__field">
                  <span>Table or layer</span>
                  <select value={sourceId} onChange={(e) => void onPickSource(e.target.value)} disabled={sourceChoices === null}>
                    <option value="">{sourceChoices === null ? 'Loading…' : 'Choose a table or layer…'}</option>
                    {(sourceChoices ?? []).map((c) => (
                      <option key={c.id} value={c.id}>{c.label}{c.kind === 'table' ? ' (table)' : ''}</option>
                    ))}
                  </select>
                </label>
                {sourceChoices !== null && sourceChoices.length === 0 && !error && (
                  <p className="modal__muted">
                    Nothing you can see can be used here. A view layer cannot be geocoded; use the table or
                    layer it is built on.
                  </p>
                )}
                {headers.length > 0 && (
                  <p className="modal__muted">
                    {fileName} — {rowCountKnown ? <><strong>{rowCount.toLocaleString()}</strong> rows</> : 'row count not known'}, {headers.length} fields.
                  </p>
                )}
                <p className="modal__muted">
                  The result is a new point layer, copied now. It does not update when this table changes
                  or when a database pull refreshes it.
                </p>
              </>
            )}
            {kmlStats && (
              <p className="modal__muted">
                {kmlStats.placemarks.toLocaleString()} placemarks; <strong>{kmlStats.withAddress.toLocaleString()}</strong> carry a
                Google Maps address{kmlStats.withAddress > 0 ? ' (chosen as the address column)' : ''}
                {kmlStats.withGeometry > 0 && (
                  <> ; {kmlStats.withGeometry.toLocaleString()} already have coordinates — import those with Catalog → Import data instead</>
                )}
                {kmlStats.skipped > 0 && <> ; {kmlStats.skipped.toLocaleString()} overlays / network links skipped</>}.
              </p>
            )}
            {overCap && (
              <p className="sign-form__error">
                {rowCount.toLocaleString()} rows exceeds this locator’s {preflight?.rowCap.toLocaleString()}-row limit —
                {source === 'file' ? 'split the file or use your internal locator.'
                  : preflight?.type === 'internal' ? 'geocode a smaller table.'
                  : 'use your internal locator, or geocode a smaller table.'}
              </p>
            )}
            {error && <p className="sign-form__error">{error}</p>}
          </section>
        )}

        {/* ── ③ Fields ── */}
        {step === 2 && (
          <section className="modal__section">
            <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: '0.5rem' }}>
              <p style={{ margin: '0 0 0.25rem' }}>
                Address column{columns.length === 1 ? '' : 's'}
              </p>
              {/* Sort only reorders the chips to make a wide table scannable; the JOIN order stays the
                  order you PICK them (the numbered chips). */}
              <select value={fieldSort} onChange={(e) => setFieldSort(e.target.value as 'file' | 'az' | 'za')}
                aria-label="Sort columns" style={{ fontSize: '0.8rem' }}>
                <option value="file">{source === 'table' ? 'Table order' : 'File order'}</option>
                <option value="az">Name (A → Z)</option>
                <option value="za">Name (Z → A)</option>
              </select>
            </div>
            <p className="modal__muted" style={{ marginTop: 0 }}>Pick one, or several to join (in the order you pick them).</p>
            {/* Cap the height so a wide table (100+ columns) scrolls INSIDE the panel instead of
                stretching the dialog past the nav buttons. */}
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.35rem', maxHeight: '14rem', overflowY: 'auto' }}>
              {(fieldSort === 'az'
                ? [...headers].sort((a, b) => fieldLabel(labels, a).localeCompare(fieldLabel(labels, b)))
                : fieldSort === 'za'
                  ? [...headers].sort((a, b) => fieldLabel(labels, b).localeCompare(fieldLabel(labels, a)))
                  : headers
              ).map((h) => {
                const idx = columns.indexOf(h)
                return (
                  <button key={h} type="button" onClick={() => toggleColumn(h)}
                    style={{
                      padding: '0.2rem 0.5rem', borderRadius: '999px', cursor: 'pointer',
                      border: '1px solid var(--border-default)',
                      background: idx >= 0 ? 'var(--brand)' : 'transparent',
                      color: idx >= 0 ? 'var(--text-inverse)' : 'inherit',
                    }}>
                    {idx >= 0 ? `${idx + 1}. ` : ''}{fieldLabel(labels, h)}
                  </button>
                )
              })}
            </div>
          </section>
        )}

        {/* ── ④ Output ── */}
        {step === 3 && (
          <section className="modal__section">
            <label className="sign-form__field">
              <span>Output layer name</span>
              <input type="text" value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
            </label>
            <label className="sign-form__field">
              <span>Match threshold ({threshold})</span>
              <input type="range" min={30} max={100} value={threshold} onChange={(e) => setThreshold(Number(e.target.value))} />
            </label>
            {needsAck && (
              <label style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
                <input type="checkbox" checked={chargesAck} onChange={(e) => setChargesAck(e.target.checked)} />
                <span>I understand this will spend ~{preflight?.estimatedCredits ?? '?'} credits ({rowCountKnown ? `${rowCount.toLocaleString()} rows` : 'every row of the table'}).</span>
              </label>
            )}
            {error && <p className="sign-form__error">{error}</p>}
          </section>
        )}

        {/* ── ⑤ Country (external locators only) ── */}
        {steps[step] === 'Country' && (
          <section className="modal__section">
            <p style={{ margin: '0 0 0.25rem' }}>
              Limit to countries {country.length > 0 && <span className="modal__muted">({country.length} selected)</span>}
            </p>
            <p className="modal__muted" style={{ marginTop: 0 }}>
              Optional — narrows results to reduce false matches. Leave empty to geocode <strong>globally</strong> (the default).
            </p>
            <input type="search" placeholder="Search countries…" value={countryQuery}
              onChange={(e) => setCountryQuery(e.target.value)} style={{ width: '100%', marginBottom: '0.4rem' }} />
            <ul style={{ listStyle: 'none', margin: 0, padding: 0, maxHeight: '12rem', overflowY: 'auto' }}>
              {countryMatches.map((c) => (
                <li key={c.code}>
                  <label style={{ display: 'flex', gap: '0.5rem', alignItems: 'center', padding: '0.15rem 0' }}>
                    <input type="checkbox" checked={country.includes(c.code)}
                      onChange={() => setCountry((cs) => cs.includes(c.code) ? cs.filter((x) => x !== c.code) : [...cs, c.code])} />
                    <span>{c.name} <span className="modal__muted">({c.code.toUpperCase()})</span></span>
                  </label>
                </li>
              ))}
            </ul>
          </section>
        )}

        {/* ── nav bar — Cancel · Back · Next / Finish ── */}
        <div className="sign-form__actions wizard__actions">
          <button type="button" className="btn btn--sm btn--danger" onClick={onClose} disabled={busy}>Cancel</button>
          <span className="wizard__actions-spacer" />
          {step > 0 && (
            <button type="button" className="btn btn--sm"
              style={{ background: 'var(--brand-subtle)', color: 'var(--text-brand)', borderColor: 'var(--border-default)' }}
              onClick={() => setStep((s) => s - 1)} disabled={busy}>
              Back
            </button>
          )}
          {step < steps.length - 1 ? (
            <button type="button" className="btn btn--sm" onClick={() => setStep((s) => s + 1)} disabled={busy || !canAdvance}>
              Next
            </button>
          ) : (
            <button type="button" className="btn btn--sm"
              style={{ background: 'var(--success-600)', color: 'var(--text-inverse)', borderColor: 'var(--success-600)' }}
              onClick={() => void onRun()} disabled={!canFinish}>
              {busy ? 'Starting…' : 'Geocode'}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
