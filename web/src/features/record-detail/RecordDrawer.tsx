import type { ReactNode } from 'react'

import CloseButton from '@/components/CloseButton'

/**
 * The record drawer's shell (handoff-record-table-3 §2.1). `RecordDetailLoader` used to hand-copy
 * `.record-detail` + `__header` + `__title` + `__close` + `__body` for its loading/error state, so
 * the loading header and the loaded header could drift apart. Both render through this now, which
 * is also what makes the loading state show the record's own identity instead of the layer's.
 *
 * The doc's `badge`/`meta` are named `swatch`/`hint` here for what they actually hold: the
 * geometry mark that matches the TOC, and the one-line answer (last edited …) that saves a click.
 */
export default function RecordDrawer({
  ariaLabel,
  swatch,
  title,
  subtitle,
  hint,
  actions,
  onClose,
  children,
}: {
  ariaLabel: string
  /** Geometry mark, tinted to the layer's symbology — the same glyph the TOC and inspector show. */
  swatch?: ReactNode
  title: ReactNode
  subtitle?: ReactNode
  hint?: ReactNode
  /** Header actions placed before the close button (Edit). */
  actions?: ReactNode
  onClose: () => void
  children: ReactNode
}) {
  return (
    <aside className="record-detail" role="dialog" aria-label={ariaLabel}>
      <div className="record-detail__header">
        {swatch}
        <div className="record-detail__ident">
          <h3 className="record-detail__title">{title}</h3>
          {subtitle != null && <p className="record-detail__subtitle">{subtitle}</p>}
        </div>
        {hint != null && <span className="record-detail__hint">{hint}</span>}
        {actions}
        <CloseButton onClick={onClose} label="Close detail" className="record-detail__close" />
      </div>
      <div className="record-detail__body">{children}</div>
    </aside>
  )
}
