import { ChevronDown, ChevronRight } from 'lucide-react'

/**
 * The one disclosure caret (handoff-record-table-3 §1.1). Same reason as CloseButton: `▾`/`▸` was
 * hand-written at every collapsible surface, so a fix at one never reached the others. Rotation is
 * left to the icon swap rather than a CSS transform so the shape is honest at any font size.
 */
export default function Caret({
  open,
  className,
  size = 14,
}: {
  open: boolean
  className?: string
  size?: number
}) {
  const Icon = open ? ChevronDown : ChevronRight
  return <Icon className={className} size={size} aria-hidden="true" />
}
