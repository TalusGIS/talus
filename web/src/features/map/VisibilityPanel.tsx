/**
 * The layer inspector's Visibility tab (decision-layer-zoom-range.md §4.2, §4.3, ruled 2026-11-23): the
 * zooms at which a layer is drawn, in two halves in one place —
 *   - THIS MAP: "use the layer's range" (the default) or "set a range for this map", for anyone who can
 *     edit the map. Applied to the map like labels and filters, and saved with it.
 *   - THE LAYER'S DEFAULT: for the layer's owner or an admin, hosted layers and views only. Saved at once
 *     to the layer's style (the existing renderer route), so it reaches every map that uses the layer's
 *     range.
 * The control is a two-ended slider with named local-government stops, a "current map view" choice and a
 * typed zoom for each end; each stop shows its zoom and the approximate scale at this map's centre. What is
 * stored is the zoom (§4.2: a scale is only shown, since a zoom's scale changes with latitude).
 *
 * Dumb about the map beyond reading its view: AssetMap owns the stored ranges and does the applying.
 */
import { useEffect, useId, useState } from 'react'
import type maplibregl from 'maplibre-gl'

import CloseButton from '@/components/CloseButton'
import {
  describeRange,
  editorFrom,
  editorProblem,
  formatScale,
  hasRange,
  inRange,
  layerPairFrom,
  mapPairFrom,
  rangeInForce,
  scaleAt,
  widensOut,
  ZOOM_CEILING,
  ZOOM_STOPS,
  type EditorRange,
  type ZoomPair,
} from '@/features/map/zoomRange'

interface View {
  zoom: number
  lat: number
}

