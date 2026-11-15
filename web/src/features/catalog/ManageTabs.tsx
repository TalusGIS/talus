/**
 * Talus 31 — the tab bar shared by the layer & map Manage dialogs. The Manage pane grew several
 * stacked sections (link/sharing, metadata, delete-protection, transfer ownership); tabs keep it
 * scannable. Presentational: the parent owns the active tab and renders the matching panel.
 *
 * Mirrors the Admin sub-section tabs (`.admin__tab`) visually, scoped to the modal (`.modal__tab`).
 */
export type ManageTab = 'sharing' | 'metadata' | 'fields' | 'relates' | 'views' | 'opendata' | 'usein' | 'settings'

// Open Data sits between Metadata (its gate) and Settings. It is layer-only (maps aren't curated in
// v1, F1), so the map dialog omits it via `showOpenData={false}` (the default). Views (Talus 38) sits
// right after Metadata and is likewise layer-only (and hidden when the layer is itself a view). Fields
// (owner-defined custom fields) is slot-layer-only (gated by `showFields`). Use in… (Interop I-1) is
// layer-only too — the desktop/script connection assistant.
const TABS: { id: ManageTab; label: string }[] = [
  { id: 'sharing', label: 'Sharing' },
  { id: 'metadata', label: 'Metadata' },
  { id: 'fields', label: 'Fields' },
  { id: 'relates', label: 'Relates' },
  { id: 'views', label: 'Views' },
  { id: 'opendata', label: 'Open Data' },
  { id: 'usein', label: 'Use in…' },
  { id: 'settings', label: 'Settings' },
]

export default function ManageTabs({
  active,
  onChange,
  showOpenData = false,
  showViews = false,
  showFields = false,
  showRelates = false,
  showUseIn = false,
}: {
  active: ManageTab
  onChange: (tab: ManageTab) => void
  /** Show the layer-only "Open Data" curation tab (Talus 33). */
  showOpenData?: boolean
  /** Show the layer-only "Views" authoring tab (Talus 38); hidden for maps and for view layers. */
  showViews?: boolean
  /** Show the slot-layer-only "Fields" authoring tab (owner-defined custom fields). */
  showFields?: boolean
  /** Show the spatial-layer-only "Relates" authoring tab (decision-relates-and-join-views.md PR A). */
  showRelates?: boolean
  /** Show the layer-only "Use in…" connection-assistant tab (Interop I-1); hidden for maps. */
  showUseIn?: boolean
}) {
  const tabs = TABS.filter(
    (t) =>
      (t.id !== 'opendata' || showOpenData) &&
      (t.id !== 'views' || showViews) &&
      (t.id !== 'fields' || showFields) &&
      (t.id !== 'relates' || showRelates) &&
      (t.id !== 'usein' || showUseIn),
  )
  return (
    <div className="modal__tabs" role="tablist">
      {tabs.map((t) => (
        <button
          key={t.id}
          type="button"
          role="tab"
          aria-selected={active === t.id}
          className={`modal__tab${active === t.id ? ' modal__tab--active' : ''}`}
          onClick={() => onChange(t.id)}
        >
          {t.label}
        </button>
      ))}
    </div>
  )
}
