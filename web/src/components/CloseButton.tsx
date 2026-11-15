import { X } from 'lucide-react'

/**
 * The one close button (handoff-record-table-3 §1.1). Every dismiss affordance in the app renders
 * through this, so the glyph is chosen in exactly one place. `×` had been fixed per-surface twice
 * and came back both times — most recently in the freshly written ConfirmDialog — because there was
 * no shell to fix. There are ~34 callers; `className` picks the block that positions it
 * (`.modal__close` for dialogs, `.record-detail__close` for the drawer), `.icon-x` centres the
 * glyph, and the icon itself is constant.
 */
export default function CloseButton({
  onClick,
  label = 'Close',
  className = 'modal__close',
  disabled = false,
  title,
  size = 16,
}: {
  onClick: () => void
  /** Accessible name — override when "Close" is ambiguous ("Close detail", "Dismiss banner"). */
  label?: string
  className?: string
  disabled?: boolean
  /** Hover text, when the button's consequence is worth spelling out ("Close (clears the selection)"). */
  title?: string
  size?: number
}) {
  return (
    <button
      type="button"
      className={`icon-x ${className}`}
      aria-label={label}
      title={title}
      onClick={onClick}
      disabled={disabled}
    >
      <X size={size} aria-hidden="true" />
    </button>
  )
}
