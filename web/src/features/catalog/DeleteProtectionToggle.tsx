import { useEffect, useState } from 'react'

import { ApiError, fetchDeleteProtection, setDeleteProtection } from '@/lib/api'
import type { FavoriteItemType } from '@/lib/api'

/**
 * Talus 24 c4 — the "Delete protection" section shared by the map and layer Manage dialogs.
 *
 * Loads the item's current `delete_protected` flag and toggles it with an immediate PUT (optimistic;
 * reverts on failure). The server is the authority (owner-or-admin enforces read/write, and rejects a
 * protected item's delete for a non-admin); this section is only mounted inside a Manage dialog the
 * caller could open (owner-or-admin), so it assumes the toggle is permitted and surfaces any 403/409
 * from the server inline rather than pre-gating.
 */
export default function DeleteProtectionToggle({
  itemType,
  itemId,
  itemNoun,
  accessToken,
  onChange,
}: {
  itemType: FavoriteItemType
  itemId: string
  /** Singular noun for the copy: 'map' | 'layer'. */
  itemNoun: string
  accessToken: string
  /** Called with the saved value after a successful toggle, so the catalog row's padlock updates live. */
  onChange?: (value: boolean) => void
}) {
  const [value, setValue] = useState<boolean | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    fetchDeleteProtection(itemType, itemId, accessToken)
      .then((v) => active && setValue(v))
      .catch((e) => active && setError(e instanceof ApiError ? e.message : String(e)))
    return () => {
      active = false
    }
  }, [itemType, itemId, accessToken])

  async function toggle(next: boolean): Promise<void> {
    setSaving(true)
    setError(null)
    setValue(next) // optimistic
    try {
      const saved = await setDeleteProtection(itemType, itemId, next, accessToken)
      setValue(saved)
      onChange?.(saved)
    } catch (e) {
      setValue(!next) // revert
      setError(e instanceof ApiError ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="modal__section">
      <p className="modal__section-title">Delete protection</p>
      {value === null && error === null ? (
        <p className="modal__muted">Loading…</p>
      ) : (
        <label className="share-dialog__row">
          <input
            type="checkbox"
            checked={value ?? false}
            disabled={saving || value === null}
            onChange={(e) => void toggle(e.target.checked)}
          />
          <span className="share-dialog__text">
            <span className="share-dialog__name">Protect this {itemNoun} from deletion</span>
            <span className="share-dialog__hint">
              While on, this {itemNoun} can’t be deleted by anyone — including administrators —
              until you turn protection off.
            </span>
          </span>
        </label>
      )}
      {error && <p className="modal__field-error">{error}</p>}
    </section>
  )
}
