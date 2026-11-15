/**
 * Add/edit a ROW of a standalone non-spatial table from the Data grid (decision-nonspatial-tables.md t4).
 * A thin modal around the same SlotForm the map uses — notes + the layer's owner-defined custom fields
 * (typed inputs, domain picklists, constraint feedback) — minus the coordinate step (no `coord` prop; a
 * table has no geometry). Submits through the existing slot API: create POSTs {notes, props}; edit PATCHes
 * with the row's If-Match version (T34 optimistic concurrency — a stale edit 409s with a friendly message);
 * Delete rides the same If-Match. The parent refreshes the grid on save.
 */
import { useMemo, useState } from 'react'

import { ApiError, makeSlotApi, uploadFeatureAttachment, editOpsOf } from '@/lib/api'
import type { MyLayer, SlotProperties, SlotUpdate } from '@/lib/api'
import SlotForm from '@/features/map/SlotForm'
import useConfirm from '@/components/useConfirm'

export interface RowFormDialogProps {
  layer: MyLayer
  accessToken: string
  /** null = create a new row; else edit this row (raw /features properties + its id). */
  editing: { recordId: string; properties: Record<string, unknown> } | null
  onClose: () => void
  onSaved: () => void // create/update/delete succeeded — parent refetches the grid
}

export default function RowFormDialog({
  layer,
  accessToken,
  editing,
  onClose,
  onSaved,
}: RowFormDialogProps) {
  const api = useMemo(() => makeSlotApi(layer.id), [layer.id])
  const { confirm, confirmDialog } = useConfirm()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const mode = editing ? 'edit' : 'create'
  // Seed SlotForm from the grid row's raw properties (notes + the props jsonb). The /features shape
  // carries the columns flat; SlotForm reads only `notes` and `props`, so a structural cast is safe.
  const initial = editing ? (editing.properties as unknown as SlotProperties) : null
  // T34 optimistic concurrency: the row's version rides If-Match on update/delete.
  const version = editing ? String(editing.properties.row_version ?? '') : ''

  async function submit(attrs: SlotUpdate, attachments?: File[]): Promise<void> {
    setBusy(true)
    setError(null)
    try {
      // Write the row first, then upload any staged attachments to its id (create returns { id }).
      let featureId: string
      if (editing) {
        await api.update(editing.recordId, attrs, accessToken, version)
        featureId = editing.recordId
      } else {
        const created = await api.create({ notes: attrs.notes ?? null, props: attrs.props }, accessToken)
        featureId = (created as unknown as { id: string }).id
      }
      let failed = ''
      if (attachments?.length && layer.attachmentsEnabled && featureId) {
        for (const f of attachments) {
          try {
            await uploadFeatureAttachment(layer.id, featureId, f, accessToken)
          } catch {
            failed += (failed ? ', ' : '') + f.name
          }
        }
      }
      onSaved() // the row is saved — refresh the grid even if an attachment upload failed
      if (failed) {
        setError(`Saved. Couldn't upload: ${failed} — check the file type/size and Save again.`)
        setBusy(false)
        return
      }
      onClose()
    } catch (e) {
      setError(
        e instanceof ApiError
          ? e.status === 409
            ? 'Someone else changed this row — close and reopen it to edit the latest version.'
            : e.message
          : 'Save failed',
      )
      setBusy(false)
    }
  }

  function remove(): void {
    if (!editing) return
    confirm({
      title: 'Delete this row?',
      // The consequence names the optimistic-concurrency behaviour (T34): the delete carries this
      // row's If-Match version, so a row someone else changed since it was opened is refused, not
      // clobbered. The 409 message below says the same thing when it happens.
      consequence:
        'The row and its attachments are removed for good. If someone else has changed it since you opened it, the delete is refused rather than overwriting their version.',
      confirmLabel: 'Delete row',
      onConfirm: () => doRemove(),
    })
  }

  async function doRemove(): Promise<void> {
    if (!editing) return
    setBusy(true)
    setError(null)
    try {
      await api.remove(editing.recordId, accessToken, version)
      onSaved()
      onClose()
    } catch (e) {
      setError(
        e instanceof ApiError
          ? e.status === 409
            ? 'Someone else changed this row — close and reopen it to delete the latest version.'
            : e.message
          : 'Delete failed',
      )
      setBusy(false)
    }
  }

  return (
    <div className="modal__backdrop" role="presentation" onClick={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label={mode === 'create' ? 'Add row' : 'Edit row'}
        onClick={(e) => e.stopPropagation()}
      >
        <SlotForm
          mode={mode}
          initial={initial}
          busy={busy}
          error={error}
          onSubmit={(attrs, attachments) => void submit(attrs, attachments)}
          onCancel={onClose}
          extraAction={
            editing && editOpsOf(layer).delete ? ( // V109: Delete row follows the Delete-features flag
              <button
                type="button"
                className="btn btn--sm btn--danger sign-form__actions-lead"
                onClick={() => remove()}
                disabled={busy}
              >
                Delete row
              </button>
            ) : undefined
          }
          layerId={layer.id}
          accessToken={accessToken}
          noun="row"
          editViaAdmin={layer.editViaAdmin}
          attachmentsEnabled={layer.attachmentsEnabled}
        />
        {confirmDialog}
      </div>
    </div>
  )
}
