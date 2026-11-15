import { useEffect, useRef, useState } from 'react'
import { HexColorInput, HexColorPicker } from 'react-colorful'

import {
  ApiError,
  createCustomField,
  createLayer,
  deleteLayer,
  fetchOrgSettings,
  fetchShareableGroups,
  listCustomDomains,
  previewImportFields,
  searchSpatialRefs,
  setLayerShares,
} from '@/lib/api'
import type { CustomDomain, CustomFieldCreate, ShareGroup, SpatialRef } from '@/lib/api'
import { useIsAdmin } from '@/lib/auth/roles'
import { GNSS_FIELD_KEYS, gnssBundleDrafts } from '@/lib/gnssBundle'

import { AddFieldForm } from '@/features/catalog/CustomFieldsSection'
import type { AddFieldFormHandle } from '@/features/catalog/CustomFieldsSection'
import type { SeedIngestContext, WizardFileSeed } from '@/features/map/addFromFile'
import SharingSection from '@/features/catalog/SharingSection'
import CloseButton from '@/components/CloseButton'

// Same 6 colours as the old dialog; first (teal) matches DEFAULT_SLOT_COLOR.
const PALETTE = ['#2a9d8f', '#7b5cd6', '#c0392b', '#16a085', '#d35400', '#8e44ad']

// Geometry options. Point/Line/Polygon are all creatable (decision-line-polygon-layers.md, V055 + c2):
// a non-point claim retypes the slot's empty geom column to Multi* via the scoped ALTER. Raster is a
// separate item type (the raster store), not a feature-layer geometry, so it stays disabled here.
/** The geometry pick the wizard offers = what POST /layers accepts ('none' = non-spatial table). */
export type WizardGeometry = 'point' | 'line' | 'polygon' | 'none'

const GEOMETRY_OPTIONS = [
  { value: 'point', label: 'Point', enabled: true },
  { value: 'line', label: 'Line', enabled: true },
  { value: 'polygon', label: 'Polygon', enabled: true },
  // A standalone non-spatial table (decision-nonspatial-tables.md): a geometry-less layer. It skips the
  // Spatial Reference step (no CRS) and isn't drawn on the map — otherwise the same Fields + Sharing flow.
  { value: 'none', label: 'Table — no geometry', enabled: true },
  { value: 'raster', label: 'Raster — separate item type', enabled: false },
]

const STEPS = ['Define', 'Spatial Reference', 'Fields', 'Sharing']
// A table has no CRS, so its wizard drops the Spatial Reference step.
const TABLE_STEPS = ['Define', 'Fields', 'Sharing']

function fieldTypeLabel(f: CustomFieldCreate): string {
  if (f.dataType === 'coded') return 'picklist'
  if (f.dataType === 'boolean') return 'yes / no'
  if (f.dataType === 'number' && f.numericSubtype) return f.numericSubtype.replace('_', ' ')
  return f.dataType
}

/**
 * Create-a-layer WIZARD (decision-create-wizard.md). DEFERRED-COMMIT: every choice (geometry, working CRS,
 * draft fields, sharing) is held in memory and written only on Finish — so Cancel/Back never leave an orphan
 * slot, and fields can be designed before the layer id exists. Reuses the same AddFieldForm + SharingSection
 * the Manage dialog uses. Replaces the old single-step CreateLayerDialog.
 */
