import { useCallback, useState, type ReactNode } from 'react'

import ConfirmDialog from './ConfirmDialog'

/**
 * The shell that retires `window.confirm` (docs/css-conventions.md §3). A browser alert can't state
 * a consequence, can't be styled, and can't show a busy state — but replacing it per-surface meant
 * wiring pending-action state into every caller, which is why twelve sites stayed on it after
 * ConfirmDialog shipped. This owns that state once.
 *
 *   const { confirm, confirmDialog } = useConfirm()
 *   …
 *   confirm({ title: 'Delete “Signs”?', confirmLabel: 'Delete layer',
 *             consequence: 'The layer and its data are removed. This can’t be undone.',
 *             onConfirm: async () => { await deleteLayer(id, token) } })
 *   …
 *   return <>{…}{confirmDialog}</>
 *
 * `onConfirm` runs with the dialog in its busy state and the dialog closes when it settles —
 * including on a throw, so an error surfaces through the caller's own notice/error path exactly as
 * it did when the work ran straight after `window.confirm`.
 */
export interface ConfirmRequest {
  title: string
  /** Context above the callout — the usage warning, what else is affected. */
  body?: ReactNode
  /** The irreversible part, rendered as a Callout toned with the button. */
  consequence?: ReactNode
  /** Names its own scope: "Delete 3 rows", never "Delete". */
  confirmLabel: string
  tone?: 'primary' | 'danger'
  onConfirm: () => void | Promise<void>
}

export default function useConfirm(): {
  confirm: (req: ConfirmRequest) => void
  confirmDialog: ReactNode
  /** True while the dialog is up. `window.confirm` was modal to the BROWSER — no key reached the
   *  app — so a surface with its own Esc handler (the map's cancel-gesture key) must check this or
   *  one Esc now does two things. */
  confirmOpen: boolean
} {
  const [req, setReq] = useState<ConfirmRequest | null>(null)
  const [busy, setBusy] = useState(false)

  const confirm = useCallback((next: ConfirmRequest) => {
    setBusy(false)
    setReq(next)
  }, [])

  const confirmDialog = req ? (
    <ConfirmDialog
      title={req.title}
      consequence={req.consequence}
      confirmLabel={req.confirmLabel}
      tone={req.tone ?? 'danger'}
      busy={busy}
      onCancel={() => setReq(null)}
      onConfirm={() => {
        setBusy(true)
        void (async () => {
          try {
            await req.onConfirm()
          } finally {
            setBusy(false)
            setReq(null)
          }
        })()
      }}
    >
      {req.body}
    </ConfirmDialog>
  ) : null

  return { confirm, confirmDialog, confirmOpen: req !== null }
}
