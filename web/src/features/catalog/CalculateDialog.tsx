import { useState, type ReactNode } from 'react'

import { ApiError, calcFeatures, searchSpatialRefs } from '@/lib/api'
import type { CalcOp, DomainValue, FeatureCalcPayload, LayerColumn, MyLayer, SpatialRef } from '@/lib/api'

import { colLabel } from './format'
import CloseButton from '@/components/CloseButton'

/**
 * Calculate — the Pro "Calculate Geometry Attributes" analog (decision-geometry-calc.md), fenced
 * geometry-only + one editing op: populate an EXISTING owner-defined field with X/Y (in a chosen CRS),
 * geodesic length/perimeter/area, or a constant ("bulk set field value"). Scope = the grid's current
 * filters + optional selection + bounds, passed in by TableView. Dry-run preview is MANDATORY before
 * Apply (the server defaults commit=false and caps the batch at 50k rows); any parameter change
 * invalidates the preview. The Fields UI owns schema — this dialog never creates a field.
 */

const LENGTH_UNITS: { code: string; label: string }[] = [
  { code: 'ft', label: 'Feet' },
  { code: 'm', label: 'Meters' },
  { code: 'mi', label: 'Miles' },
  { code: 'km', label: 'Kilometers' },
]
const AREA_UNITS: { code: string; label: string }[] = [
  { code: 'acres', label: 'Acres' },
  { code: 'sqft', label: 'Square feet' },
  { code: 'sqm', label: 'Square meters' },
  { code: 'sqkm', label: 'Square kilometers' },
  { code: 'hectares', label: 'Hectares' },
]

/** The ops a layer's geometry family offers (gated server-side too); 'set' applies everywhere. */
function opsFor(geomType: MyLayer['geomType']): { op: CalcOp; label: string }[] {
  const geometry: { op: CalcOp; label: string }[] =
    geomType === 'point'
      ? [
          { op: 'x', label: 'X coordinate' },
          { op: 'y', label: 'Y coordinate' },
        ]
      : geomType === 'line'
        ? [{ op: 'length', label: 'Length' }]
        : geomType === 'polygon'
          ? [
              { op: 'area', label: 'Area' },
              { op: 'perimeter', label: 'Perimeter' },
            ]
          : []
  return [...geometry, { op: 'set', label: 'Set value' }]
}

