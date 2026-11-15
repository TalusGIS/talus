import { EXT_PALETTE } from '@/features/map/addFromUrl'
import { GeometryIcon } from '@/features/catalog/ItemTypeIcons'
import type { SketchFeature } from '@/lib/api'
import CloseButton from '@/components/CloseButton'

/**
 * The sketch toolbar + per-feature edit card (decision-sketch-layer.md §2.2). Toolbar: pick a
 * geometry tool (stays active for successive notes, AGO-style), a color, and an optional label
 * applied to the NEXT drawn feature. Card: opened by clicking a sketch feature on the map —
 * recolor / relabel / reshape (terra-draw select; point = drag) / delete. Both are dumb controls;
 * AssetMap owns the state and the terra-draw sessions.
 */

export type SketchTool = 'point' | 'line' | 'polygon'

const TOOLS: { key: SketchTool; label: string }[] = [
  { key: 'point', label: 'Point' },
  { key: 'line', label: 'Line' },
  { key: 'polygon', label: 'Polygon' },
]

function Palette({ value, onPick }: { value: string; onPick: (c: string) => void }) {
  return (
    <div className="modal__swatches sketch__swatches">
      {EXT_PALETTE.map((c) => (
        <button
          key={c}
          type="button"
          className={`modal__swatch${value === c ? ' is-selected' : ''}`}
          style={{ background: c }}
          aria-label={`Color ${c}`}
          aria-pressed={value === c}
          onClick={() => onPick(c)}
        />
      ))}
    </div>
  )
}

export function SketchToolbar({
  tool,
  onTool,
  color,
  onColor,
  label,
  onLabel,
  onClose,
}: {
  tool: SketchTool | null
  onTool: (t: SketchTool | null) => void
  color: string
  onColor: (c: string) => void
  label: string
  onLabel: (v: string) => void
  onClose: () => void
}) {
  return (
    <div className="map__panel sketch__toolbar">
      <div className="add-layers__header">
        <span className="add-layers__title">Sketch</span>
        <CloseButton onClick={onClose} label="Close sketch toolbar" className="add-layers__close" />
      </div>
      <div className="sketch__tools" role="group" aria-label="Sketch tool">
        {TOOLS.map((t) => (
          <button
            key={t.key}
            type="button"
            className={`add-layers__source${tool === t.key ? ' is-active' : ''}`}
            aria-pressed={tool === t.key}
            onClick={() => onTool(tool === t.key ? null : t.key)}
          >
            <GeometryIcon geomType={t.key} className="sketch__tool-icon" />
            {t.label}
          </button>
        ))}
      </div>
      <Palette value={color} onPick={onColor} />
      <input
        type="text"
        className="sketch__label-input"
        placeholder="Label for the next note (optional)"
        value={label}
        maxLength={80}
        onChange={(e) => onLabel(e.target.value)}
        aria-label="Label for the next sketch note"
      />
      <p className="modal__muted sketch__hint">
        {tool
          ? tool === 'point'
            ? 'Click the map to place a note.'
            : `Click to add vertices; ${tool === 'line' ? 'double-click' : 'close the ring'} to finish.`
          : 'Pick a tool, then draw on the map. Notes save with the map.'}
      </p>
    </div>
  )
}

export function SketchFeatureCard({
  feature,
  reshaping,
  onColor,
  onLabel,
  onReshape,
  onDelete,
  onClose,
}: {
  feature: SketchFeature
  reshaping: boolean
  onColor: (c: string) => void
  onLabel: (v: string) => void
  onReshape: () => void
  onDelete: () => void
  onClose: () => void
}) {
  // Docked into the inspector region since handoff-map-2a §2 (commit 4) — the float-collision
  // offset variant is gone with it (E6).
  return (
    <div className="sketch__card">
      <div className="add-layers__header">
        <span className="add-layers__title">
          <GeometryIcon geomType={feature.geometryType} className="sketch__tool-icon" /> Sketch note
        </span>
        <CloseButton onClick={onClose} label="Close note editor" className="add-layers__close" />
      </div>
      <Palette value={feature.color} onPick={onColor} />
      <input
        type="text"
        className="sketch__label-input"
        placeholder="Label (optional)"
        value={feature.label ?? ''}
        maxLength={80}
        onChange={(e) => onLabel(e.target.value)}
        aria-label="Sketch note label"
      />
      <div className="sign-form__actions">
        <button
          type="button"
          className={`btn btn--sm${reshaping ? ' is-active' : ''}`}
          aria-pressed={reshaping}
          onClick={onReshape}
        >
          {reshaping
            ? 'Done reshaping'
            : feature.geometryType === 'point'
              ? 'Move'
              : 'Reshape'}
        </button>
        <button type="button" className="btn btn--sm" onClick={onDelete}>
          Delete
        </button>
      </div>
      {reshaping && (
        <p className="modal__muted sketch__hint">
          {feature.geometryType === 'point'
            ? 'Drag the marker to its new spot.'
            : 'Drag vertices; drag a midpoint to add one; right-click a vertex to delete it.'}
        </p>
      )}
    </div>
  )
}
