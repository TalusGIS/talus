import { useEffect, useState } from 'react'

import {
  ApiError,
  clearLayerRenderer,
  fetchFieldValues,
  fetchLayerFields,
  getCustomDomain,
  setLayerRenderer,
} from '@/lib/api'
import type { GeometryType, Renderer, RendererStop, SymbolizableField } from '@/lib/api'
import { RAMPS, rampColors, rampPreview } from '@/features/map/ramps'
import CloseButton from '@/components/CloseButton'
import IconPicker from '@/features/map/IconPicker'

// c7 — line dash presets (the renderer stores the raw MapLibre line-dasharray; solid = absent).
const DASH_PRESETS: { key: string; label: string; value: number[] | null }[] = [
  { key: 'solid', label: 'Solid', value: null },
  { key: 'dashed', label: 'Dashed', value: [3, 2] },
  { key: 'dotted', label: 'Dotted', value: [1, 2] },
]
function dashKeyOf(d: number[] | null | undefined): string {
  if (!d || !d.length) return 'solid'
  if (d.length === 2 && d[0] === 1 && d[1] === 2) return 'dotted'
  return 'dashed'
}

// Categorical palette for auto-assigning a color to each unique value. Distinct hues;
// the owner can recolor any stop. (A semantic ramp like Good->Replace is the SEED's job;
// re-picking a field here just needs distinguishable starting colors.)
const PALETTE = [
  '#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd',
  '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf',
]
const NO_VALUE_COLOR = '#cccccc'
const NO_VALUE_LABEL = '«No value»'
const CARDINALITY_SOFT = 50 // mirror the API/AGOL soft-warning threshold
// Hard ceiling on a REBUILT stop list (decision-dashboards-polish.md §3). Well past any defensible
// unique-value map, far short of rendering a swatch row per value of a 1,155-entry MUTCD domain.
const CARDINALITY_MAX = 200
// Seed for the size control when the user switches OFF "Scale with zoom" — the diameter the default
// ramp reaches at its top zoom (radius 7), so unchecking the box does not visibly jump.
const DEFAULT_POINT_DIAMETER = 14

// Pretty field labels: presentation only (the field LIST is DB-introspected). Strip a
// trailing _code, title-case, with a small override map for acronyms.
const LABEL_OVERRIDES: Record<string, string> = { mutcd_code: 'MUTCD' }
function fieldLabel(field: string): string {
  if (LABEL_OVERRIDES[field]) return LABEL_OVERRIDES[field]
  return field
    .replace(/_code$/, '')
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase())
}

interface StylesPanelProps {
  layerId: string
  layerLabel: string
  geometryType: GeometryType // c7 — gates the line/polygon style controls
  fallbackColor: string // single-symbol seed (displayColor ?? static LayerDef color)
  renderer: Renderer | null // the layer's current default
  accessToken: string
  onSaved: (renderer: Renderer | null) => void // parent updates caps + repaints
  onClose: () => void
}

type RType = 'simple' | 'uniqueValue'

