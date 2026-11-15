import { useEffect, useRef, useState, type ReactNode } from 'react'

import Caret from './Caret'

/**
 * One collapsible section (handoff-record-table-3 §2.2). Before this, only History collapsed —
 * Attachments and each relate block were fixed-open sections that merely *looked* collapsible
 * (RelatedRecords' own comment said "collapsible-feeling"), so a record with attachments, three
 * relates and a history was a scroll with no way to fold anything away.
 *
 * `lazy` + `onFirstOpen` reproduces History's load-on-first-expand: the callback fires once, the
 * first time the section opens, and never again. A parent that reuses the component across records
 * (the drawer does) must give it a `key` so the open state and the first-open latch reset with the
 * record.
 *
 * Deliberately has NO key handler: the drawer's Esc closes the lightbox before the drawer, and a
 * section that swallowed Esc would break that (§3.6).
 */
export default function PanelSection({
  title,
  count,
  hint,
  defaultOpen = false,
  lazy = false,
  onFirstOpen,
  children,
}: {
  title: string
  /** Shown after the title in the mono face — how many rows are in here. */
  count?: number
  /** Right-aligned aside: the answer someone would otherwise expand to find. */
  hint?: ReactNode
  defaultOpen?: boolean
  /** Defer the section's fetch until it is first opened. */
  lazy?: boolean
  onFirstOpen?: () => void
  children: ReactNode
}) {
  const [open, setOpen] = useState(defaultOpen)
  const firstOpened = useRef(false)

  useEffect(() => {
    if (!open || firstOpened.current) return
    firstOpened.current = true
    if (lazy) onFirstOpen?.()
  }, [open, lazy, onFirstOpen])

  return (
    <section className={`panel-section${open ? ' is-open' : ''}`}>
      <button
        type="button"
        className="panel-section__head"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <Caret open={open} className="panel-section__caret" />
        <span className="panel-section__title">{title}</span>
        {count != null && <span className="panel-section__count">{count}</span>}
        {hint != null && <span className="panel-section__hint">{hint}</span>}
      </button>
      {open && <div className="panel-section__body">{children}</div>}
    </section>
  )
}
