/**
 * Map header bar (docs/handoff-map-2a.md §2, chrome commit 1/4): the DOCUMENT actions in one
 * row — title, save-state chips, Add layers / Sketch / Export, and Save — moved out of the
 * floating rails so a button that changes the SAVED map never sits beside one that only changes
 * the view. Dumb control: AssetMap owns every piece of state; this renders it. Share and the
 * Add-layers/Export modals arrive with commit 4 — until then the buttons toggle today's
 * floating panels. The embed renders no header bar at all (ruled 2026-08-25).
 */

import type { ReactNode } from 'react'

export default function MapHeaderBar({
  title,
  titleEditable,
  onTitle,
  unsaved,
  readOnly,
  savedFlash,
  fieldReady,
  onFieldReady,
  packs,
  addOpen,
  onAddLayers,
  sketchOpen,
  onSketch,
  exportOpen,
  onExport,
  editing,
  canSave,
  saveDisabled,
  saving,
  saveTitle,
  onSave,
}: {
  title: string
  // A saved map the opener manages renames in place; the rename persists on the next Save
  // (updateWebMap already carries the title). Unsaved and read-only maps show static text.
  titleEditable: boolean
  onTitle: (title: string) => void
  unsaved: boolean
  readOnly: boolean
  savedFlash: boolean
  // Talus Field "Field-ready" (decision-talus-field-f2.md §4.4): a document property like the title —
  // crews see this map in the field app only when on. Persists on the next Save; shown when the map is
  // saveable (the switch changes the SAVED map, so it sits in this row, not a view rail).
  fieldReady: boolean
  onFieldReady: (on: boolean) => void
  // Talus Field packs (f3g): the '@paid' seam's action beside the switch — inert in a public build.
  packs?: ReactNode
  addOpen: boolean
  onAddLayers: () => void
  sketchOpen: boolean
  onSketch: () => void
  exportOpen: boolean
  onExport: () => void
  editing: boolean
  canSave: boolean
  saveDisabled: boolean
  saving: boolean
  saveTitle: string
  onSave: () => void
}) {
  return (
    <div className="map-header">
      {titleEditable ? (
        <input
          className="map-header__title"
          value={title}
          aria-label="Map title"
          title="Rename this map — the new name is kept on the next Save"
          onChange={(e) => onTitle(e.target.value)}
        />
      ) : (
        <span className="map-header__title map-header__title--static">{title}</span>
      )}
      {unsaved && <span className="map-header__chip">Unsaved</span>}
      {readOnly && <span className="map-header__chip">Read-only</span>}
      {savedFlash && <span className="map__saved-flash">Saved ✓</span>}

      <div className="map-header__divider" aria-hidden="true" />

      <div className="map-header__actions">
        <button
          type="button"
          className={`btn btn--sm btn--secondary${addOpen ? ' is-active' : ''}`}
          aria-pressed={addOpen}
          onClick={onAddLayers}
        >
          Add layers
        </button>
        <button
          type="button"
          className={`btn btn--sm btn--ghost${sketchOpen ? ' is-active' : ''}`}
          aria-pressed={sketchOpen}
          title="Draw ad-hoc notes — points, lines, polygons with labels — that save with this map"
          onClick={onSketch}
        >
          Sketch
        </button>
        <button
          type="button"
          className={`btn btn--sm btn--ghost${exportOpen ? ' is-active' : ''}`}
          aria-pressed={exportOpen}
          title="Export the current view as PNG or PDF"
          onClick={onExport}
        >
          Export
        </button>
      </div>

      <div className="map-header__spacer" />

      {editing && <span className="map-header__editing">Editing on</span>}
      {canSave && (
        <button
          type="button"
          className={`btn btn--sm btn--ghost${fieldReady ? ' is-active' : ''}`}
          aria-pressed={fieldReady}
          title="Field-ready — crews see this map in Talus Field (saved with the map)"
          onClick={() => onFieldReady(!fieldReady)}
        >
          {fieldReady ? 'Field-ready ✓' : 'Field-ready'}
        </button>
      )}
      {canSave && packs}
      {canSave && (
        <button
          type="button"
          className="btn btn--sm"
          disabled={saveDisabled}
          title={saveTitle}
          onClick={onSave}
        >
          {saving ? 'Saving…' : 'Save map'}
        </button>
      )}
    </div>
  )
}
