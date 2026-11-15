import type { ChangeEvent } from 'react'
import CloseButton from '@/components/CloseButton'

/**
 * Attachment staging control for the feature edit forms (decision-attachments-in-edit-form.md).
 * Controlled: the parent form owns the `staged` File list and passes it up on Save, where AssetMap /
 * RowFormDialog upload each file to the (by-then-known) feature id. Nothing persists until Save;
 * removing a staged file here just drops it from the list. Rendered only when the layer has
 * attachments enabled and the caller can edit.
 */

// Mirrors the server allowlist (decision-attachments.md §4.4) — the picker's friendly filter only;
// the server (type + extension + magic bytes) stays the authority. Kept in sync with RecordDetail's.
const ACCEPT =
  'image/jpeg,image/png,image/gif,image/webp,image/tiff,image/heic,image/heif,image/bmp,' +
  'application/pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,text/plain,text/csv'

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}

export interface AttachmentStageProps {
  staged: File[]
  onChange: (files: File[]) => void
  disabled?: boolean
}

export default function AttachmentStage({ staged, onChange, disabled = false }: AttachmentStageProps) {
  function onPick(e: ChangeEvent<HTMLInputElement>): void {
    const picked = Array.from(e.target.files ?? [])
    if (picked.length) onChange([...staged, ...picked])
    e.target.value = '' // let the same file be re-picked after removal
  }

  return (
    <div className="attach-stage">
      <div className="attach-stage__head">
        <span className="attach-stage__title">Attachments</span>
        <label className={`btn btn--sm${disabled ? ' is-disabled' : ''}`} title="Add an attachment">
          + Add
          <input
            type="file"
            multiple
            accept={ACCEPT}
            disabled={disabled}
            onChange={onPick}
            style={{ display: 'none' }}
          />
        </label>
      </div>

      {staged.length > 0 && (
        <ul className="attach-stage__list">
          {staged.map((f, i) => (
            <li className="attach-stage__item" key={`${f.name}-${f.size}-${i}`}>
              <span className="attach-stage__name" title={f.name}>
                {f.name}
              </span>
              <span className="attach-stage__meta">{fmtBytes(f.size)}</span>
              <CloseButton
                onClick={() => onChange(staged.filter((_, idx) => idx !== i))}
                label={`Remove ${f.name}`}
                className="attach-stage__del"
                disabled={disabled}
              />
            </li>
          ))}
        </ul>
      )}

      <p className="attach-stage__note">
        {staged.length > 0 ? 'Files upload when you Save.' : 'Add photos or documents — they upload when you Save.'}
      </p>
    </div>
  )
}