/** The map's zoom and centre latitude, kept current while the panel is open. */
function useMapView(map: maplibregl.Map | null): View {
  const read = (): View =>
    map ? { zoom: Math.round(map.getZoom() * 10) / 10, lat: map.getCenter().lat } : { zoom: 0, lat: 0 }
  const [view, setView] = useState<View>(read)
  useEffect(() => {
    if (!map) return
    const onEnd = (): void => setView(read())
    onEnd()
    map.on('moveend', onEnd)
    return () => {
      map.off('moveend', onEnd)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `read` closes over `map` only
  }, [map])
  return view
}

const z1 = (v: number): string => String(Math.round(v * 10) / 10)

/** One end of the range: a named stop, the current view, no limit, or a typed zoom. */
function EndPicker({
  label,
  end,
  value,
  view,
  onChange,
}: {
  label: string
  end: 'min' | 'max'
  value: number | null
  view: View
  onChange: (v: number | null) => void
}) {
  const id = useId()
  const stop = value === null ? 'none' : (ZOOM_STOPS.find((s) => s.zoom === value)?.name ?? 'custom')
  // The typed box keeps its own text so a half-typed "1" is not snapped to a stop while the user types.
  const [text, setText] = useState(value === null ? '' : z1(value))
  useEffect(() => setText(value === null ? '' : z1(value)), [value])
  const scale = (z: number): string => formatScale(scaleAt(z, view.lat))
  return (
    <div className="visibility-panel__end">
      <label htmlFor={`${id}-stop`}>{label}</label>
      <select
        id={`${id}-stop`}
        value={stop}
        onChange={(e) => {
          const v = e.target.value
          if (v === 'none') onChange(null)
          else if (v === 'view') onChange(view.zoom)
          else {
            const s = ZOOM_STOPS.find((x) => x.name === v)
            if (s) onChange(s.zoom)
          }
        }}
      >
        <option value="none">{end === 'min' ? 'No limit (all the way out)' : 'No limit (all the way in)'}</option>
        {ZOOM_STOPS.map((s) => (
          <option key={s.name} value={s.name}>
            {s.name} · zoom {s.zoom} · about {scale(s.zoom)}
          </option>
        ))}
        <option value="view">
          Current map view · zoom {z1(view.zoom)} · about {scale(view.zoom)}
        </option>
        {stop === 'custom' && value !== null && (
          <option value="custom">
            Zoom {z1(value)} · about {scale(value)}
          </option>
        )}
      </select>
      <input
        type="number"
        min={0}
        max={ZOOM_CEILING}
        step={0.1}
        inputMode="decimal"
        aria-label={`${label}, as a zoom`}
        placeholder="none"
        value={text}
        onChange={(e) => {
          setText(e.target.value)
          const t = e.target.value.trim()
          if (t === '') onChange(null)
          else if (!Number.isNaN(Number(t))) onChange(Number(t))
        }}
      />
    </div>
  )
}

/** The two-ended slider plus the two end pickers. The slider's far ends mean "no limit". */
function RangeEditor({
  value,
  onChange,
  view,
  disabled,
}: {
  value: EditorRange
  onChange: (v: EditorRange) => void
  view: View
  disabled?: boolean
}) {
  const lo = value.min ?? 0
  const hi = value.max ?? ZOOM_CEILING
  const pct = (z: number): string => `${(Math.max(0, Math.min(ZOOM_CEILING, z)) / ZOOM_CEILING) * 100}%`
  return (
    <fieldset className="visibility-panel__editor" disabled={disabled}>
      <div className="visibility-panel__slider">
        <div className="visibility-panel__track" aria-hidden="true">
          <div className="visibility-panel__fill" style={{ left: pct(lo), right: `calc(100% - ${pct(hi)})` }} />
          {ZOOM_STOPS.map((s) => (
            <span key={s.name} className="visibility-panel__tick" style={{ left: pct(s.zoom) }} title={s.name} />
          ))}
          <span
            className="visibility-panel__here"
            style={{ left: pct(view.zoom) }}
            title={`The map is at zoom ${z1(view.zoom)}`}
          />
        </div>
        <input
          type="range"
          min={0}
          max={ZOOM_CEILING}
          step={0.5}
          value={lo}
          aria-label="Lowest zoom it is drawn at"
          onChange={(e) => {
            const v = Math.min(Number(e.target.value), hi - 0.5)
            onChange({ ...value, min: v <= 0 ? null : v })
          }}
        />
        <input
          type="range"
          min={0}
          max={ZOOM_CEILING}
          step={0.5}
          value={hi}
          aria-label="Highest zoom it is drawn at"
          onChange={(e) => {
            const v = Math.max(Number(e.target.value), lo + 0.5)
            onChange({ ...value, max: v >= ZOOM_CEILING ? null : v })
          }}
        />
        <div className="visibility-panel__scale-ends" aria-hidden="true">
          <span>Zoomed out</span>
          <span>Zoomed in</span>
        </div>
      </div>
      <EndPicker label="Show from" end="min" value={value.min} view={view} onChange={(v) => onChange({ ...value, min: v })} />
      <EndPicker label="Up to" end="max" value={value.max} view={view} onChange={(v) => onChange({ ...value, max: v })} />
    </fieldset>
  )
}

export default function VisibilityPanel({
  layerLabel,
  mapPair,
  layerDefault,
  canSetDefault,
  map,
  onApplyMap,
  onSaveDefault,
  onClose,
}: {
  layerLabel: string
  /** This map's own pair for the layer; absent = it uses the layer's range. */
  mapPair: ZoomPair | undefined
  /** The layer's default (its style's pair), or null when it has none or no style can carry one. */
  layerDefault: ZoomPair | null
  /** Owner or admin of a hosted layer or view — may change the default. */
  canSetDefault: boolean
  map: maplibregl.Map | null
  /** null = go back to using the layer's range. */
  onApplyMap: (pair: ZoomPair | null) => void
  /** null = remove the default. Rejects with a message on failure. */
  onSaveDefault: (pair: ZoomPair | null) => Promise<void>
  onClose: () => void
}) {
  const view = useMapView(map)
  const defaultRange = rangeInForce(null, layerDefault)
  const scale = (z: number): string => formatScale(scaleAt(z, view.lat))

  // --- this map ---
  const [own, setOwn] = useState(hasRange(mapPair))
  const [mapEdit, setMapEdit] = useState<EditorRange>(() => editorFrom(hasRange(mapPair) ? mapPair : layerDefault))
  const mapProblem = own ? editorProblem(mapEdit) : null
  const pendingMap = own ? mapPairFrom(mapEdit) : null
  const inForce = rangeInForce(pendingMap, layerDefault)
  const slow = own && widensOut(pendingMap, layerDefault)
  // What the map draws NOW — the applied range, not the edit waiting for Apply.
  const drawnNow = inRange(rangeInForce(mapPair, layerDefault), view.zoom)

  // --- the layer's default ---
  const [defEdit, setDefEdit] = useState<EditorRange>(() => editorFrom(layerDefault))
  const defProblem = editorProblem(defEdit)
  const [defBusy, setDefBusy] = useState(false)
  const [defError, setDefError] = useState<string | null>(null)
  const [defSaved, setDefSaved] = useState(false)
  const defChanged = JSON.stringify(layerPairFrom(defEdit)) !== JSON.stringify(layerPairFrom(editorFrom(layerDefault)))

  async function saveDefault(): Promise<void> {
    setDefBusy(true)
    setDefError(null)
    setDefSaved(false)
    try {
      await onSaveDefault(layerPairFrom(defEdit))
      setDefSaved(true)
    } catch (err) {
      setDefError(err instanceof Error ? err.message : 'Save failed')
    } finally {
      setDefBusy(false)
    }
  }

  return (
    <div className="styles-panel visibility-panel">
      <div className="styles-panel__header">
        <h3 className="styles-panel__title">Visibility · {layerLabel}</h3>
        <CloseButton onClick={onClose} />
      </div>

      <p className={`visibility-panel__now${drawnNow ? '' : ' is-out'}`}>
        The map is at zoom {z1(view.zoom)} (about {scale(view.zoom)}).{' '}
        {drawnNow ? 'This layer is drawn here.' : 'This layer is not drawn here.'}
      </p>

      <section className="visibility-panel__section" aria-label="This map">
        <h4 className="visibility-panel__heading">On this map</h4>
        <label className="styles-panel__radio">
          <input type="radio" checked={!own} onChange={() => setOwn(false)} />
          Use the layer’s range: {describeRange(defaultRange)}
        </label>
        <label className="styles-panel__radio">
          <input type="radio" checked={own} onChange={() => setOwn(true)} />
          Set a range for this map
        </label>
        {own && <RangeEditor value={mapEdit} onChange={setMapEdit} view={view} />}
        {own && !mapProblem && <p className="styles-panel__muted">{describeRange(inForce)} on this map.</p>}
        {mapProblem && <p className="sign-form__error">{mapProblem}</p>}
        {slow && (
          <p className="visibility-panel__warn">
            The layer’s owner set it to show from zoom {z1(defaultRange.min)}. Drawing it further out can be slow.
          </p>
        )}
        <div className="sign-form__actions">
          <button type="button" className="btn btn--sm" onClick={onClose}>
            Cancel
          </button>
          <button type="button" className="btn btn--sm" disabled={!!mapProblem} onClick={() => onApplyMap(pendingMap)}>
            Apply
          </button>
        </div>
      </section>

      {canSetDefault && (
        <section className="visibility-panel__section" aria-label="The layer's default">
          <h4 className="visibility-panel__heading">The layer’s default</h4>
          <p className="styles-panel__muted">
            Every map that uses the layer’s range draws it here. Saved to the layer straight away.
          </p>
          <RangeEditor
            value={defEdit}
            onChange={(v) => {
              setDefEdit(v)
              setDefSaved(false)
            }}
            view={view}
            disabled={defBusy}
          />
          {defProblem && <p className="sign-form__error">{defProblem}</p>}
          {defError && <p className="sign-form__error">{defError}</p>}
          {defSaved && !defChanged && <p className="styles-panel__muted">Saved.</p>}
          <div className="sign-form__actions">
            <button
              type="button"
              className="btn btn--sm"
              disabled={defBusy || !!defProblem || !defChanged}
              onClick={() => void saveDefault()}
            >
              {defBusy ? 'Saving…' : 'Save default'}
            </button>
          </div>
        </section>
      )}
    </div>
  )
}