export default function CalculateDialog({
  layer,
  columns,
  customDomains,
  scope,
  scopeLabel,
  token,
  onClose,
  onApplied,
}: {
  layer: MyLayer
  columns: LayerColumn[]
  customDomains: Record<string, DomainValue[]>
  /** The grid's CURRENT predicates, verbatim (filters incl. pinned; ids = the selection; bbox = bounds). */
  scope: { filters?: string[]; ids?: string[]; bbox?: string }
  scopeLabel: string
  token?: string
  onClose: () => void
  onApplied: () => void
}) {
  const ops = opsFor(layer.geomType)
  const [op, setOp] = useState<CalcOp>(ops[0].op)
  const [targetField, setTargetField] = useState('')
  const [unit, setUnit] = useState('')
  // X/Y output CRS: null = the layer's working CRS (the server default).
  const [srid, setSrid] = useState<number | null>(null)
  const [sridName, setSridName] = useState('')
  const [crsQuery, setCrsQuery] = useState('')
  const [crsResults, setCrsResults] = useState<SpatialRef[]>([])
  const [valueText, setValueText] = useState('')
  const [clearValue, setClearValue] = useState(false) // set → null (clears the field)
  const [preview, setPreview] = useState<number | null>(null) // dry-run matched count
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<number | null>(null) // applied row count

  // Owner-defined fields only (the write target is always props); geometry results need a number field.
  const targets = columns.filter((c) => c.props && (op === 'set' || c.type === 'numeric'))
  const target = targets.find((c) => c.name === targetField)
  const geometryOp = op !== 'set'
  const isLength = op === 'length' || op === 'perimeter'
  const units = isLength ? LENGTH_UNITS : AREA_UNITS

  /** Any parameter change invalidates the preview (Apply re-arms only after a fresh dry run). */
  function touch(): void {
    setPreview(null)
    setDone(null)
    setError(null)
  }

  function buildValue(): unknown {
    if (clearValue) return null
    if (!target) return valueText
    if (target.type === 'numeric') return Number(valueText)
    if (target.type === 'boolean') return valueText === 'true'
    return valueText
  }

  function paramsReady(): boolean {
    if (!targetField) return false
    if (op === 'length' || op === 'perimeter' || op === 'area') return unit !== ''
    if (op === 'set') {
      if (clearValue) return true
      if (valueText === '') return false
      if (target?.type === 'numeric') return Number.isFinite(Number(valueText))
      return true
    }
    return true // x / y (srid defaults server-side to the working CRS)
  }

  function payload(commit: boolean): FeatureCalcPayload {
    const p: FeatureCalcPayload = { op, target_field: targetField, scope, commit }
    if (op === 'length' || op === 'perimeter' || op === 'area') p.unit = unit
    if ((op === 'x' || op === 'y') && srid != null) p.srid = srid
    if (op === 'set') p.value = buildValue()
    return p
  }

  async function run(commit: boolean): Promise<void> {
    if (!token || !paramsReady()) return
    setBusy(true)
    setError(null)
    try {
      const r = await calcFeatures(layer.id, payload(commit), token)
      if (commit) {
        setDone(r.updated ?? 0)
        onApplied()
      } else {
        setPreview(r.matched)
      }
    } catch (e) {
      if (commit) setPreview(null) // scope may have shifted — re-preview before another apply
      setError(e instanceof ApiError ? e.message : String(e))
    } finally {
      setBusy(false)
    }
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

  /** The set-value input, typed by the target field (coded → its custom domain's codes). */
  function valueInput(): ReactNode {
    if (!target) return null
    const domainValues = target.customDomain ? (customDomains[target.customDomain] ?? []) : null
    if (domainValues) {
      return (
        <select value={valueText} onChange={(e) => { setValueText(e.target.value); touch() }} disabled={clearValue}>
          <option value="">Choose a value…</option>
          {domainValues.map((v) => (
            <option key={v.code} value={v.code}>
              {v.label}
            </option>
          ))}
        </select>
      )
    }
    if (target.type === 'boolean') {
      return (
        <select value={valueText} onChange={(e) => { setValueText(e.target.value); touch() }} disabled={clearValue}>
          <option value="">Choose…</option>
          <option value="true">Yes</option>
          <option value="false">No</option>
        </select>
      )
    }
    const inputType = target.type === 'numeric' ? 'number' : target.type === 'date' ? 'date' : 'text'
    return (
      <input
        type={inputType}
        value={valueText}
        onChange={(e) => { setValueText(e.target.value); touch() }}
        disabled={clearValue}
      />
    )
  }

  return (
    <div className="modal__backdrop" role="presentation" onClick={onClose}>
      <div className="modal" role="dialog" aria-modal="true" aria-label="Calculate field" onClick={(e) => e.stopPropagation()}>
        <div className="modal__header">
          <h2 className="modal__title">Calculate</h2>
          <CloseButton onClick={onClose} />
        </div>

        {columns.every((c) => !c.props) ? (
          <p className="modal__muted" style={{ marginTop: 0 }}>
            This layer has no fields to calculate into yet — define fields under Manage → Fields first.
          </p>
        ) : (
          <>
            <label className="sign-form__field">
              <span>Calculation</span>
              <select value={op} onChange={(e) => { setOp(e.target.value as CalcOp); setTargetField(''); setUnit(''); setValueText(''); setClearValue(false); touch() }}>
                {ops.map((o) => (
                  <option key={o.op} value={o.op}>
                    {o.label}
                  </option>
                ))}
              </select>
            </label>

            <label className="sign-form__field">
              <span>Into field {geometryOp && <span className="modal__muted">(number fields)</span>}</span>
              <select value={targetField} onChange={(e) => { setTargetField(e.target.value); setValueText(''); setClearValue(false); touch() }}>
                <option value="">Choose a field…</option>
                {targets.map((c) => (
                  <option key={c.name} value={c.name}>
                    {colLabel(c)}
                  </option>
                ))}
              </select>
              {targets.length === 0 && (
                <span className="modal__muted" style={{ fontSize: '0.85em' }}>
                  No {geometryOp ? 'number ' : ''}fields on this layer — define one under Manage → Fields.
                </span>
              )}
            </label>

            {(op === 'length' || op === 'perimeter' || op === 'area') && (
              <label className="sign-form__field">
                <span>Unit <span className="modal__muted">(geodesic — measured on the ellipsoid)</span></span>
                <select value={unit} onChange={(e) => { setUnit(e.target.value); touch() }}>
                  <option value="">Choose a unit…</option>
                  {units.map((u) => (
                    <option key={u.code} value={u.code}>
                      {u.label}
                    </option>
                  ))}
                </select>
              </label>
            )}

            {(op === 'x' || op === 'y') && (
              <div className="sign-form__field">
                <span>Coordinate system</span>
                <div style={{ fontSize: '0.9em' }}>
                  {srid == null ? (
                    <>Layer’s working CRS <span className="modal__muted">(EPSG:{layer.workingSrid ?? 4326})</span></>
                  ) : (
                    <>
                      {sridName} <span className="modal__muted">(EPSG:{srid})</span>{' '}
                      <button
                        type="button"
                        style={{ fontSize: '0.85em' }}
                        onClick={() => { setSrid(null); setSridName(''); setCrsQuery(''); setCrsResults([]); touch() }}
                      >
                        × use working CRS
                      </button>
                    </>
                  )}
                </div>
                <input
                  type="text"
                  placeholder="Search CRS (e.g. State Plane, UTM, 3857)"
                  value={crsQuery}
                  onChange={(e) => void runCrsSearch(e.target.value)}
                />
                {crsResults.length > 0 && (
                  <ul style={{ listStyle: 'none', margin: '0.25rem 0 0', padding: 0, maxHeight: '8rem', overflow: 'auto' }}>
                    {crsResults.map((r) => (
                      <li key={r.srid}>
                        <button
                          type="button"
                          style={{ width: '100%', textAlign: 'left', fontSize: '0.85em' }}
                          onClick={() => { setSrid(r.srid); setSridName(r.name); setCrsQuery(''); setCrsResults([]); touch() }}
                        >
                          {r.name} <span className="modal__muted">(EPSG:{r.srid})</span>
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}

            {op === 'set' && targetField && (
              <div className="sign-form__field">
                <span>Value</span>
                {valueInput()}
                <label style={{ fontSize: '0.85em', fontWeight: 'normal' }}>
                  <input type="checkbox" checked={clearValue} onChange={(e) => { setClearValue(e.target.checked); touch() }} /> Clear
                  the field (set to empty)
                </label>
              </div>
            )}

            <p className="modal__muted" style={{ fontSize: '0.9em' }}>
              Applies to <strong>{scopeLabel}</strong>.
            </p>

            {busy && <p className="modal__muted">Working…</p>}
            {error && <p className="sign-form__error">{error}</p>}
            {done !== null ? (
              <p style={{ color: 'var(--success-600)', fontWeight: 600 }}>
                Updated {done.toLocaleString()} row{done === 1 ? '' : 's'}.
              </p>
            ) : (
              preview !== null && (
                <p>
                  <strong>{preview.toLocaleString()}</strong> row{preview === 1 ? '' : 's'} will be updated.
                </p>
              )
            )}

            <div className="sign-form__actions">
              <button type="button" className="btn btn--sm" onClick={onClose} disabled={busy}>
                {done !== null ? 'Close' : 'Cancel'}
              </button>
              {done === null && (
                <>
                  <button type="button" className="btn btn--sm" onClick={() => void run(false)} disabled={busy || !paramsReady()}>
                    Preview count
                  </button>
                  <button
                    type="button"
                    className="btn btn--sm"
                    onClick={() => void run(true)}
                    disabled={busy || preview === null || preview === 0}
                    title={preview === null ? 'Preview first' : undefined}
                  >
                    Apply{preview ? ` to ${preview.toLocaleString()} rows` : ''}
                  </button>
                </>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  )
}
