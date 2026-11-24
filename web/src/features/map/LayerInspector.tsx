import { useState, type ReactNode } from 'react'
import { PanelRightClose, Pin } from 'lucide-react'

import { GeometryIcon, LinkIcon } from '@/features/catalog/ItemTypeIcons'
import type { GeometryType } from '@/lib/api'
import CloseButton from '@/components/CloseButton'

/**
 * The docked layer inspector (docs/handoff-map-2a.md §1, commit 2/4): ONE right-docked surface
 * for per-layer configuration — Style · Labels · Filter · Popup · Visibility — driven by the TOC selection,
 * replacing the four rail flyout tools (each of which re-asked "which layer?", a question the
 * TOC had already answered) and their four floating panels. Dumb control: AssetMap owns the
 * selection, the tab, and every panel body; this renders the frame.
 *
 * Only rendered on editable maps (configuring is authoring — viewers still SEE filters, labels
 * and pop-ups applied). The Style tab additionally needs manage rights on the LAYER itself
 * (styling saves the layer's renderer, not map state) — the same fence the TOC swatch had.
 * A Fields tab is deliberately absent for now: catalog FieldsView is item-scoped (owner's
 * MyLayer), not map-scoped — wiring it here is its own slice.
 *
 * No key handlers: Esc stays AssetMap's (draw sessions, menus — handoff §5).
 */

export type InspectorTab = 'style' | 'labels' | 'filter' | 'popup' | 'visibility' | 'form'

/**
 * The right dock's collapse frame — the layers TOC's pin
 * treatment (LayerPane), mirrored to the right edge, because an idle inspector burns
 * ~15-20% of the screen. PINNED keeps the column open; UNPINNED (the default) collapses
 * it to an edge tab whenever nothing is selected. A selection — layer or sketch note — always
 * opens it, so the TOC click still lands somewhere visible; closing that selection collapses it
 * again. Unlike the TOC's pin, this one is deliberately NOT persisted: every map
 * open starts collapsed, at full map width — the pin holds the dock open for the session only.
 *
 * OPENING IS DELIBERATE, NEVER INCIDENTAL. It used to peek open on
 * hover, and because the collapsed dock IS the edge tab strip, a cursor merely crossing the right
 * edge on its way somewhere else threw the panel to full width. The only three things that open
 * it now are a selection, a click on the "Inspector" tab, and the pin. That matters most on the
 * map — the one surface where the pointer is constantly in motion for reasons that have nothing
 * to do with the chrome.
 *
 * Opened by the tab it STAYS open until explicitly collapsed (the dockbar control) — an explicit
 * open undone by the pointer wandering off is the same mistake in the other direction.
 */
export function InspectorDock({
  hasContent,
  ariaLabel,
  children,
}: {
  /** Something is selected for the dock to show — forces it open while true. */
  hasContent: boolean
  ariaLabel: string
  children: ReactNode
}) {
  const [pinned, setPinned] = useState(false)
  // Opened by clicking the edge tab. Sticky — only the collapse control clears it.
  const [clickOpen, setClickOpen] = useState(false)

  const open = pinned || clickOpen || hasContent
  const togglePin = (): void => {
    // Either direction clears the click-open: pinning supersedes it, and unpinning should return
    // the dock to its resting state rather than leave it held open by a click made long ago.
    setClickOpen(false)
    setPinned((p) => !p)
  }
  // Offered only when the click is the ONLY thing holding it open — pinned has the pin, and a
  // selection collapses by being closed. A control that silently did nothing would be worse.
  const showCollapse = clickOpen && !pinned && !hasContent

  return (
    <aside className={`layer-inspector${open ? '' : ' is-collapsed'}`} aria-label={ariaLabel}>
      {open ? (
        <>
          <div className="layer-inspector__dockbar">
            {showCollapse && (
              <button
                type="button"
                className="layer-inspector__collapse"
                onClick={() => setClickOpen(false)}
                title="Collapse the inspector"
                aria-label="Collapse the inspector"
              >
                <PanelRightClose size={15} aria-hidden="true" />
              </button>
            )}
            <button
              type="button"
              className={`layer-pane__pin${pinned ? ' is-on' : ''}`}
              onClick={togglePin}
              title={pinned ? 'Unpin (auto-hide when nothing is selected)' : 'Pin open'}
              aria-pressed={pinned}
              aria-label={pinned ? 'Unpin inspector' : 'Pin inspector open'}
            >
              <Pin size={15} aria-hidden="true" />
            </button>
          </div>
          {children}
        </>
      ) : (
        <button
          type="button"
          className="layer-inspector__edgetab"
          onClick={() => setClickOpen(true)}
          title="Show the layer inspector"
        >
          Inspector
        </button>
      )}
    </aside>
  )
}