export default function StylesPanel({
  layerId,
  layerLabel,
  geometryType,
  fallbackColor,
  renderer,
  accessToken,
  onSaved,
  onClose,
}: StylesPanelProps) {
  const [rtype, setRtype] = useState<RType>(renderer?.type ?? 'simple')
  const [simpleColor, setSimpleColor] = useState(renderer?.symbol?.color ?? fallbackColor)

  // c7 — layer-wide geometry style (independent of simple/uniqueValue). Seeded from the renderer, else the
  // map's defaults. Line: width + dash. Polygon: fill opacity + outline colour/width. Unused for point.
  const [lineWidth, setLineWidth] = useState<number>(renderer?.lineWidth ?? 2)
  const [lineDash, setLineDash] = useState<number[] | null>(renderer?.lineDash ?? null)
  const [fillOpacity, setFillOpacity] = useState<number>(renderer?.fillOpacity ?? 0.3)
  const [outlineColor, setOutlineColor] = useState<string>(renderer?.outlineColor ?? fallbackColor)
  const [outlineWidth, setOutlineWidth] = useState<number>(renderer?.outlineWidth ?? 1.5)
  // POINT size — the rendered DIAMETER in px (ArcGIS/QGIS mean diameter by "size"). `null` = the
  // zoom ramp points have always used, which is AGO's "Adjust size automatically" checked.
  const [pointSize, setPointSize] = useState<number | null>(renderer?.pointSize ?? null)
  // The bundled marker name, or null for the plain circle (decision-point-icons.md).
  const [pointIcon, setPointIcon] = useState<string | null>(renderer?.pointIcon ?? null)
  // Icon handover zoom (renderer.minZoom, wired 2026-09-03): below it the layer draws as circles.
  // null = the measured default (14). Only meaningful when an icon is chosen.
  const [iconFromZoom, setIconFromZoom] = useState<number | null>(renderer?.minZoom ?? null)

  const [fields, setFields] = useState<SymbolizableField[] | null>(null)
  const [fieldsError, setFieldsError] = useState<string | null>(null)
  const [field, setField] = useState<string>(renderer?.type === 'uniqueValue' ? (renderer.field ?? '') : '')

  const [stops, setStops] = useState<RendererStop[]>(
    renderer?.type === 'uniqueValue' ? (renderer.stops ?? []) : [],
  )
  const [defaultColor, setDefaultColor] = useState(renderer?.default?.color ?? NO_VALUE_COLOR)
  const [valuesLoading, setValuesLoading] = useState(false)
  const [valuesWarning, setValuesWarning] = useState<string | null>(null)
  const [rampName, setRampName] = useState('')
  const [rampFlip, setRampFlip] = useState(false)

  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Load the DB-introspected symbolizable field list once.
  useEffect(() => {
    let cancelled = false
    fetchLayerFields(layerId, accessToken)
      .then((res) => {
        if (!cancelled) setFields(res.fields)
      })
      .catch((err) => {
        if (!cancelled) setFieldsError(err instanceof ApiError ? `HTTP ${err.status}` : 'request failed')
      })
    return () => {
      cancelled = true
    }
  }, [layerId, accessToken])

  // (Re)build the stop list when the chosen field changes. Coded fields take their full
  // ordered/labelled list from their DOMAIN (incl. values not yet present); non-coded fields
  // scan distinct present values. Colors for values already in the current stops are
  // preserved; new values get the next palette color.
  //
  // Two coded systems, deliberately separate (decision-stylespanel-custom-domains.md):
  // a BESPOKE layer's field names an FK code table via `domain` and reads from /domains;
  // an OWNER-DEFINED field carries a V046 domain id via `customDomain` and is fetched
  // lazily. Before this branch existed every hosted layer fell through to the scan, which
  // labelled stops with raw codes AND silently dropped domain values that had no rows —
  // the Signs twin's Good->Replace ramp lost `replace` entirely (zero rows) on a re-pick.
  //
  // Fork A ruling: the domain list WINS. A value present in the data but absent from the
  // domain gets no stop and paints as the «No value» default, exactly as the bespoke path
  // has always behaved.
  async function loadFieldValues(f: SymbolizableField): Promise<void> {
    setValuesLoading(true)
    setValuesWarning(null)
    setError(null)
    const prior = new Map(stops.map((s) => [s.value, s.color]))
    const colorFor = (value: string, i: number): string => prior.get(value) ?? PALETTE[i % PALETTE.length]
    try {
      let next: RendererStop[]
      let serverTruncated = false
      if (f.customDomain) {
        // Active values only, in the author's sortOrder — the same treatment FilterPanel
        // gives owner custom domains.
        const d = await getCustomDomain(f.customDomain, accessToken)
        next = d.values
          .filter((v) => v.active)
          .sort((a, b) => a.sortOrder - b.sortOrder)
          .map((v, i) => ({ value: v.code, color: colorFor(v.code, i), label: v.label }))
      } else {
        const res = await fetchFieldValues(layerId, f.field, accessToken)
        next = res.values.map((v, i) => ({ value: v, color: colorFor(v, i), label: v }))
        serverTruncated = res.truncated
      }
      // Fork B ruling: the soft cardinality warning applies to the DOMAIN paths too, not just
      // the scan. It never used to matter — the largest built-in code table is 14 values — but
      // a hosted layer can bind a 1,155-value MUTCD domain, and building that many stops in
      // silence is a surprise, not a choice.
      //
      // ★ The CAP (decision-dashboards-polish.md §3) makes that a limit rather than advice, and
      // applies it to every path. The scan already truncated server-side and said so; the domain
      // branch built one swatch row per value, unbounded — the two disagreed for the same user
      // action. One cap, one wording, however the values arrived.
      //
      // ★ A REBUILD only. loadFieldValues runs from onPickField (a user action), never on mount,
      // so a renderer already persisted with more stops than this opens and saves untouched — the
      // cap never silently rewrites someone's existing map.
      const total = next.length
      const totalLabel = serverTruncated ? `${total}+` : `${total}`
      if (total > CARDINALITY_MAX) next = next.slice(0, CARDINALITY_MAX)
      if (total > CARDINALITY_MAX)
        setValuesWarning(
          `Showing the first ${CARDINALITY_MAX} of ${totalLabel} values (capped) — this may not be a useful map.`,
        )
      else if (serverTruncated) setValuesWarning(`Showing the first ${total} values (capped).`)
      else if (total > CARDINALITY_SOFT)
        setValuesWarning(`${total} unique values — this may not be a useful map.`)
      setStops(next)
    } catch (err) {
      setError(err instanceof ApiError ? `Could not load values (HTTP ${err.status})` : 'Could not load values')
      setStops([])
    } finally {
      setValuesLoading(false)
    }
  }

  function onPickField(f: string): void {
    setField(f)
    setRampName('') // new value set -> back to auto palette until a ramp is chosen
    const sf = fields?.find((x) => x.field === f)
    if (sf) void loadFieldValues(sf)
  }

  function setStopColor(value: string, color: string): void {
    setStops((prev) => prev.map((s) => (s.value === value ? { ...s, color } : s)))
  }

  // Apply a color ramp across the current stops (categorical cycles, sequential
  // interpolates). Individual swatches stay editable afterward.
  function applyRamp(name: string, flip: boolean): void {
    setRampName(name)
    setRampFlip(flip)
    const ramp = RAMPS.find((r) => r.name === name)
    if (!ramp) return
    setStops((prev) => {
      const cols = rampColors(ramp, prev.length, flip)
      return prev.map((s, i) => ({ ...s, color: cols[i] ?? s.color }))
    })
  }

  async function save(): Promise<void> {
    setBusy(true)
    setError(null)
    // c7 — layer-wide style attached to whichever renderer shape; only the active geometry's keys.
    const style: Partial<Renderer> =
      geometryType === 'line'
        ? { lineWidth, lineDash }
        : geometryType === 'polygon'
          ? { fillOpacity, outlineColor, outlineWidth }
          : { pointSize, pointIcon, minZoom: pointIcon !== null ? iconFromZoom : null }
    let payload: Renderer
    if (rtype === 'simple') {
      payload = { type: 'simple', symbol: { color: simpleColor }, ...style }
    } else {
      if (!field || stops.length === 0) {
        setError('Choose a field with at least one value.')
        setBusy(false)
        return
      }
      payload = {
        type: 'uniqueValue',
        field,
        minZoom: null,
        default: { color: defaultColor, label: NO_VALUE_LABEL },
        stops,
        ...style,
      }
    }
    try {
      await setLayerRenderer(layerId, payload, accessToken)
      onSaved(payload)
    } catch (err) {
      setError(err instanceof ApiError ? `${err.message} (HTTP ${err.status})` : 'Save failed')
    } finally {
      // Clear busy on SUCCESS too — as a float, onSaved unmounted this panel and took the
      // flag with it; docked (map-2a: Apply no longer self-closes) the panel stays mounted,
      // and a success-path leak left Save stuck on "Saving…".
      setBusy(false)
    }
  }

  async function resetToDefault(): Promise<void> {
    setBusy(true)
    setError(null)
    try {
      await clearLayerRenderer(layerId, accessToken)
      onSaved(null)
    } catch (err) {
      setError(err instanceof ApiError ? `${err.message} (HTTP ${err.status})` : 'Reset failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="styles-panel">
      <div className="styles-panel__header">
        <h3 className="styles-panel__title">Style · {layerLabel}</h3>
        <CloseButton onClick={onClose} />
      </div>

      <div className="styles-panel__row" role="radiogroup" aria-label="Symbology type">
        <label className="styles-panel__radio">
          <input type="radio" checked={rtype === 'simple'} onChange={() => setRtype('simple')} />
          Single symbol
        </label>
        <label className="styles-panel__radio">
          <input type="radio" checked={rtype === 'uniqueValue'} onChange={() => setRtype('uniqueValue')} />
          Types (unique values)
        </label>
      </div>

      {rtype === 'simple' && (
        <div className="styles-panel__field">
          <span>Color</span>
          <input type="color" value={simpleColor} onChange={(e) => setSimpleColor(e.target.value)} />
        </div>
      )}

      {rtype === 'uniqueValue' && (
        <>
          <label className="styles-panel__field">
            <span>Attribute</span>
            {fieldsError ? (
              <span className="styles-panel__muted">Could not load fields ({fieldsError})</span>
            ) : (
              <select value={field} onChange={(e) => onPickField(e.target.value)} disabled={!fields}>
                <option value="">{fields ? 'Choose a field…' : 'Loading…'}</option>
                {fields?.map((f) => (
                  <option key={f.field} value={f.field}>
                    {fieldLabel(f.field)}
                  </option>
                ))}
              </select>
            )}
          </label>

          {valuesLoading && <p className="styles-panel__muted">Loading values…</p>}
          {valuesWarning && <p className="styles-panel__warn">{valuesWarning}</p>}

          {!valuesLoading && stops.length > 0 && (
            <div className="styles-panel__ramps">
              <span className="styles-panel__muted">Color ramp</span>
              <div className="styles-panel__ramps-row">
                {RAMPS.map((r) => (
                  <button
                    key={r.name}
                    type="button"
                    className={`styles-panel__ramp${rampName === r.name ? ' is-selected' : ''}`}
                    style={{ background: rampPreview(r) }}
                    title={r.name}
                    aria-label={`Apply ${r.name} ramp`}
                    onClick={() => applyRamp(r.name, rampFlip)}
                  />
                ))}
                <label className="styles-panel__ramp-flip" title="Reverse the ramp">
                  <input
                    type="checkbox"
                    checked={rampFlip}
                    disabled={!rampName}
                    onChange={() => applyRamp(rampName, !rampFlip)}
                  />
                  Flip
                </label>
              </div>
            </div>
          )}

          {!valuesLoading && stops.length > 0 && (
            <ul className="styles-panel__stops">
              {stops.map((s) => (
                <li className="styles-panel__stop" key={s.value}>
                  <input
                    type="color"
                    value={s.color}
                    aria-label={`Color for ${s.label ?? s.value}`}
                    onChange={(e) => setStopColor(s.value, e.target.value)}
                  />
                  <span className="styles-panel__stop-label">{s.label ?? s.value}</span>
                </li>
              ))}
              <li className="styles-panel__stop styles-panel__stop--muted">
                <input
                  type="color"
                  value={defaultColor}
                  aria-label="Color for «No value»"
                  onChange={(e) => setDefaultColor(e.target.value)}
                />
                <span className="styles-panel__stop-label">{NO_VALUE_LABEL}</span>
              </li>
            </ul>
          )}
        </>
      )}

      {/* Layer-wide geometry style (shown per geometry, for both renderer types). */}
      {geometryType === 'point' && (
        <div className="styles-panel__geom">
          <div className="styles-panel__field styles-panel__field--stack">
            <span>Symbol</span>
            <IconPicker
              value={pointIcon}
              onChange={setPointIcon}
              color={rtype === 'simple' ? simpleColor : (stops[0]?.color ?? simpleColor)}
            />
          </div>
          {pointIcon !== null && (
            <>
              <div className="styles-panel__field">
                <span>Icons from zoom</span>
                <input
                  type="number"
                  min={6}
                  max={16}
                  step={1}
                  value={iconFromZoom ?? 14}
                  onChange={(e) => {
                    const v = Number(e.target.value)
                    setIconFromZoom(Number.isFinite(v) ? Math.min(16, Math.max(6, v)) : null)
                  }}
                />
              </div>
              <p className="styles-panel__muted" style={{ margin: '0 0 0.5rem' }}>
                Zoomed out past this, the layer draws as circles — icons are expensive to place at
                low zoom on large layers (the default, 14, was measured on a 300k-point county layer).
              </p>
            </>
          )}
          <div className="styles-panel__field">
            <span>Size</span>
            <input
              type="range"
              min={2}
              max={40}
              step={1}
              value={pointSize ?? DEFAULT_POINT_DIAMETER}
              disabled={pointSize === null}
              onChange={(e) => setPointSize(Number(e.target.value))}
            />
            <input
              type="number"
              min={2}
              max={40}
              step={1}
              value={pointSize ?? DEFAULT_POINT_DIAMETER}
              disabled={pointSize === null}
              onChange={(e) => setPointSize(Number(e.target.value))}
            />
            <span className="styles-panel__muted">px</span>
          </div>
          {/* AGO's "Adjust size automatically" by another name — and it is what points have always
              done: a zoom ramp from 3px at z10 to 14px at z17. Checked = no stored size. */}
          <label className="styles-panel__check">
            <input
              type="checkbox"
              checked={pointSize === null}
              onChange={(e) => setPointSize(e.target.checked ? null : DEFAULT_POINT_DIAMETER)}
            />
            Scale with zoom
          </label>
        </div>
      )}

      {geometryType === 'line' && (
        <div className="styles-panel__geom">
          <div className="styles-panel__field">
            <span>Line width</span>
            <input
              type="number"
              min={0.5}
              max={20}
              step={0.5}
              value={lineWidth}
              onChange={(e) => setLineWidth(Number(e.target.value))}
            />
            <span className="styles-panel__muted">px</span>
          </div>
          <label className="styles-panel__field">
            <span>Line style</span>
            <select
              value={dashKeyOf(lineDash)}
              onChange={(e) =>
                setLineDash(DASH_PRESETS.find((d) => d.key === e.target.value)?.value ?? null)
              }
            >
              {DASH_PRESETS.map((d) => (
                <option key={d.key} value={d.key}>
                  {d.label}
                </option>
              ))}
            </select>
          </label>
        </div>
      )}

      {geometryType === 'polygon' && (
        <div className="styles-panel__geom">
          <div className="styles-panel__field">
            <span>Fill opacity</span>
            <input
              type="range"
              min={0}
              max={1}
              step={0.05}
              value={fillOpacity}
              onChange={(e) => setFillOpacity(Number(e.target.value))}
            />
            <span className="styles-panel__muted">{Math.round(fillOpacity * 100)}%</span>
          </div>
          <div className="styles-panel__field">
            <span>Outline color</span>
            <input
              type="color"
              value={outlineColor}
              onChange={(e) => setOutlineColor(e.target.value)}
            />
          </div>
          <div className="styles-panel__field">
            <span>Outline width</span>
            <input
              type="number"
              min={0}
              max={20}
              step={0.5}
              value={outlineWidth}
              onChange={(e) => setOutlineWidth(Number(e.target.value))}
            />
            <span className="styles-panel__muted">px</span>
          </div>
        </div>
      )}

      {error && <p className="sign-form__error">{error}</p>}

      <div className="sign-form__actions">
        <button type="button" className="btn btn--sm" onClick={() => void resetToDefault()} disabled={busy}>
          Reset to default
        </button>
        <button type="button" className="btn btn--sm" onClick={onClose} disabled={busy}>
          Cancel
        </button>
        <button type="button" className="btn btn--sm" onClick={() => void save()} disabled={busy}>
          {busy ? 'Saving…' : 'Save'}
        </button>
      </div>
    </div>
  )
}
