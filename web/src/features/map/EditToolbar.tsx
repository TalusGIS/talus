/**
 * Top-center editing toolbar (decision-editing-toolbar.md, + icon chrome) — shown
 * while Edit mode is ON, replacing the old per-layer Create/Draw rail button stack. One armed
 * TARGET layer (flyout picker, the Filter/Labels/Select rail idiom's list styles) + an
 * add-features split button: "+" toggles the layer's default create gesture; for polygon layers a
 * caret opens the shape variants (draw/rectangle/circle/freehand — all emit ordinary Polygons).
 * Dumb control: AssetMap owns the armed state, the terra-draw session, and Esc semantics
 * (Esc = cancel + disarm, §3.1). Delete-selected + edit-vertices icons arrive with the F4
 * follow-up slice — the toolbar deliberately leaves room beside the split button.
 */
import { useState } from 'react'
import { ChevronDown, Plus, Trash2, Waypoints } from 'lucide-react'

import { GeometryIcon } from '@/features/catalog/ItemTypeIcons'
import type { GeometryType } from '@/lib/api'
import type { DrawTool } from '@/features/map/useFeatureDraw'

export interface EditToolbarLayer {
  id: string
  label: string
  geometryType: GeometryType
  // V109 (decision-layer-edit-capabilities.md): the owner's flags per layer — the toolbar hides "+" /
  // vertices / delete for the target where one is off. Absent (a snap-only reference layer) = no gating.
  canAdd?: boolean
  canShape?: boolean
  canDelete?: boolean
}

/** An armed create gesture: point click-to-place, or one of the terra-draw tools. */
export type EditGesture = 'place' | Exclude<DrawTool, 'point'>

// The "+" button's default gesture per geometry family; polygon's shape variants live in the caret.
const DEFAULT_GESTURE: Record<GeometryType, EditGesture> = {
  point: 'place',
  line: 'line',
  polygon: 'polygon',
}

const POLYGON_GESTURES: { key: EditGesture; label: string }[] = [
  { key: 'polygon', label: 'Draw' },
  { key: 'rectangle', label: 'Rectangle' },
  { key: 'circle', label: 'Circle' },
  { key: 'freehand', label: 'Freehand' },
]

const HINTS: Record<EditGesture, string> = {
  place: 'Click the map to place.',
  line: 'Click to add vertices; double-click to finish.',
  polygon: 'Click to add vertices; double-click to finish.',
  rectangle: 'Click a corner, then click the opposite corner.',
  circle: 'Click the center, then click to set the radius.',
  freehand: 'Click, move to draw, click to finish.',
}

const GESTURE_LABEL: Record<EditGesture, string> = {
  place: 'Place',
  line: 'Draw',
  polygon: 'Draw',
  rectangle: 'Rectangle',
  circle: 'Circle',
  freehand: 'Freehand',
}

// Icons are Lucide at one stroke weight (handoff-map-2a §3.1, E4) — the three hand-rolled
// glyphs (add-features plus, connected-vertices, trash) and the ▾ carets are gone.

