import type { MouseEvent, ReactNode } from 'react'
import { ChevronDown, ChevronUp, Crosshair, Download, Ellipsis, Funnel, X } from 'lucide-react'

import { GeometryIcon, LinkIcon } from '@/features/catalog/ItemTypeIcons'
import type { LayerConfig } from '@/features/map/types'
import { symbolByName } from '@/features/map/pointIcons'

/**
 * The unified layer list + read-only legend, rendered INSIDE the dockable LayerPane (the AGOL
 * "Contents" TOC). ONE ordered list interleaving vector layers and raster operational layers (V045+)
 * in draw order (top of the list = top of the draw stack) — so a user can reorder a raster above or
 * below a vector. A uniqueValue vector nests its legend; a raster row carries an opacity slider.
 * Filtered by GET /me/layers so a user never sees a layer they can't read.
 *
 * Click-to-edit (Talus 14): for a vector layer the caller can MANAGE, the symbol swatches are buttons
 * that open the Styles panel (ArcGIS Pro convention). For everyone else the swatches are static legend.
 */

/** One TOC entry: a vector layer (with its config), a raster operational layer, or the map's ONE
 *  sketch-notes row (decision-sketch-layer.md — features are managed by clicking them on-map). */
export type TocItem =
  | { kind: 'layer'; config: LayerConfig<unknown> }
  | {
      kind: 'raster'
      id: string
      title: string
      opacity: number
      bandCount?: number
      colormap?: string
      /** V050: false = view-only (no export button shown). Defaults to downloadable when absent. */
      downloadable?: boolean
    }
  | { kind: 'sketch'; count: number }

/** Named rio-tiler colormaps offered for a single-band raster (a curated subset). '' = default/grayscale. */
// eslint-disable-next-line react-refresh/only-export-components -- deliberate co-location; HMR full-reload accepted
export const RASTER_COLORMAPS: { value: string; label: string }[] = [
  { value: '', label: 'Grayscale (default)' },
  { value: 'viridis', label: 'Viridis' },
  { value: 'magma', label: 'Magma' },
  { value: 'plasma', label: 'Plasma' },
  { value: 'inferno', label: 'Inferno' },
  { value: 'cividis', label: 'Cividis' },
  { value: 'turbo', label: 'Turbo' },
  { value: 'ylorrd', label: 'Yellow-Orange-Red' },
  { value: 'rdylgn', label: 'Red-Yellow-Green' },
  { value: 'spectral', label: 'Spectral' },
  { value: 'terrain', label: 'Terrain' },
]

export interface LayerToggleProps {
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
  /** Right-click layer options (decision-map-layer-table.md): a row's onContextMenu and its ⋯ kebab
   *  both ask the caller to open the menu at viewport (x, y). Raster rows wire it too since the
   *  visible zoom range (decision-layer-zoom-range.md): Zoom to and Visible range; the caller leaves
   *  out what a raster has no use for (no attribute table, no selection). */
  onOpenMenu?: (layerId: string, x: number, y: number) => void
  /** Ids of ops seeded from a shared catalog reference (decision-external-catalog-items.md) — their
   *  rows wear the small link glyph so "referenced" reads at a glance, matching Catalog + picker. */
  referencedIds?: Set<string>
  /** Layer-first selection (handoff-map-2a §1): clicking a vector row's NAME selects the layer —
   *  the selection drives the docked inspector and the rail's Select tool. The checkbox keeps
   *  visibility; the swatch keeps its style route. Amber marks the selected row (§3.2). */
  selectedId?: string | null
  onSelect?: (layerId: string) => void
  /** Layers with an active attribute filter (handoff-map-2a §4) — their rows wear a small amber
   *  funnel, the signal the deleted rail FilterTool's badge used to carry. */
  filteredIds?: Set<string>
  /** Rows the map is outside the visible zoom range of (decision-layer-zoom-range.md §5), keyed by
   *  id: the line shown under the greyed name ("Shown from zoom 14"), and whether clicking it can
   *  zoom the map into range (not for a range that draws nowhere). Absent = drawn at this zoom. */
  rangeNotes?: Record<string, { text: string; canZoom: boolean }>
  onZoomIntoRange?: (id: string) => void
}

