import type { ReactNode } from 'react'

/**
 * A small status/value chip (handoff-record-table-3 §4.2). Its first job is in the data grid: a
 * coded-domain value and a free-text value used to render identically, so a sign's condition code
 * and its notes read as the same kind of thing. A chip says "this came from a fixed list" at a
 * glance.
 *
 * Colour is never the only channel — the value's own text is inside the chip — so `tone` adds
 * emphasis, it doesn't carry meaning on its own. Domains have no colour of their own today
 * (`DomainValue` is code/label/active/sortOrder), so coded values render `neutral`.
 */
export default function Badge({
  children,
  tone = 'neutral',
  title,
}: {
  children: ReactNode
  tone?: 'neutral' | 'success' | 'warning' | 'danger'
  title?: string
}) {
  return (
    <span className={`badge badge--${tone}`} title={title}>
      {children}
    </span>
  )
}
