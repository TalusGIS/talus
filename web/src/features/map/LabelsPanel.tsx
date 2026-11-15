/**
 * Per-layer map labels config panel (decision-map-labels.md). Opened from the Labels rail tool's layer
 * picker or the layer's right-click menu, it configures the ONE label class v1 allows: text = an attribute
 * field (no expressions, no concatenation — the standing no-expression-engine fence), plus size / color /
 * halo color / bold / min-zoom. On Apply the parent (AssetMap) stores the config on the layer, renders the
 * symbol layer (swapping a slot to the GeoJSON path — its tiles carry no attributes), and persists it with
 * the map. "Remove labels" clears the config entirely (onApply(null)).
 *
 * Field list = the same /layers/{id}/columns source the FilterPanel uses (owner-defined props fields +
 * built-in columns), with the same internal columns hidden — NOT /symbolizable-fields (raw table columns).
 */
import { useEffect, useState } from 'react'

import { HIDDEN_FIELDS, fieldLabel } from '@/features/map/fields'
import { ApiError, fetchLayerColumns } from '@/lib/api'
import type { LayerColumn, WebMapLabelConfig } from '@/lib/api'
import CloseButton from '@/components/CloseButton'

// The shared style defaults (§3.5 of the decision doc): 12px near-black text with a white halo reads on
// BOTH light and dark basemaps, and never depends on which basemap was active when the author clicked.
// AssetMap's renderer falls back to these for any knob a persisted config omits (the server model fills
// the same values).
// eslint-disable-next-line react-refresh/only-export-components -- deliberate co-location (AssetMap's renderer shares these defaults); HMR full-reload accepted
export const LABEL_DEFAULTS = {
  size: 12,
  color: '#1a1a1a',
  haloColor: '#ffffff',
  bold: false,
} as const

const SIZE_MIN = 8
const SIZE_MAX = 32 // mirrors LabelConfig bounds (web_maps.py)

export interface LabelsPanelProps {
  layerId: string
  layerLabel: string
  accessToken: string
  initial: WebMapLabelConfig | null // the layer's current labels (null = off)
  onApply: (cfg: WebMapLabelConfig | null) => void // apply + persist-in-state; null removes labels
  onClose: () => void
  /** EXTERNAL layers (decision-add-from-url.md rail parity): the caller supplies the column
   *  schema from the remote service's metadata — no Talus /columns fetch. */
  columnsOverride?: LayerColumn[]
}

export default function LabelsPanel({
  layerId,
  layerLabel,
  accessToken,
  initial,
  onApply,
  onClose,
  columnsOverride,
}: LabelsPanelProps) {
  const [columns, setColumns] = useState<LayerColumn[] | null>(null)
  const [columnsError, setColumnsError] = useState<string | null>(null)
  const [field, setField] = useState(initial?.field ?? '')
  const [size, setSize] = useState(String(initial?.size ?? LABEL_DEFAULTS.size))
  const [color, setColor] = useState(initial?.color ?? LABEL_DEFAULTS.color)
  const [haloColor, setHaloColor] = useState(initial?.haloColor ?? LABEL_DEFAULTS.haloColor)
  const [bold, setBold] = useState(initial?.bold ?? LABEL_DEFAULTS.bold)
  const [minZoom, setMinZoom] = useState(initial?.minZoom != null ? String(initial.minZoom) : '')

  // Load the layer's column schema once — the field dropdown (same source + hidden set as FilterPanel).
  // Owner-defined VIRTUAL columns (c.props) are IN since the arc close-out (2026-07-03): the renderer
  // reads them off the GeoJSON path via a nested get (labeled slots always ride that path), so owner
  // fields are now first-class on every surface — this was the last holdout.
  useEffect(() => {
    if (columnsOverride) {
      setColumns(columnsOverride)
      return
    }
    let cancelled = false
    fetchLayerColumns(layerId, accessToken)
      .then((res) => {
        if (!cancelled) setColumns(res.columns.filter((c) => !HIDDEN_FIELDS.has(c.name)))
      })
      .catch((err) => {
        if (!cancelled) setColumnsError(err instanceof ApiError ? `HTTP ${err.status}` : 'request failed')
      })
    return () => {
      cancelled = true
    }
  }, [layerId, accessToken, columnsOverride])

  function apply(): void {
    if (!field) return
    const sz = Math.min(SIZE_MAX, Math.max(SIZE_MIN, Math.round(Number(size) || LABEL_DEFAULTS.size)))
    const mz = minZoom.trim() === '' ? undefined : Math.min(24, Math.max(0, Number(minZoom)))
    onApply({
      field,
      size: sz,
      color,
      haloColor,
      bold,
      ...(mz !== undefined && !Number.isNaN(mz) ? { minZoom: mz } : {}),
    })
  }

  return (
    <div className="styles-panel labels-panel">
      <div className="styles-panel__header">
        <h3 className="styles-panel__title">Labels · {layerLabel}</h3>
        <CloseButton onClick={onClose} />
      </div>

      {columnsError ? (
        <p className="sign-form__error">Could not load fields ({columnsError})</p>
      ) : !columns ? (
        <p className="styles-panel__muted">Loading fields…</p>
      ) : (
        <div className="labels-panel__grid">
          <label htmlFor="labels-field">Label by</label>
          <select
            id="labels-field"
            className="labels-panel__field"
            value={field}
            onChange={(e) => setField(e.target.value)}
          >
            <option value="">Field…</option>
            {columns.map((c) => (
              <option key={c.name} value={c.name}>
                {c.label ?? fieldLabel(c.name)}
              </option>
            ))}
          </select>

          <label htmlFor="labels-size">Size</label>
          <input
            id="labels-size"
            type="number"
            min={SIZE_MIN}
            max={SIZE_MAX}
            value={size}
            onChange={(e) => setSize(e.target.value)}
          />

          <label htmlFor="labels-color">Text color</label>
          <input
            id="labels-color"
            className="labels-panel__color"
            type="color"
            value={color}
            onChange={(e) => setColor(e.target.value)}
          />

          <label htmlFor="labels-halo">Halo color</label>
          <input
            id="labels-halo"
            className="labels-panel__color"
            type="color"
            value={haloColor}
            onChange={(e) => setHaloColor(e.target.value)}
          />

          <label htmlFor="labels-bold">Bold</label>
          <input
            id="labels-bold"
            className="labels-panel__check"
            type="checkbox"
            checked={bold}
            onChange={(e) => setBold(e.target.checked)}
          />

          <label htmlFor="labels-minzoom" title="Hide labels when zoomed out further than this">
            From zoom
          </label>
          <input
            id="labels-minzoom"
            type="number"
            min={0}
            max={24}
            step={1}
            placeholder="always"
            value={minZoom}
            onChange={(e) => setMinZoom(e.target.value)}
          />
        </div>
      )}

      <div className="sign-form__actions">
        {initial && (
          <button type="button" className="btn btn--sm labels-panel__remove" onClick={() => onApply(null)}>
            Remove labels
          </button>
        )}
        <button type="button" className="btn btn--sm" onClick={onClose}>
          Cancel
        </button>
        <button type="button" className="btn btn--sm" onClick={apply} disabled={!columns || !field}>
          Apply
        </button>
      </div>
    </div>
  )
}
