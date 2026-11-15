import AttachmentStrip, { type AttachmentStripProps } from '@/features/map/AttachmentStrip'
import { InfoIcon } from '@/features/catalog/ItemTypeIcons'
import type { ReactNode } from 'react'

import type { LayerConfig } from '@/features/map/types'
import { formatTemporal } from '@/lib/formatTemporal'

/**
 * Generic map popup. Renders the label/value rows the layer's config produces
 * plus the editor action buttons. Replaces the signs-specific SignPopup — the
 * rows are data (config.popupRows), so a new layer needs no new popup.
 */
export interface AssetPopupProps {
  config: LayerConfig<unknown>
  props: unknown
  /** Resolved by the caller (decision-map-popups.md): the page title (a configured titleField's value,
   * or the layer label), the optional eyebrow (the layer label above a feature-value title), and the
   * ordered [label, value] rows. Fallbacks keep the component safe standalone. */
  title?: string
  eyebrow?: string
  rows?: Array<[string, string]>
  /** The Attributes affordance (V109: canEdit AND the layer's Edit-attributes flag). */
  canEdit: boolean
  // Whether the MOVE affordance applies. Point features drag-to-move; line/polygon geometry editing is a
  // separate tool (terra-draw select, c6b), so Move is hidden for them while Edit/Delete still apply.
  canMove: boolean
  /** The Delete affordance (V109: canEdit AND the layer's Delete-features flag). */
  canDelete: boolean
  onEdit: () => void
  onMove: () => void
  onDelete: () => void
  /** Enter line/polygon vertex/ring editing (terra-draw select, c6b). Provided only for editable
   * line/polygon features (the geometry sibling of Move); the button renders only when provided. */
  onEditShape?: () => void
  /** Open the full record-detail drawer. Omitted for layers without a per-feature record
   * (polygon reference layers) — the button only renders when provided. View-independent. */
  onDetails?: () => void
  /** A-4: the lazy attachments strip for this feature. Omitted for pages that can't carry attachments
   * (e.g. views). The strip renders nothing until its per-layer probe + list say there's something. */
  attachments?: AttachmentStripProps
  /** Extra title-row actions beside the Details icon (decision-scheduled-reports.md F11). A slot, not a
   * feature: core renders whatever the caller hands it and knows nothing about what it is — the '@paid'
   * seam fills it (the Generate-sheet icon), the public build's stub renders nothing. */
  titleActions?: ReactNode
}

export default function AssetPopup({
  config,
  props,
  title,
  eyebrow,
  rows: rowsProp,
  canEdit,
  canMove,
  canDelete,
  onEdit,
  onMove,
  onDelete,
  onDetails,
  onEditShape,
  attachments,
  titleActions,
}: AssetPopupProps) {
  const rows = rowsProp ?? config.popupRows(props)
  return (
    <div className="sign-popup">
      {eyebrow && <div className="sign-popup__eyebrow">{eyebrow}</div>}
      <div className="sign-popup__title">
        {title ?? config.label}
        {/* Details moved to an info icon by the name (replaces the old "Details" button). View-independent —
            only rendered when a per-feature record exists (openPopup omits onDetails for view/reference layers). */}
        {onDetails && (
          <button className="sign-popup__info" onClick={onDetails} aria-label="Details" title="Details">
            <InfoIcon />
          </button>
        )}
        {titleActions}
      </div>
      {rows.map(([k, val]) => {
        // Dates and times read as dates and times; the stored value is the tooltip. Done here, at
        // render, so every row source is covered: a map's configured fields, the slot defaults,
        // a view's own columns, a reference layer's properties.
        const when = formatTemporal(val)
        return (
          <div className="sign-popup__row" key={k}>
            <span className="sign-popup__k">{k}</span>
            <span className="sign-popup__v" title={when?.raw}>{when?.text ?? val}</span>
          </div>
        )
      })}
      {attachments && <AttachmentStrip {...attachments} />}
      {(canEdit || canMove || onEditShape || canDelete) && (
        <div className="sign-popup__actions">
          {canEdit && (
            <button className="btn btn--sm" onClick={onEdit}>
              Attributes
            </button>
          )}
          {canMove && (
            <button className="btn btn--sm" onClick={onMove}>
              Move
            </button>
          )}
          {onEditShape && (
            <button className="btn btn--sm" onClick={onEditShape}>
              Edit shape
            </button>
          )}
          {canDelete && (
            <button className="btn btn--sm btn--danger" onClick={onDelete}>
              Delete
            </button>
          )}
        </div>
      )}
    </div>
  )
}