export default function CreateLayerWizard({
  open,
  onClose,
  onCreated,
  accessToken,
  initialGeometry = 'point',
  seed = null,
}: {
  open: boolean
  onClose: () => void
  /** ingest is set only for a file-seeded wizard: what the post-Finish import needs (decision-add-from-file.md). */
  onCreated: (layerId: string, ingest?: SeedIngestContext) => void
  accessToken: string
  // Pre-select the geometry when opening — 'none' opens the wizard as "Create table" (decision-nonspatial-tables.md).
  initialGeometry?: WizardGeometry
  /** Add-from-file: open pre-filled from a picked file (title/geometry/fields/CRS + csv column pick). */
  seed?: WizardFileSeed | null
}) {
  const isAdmin = useIsAdmin()
  const [step, setStep] = useState(0)

  // ① Define
  const [title, setTitle] = useState('')
  const [touched, setTouched] = useState(false)
  const [color, setColor] = useState(PALETTE[0])
  // Typed to the union the server accepts ('none' = non-spatial table) — the payload assignment
  // then needs no cast (tsc -b reconciliation, 2026-07-03).
  const [geometry, setGeometry] = useState<WizardGeometry>('point')
  const [schemaMode, setSchemaMode] = useState<'manual' | 'import'>('manual')

  // ② Spatial Reference
  const [workingSrid, setWorkingSrid] = useState(4326)
  const [sridName, setSridName] = useState('WGS 84')
  const [crsQuery, setCrsQuery] = useState('')
  const [crsResults, setCrsResults] = useState<SpatialRef[]>([])
  // Explicit-CRS gate (see crsUndeclared below). MUST live up here with the other hooks — the
  // component early-returns at `if (!open) return null`, and a hook below that line crashes the
  // wizard with React #310 the moment it opens (shipped + caught live, B5 2026-08-11).
  const [crsConfirmed, setCrsConfirmed] = useState(false)
  // Add-from-file CSV seeds only: which columns hold the coordinates (pre-picked by the preview's
  // auto-detect, user-overridable — decision-add-from-file.md §2.3).
  const [xCol, setXCol] = useState('')
  const [yCol, setYCol] = useState('')
  // Advanced: store geometry natively in the picked CRS (byte-exact for survey/high-accuracy). Only offered
  // for a projected CRS (working_srid <> 4326); decision-crs-native-slice1.md.
  const [nativeStorage, setNativeStorage] = useState(false)

  // ③ Fields (draft)
  const [draftFields, setDraftFields] = useState<CustomFieldCreate[]>([])
  // GNSS metadata bundle (decision-gnss-metadata-fields.md): checked appends the bundle drafts,
  // unchecked removes still-present bundle keys. The drafts stay ordinary fields — individually
  // removable, so the box records the last toggle action, not a live all-present invariant.
  const [gnssChecked, setGnssChecked] = useState(false)
  const [domains, setDomains] = useState<CustomDomain[]>([])
  const [importBusy, setImportBusy] = useState(false)
  const importInput = useRef<HTMLInputElement>(null)
  // Drive the bottom-bar "+ Field" button (the form's submit is exposed via ref; validity via callback).
  const fieldFormRef = useRef<AddFieldFormHandle>(null)
  // Backdrop-close guard: only a click that also STARTED on the backdrop may close. A drag that
  // begins inside the dialog (color-picker, text selection) and releases over the backdrop
  // dispatches its click on the backdrop (common-ancestor rule) — stopPropagation on the dialog
  // can't intercept that path, and without the guard it discarded the whole draft (B5b 2026-08-11).
  const backdropArmed = useRef(false)
  const [fieldFormCanAdd, setFieldFormCanAdd] = useState(false)

  // ④ Sharing (draft)
  const [level, setLevel] = useState<'owner' | 'org' | 'public'>('owner')
  const [selectedGroups, setSelectedGroups] = useState<Set<string>>(new Set())
  const [groups, setGroups] = useState<ShareGroup[]>([])
  const [publicEnabled, setPublicEnabled] = useState(false)
  const [nonadminMayPublic, setNonadminMayPublic] = useState(false)

  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  function refreshDomains(): void {
    listCustomDomains(accessToken).then(setDomains).catch(() => {})
  }

  // Reset + load reference data each open. A file seed then overlays its pre-fill (title from the
  // filename, geometry/fields/CRS from the file, csv coordinate candidates) on the fresh state.
  // accessToken must NOT be a dependency: the OIDC silent renew swaps the token every ~4 min, and
  // re-running this reset wiped an open wizard back to step 1 mid-work (B5b 2026-08-11). The
  // fetches below capture the token at open; later calls read the fresh prop from render scope.
  useEffect(() => {
    if (!open) return
    setStep(0)
    setTitle(seed ? seed.fileName.replace(/\.[^.]+$/, '') : '')
    setTouched(false)
    setColor(PALETTE[0])
    setGeometry(seed ? (seed.geometry ?? 'point') : initialGeometry)
    setSchemaMode('manual')
    setWorkingSrid(seed?.detectedSrid ?? 4326)
    setSridName(seed?.detectedSrid && seed.detectedSrid !== 4326 ? `EPSG:${seed.detectedSrid}` : 'WGS 84')
    setCrsConfirmed(false)
    setCrsQuery('')
    setDraftFields(seed ? seed.fields : [])
    setGnssChecked(false)
    setXCol(seed?.xColumn ?? '')
    setYCol(seed?.yColumn ?? '')
    setLevel('owner')
    setSelectedGroups(new Set())
    setBusy(false)
    setError(null)
    refreshDomains()
    if (seed?.detectedSrid && seed.detectedSrid !== 4326) {
      // Resolve the detected EPSG code to its display name (best-effort; the code label stands in).
      const srid = seed.detectedSrid
      searchSpatialRefs(String(srid), accessToken)
        .then((rs) => {
          const hit = rs.find((r) => r.srid === srid)
          if (hit) setSridName(hit.name)
        })
        .catch(() => {})
    }
    searchSpatialRefs('', accessToken).then(setCrsResults).catch(() => {})
    fetchShareableGroups(accessToken).then(setGroups).catch(() => {})
    fetchOrgSettings(accessToken)
      .then((o) => {
        setPublicEnabled(o.publicAnonAccessEnabled)
        setNonadminMayPublic(o.nonadminCanSharePublic)
      })
      .catch(() => {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  if (!open) return null

  const titleMissing = title.trim() === ''
  const mayTogglePublic = isAdmin || nonadminMayPublic
  const isTable = geometry === 'none' // a standalone non-spatial table (no CRS step, not drawn)
  const steps = isTable ? TABLE_STEPS : STEPS

  function runCrsSearch(q: string): void {
    setCrsQuery(q)
    searchSpatialRefs(q, accessToken).then(setCrsResults).catch(() => {})
  }

  function addDraftFields(fields: CustomFieldCreate[]): void {
    setDraftFields((prev) => {
      const have = new Set(prev.map((f) => f.fieldKey))
      const fresh = fields.filter((f) => !have.has(f.fieldKey))
      // keep the draft order stable; re-number sortOrder on commit isn't needed (server assigns by insert order)
      return [...prev, ...fresh]
    })
  }

  function toggleGnssBundle(checked: boolean): void {
    setGnssChecked(checked)
    if (checked) {
      // Sort after everything already drafted (the bundle is meaningless for desk digitizing —
      // it must never crowd the top of the attribute form). addDraftFields skips existing keys.
      addDraftFields(gnssBundleDrafts((draftFields.length + 1) * 10))
    } else {
      setDraftFields((prev) => prev.filter((f) => !GNSS_FIELD_KEYS.has(f.fieldKey)))
    }
  }

  async function onImportFile(file: File): Promise<void> {
    setImportBusy(true)
    setError(null)
    try {
      const lower = file.name.toLowerCase()
      let result
      if (lower.endsWith('.csv')) {
        result = await previewImportFields({ format: 'csv', csv_text: await file.text() }, accessToken)
      } else {
        const b64 = await fileToBase64(file)
        const format = lower.endsWith('.gpkg') ? 'geopackage' : 'esri-xml'
        result = await previewImportFields({ format, payload_base64: b64 }, accessToken)
        if (result.needsTable) {
          // A multi-table GDB — pick the first feature class for v1 (the Manage import has the full picker).
          const first = result.tables?.[0]?.name
          if (first) result = await previewImportFields({ format, payload_base64: b64, table: first }, accessToken)
        }
        // needsTable with NO tables leaves `result` as the picker response, which carries no
        // fields/rows/summary — every line below would throw on it. Pre-existing; guarded here rather
        // than left to surface as "Import failed" with a TypeError behind it.
        if (result.needsTable) {
          setError('That workspace lists feature classes but none could be read — use Manage → Fields to pick one.')
          return
        }
      }
      const n = result.fields.length
      const coded = result.summary.coded ?? 0
      addDraftFields(result.fields)
      // NAME the fields that did not import, and why. The server already returns a per-field verdict
      // (`rows[]` = {fieldKey, status, reason}) and the Manage → Fields importer renders it as a table;
      // this wizard used to collapse it to "N row(s) had errors", which told an operator that something
      // was wrong but not WHAT or WHICH — unactionable, and it cost a Pro-rig thread chasing a
      // non-existent server gap before anyone noticed the detail was being thrown away here.
      // Duplicates are reported separately: an already-defined field is a SKIP, not an error, and
      // merging the two counts was half of why the old message read as a failure.
      const failed = result.rows.filter((r) => r.status === 'error')
      const dupes = result.rows.filter((r) => r.status === 'duplicate')
      // A renamed field IMPORTED — it is not a failure — but the operator has to learn the new key here,
      // because this wizard has no per-row table to read it from (decision-esri-field-name-clash.md §3.5).
      const renamed = result.rows.filter((r) => r.status === 'renamed')
      const detail = (rows: typeof failed, max = 3): string => {
        const shown = rows.slice(0, max).map((r) => `${r.fieldKey || '(unnamed)'}${r.reason ? ` — ${r.reason}` : ''}`)
        const rest = rows.length - shown.length
        return shown.join('; ') + (rest > 0 ? `; +${rest} more` : '')
      }
      setError(
        `Imported ${n} field${n === 1 ? '' : 's'}` +
          (renamed.length ? `; ${renamed.length} renamed (${detail(renamed)})` : '') +
          (coded ? `; ${coded} picklist field(s) skipped (add a domain after create)` : '') +
          (dupes.length ? `; ${dupes.length} already defined (${detail(dupes)})` : '') +
          (failed.length
            ? `; ${failed.length} field${failed.length === 1 ? '' : 's'} could not be imported — ${detail(failed)}`
            : ''),
      )
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Import failed')
    } finally {
      setImportBusy(false)
      if (importInput.current) importInput.current.value = ''
    }
  }

  function toggleGroup(id: string): void {
    setSelectedGroups((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  // Finish — the whole deferred commit: claim the slot, create the draft fields, apply sharing. On any field/
  // sharing failure, roll the slot back so a half-built layer never lingers.
  async function finish(): Promise<void> {
    if (titleMissing) {
      setStep(0)
      setTouched(true)
      return
    }
    setBusy(true)
    setError(null)
    // A committable ADD A FIELD form is part of the user's intent — commit it now, exactly as if
    // "+ Field" were clicked, rather than silently dropping the typed work (B5b 2026-08-11: layers
    // Finished with a filled form landed with zero fields). On failure, return to the Fields step
    // so the form's inline error is visible. draftFields is this render's closure, so the fresh
    // payload is carried explicitly instead of re-read from state.
    let fields = draftFields
    if (fieldFormCanAdd) {
      const added = await fieldFormRef.current?.submit()
      if (!added) {
        setStep(steps.indexOf('Fields'))
        setBusy(false)
        return
      }
      if (!fields.some((f) => f.fieldKey === added.fieldKey)) fields = [...fields, added]
    } else if (fieldFormRef.current?.isDirty()) {
      // Typed-but-incomplete field: refuse to Finish past it rather than silently dropping it.
      setStep(steps.indexOf('Fields'))
      setError('The field being added is incomplete — finish it (name, alias, and picklist values if coded), or clear its name and alias to skip it.')
      setBusy(false)
      return
    }
    try {
      const created = await createLayer(
        {
          display_name: title.trim(),
          display_color: color,
          working_srid: workingSrid,
          native_storage: nativeStorage && workingSrid !== 4326,
          geom_type: geometry, // 'none' = a non-spatial table; raster is disabled (CreateLayerPayload carries the full union)
        },
        accessToken,
      )
      try {
        for (const f of fields) {
          await createCustomField(created.layer_id, f, accessToken)
        }
        if (level !== 'owner' || selectedGroups.size > 0) {
          await setLayerShares(
            created.layer_id,
            { organizationOnly: level === 'org', groups: [...selectedGroups], publicAccess: level === 'public' },
            accessToken,
          )
        }
      } catch (inner) {
        await deleteLayer(created.layer_id, accessToken).catch(() => {})
        throw inner
      }
      // File-seeded: hand back what the ingest needs. The CRS-step pick doubles as the SOURCE declaration
      // exactly when the file didn't declare one (csv always; a codeless .prj) — decision-add-from-file §3.
      const ingest: SeedIngestContext | undefined = seed
        ? {
            sourceSrid: seed.format === 'csv-data' || seed.detectedSrid === null ? workingSrid : undefined,
            xColumn: seed.format === 'csv-data' && xCol !== '' ? xCol : undefined,
            yColumn: seed.format === 'csv-data' && yCol !== '' ? yCol : undefined,
          }
        : undefined
      onCreated(created.layer_id, ingest)
      onClose()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Create failed')
    } finally {
      setBusy(false)
    }
  }

  // A file that declared no usable CRS (shapefile with a codeless .prj, CSV) must get an EXPLICIT
  // pick — the pre-selected WGS-84 default silently voided two county imports (B5, 2026-08-10:
  // state-plane feet read as lon/lat → every row skipped). Gate, don't just warn.
  const crsUndeclared = seed !== null && seed.detectedSrid === null && !crsConfirmed
  // Next locks only ON the Spatial Reference step (the picker lives there — gating earlier steps
  // would deadlock the wizard); Finish is reachable from every step, so it gates unconditionally.
  const canAdvance = !titleMissing && !(steps[step] === 'Spatial Reference' && crsUndeclared)
  // A csv seed must have a distinct coordinate pair before Finish (the ingest can't run without one).
  const csvColsBad = seed?.format === 'csv-data' && (xCol === '' || yCol === '' || xCol === yCol)
  // Seeded geometry is the FILE's geometry — changing it would make the ingest skip every row.
  const geometryLocked = seed !== null && (seed.format === 'csv-data' || seed.geometry !== null)

  return (
    <div
      className="modal__backdrop"
      role="presentation"
      onMouseDown={(e) => { backdropArmed.current = e.target === e.currentTarget }}
      onClick={(e) => { if (backdropArmed.current && e.target === e.currentTarget) onClose() }}
    >
      <div className="modal modal--wizard" role="dialog" aria-modal="true" aria-label="Create layer" onClick={(e) => e.stopPropagation()}>
        <div className="modal__header">
          <h2 className="modal__title">{isTable ? 'Create table' : 'Create layer'}</h2>
          <CloseButton onClick={onClose} />
        </div>

        {/* Step indicator (ArcGIS-style dots + "Step N of M") — a table drops the Spatial Reference step. */}
        <div className="wizard__steps" aria-hidden>
          {steps.map((s, i) => (
            <span key={s} className={`wizard__dot${i === step ? ' wizard__dot--active' : ''}${i < step ? ' wizard__dot--done' : ''}`} />
          ))}
        </div>
        <p className="modal__muted" style={{ marginTop: 0 }}>
          Step {step + 1} of {steps.length} — <strong>{steps[step]}</strong>
        </p>

        {/* ───────── ① Define ───────── */}
        {step === 0 && (
          <>
            <section className="modal__section">
              <label className="sign-form__field">
                <span>Title *</span>
                <input type="text" value={title} onChange={(e) => setTitle(e.target.value)} onBlur={() => setTouched(true)} autoFocus />
              </label>
              {touched && titleMissing && <p className="modal__field-error">Name is required</p>}

              <label className="sign-form__field">
                <span>Geometry</span>
                <select value={geometry} disabled={geometryLocked} onChange={(e) => setGeometry(e.target.value as WizardGeometry)}>
                  {GEOMETRY_OPTIONS.map((g) => (
                    <option key={g.value} value={g.value} disabled={!g.enabled}>
                      {g.label}
                    </option>
                  ))}
                </select>
                {geometryLocked && <span className="modal__muted">From the file — {seed?.format === 'csv-data' ? 'CSV imports are point layers' : 'the import matches this geometry'}.</span>}
              </label>
              {seed !== null && (
                <p className="modal__muted">
                  Seeded from <strong>{seed.fileName}</strong>
                  {seed.featureCount !== null ? <> — {seed.featureCount.toLocaleString()} features will be imported after Finish.</> : '.'}
                </p>
              )}

              {!isTable && <div className="sign-form__field">
                <span>Default Map Color</span>
                <div className="modal__swatches">
                  {PALETTE.map((c) => (
                    <button
                      key={c}
                      type="button"
                      className={`modal__swatch${color.toLowerCase() === c.toLowerCase() ? ' is-selected' : ''}`}
                      style={{ background: c }}
                      aria-label={`Color ${c}`}
                      aria-pressed={color.toLowerCase() === c.toLowerCase()}
                      onClick={() => setColor(c)}
                    />
                  ))}
                </div>
                <div className="wizard__colorpicker">
                  <HexColorPicker color={color} onChange={setColor} />
                  <div className="wizard__colorpicker-hex">
                    <span className="wizard__color-dot" style={{ background: color }} />
                    <HexColorInput color={color} onChange={setColor} prefixed aria-label="Hex color" />
                  </div>
                </div>
              </div>}

              <div className="sign-form__field">
                <span>Schema</span>
                <div className="wizard__toggle" role="tablist">
                  <button
                    type="button"
                    role="tab"
                    aria-selected={schemaMode === 'manual'}
                    className={`wizard__toggle-btn${schemaMode === 'manual' ? ' is-active' : ''}`}
                    onClick={() => setSchemaMode('manual')}
                  >
                    Define fields manually
                  </button>
                  <button
                    type="button"
                    role="tab"
                    aria-selected={schemaMode === 'import'}
                    className={`wizard__toggle-btn${schemaMode === 'import' ? ' is-active' : ''}`}
                    onClick={() => setSchemaMode('import')}
                  >
                    Import a schema
                  </button>
                </div>
              </div>
            </section>
          </>
        )}

        {/* ───────── ② Spatial Reference (skipped for a table) ───────── */}
        {steps[step] === 'Spatial Reference' && (
          <section className="modal__section">
            <p className="modal__muted">The coordinate system for this layer’s data.</p>
            {seed !== null && seed.format === 'csv-data' && (
              <div className="sign-form__field">
                <span>Coordinate columns</span>
                <p className="modal__muted" style={{ margin: 0 }}>
                  Which CSV columns hold the coordinates — pick the CRS they’re in below.
                </p>
                <div style={{ display: 'flex', gap: '0.5rem' }}>
                  <label style={{ flex: 1 }}>
                    <span className="modal__muted">X / longitude</span>
                    <select value={xCol} onChange={(e) => setXCol(e.target.value)}>
                      <option value="">— pick —</option>
                      {(seed.columns ?? []).map((c) => (
                        <option key={c} value={c}>{c}</option>
                      ))}
                    </select>
                  </label>
                  <label style={{ flex: 1 }}>
                    <span className="modal__muted">Y / latitude</span>
                    <select value={yCol} onChange={(e) => setYCol(e.target.value)}>
                      <option value="">— pick —</option>
                      {(seed.columns ?? []).map((c) => (
                        <option key={c} value={c}>{c}</option>
                      ))}
                    </select>
                  </label>
                </div>
                {csvColsBad && <p className="modal__field-error">Pick two different columns for X and Y.</p>}
              </div>
            )}
            {/* Shown for CSV too (it used to be suppressed there): a CSV never declares a CRS, so the user
                saw a pre-selected "WGS 84" and a dead Next with NO stated reason (found live 2026-08-13). */}
            {crsUndeclared && (
              <p className="modal__field-error">
                {seed?.format === 'csv-data'
                  ? 'A CSV carries no coordinate system — choose the one its X/Y values are in.'
                  : 'The file didn’t declare a usable coordinate system (no EPSG code) — pick the one its coordinates are in.'}{' '}
                Click it in the list below to confirm, even if it already looks highlighted.
              </p>
            )}
            <p>
              Selected:{' '}
              {crsUndeclared ? (
                <span className="modal__muted">— not chosen yet —</span>
              ) : (
                <>
                  <strong>{sridName}</strong> <span className="modal__muted">(EPSG:{workingSrid})</span>
                </>
              )}
            </p>
            <input
              type="text"
              placeholder="Search by name or EPSG code (e.g. State Plane, 3857)"
              value={crsQuery}
              onChange={(e) => runCrsSearch(e.target.value)}
            />
            <ul className="wizard__crs-list">
              {crsResults.map((r) => (
                <li key={r.srid}>
                  <label className={r.srid === workingSrid && !crsUndeclared ? 'is-selected' : ''}>
                    <input
                      type="radio"
                      name="crs"
                      // Nothing reads as chosen until the user actually chooses: a pre-selected default that
                      // LOOKS picked but doesn't satisfy the gate is the dead end itself.
                      checked={r.srid === workingSrid && !crsUndeclared}
                      onChange={() => {
                        setWorkingSrid(r.srid)
                        setSridName(r.name)
                        setCrsConfirmed(true)
                        if (r.srid === 4326) setNativeStorage(false) // native is meaningless in 4326
                      }}
                      // onChange does NOT fire when the ALREADY-CHECKED radio is clicked — so an undeclared
                      // file, which pre-selects WGS 84, could never be confirmed and Next stayed dead until
                      // the user detoured through a wrong CRS and back (found 2026-08-13). onClick fires on
                      // every click, so re-picking the pre-selected default counts as the explicit choice.
                      onClick={() => setCrsConfirmed(true)}
                    />{' '}
                    {r.name} <span className="modal__muted">(EPSG:{r.srid})</span>
                  </label>
                </li>
              ))}
              {crsResults.length === 0 && <li className="modal__muted">No matches.</li>}
            </ul>
            {workingSrid !== 4326 && (
              <label className="sign-form__field" style={{ flexDirection: 'row', alignItems: 'center', gap: '0.5rem', marginTop: '0.75rem' }}>
                <input type="checkbox" checked={nativeStorage} onChange={(e) => setNativeStorage(e.target.checked)} />
                <span>
                  <strong>Advanced — keep the layer in this coordinate system</strong>{' '}
                  <span
                    className="modal__muted"
                    title="Keeps survey or high-accuracy coordinates exactly as surveyed, with no datum conversion. Left off, the layer is stored in WGS 84; the map looks the same either way."
                  >
                    For survey-grade data. Most users leave this off.
                  </span>
                </span>
              </label>
            )}
          </section>
        )}

        {/* ───────── ③ Fields (draft) ─────────
            Hidden, not unmounted, off-step: unmounting destroyed the ADD A FIELD form's typed
            state on Next/Back (B5b 2026-08-11 — picklist values vanished), and the mounted ref
            is what lets Next/Finish auto-commit a filled form from any step. */}
        <div style={{ display: steps[step] === 'Fields' ? undefined : 'none' }}>
            <section className="modal__section">
              <div className="cf-fields__head">
                <h3 className="modal__section-title">Fields</h3>
                {schemaMode === 'import' && (
                  <div className="cf-fields__head-actions">
                    <input
                      ref={importInput}
                      type="file"
                      accept=".csv,.gpkg,.xml"
                      style={{ display: 'none' }}
                      onChange={(e) => {
                        const f = e.target.files?.[0]
                        if (f) void onImportFile(f)
                      }}
                    />
                    <button type="button" className="btn btn--xs" disabled={importBusy} onClick={() => importInput.current?.click()}>
                      {importBusy ? 'Importing…' : 'Import schema file'}
                    </button>
                  </div>
                )}
              </div>
              {!isTable && (
                <div style={{ marginBottom: '0.75rem' }}>
                  <label className="sign-form__field" style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'flex-start', gap: '0.5rem' }}>
                    {/* width:auto — the form CSS stretches inputs to 100%, which shoves the label text away. */}
                    <input type="checkbox" checked={gnssChecked} onChange={(e) => toggleGnssBundle(e.target.checked)} style={{ width: 'auto', margin: 0 }} />
                    <strong>Add GNSS metadata fields</strong>
                  </label>
                  {/* Outside the label: .sign-form__field uppercases its text (the form-label idiom). */}
                  <details className="cf-moreinfo">
                    <summary>More info</summary>
                    <p className="modal__muted">
                      Accuracy, fix type, satellites, DOPs, receiver — the per-feature provenance/QA
                      set a field app (QField today, Talus Field later) fills at capture. They’re
                      ordinary fields: remove any you don’t want; never required; they sort after
                      your own fields.
                    </p>
                  </details>
                </div>
              )}
              {draftFields.length === 0 ? (
                <p className="modal__muted">
                  {schemaMode === 'import'
                    ? 'No fields yet — add them below or import a schema file. (csv, gpkg, or ESRI XML)'
                    : 'No fields yet — add them below.'}
                </p>
              ) : (
                <ul className="cf-fieldlist">
                  {draftFields.map((f, i) => (
                    <li key={f.fieldKey} className="cf-field">
                      <div className="cf-field__row">
                        <div className="cf-field__info">
                          <strong>{f.label}</strong>
                          <div className="modal__muted">
                            <code>{f.fieldKey}</code> · {fieldTypeLabel(f)}
                            {f.required ? ' · required' : ''}
                          </div>
                        </div>
                        <div className="cf-field__actions">
                          <button
                            type="button"
                            className="btn btn--xs"
                            onClick={() => setDraftFields((prev) => prev.filter((_, idx) => idx !== i))}
                          >
                            Remove
                          </button>
                        </div>
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </section>

            <AddFieldForm
              ref={fieldFormRef}
              hideSubmit
              onCanAddChange={setFieldFormCanAdd}
              domains={domains}
              accessToken={accessToken}
              busy={false}
              submitLabel="+ Field"
              nextSortOrder={(draftFields.length + 1) * 10}
              onDomainsChanged={refreshDomains}
              onSubmit={(payload) => addDraftFields([payload])}
            />
        </div>

        {/* ───────── ④ Sharing (draft) ───────── */}
        {steps[step] === 'Sharing' && (
          <section className="modal__section">
            <SharingSection
              itemNoun={isTable ? 'table' : 'layer'}
              groups={groups}
              level={level}
              onLevelChange={setLevel}
              selected={selectedGroups}
              onToggleGroup={toggleGroup}
              publicEnabled={publicEnabled}
              mayTogglePublic={mayTogglePublic}
            />
          </section>
        )}

        {error !== null && <p className="sign-form__error">{error}</p>}

        {/* Button bar — Cancel(red) · Back(blue) · Next · Finish(green) */}
        <div className="sign-form__actions wizard__actions">
          <button type="button" className="btn btn--sm btn--danger" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <span className="wizard__actions-spacer" />
          {step > 0 && (
            <button
              type="button"
              className="btn btn--sm"
              style={{ background: 'var(--brand-subtle)', color: 'var(--text-brand)', borderColor: 'var(--border-default)' }}
              onClick={() => setStep((s) => s - 1)}
              disabled={busy}
            >
              Back
            </button>
          )}
          {steps[step] === 'Fields' && (
            <button
              type="button"
              className="btn btn--sm"
              style={{ whiteSpace: 'nowrap' }}
              onClick={() => void fieldFormRef.current?.submit()}
              disabled={busy || !fieldFormCanAdd}
            >
              + Field
            </button>
          )}
          {step < steps.length - 1 && (
            <button
              type="button"
              className="btn btn--sm"
              onClick={() =>
                void (async () => {
                  // Leaving Fields with a committable form auto-adds it ("+ Field" semantics) —
                  // Next used to discard the typed field silently (B5b 2026-08-11). A failed
                  // commit stays here so the form's inline error is visible.
                  if (steps[step] === 'Fields' && fieldFormCanAdd) {
                    const added = await fieldFormRef.current?.submit()
                    if (!added) return
                  }
                  setStep((s) => s + 1)
                })()
              }
              disabled={busy || !canAdvance}
            >
              Next
            </button>
          )}
          <button
            type="button"
            className="btn btn--sm"
            style={{ background: 'var(--success-600)', color: 'var(--text-inverse)', borderColor: 'var(--success-600)' }}
            onClick={() => void finish()}
            disabled={busy || titleMissing || csvColsBad || crsUndeclared}
          >
            {busy ? 'Creating…' : 'Finish'}
          </button>
        </div>
      </div>
    </div>
  )
}

function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      const res = reader.result as string
      resolve(res.includes(',') ? res.slice(res.indexOf(',') + 1) : res)
    }
    reader.onerror = () => reject(reader.error)
    reader.readAsDataURL(file)
  })
}
