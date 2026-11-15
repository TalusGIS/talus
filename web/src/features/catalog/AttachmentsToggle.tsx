import { useEffect, useState } from 'react'

import { ApiError, fetchLayerAttachmentsEnabled, setLayerAttachmentsEnabled } from '@/lib/api'

/**
 * decision-attachments.md A-1 — the "Attachments" section in a layer's Manage → Settings tab (sibling to
 * Editing + Delete Protection + Transfer Ownership).
 *
 * Loads the layer's `attachments_enabled` flag and toggles it with an immediate PUT (optimistic; reverts on
 * failure). Read/write is owner-or-admin. DEFAULT off (opt-in): a layer accepts NO attachment uploads and
 * advertises hasAttachments:false until the owner turns this on. The switch is a HARD gate server-side (the
 * attachment write path 403s when off — for the owner and admin alike; unlike the soft editing switch,
 * there is no admin override), so this section only surfaces the flag and any 403 inline.
 */
export default function AttachmentsToggle({
  layerId,
  accessToken,
  onChange,
}: {
  layerId: string
  accessToken: string
  /** Called with the saved value after a successful toggle (e.g. to refresh a popup's attachments affordance). */
  onChange?: (value: boolean) => void
}) {
  const [value, setValue] = useState<boolean | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    fetchLayerAttachmentsEnabled(layerId, accessToken)
      .then((v) => active && setValue(v))
      .catch((e) => active && setError(e instanceof ApiError ? e.message : String(e)))
    return () => {
      active = false
    }
  }, [layerId, accessToken])

  async function toggle(next: boolean): Promise<void> {
    setSaving(true)
    setError(null)
    setValue(next) // optimistic
    try {
      const saved = await setLayerAttachmentsEnabled(layerId, next, accessToken)
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
      <p className="modal__section-title">Attachments</p>
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
            <span className="share-dialog__name">Allow attachments on this layer</span>
            <span className="share-dialog__hint">
              While on, editors can attach photos and documents (images, PDF, Office files — 25&nbsp;MB each) to
              this layer’s features, in the web app and in ArcGIS&nbsp;Pro / QGIS. While off, no attachments can
              be added. Existing attachments are kept either way.
            </span>
          </span>
        </label>
      )}
      {error && <p className="modal__field-error">{error}</p>}
    </section>
  )
}
