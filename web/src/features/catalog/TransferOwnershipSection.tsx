import { useEffect, useMemo, useState } from 'react'

import useConfirm from '@/components/useConfirm'
import { ApiError, fetchTransferTargets, transferOwnership } from '@/lib/api'
import type { FavoriteItemType, TransferTarget } from '@/lib/api'

/**
 * Talus 26 — the "Transfer ownership" control shared by the map and layer Manage dialogs. Hands an
 * item to another member: they become the owner, the caller drops to whatever they're separately
 * shared (an administrator can always transfer it back).
 *
 * Only mounted when the caller MAY transfer (admin always; a non-admin owner only when the org
 * policy `nonadminCanTransferOwnership` is on) — the server re-enforces regardless. The picker is a
 * **type-to-filter combobox** (Talus 31): orgs with hundreds of members shouldn't scroll a giant
 * dropdown — type a name to narrow, click to choose. On success it calls `onTransferred` so the
 * catalog refreshes (the item may leave the caller's Owned list) and the dialog closes.
 */
const _MAX_SHOWN = 50 // cap the rendered matches; "keep typing" narrows the rest

export default function TransferOwnershipSection({
  itemType,
  itemId,
  itemNoun,
  currentOwnerId,
  accessToken,
  onTransferred,
}: {
  itemType: FavoriteItemType
  itemId: string
  itemNoun: 'map' | 'layer' | 'raster' | 'reference' | 'field project'
  currentOwnerId?: string | null
  accessToken: string
  onTransferred: () => void
}) {
  const { confirm, confirmDialog } = useConfirm()
  const [targets, setTargets] = useState<TransferTarget[] | null>(null)
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState<TransferTarget | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    fetchTransferTargets(accessToken)
      .then((t) => {
        if (active) setTargets(t.filter((u) => u.id !== currentOwnerId))
      })
      .catch((e) => active && setError(e instanceof ApiError ? e.message : String(e)))
    return () => {
      active = false
    }
  }, [accessToken, currentOwnerId])

  const q = query.trim().toLowerCase()
  const matches = useMemo(() => {
    const all = targets ?? []
    return q ? all.filter((u) => u.display_name.toLowerCase().includes(q)) : all
  }, [targets, q])

  function transfer(): void {
    if (!selected) return
    const target = selected
    confirm({
      title: `Transfer this ${itemNoun} to ${target.display_name}?`,
      // Broad but not destructive — an administrator can always transfer it back, so this is the
      // reversible tone, not `danger`.
      tone: 'primary',
      consequence: `${target.display_name} becomes the owner and you’ll no longer manage this ${itemNoun}. An administrator still can, and can transfer it back.`,
      confirmLabel: `Transfer ${itemNoun}`,
      onConfirm: async () => {
        setBusy(true)
        setError(null)
        try {
          await transferOwnership(itemType, itemId, target.id, accessToken)
          onTransferred()
        } catch (e) {
          setError(e instanceof ApiError ? e.message : String(e))
          setBusy(false)
        }
      },
    })
  }

  // The dropdown of matches shows only while actively searching (a query typed, nothing chosen yet) —
  // so an org with hundreds of members never renders the full list at once.
  const searching = selected === null && query.trim() !== ''

  return (
    <section className="modal__section">
      <p className="modal__section-title">Transfer ownership</p>
      <p className="share-dialog__hint">
        Hand this {itemNoun} to another member — they become the owner and you’ll no longer manage
        it. An administrator can always transfer it back.
      </p>
      {targets === null && error === null ? (
        <p className="modal__muted">Loading…</p>
      ) : (
        <>
          <div className="share-dialog__link-row">
            <input
              type="text"
              className="share-dialog__link-input"
              placeholder="Search members by name…"
              value={selected ? selected.display_name : query}
              disabled={busy}
              onChange={(e) => {
                setSelected(null)
                setQuery(e.target.value)
              }}
              aria-label={`New owner for this ${itemNoun}`}
            />
            <button
              type="button"
              className="btn btn--sm"
              disabled={busy || !selected}
              onClick={() => transfer()}
            >
              {busy ? 'Transferring…' : 'Transfer'}
            </button>
          </div>

          {searching &&
            (matches.length === 0 ? (
              <p className="modal__muted">No members match “{query.trim()}”.</p>
            ) : (
              <ul className="member-picker__list">
                {matches.slice(0, _MAX_SHOWN).map((u) => (
                  <li key={u.id}>
                    <button
                      type="button"
                      className="member-picker__opt"
                      onClick={() => {
                        setSelected(u)
                        setQuery('')
                      }}
                    >
                      {u.display_name}
                    </button>
                  </li>
                ))}
                {matches.length > _MAX_SHOWN && (
                  <li className="member-picker__more">
                    +{matches.length - _MAX_SHOWN} more — keep typing to narrow
                  </li>
                )}
              </ul>
            ))}
        </>
      )}
      {error && <p className="modal__field-error">{error}</p>}
      {confirmDialog}
    </section>
  )
}
