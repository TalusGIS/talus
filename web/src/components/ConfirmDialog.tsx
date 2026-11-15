import { useEffect, type ReactNode } from 'react'
import CloseButton from './CloseButton'

/**
 * The one confirmation dialog (handoff-admin-1a §5) — replaces window.confirm at the
 * priority sites so consequences render as designed copy instead of a browser alert. Built
 * on the existing .modal vocabulary so it inherits the reskin; `consequence` renders as a
 * Callout whose tone follows the button: `danger` strictly for destructive/irreversible
 * actions (red confirm + danger callout), `primary` for reversible-but-broad ones (warning
 * callout) — the button system in decision-ui-refresh.md. Cancel takes initial focus: the
 * safe default when the other button destroys something.
 */
export default function ConfirmDialog({
  title,
  children,
  consequence,
  confirmLabel,
  tone = 'primary',
  onConfirm,
  onCancel,
  busy = false,
}: {
  title: string
  children?: ReactNode
  consequence?: ReactNode
  confirmLabel: string
  tone?: 'primary' | 'danger'
  onConfirm: () => void
  onCancel: () => void
  busy?: boolean
}) {
  // Esc cancels — the keyboard affordance window.confirm gave for free.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !busy) onCancel()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onCancel, busy])

  return (
    <div className="modal__backdrop" role="presentation" onClick={busy ? undefined : onCancel}>
      <div
        className="modal modal--confirm"
        role="alertdialog"
        aria-modal="true"
        aria-label={title}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal__header">
          <h2 className="modal__title">{title}</h2>
          <CloseButton onClick={onCancel} disabled={busy} />
        </div>
        <section className="modal__section">
          {children}
          {consequence != null && (
            <div className={`callout callout--${tone === 'danger' ? 'danger' : 'warning'}`}>
              {consequence}
            </div>
          )}
          <div className="sign-form__actions">
            <button
              type="button"
              className="btn btn--sm btn--ghost"
              onClick={onCancel}
              disabled={busy}
              autoFocus
            >
              Cancel
            </button>
            <button
              type="button"
              className={`btn btn--sm${tone === 'danger' ? ' btn--danger' : ''}`}
              onClick={onConfirm}
              disabled={busy}
            >
              {busy ? 'Working…' : confirmLabel}
            </button>
          </div>
        </section>
      </div>
    </div>
  )
}