export default function LayerToggle({
  items,
  visible,
  onToggle,
  onStyle,
  onRemove,
  onReorder,
  onZoom,
  onOpacity,
  onColormap,
  onExport,
  onOpenMenu,
  referencedIds,
  selectedId,
  onSelect,
  filteredIds,
  rangeNotes,
  onZoomIntoRange,
}: LayerToggleProps) {
  if (items.length === 0) return null
  // The sketch row (always items[0] when present) is not reorderable and not in the draw-order
  // array, so the first/last arrow-disable indices count REAL rows only.
  const sketchOffset = items[0]?.kind === 'sketch' ? 1 : 0
  const last = items.length - 1 - sketchOffset
  const showActions = !!onZoom || !!onReorder || !!onRemove
  // The referenced-item tell (decision-external-catalog-items.md) — decorative glyph, meaning on title.
  const refMark = (id: string) =>
    referencedIds?.has(id) ? (
      <span
        className="layer-toggle__refmark"
        title="Referenced from the catalog"
        aria-label="Referenced from the catalog"
        role="img"
      >
        <LinkIcon />
      </span>
    ) : null

  // Out of range (§5): the name greys and a line says when it is drawn. The tick box is untouched —
  // being out of range is not being switched off. The line is a button that zooms into range.
  const rangeLine = (id: string, label: string) => {
    const note = rangeNotes?.[id]
    if (!note) return null
    return note.canZoom && onZoomIntoRange ? (
      <button
        type="button"
        className="layer-toggle__range"
        onClick={() => onZoomIntoRange(id)}
        title={`Zoom the map to where ${label} is drawn`}
      >
        {note.text}
      </button>
    ) : (
      <span className="layer-toggle__range">{note.text}</span>
    )
  }
  const outClass = (id: string): string => (rangeNotes?.[id] ? ' is-out-of-range' : '')
  // Right-click on the ROW opens the layer-options menu (rows only — the browser's own context menu
  // stays available elsewhere in the pane); the ⋯ kebab is the visible/touch twin of the same gesture.
  const menuHandler = (id: string) =>
    onOpenMenu
      ? (e: MouseEvent) => {
          e.preventDefault()
          onOpenMenu(id, e.clientX, e.clientY)
        }
      : undefined
  const kebabFor = (id: string, label: string) =>
    onOpenMenu ? (
      <button
        type="button"
        className="layer-toggle__act"
        onClick={(e) => {
          const r = e.currentTarget.getBoundingClientRect()
          onOpenMenu(id, r.left, r.bottom + 2)
        }}
        title="Layer options"
        aria-label={`Layer options for ${label}`}
        aria-haspopup="menu"
      >
        <Ellipsis size={14} aria-hidden="true" />
      </button>
    ) : undefined

  // withZoom: a row with the layer-options menu has "Zoom to" IN it (decision-map-layer-table.md),
  // so its cluster drops the ⌖; without a menu (the embed's legend list) the ⌖ stays.
  const actions = (id: string, label: string, i: number, extra?: ReactNode, withZoom = true) =>
    showActions || extra ? (
      <div className="layer-toggle__actions">
        {extra}
        {onZoom && withZoom && (
          <button
            type="button"
            className="layer-toggle__act"
            onClick={() => onZoom(id)}
            title="Zoom to"
            aria-label={`Zoom to ${label}`}
          >
            <Crosshair size={13} aria-hidden="true" />
          </button>
        )}
        {onReorder && (
          <>
            <button
              type="button"
              className="layer-toggle__act"
              onClick={() => onReorder(id, -1)}
              disabled={i === 0}
              title="Move up"
              aria-label={`Move ${label} up`}
            >
              <ChevronUp size={13} aria-hidden="true" />
            </button>
            <button
              type="button"
              className="layer-toggle__act"
              onClick={() => onReorder(id, 1)}
              disabled={i === last}
              title="Move down"
              aria-label={`Move ${label} down`}
            >
              <ChevronDown size={13} aria-hidden="true" />
            </button>
          </>
        )}
        {onRemove && (
          <button
            type="button"
            className="layer-toggle__act layer-toggle__act--remove"
            onClick={() => onRemove(id)}
            title="Remove from map"
            aria-label={`Remove ${label} from map`}
          >
            <X size={13} aria-hidden="true" />
          </button>
        )}
      </div>
    ) : null

  return (
    <div className="layer-toggle__list" role="group" aria-label="Layers">
      {items.map((it, i) => {
        if (it.kind === 'sketch') {
          // The single sketch-notes row: visibility + zoom-to + remove-ALL (no reorder — notes
          // always draw on top; no style/menu — per-note edits live on the on-map card).
          return (
            <div className="layer-toggle__layer" key="__sketch">
              <div className="layer-toggle__row">
                <label className="layer-toggle__item">
                  <input
                    type="checkbox"
                    checked={visible.__sketch ?? true}
                    onChange={(e) => onToggle('__sketch', e.target.checked)}
                  />
                  <span className="layer-toggle__swatch layer-toggle__swatch--geom">
                    <GeometryIcon geomType="point" className="layer-toggle__geom" />
                  </span>
                  <span>
                    Sketch <span className="layer-toggle__count">({it.count})</span>
                  </span>
                </label>
                <div className="layer-toggle__actions">
                  {onZoom && (
                    <button
                      type="button"
                      className="layer-toggle__act"
                      onClick={() => onZoom('__sketch')}
                      title="Zoom to notes"
                      aria-label="Zoom to sketch notes"
                    >
                      <Crosshair size={13} aria-hidden="true" />
                    </button>
                  )}
                  {onRemove && (
                    <button
                      type="button"
                      className="layer-toggle__act layer-toggle__act--remove"
                      onClick={() => onRemove('__sketch')}
                      title="Remove all notes"
                      aria-label="Remove all sketch notes"
                    >
                      <X size={13} aria-hidden="true" />
                    </button>
                  )}
                </div>
              </div>
            </div>
          )
        }
        if (it.kind === 'raster') {
          return (
            <div
              className={`layer-toggle__layer${selectedId === it.id ? ' is-selected' : ''}${outClass(it.id)}`}
              key={it.id}
            >
              <div className="layer-toggle__row" onContextMenu={menuHandler(it.id)}>
                <label className="layer-toggle__item">
                  <input
                    type="checkbox"
                    checked={visible[it.id] ?? true}
                    onChange={(e) => onToggle(it.id, e.target.checked)}
                  />
                  <span className="layer-toggle__swatch layer-toggle__swatch--raster" aria-hidden="true" />
                  <span>{it.title}</span>
                  {refMark(it.id)}
                </label>
                {actions(
                  it.id,
                  it.title,
                  i - sketchOffset,
                  (onExport && it.downloadable !== false) || onOpenMenu ? (
                    <>
                      {onExport && it.downloadable !== false && (
                        <button
                          type="button"
                          className="layer-toggle__act"
                          onClick={() => onExport(it.id)}
                          title="Export / download"
                          aria-label={`Export ${it.title}`}
                        >
                          <Download size={13} aria-hidden="true" />
                        </button>
                      )}
                      {kebabFor(it.id, it.title)}
                    </>
                  ) : undefined,
                  !onOpenMenu,
                )}
              </div>
              {rangeLine(it.id, it.title)}
              {onOpacity && (
                <input
                  className="layer-toggle__opacity"
                  type="range"
                  min={0}
                  max={1}
                  step={0.05}
                  value={it.opacity}
                  onChange={(e) => onOpacity(it.id, Number(e.target.value))}
                  aria-label={`${it.title} opacity`}
                  title={`Opacity ${Math.round(it.opacity * 100)}%`}
                />
              )}
              {/* Colormap is meaningful only for a single-band raster (e.g. a hotspot/KDE surface). */}
              {onColormap && it.bandCount === 1 && (
                <select
                  className="layer-toggle__colormap"
                  value={it.colormap ?? ''}
                  onChange={(e) => onColormap(it.id, e.target.value)}
                  aria-label={`${it.title} colormap`}
                  title="Colormap"
                >
                  {RASTER_COLORMAPS.map((c) => (
                    <option key={c.value} value={c.value}>
                      {c.label}
                    </option>
                  ))}
                </select>
              )}
            </div>
          )
        }

        const c = it.config
        const r = c.renderer
        const stops = r?.type === 'uniqueValue' ? (r.stops ?? []) : []
        const isUnique = stops.length > 0
        const rowColor = r?.type === 'simple' ? (r.symbol?.color ?? c.color) : c.color
        const canStyle = c.canManage && !!onStyle
        // The legend mark is the layer's by-geometry glyph tinted to the symbology color (point dots /
        // line / filled polygon ring), so the TOC shows BOTH shape and colour — the AGOL/Pro legend
        // convention. The glyph inherits `color`, so each uniqueValue category renders in its own hue.
        // (Merge note: kept the icons session's geometry-glyph swatch over c7b's literal stroke/fill
        // sample — both are valid per-geometry legends; the c7 style controls + map paint are unaffected.)
        // Fork D: when a point layer carries an icon, the legend shows THAT icon tinted — a dot
        // beside a hydrant layer defeats the feature. Falls back to the geometry glyph whenever the
        // symbol set has not loaded or the name is unknown.
        const iconSym = c.geometryType === 'point' ? symbolByName(r?.pointIcon) : undefined
        const mark = iconSym ? (
          <svg className="layer-toggle__geom" viewBox={iconSym.viewBox} aria-hidden="true" fill="currentColor">
            {iconSym.paths.map((d, i) => (
              <path key={i} d={d} />
            ))}
          </svg>
        ) : (
          <GeometryIcon geomType={c.geometryType} className="layer-toggle__geom" />
        )
        const swatch = (color: string) =>
          canStyle ? (
            <button
              type="button"
              className="layer-toggle__swatch layer-toggle__swatch--geom layer-toggle__swatch--btn"
              style={{ color }}
              title={`Edit symbology for ${c.label}`}
              aria-label={`Edit symbology for ${c.label}`}
              onClick={() => onStyle!(c.id)}
            >
              {mark}
            </button>
          ) : (
            <span className="layer-toggle__swatch layer-toggle__swatch--geom" style={{ color }}>
              {mark}
            </span>
          )

        const openMenu = menuHandler(c.id)
        const kebab = kebabFor(c.id, c.label)

        return (
          <div
            className={`layer-toggle__layer${selectedId === c.id ? ' is-selected' : ''}${outClass(c.id)}`}
            key={c.id}
          >
            <div className="layer-toggle__row" onContextMenu={openMenu}>
              <label className="layer-toggle__item">
                <input
                  type="checkbox"
                  checked={visible[c.id] ?? true}
                  onChange={(e) => onToggle(c.id, e.target.checked)}
                />
                {!isUnique && swatch(rowColor)}
                {onSelect ? (
                  <button
                    type="button"
                    className="layer-toggle__name"
                    title={`Select ${c.label}`}
                    onClick={(e) => {
                      e.preventDefault() // a selection gesture — never the label's checkbox toggle
                      onSelect(c.id)
                    }}
                  >
                    {c.label}
                  </button>
                ) : (
                  <span>{c.label}</span>
                )}
                {refMark(c.id)}
                {filteredIds?.has(c.id) && (
                  <span
                    className="layer-toggle__funnel"
                    title="Filtered"
                    aria-label="Filtered"
                    role="img"
                  >
                    <Funnel size={12} />
                  </span>
                )}
              </label>
              {actions(c.id, c.label, i - sketchOffset, kebab, !onOpenMenu)}
            </div>
            {rangeLine(c.id, c.label)}
            {isUnique && (
              <ul className="layer-toggle__legend">
                {stops.map((s) => (
                  <li className="layer-toggle__legend-item" key={s.value}>
                    {swatch(s.color)}
                    <span>{s.label ?? s.value}</span>
                  </li>
                ))}
                {r?.default && (
                  <li className="layer-toggle__legend-item layer-toggle__legend-item--muted" key="__default">
                    {swatch(r.default.color)}
                    <span>{r.default.label ?? 'Other'}</span>
                  </li>
                )}
              </ul>
            )}
          </div>
        )
      })}
    </div>
  )
}