export interface InspectorLayer {
  id: string
  label: string
  geometryType: GeometryType
  color: string
  /** Manage rights on the layer item — gates the Style tab (renderer saves to the layer). */
  canStyle: boolean
  /** Manage rights AND a hosted slot layer — gates the Form tab (views/externals have no form). */
  canForm: boolean
  /** Referenced external service — wears the link glyph, like the TOC row and picker. */
  external: boolean
}

const TABS: { key: InspectorTab; label: string }[] = [
  { key: 'style', label: 'Style' },
  { key: 'labels', label: 'Labels' },
  { key: 'filter', label: 'Filter' },
  { key: 'popup', label: 'Popup' },
  // The zooms the layer is drawn at (decision-layer-zoom-range.md §4.3): this map's own range for anyone
  // who can edit the map, and the layer's default for its owner or an admin.
  { key: 'visibility', label: 'Visibility' },
  // Talus Field f2 (decision-talus-field-f2.md §4.6): the layer's form document — sections, rules,
  // templates. Manage-gated like Style (it saves to the layer); hosted slot layers only.
  { key: 'form', label: 'Form' },
]

export default function LayerInspector({
  layer,
  tab,
  onTab,
  configured,
  onClose,
  children,
}: {
  /** The selected TOC layer, or null → the empty state. */
  layer: InspectorLayer | null
  tab: InspectorTab
  onTab: (tab: InspectorTab) => void
  /** Which tabs carry a live config — each shows the small amber dot the deleted rail tools
   *  used to carry as their badge, now on the layer where it belongs. */
  configured: Record<InspectorTab, boolean>
  /** Clear the selection (the × — and the panels' own Cancel/close routes land here too). */
  onClose: () => void
  children: ReactNode
}) {
  // No aside of its own — InspectorDock owns the column (and its pin/collapse state).
  return (
    <>
      {layer === null ? (
        <p className="layer-inspector__empty">
          Select a layer in the Layers panel to style, label, filter, configure its pop-up, its visible zooms or its form.
        </p>
      ) : (
        <>
          <div className="layer-inspector__header">
            <span className="layer-toggle__swatch layer-toggle__swatch--geom" style={{ color: layer.color }}>
              <GeometryIcon geomType={layer.geometryType} className="layer-toggle__geom" />
            </span>
            <span className="layer-inspector__name" title={layer.label}>
              {layer.label}
            </span>
            {layer.external && (
              <span className="layer-toggle__refmark" title="Referenced external service" role="img">
                <LinkIcon />
              </span>
            )}
            <span className="layer-inspector__geomchip">{layer.geometryType}</span>
            <CloseButton
              onClick={onClose}
              label="Close layer configuration"
              className="layer-inspector__close"
              title="Close (clears the selection)"
            />
          </div>
          <div className="layer-inspector__tabs" role="tablist" aria-label="Layer configuration tabs">
            {TABS.map((t) => (
              <button
                key={t.key}
                type="button"
                role="tab"
                aria-selected={tab === t.key}
                className={`layer-inspector__tab${tab === t.key ? ' is-active' : ''}`}
                disabled={(t.key === 'style' && !layer.canStyle) || (t.key === 'form' && !layer.canForm)}
                title={
                  t.key === 'style' && !layer.canStyle
                    ? 'Styling needs manage rights on this layer'
                    : t.key === 'form' && !layer.canForm
                      ? 'The form is configured on hosted layers you manage'
                      : undefined
                }
                onClick={() => onTab(t.key)}
              >
                {t.label}
                {configured[t.key] && <span className="layer-inspector__dot" aria-hidden="true" />}
              </button>
            ))}
          </div>
          <div className="layer-inspector__body">{children}</div>
        </>
      )}
    </>
  )
}
