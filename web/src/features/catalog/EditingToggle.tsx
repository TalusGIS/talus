import { useEffect, useState } from 'react'

import { ApiError, fetchLayerEditing, setLayerEditing } from '@/lib/api'
import type { EditOps, LayerEditing } from '@/lib/api'

/**
 * decision-editing-toggle.md — the "Editing" section in a layer's Manage → Settings tab (sibling to Delete
 * Protection + Transfer Ownership) — widened by decision-layer-edit-capabilities.md (V109) with the four
 * capability checkboxes under the master switch: Add features · Edit attributes · Move or reshape · Delete
 * features. One door (GET/PUT /layers/{id}/editing) carries both.
 *
 * The server is the authority — owner-or-admin enforces read/write, and `edit_allowed(op)` enforces every flag
 * at every write door (no admin override on the flags: they are the owner's data policy, like `editable`) —
 * so this section only mounts inside a Manage dialog the caller could open and surfaces any 403 inline.
 * Each change is an immediate PUT (optimistic; reverts on failure). A master-switch flip writes the switch
 * alone (the server leaves the flags as they are), so turning editing back on restores the same policy.
 */
const OPS: { key: keyof EditOps; label: string; hint: string }[] = [
  { key: 'add', label: 'Add features', hint: 'Place new points, or draw new lines and polygons.' },
  { key: 'attributes', label: 'Edit attributes', hint: 'Change field values, notes and photos on existing features.' },
  { key: 'geometry', label: 'Move or reshape', hint: 'Move a point, or reshape a line or polygon that already exists.' },
  { key: 'delete', label: 'Delete features', hint: 'Remove features, and their photos with them.' },
]

export default function EditingToggle({
  layerId,
  accessToken,
  onChange,
}: {
  layerId: string
  accessToken: string
  /** Called with the saved master-switch value after a successful toggle (e.g. a future catalog read-only indicator). */
  onChange?: (value: boolean) => void
}) {
  const [value, setValue] = useState<LayerEditing | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    fetchLayerEditing(layerId, accessToken)
      .then((v) => active && setValue(v))
      .catch((e) => active && setError(e instanceof ApiError ? e.message : String(e)))
    return () => {
      active = false
    }
  }, [layerId, accessToken])

  async function save(next: LayerEditing, body: { editingEnabled: boolean; editOps?: EditOps }): Promise<void> {
    const before = value
    setSaving(true)
    setError(null)
    setValue(next) // optimistic
    try {
      const saved = await setLayerEditing(layerId, body, accessToken)
      setValue(saved)
      onChange?.(saved.editingEnabled)
    } catch (e) {
      setValue(before) // revert
      setError(e instanceof ApiError ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  function toggleMaster(next: boolean): void {
    if (!value) return
    void save({ ...value, editingEnabled: next }, { editingEnabled: next })
  }

  function toggleOp(key: keyof EditOps, next: boolean): void {
    if (!value) return
    const editOps = { ...value.editOps, [key]: next }
    void save({ ...value, editOps }, { editingEnabled: value.editingEnabled, editOps })
  }

  const on = value?.editingEnabled ?? false

  return (
    <section className="modal__section">
      <p className="modal__section-title">Editing</p>
      {value === null && error === null ? (
        <p className="modal__muted">Loading…</p>
      ) : (
        <>
          <label className="share-dialog__row">
            <input
              type="checkbox"
              checked={on}
              disabled={saving || value === null}
              onChange={(e) => toggleMaster(e.target.checked)}
            />
            <span className="share-dialog__text">
              <span className="share-dialog__name">Enable editing on this layer</span>
              <span className="share-dialog__hint">
                While off, this layer is read-only — editors can’t add, move, or delete features until you turn
                editing back on. Administrators can still edit.
              </span>
            </span>
          </label>
          <div className={`editing-ops${on ? '' : ' editing-ops--off'}`} role="group" aria-label="What editors may do">
            {OPS.map((op) => (
              <label className="share-dialog__row" key={op.key}>
                <input
                  type="checkbox"
                  checked={value?.editOps[op.key] ?? true}
                  disabled={saving || value === null || !on}
                  onChange={(e) => toggleOp(op.key, e.target.checked)}
                />
                <span className="share-dialog__text">
                  <span className="share-dialog__name">{op.label}</span>
                  <span className="share-dialog__hint">{op.hint}</span>
                </span>
              </label>
            ))}
            <p className="share-dialog__hint">
              What crews and editors may do on this layer. Everyone with edit access gets the same set — including
              administrators.
            </p>
          </div>
        </>
      )}
      {error && <p className="modal__field-error">{error}</p>}
    </section>
  )
}
