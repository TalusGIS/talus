import { useState } from 'react'
import { Ellipsis, Pin, X } from 'lucide-react'

import LayerToggle, { type TocItem } from '@/features/map/LayerToggle'
import { TableIcon } from '@/features/catalog/ItemTypeIcons'

// Dockable Table-of-Contents pane (Talus 14). Left-docked. PINNED (default) keeps it open;
// UNPINNED auto-hides to a tab on the left edge that slides open on hover. Pin state
// persists across reloads. The current light/floating visual treatment is retained.
const PIN_KEY = 'talus.toc.pinned'

export interface LayerPaneProps {
  items: TocItem[]
  visible: Record<string, boolean>
  onToggle: (id: string, visible: boolean) => void
  onStyle?: (layerId: string) => void
  onRemove?: (id: string) => void
  onReorder?: (id: string, dir: -1 | 1) => void
  onZoom?: (id: string) => void
  onOpacity?: (rasterId: string, opacity: number) => void
  onColormap?: (rasterId: string, colormap: string) => void
  onExport?: (rasterId: string) => void
  /** Tables on this map (decision-map-tables.md): the AGO-style Tables section below the layer list.
   *  Nothing draws — clicking a table opens it in the docked grid panel. */
  tables?: { id: string; label: string }[]
  openTableId?: string | null
  onOpenTable?: (id: string) => void
  onRemoveTable?: (id: string) => void
  /** Right-click layer options (decision-map-layer-table.md) — passed through to LayerToggle. */
  onOpenMenu?: (layerId: string, x: number, y: number) => void
  /** Right-click/⋯ options on a TABLES-section row (the part-(c) fast-follow, arc close-out
   *  2026-07-03) — same gesture pair as layer rows; the caller shows the table-flavored menu. */
  onOpenTableMenu?: (tableId: string, x: number, y: number) => void
  /** Ops seeded from a catalog reference (decision-external-catalog-items.md) — passed to LayerToggle. */
  referencedIds?: Set<string>
  /** Layer-first selection (handoff-map-2a §1) — passed to LayerToggle. */
  selectedId?: string | null
  onSelect?: (layerId: string) => void
  /** Layers with an active filter (handoff-map-2a §4) — passed to LayerToggle. */
  filteredIds?: Set<string>
  /** Rows outside their visible zoom range (decision-layer-zoom-range.md §5) — passed to LayerToggle. */
  rangeNotes?: Record<string, { text: string; canZoom: boolean }>
  onZoomIntoRange?: (id: string) => void
}

export default function LayerPane(props: LayerPaneProps) {
  const [pinned, setPinned] = useState<boolean>(() => {
    try {
      return localStorage.getItem(PIN_KEY) !== 'false'
    } catch {
      return true
    }
  })
  const [hoverOpen, setHoverOpen] = useState(false)

  const tables = props.tables ?? []
  if (props.items.length === 0 && tables.length === 0) return null

  const open = pinned || hoverOpen
  const togglePin = (): void => {
    setPinned((p) => {
      const next = !p
      try {
        localStorage.setItem(PIN_KEY, String(next))
      } catch {
        /* ignore */
      }
      if (next) setHoverOpen(false)
      return next
    })
  }

  return (
    <div
      className={`layer-pane${open ? ' is-open' : ' is-collapsed'}`}
      onMouseEnter={() => {
        if (!pinned) setHoverOpen(true)
      }}
      onMouseLeave={() => {
        if (!pinned) setHoverOpen(false)
      }}
    >
      {open ? (
        <div className="layer-pane__panel">
          <div className="layer-pane__header">
            <span className="layer-pane__title">Layers</span>
            <button
              type="button"
              className={`layer-pane__pin${pinned ? ' is-on' : ''}`}
              onClick={togglePin}
              title={pinned ? 'Unpin (auto-hide)' : 'Pin open'}
              aria-pressed={pinned}
              aria-label={pinned ? 'Unpin layers panel' : 'Pin layers panel open'}
            >
              <Pin size={15} aria-hidden="true" />
            </button>
          </div>
          <LayerToggle {...props} />
          {tables.length > 0 && (
            <div className="layer-pane__tables">
              <div className="layer-pane__tables-hdr">Tables</div>
              {tables.map((t) => (
                <div
                  key={t.id}
                  className="layer-pane__table-row"
                  onContextMenu={
                    props.onOpenTableMenu
                      ? (e) => {
                          e.preventDefault()
                          props.onOpenTableMenu?.(t.id, e.clientX, e.clientY)
                        }
                      : undefined
                  }
                >
                  <button
                    type="button"
                    className={`layer-pane__table-open${props.openTableId === t.id ? ' is-open' : ''}`}
                    onClick={() => props.onOpenTable?.(t.id)}
                    title={`Open ${t.label}`}
                  >
                    <TableIcon className="layer-pane__table-icon" />
                    <span className="layer-pane__table-name">{t.label}</span>
                  </button>
                  {props.onOpenTableMenu && (
                    <button
                      type="button"
                      className="layer-toggle__act"
                      onClick={(e) => {
                        const r = e.currentTarget.getBoundingClientRect()
                        props.onOpenTableMenu?.(t.id, r.left, r.bottom + 2)
                      }}
                      title="Table options"
                      aria-label={`Table options for ${t.label}`}
                      aria-haspopup="menu"
                    >
                      <Ellipsis size={14} aria-hidden="true" />
                    </button>
                  )}
                  {props.onRemoveTable && (
                    <button
                      type="button"
                      className="layer-toggle__act layer-toggle__act--remove"
                      onClick={() => props.onRemoveTable?.(t.id)}
                      title="Remove from map"
                      aria-label={`Remove ${t.label} from map`}
                    >
                      <X size={13} aria-hidden="true" />
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      ) : (
        <button
          type="button"
          className="layer-pane__tab"
          onClick={() => setHoverOpen(true)}
          title="Show layers"
        >
          Layers
        </button>
      )}
    </div>
  )
}