export default function EditToolbar({
  layers,
  targetId,
  armed,
  onTarget,
  onArm,
  shapeAvailable,
  shapeActive,
  onShape,
  selectedCount,
  onDeleteSelected,
  snapOn,
  snapExtras,
  snapChecked,
  snapPaused,
  onSnapToggle,
  onSnapCheck,
}: {
  // The map's creatable/drawable editable layers, TOC order (never empty — AssetMap gates render).
  layers: EditToolbarLayer[]
  targetId: string
  // The armed create gesture for the target layer (null = idle; identify/popups work normally).
  armed: EditGesture | null
  onTarget: (layerId: string) => void
  onArm: (gesture: EditGesture | null) => void
  // F4 slice: vertices tool (line/polygon targets only — hidden otherwise, F1) + delete-selected.
  shapeAvailable: boolean
  shapeActive: boolean
  onShape: () => void
  // The TARGET layer's current selection size; 0 disables delete (F1).
  selectedCount: number
  onDeleteSelected: () => void
  // Snapping (decision-web-editor-snapping.md §3.3): master toggle + per-layer checks live IN
  // this flyout (AGO Editor-Settings pattern). `snapExtras` = visible reference
  // layers — snappable, never armable. `snapChecked` absent = checked; `snapPaused` marks
  // density-paused layers ("zoom in to snap").
  snapOn: boolean
  snapExtras: EditToolbarLayer[]
  snapChecked: Record<string, boolean>
  snapPaused: Record<string, boolean>
  onSnapToggle: (on: boolean) => void
  onSnapCheck: (layerId: string, on: boolean) => void
}) {
  const [pickerOpen, setPickerOpen] = useState(false)
  const [shapesOpen, setShapesOpen] = useState(false)
  // The polygon gesture "+" re-arms — sticky per session so Rectangle stays one click after the
  // first flyout pick.
  const [lastShape, setLastShape] = useState<EditGesture>('polygon')
  const target = layers.find((l) => l.id === targetId) ?? layers[0]
  const isPolygon = target.geometryType === 'polygon'
  const plusGesture = isPolygon ? lastShape : DEFAULT_GESTURE[target.geometryType]

  return (
    <div className="edit-toolbar" role="toolbar" aria-label="Editing tools">
      <div className="edit-toolbar__target">
        <button
          type="button"
          className="edit-toolbar__picker"
          aria-expanded={pickerOpen}
          aria-haspopup="listbox"
          title="Choose the layer to edit"
          onClick={() => {
            setShapesOpen(false)
            setPickerOpen((o) => !o)
          }}
        >
          <GeometryIcon geomType={target.geometryType} className="rail-flyout__geom" />
          <span className="edit-toolbar__picker-name">{target.label}</span>
          <ChevronDown className="edit-toolbar__caret" size={14} aria-hidden="true" />
        </button>
        {pickerOpen && (
          <div className="edit-toolbar__flyout">
            {/* Snapping master toggle (decision-web-editor-snapping.md §3.3 — try-it placement,
                circle back to a toolbar magnet if it feels clunky mid-digitizing). */}
            <label className="edit-toolbar__snaprow">
              <input
                type="checkbox"
                checked={snapOn}
                onChange={(e) => onSnapToggle(e.target.checked)}
              />
              Enable snapping
            </label>
            {/* ArcMap Snapping-Environment idiom: ONE list of every
                visible vector layer with an Edit column (radio — the single armed target; empty
                cell = not editable) and a Snap column (checkbox). Picking a target closes the
                flyout (the old row-click flow); snap changes keep it open. */}
            <div className="edit-toolbar__gridhead" aria-hidden="true">
              <span className="edit-toolbar__gridname">Layer</span>
              <span className="edit-toolbar__gridcol">Edit</span>
              <span className="edit-toolbar__gridcol">Snap</span>
            </div>
            {[...layers.map((l) => ({ l, editable: true })), ...snapExtras.map((l) => ({ l, editable: false }))].map(
              ({ l, editable }) => (
                <div key={l.id} className="edit-toolbar__gridrow">
                  {editable ? (
                    <button
                      type="button"
                      className="rail-flyout__opt edit-toolbar__gridpick"
                      title={`Edit ${l.label}`}
                      onClick={() => {
                        onTarget(l.id)
                        setPickerOpen(false)
                      }}
                    >
                      <GeometryIcon geomType={l.geometryType} className="rail-flyout__geom" />
                      <span className="rail-flyout__opt-name">{l.label}</span>
                      {snapOn && snapPaused[l.id] && (snapChecked[l.id] ?? true) && (
                        <span className="edit-toolbar__snaphint">zoom in to snap</span>
                      )}
                    </button>
                  ) : (
                    <span className="edit-toolbar__gridpick edit-toolbar__gridpick--static">
                      <GeometryIcon geomType={l.geometryType} className="rail-flyout__geom" />
                      <span className="rail-flyout__opt-name">{l.label}</span>
                      {snapOn && snapPaused[l.id] && (snapChecked[l.id] ?? true) && (
                        <span className="edit-toolbar__snaphint">zoom in to snap</span>
                      )}
                    </span>
                  )}
                  <span className="edit-toolbar__gridcol">
                    {editable ? (
                      <input
                        type="radio"
                        name="edit-target-layer"
                        aria-label={`Edit ${l.label}`}
                        checked={l.id === target.id}
                        onChange={() => {
                          onTarget(l.id)
                          setPickerOpen(false)
                        }}
                      />
                    ) : (
                      <span className="edit-toolbar__griddash">–</span>
                    )}
                  </span>
                  <span className="edit-toolbar__gridcol">
                    <input
                      type="checkbox"
                      title={`Snap to ${l.label}`}
                      aria-label={`Snap to ${l.label}`}
                      disabled={!snapOn}
                      checked={snapOn && (snapChecked[l.id] ?? true)}
                      onChange={(e) => onSnapCheck(l.id, e.target.checked)}
                    />
                  </span>
                </div>
              ),
            )}
          </div>
        )}
      </div>

      {/* V109: the whole add group (the "+", the polygon shapes caret, the shapes flyout) hides where the
          target's owner turned Add features off; the vertices tool and delete follow their own flags. */}
      {target.canAdd !== false && (
      <div className="edit-toolbar__gestures">
        <button
          type="button"
          className={`map__iconbtn edit-toolbar__iconbtn${armed ? ' is-active' : ''}`}
          aria-pressed={armed !== null}
          title={
            armed
              ? `${GESTURE_LABEL[armed]} — click to stop`
              : `Add features (${GESTURE_LABEL[plusGesture].toLowerCase()})`
          }
          onClick={() => {
            setShapesOpen(false)
            onArm(armed ? null : plusGesture)
          }}
        >
          <Plus size={16} aria-hidden="true" />
        </button>
        {isPolygon && (
          <button
            type="button"
            className="map__iconbtn edit-toolbar__iconbtn edit-toolbar__more"
            aria-expanded={shapesOpen}
            aria-haspopup="listbox"
            title="More shapes"
            onClick={() => {
              setPickerOpen(false)
              setShapesOpen((o) => !o)
            }}
          >
            <ChevronDown className="edit-toolbar__caret" size={14} aria-hidden="true" />
          </button>
        )}
        {shapesOpen && (
          <div className="edit-toolbar__flyout edit-toolbar__flyout--shapes">
            <div className="rail-flyout__head">Shape</div>
            <div className="rail-flyout__list" role="listbox" aria-label="Polygon shape gestures">
              {POLYGON_GESTURES.map((g) => (
                <button
                  key={g.key}
                  type="button"
                  role="option"
                  aria-selected={armed === g.key}
                  className="rail-flyout__opt"
                  onClick={() => {
                    setLastShape(g.key)
                    onArm(g.key)
                    setShapesOpen(false)
                  }}
                >
                  <span className="rail-flyout__opt-name">{g.label}</span>
                </button>
              ))}
            </div>
          </div>
        )}
      </div>
      )}

      {shapeAvailable && (
        <button
          type="button"
          className={`map__iconbtn edit-toolbar__iconbtn${shapeActive ? ' is-active' : ''}`}
          aria-pressed={shapeActive}
          title={shapeActive ? 'Editing shapes — click to stop' : 'Edit vertices (click a feature)'}
          onClick={() => {
            setPickerOpen(false)
            setShapesOpen(false)
            onShape()
          }}
        >
          <Waypoints size={16} aria-hidden="true" />
        </button>
      )}

      {target.canDelete !== false && (
      <span className="edit-toolbar__delwrap">
        <button
          type="button"
          className="map__iconbtn edit-toolbar__iconbtn"
          disabled={selectedCount === 0}
          title={
            selectedCount === 0
              ? 'Delete selected — select features first'
              : `Delete ${selectedCount} selected feature${selectedCount === 1 ? '' : 's'}`
          }
          onClick={() => {
            setPickerOpen(false)
            setShapesOpen(false)
            onDeleteSelected()
          }}
        >
          <Trash2 size={16} aria-hidden="true" />
        </button>
        {selectedCount > 0 && (
          <span className="rail-flyout__badge" aria-hidden="true">
            {selectedCount}
          </span>
        )}
      </span>
      )}

      {armed && <span className="edit-toolbar__hint">{HINTS[armed]} Esc cancels.</span>}
      {!armed && shapeActive && (
        <span className="edit-toolbar__hint">Click a feature to edit its shape. Esc exits.</span>
      )}
    </div>
  )
}
