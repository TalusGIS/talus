import { type ChangeEvent, useEffect, useState } from 'react'

import { ApiError, uploadFieldProject } from '@/lib/api'
import CloseButton from '@/components/CloseButton'

interface UploadFieldProjectDialogProps {
  open: boolean
  onClose: () => void
  onUploaded: (projectId: string) => void // parent refreshes /me/field-projects
  accessToken: string
}

/**
 * Upload-a-field-project modal (V085, decision-field-projects.md). The portal stores & serves a
 * QField/QGIS project (.qgz) as an OPAQUE artifact — it is never opened or edited here; "edit" =
 * download → QGIS → re-upload (THE FENCE). The file bytes are POSTed as the request body; this
 * dialog collects the file + title and surfaces the api's zip-lip validation errors.
 * Publisher/admin only (enforced server-side).
 */
export default function UploadFieldProjectDialog({
  open,
  onClose,
  onUploaded,
  accessToken,
}: UploadFieldProjectDialogProps) {
  const [file, setFile] = useState<File | null>(null)
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [touched, setTouched] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Reset to a clean form each time the dialog opens.
  useEffect(() => {
    if (open) {
      setFile(null)
      setTitle('')
      setDescription('')
      setTouched(false)
      setBusy(false)
      setError(null)
    }
  }, [open])

  if (!open) return null

  const nameMissing = title.trim() === ''
  const fileMissing = file === null

  function onPickFile(e: ChangeEvent<HTMLInputElement>): void {
    const picked = e.target.files?.[0] ?? null
    setFile(picked)
    if (picked && title.trim() === '') {
      setTitle(picked.name.replace(/\.qgz$/i, ''))
    }
  }

  async function submit(): Promise<void> {
    if (nameMissing || fileMissing) {
      setTouched(true)
      return
    }
    setBusy(true)
    setError(null)
    try {
      const resp = await uploadFieldProject(file, title.trim(), description.trim(), accessToken)
      onUploaded(resp.id)
      onClose()
    } catch (err) {
      // 400 "not a .qgz", 413 over size limit, 403 etc. surface readably via ApiError.message.
      setError(err instanceof ApiError ? err.message : 'Upload failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal__backdrop" role="presentation" onClick={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label="Upload field project"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal__header">
          <h2 className="modal__title">Upload field project</h2>
          <CloseButton onClick={onClose} />
        </div>

        {/* ① Type — a fixed fact: a phone-bound QField project, stored as-is, never opened here. */}
        <section className="modal__section">
          <h3 className="modal__section-title">Type</h3>
          <p className="modal__muted">Field project · a QGIS/QField project file (.qgz)</p>
        </section>

        {/* ② File */}
        <section className="modal__section">
          <h3 className="modal__section-title">File</h3>
          <input type="file" accept=".qgz" onChange={onPickFile} />
          {file && <p className="modal__muted">{file.name} · {(file.size / (1024 * 1024)).toFixed(1)} MB</p>}
          {touched && fileMissing && <p className="modal__field-error">Choose a .qgz file to upload</p>}
          <p className="modal__muted">
            Export from QGIS as a single .qgz. Talus stores and shares it exactly as uploaded —
            projects are never opened or edited here. To change one: download, edit in QGIS,
            upload the replacement. If the project embeds an access token, treat it like a
            credential: share narrowly, and revoke the token when the project retires.
          </p>
        </section>

        {/* ③ Details */}
        <section className="modal__section">
          <h3 className="modal__section-title">Details</h3>
          <label className="sign-form__field">
            <span>Title *</span>
            <input
              type="text"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              onBlur={() => setTouched(true)}
            />
          </label>
          {touched && nameMissing && <p className="modal__field-error">Title is required</p>}

          <label className="sign-form__field">
            <span>Description</span>
            <input type="text" value={description} onChange={(e) => setDescription(e.target.value)} />
          </label>
        </section>

        {error !== null && <p className="sign-form__error">{error}</p>}

        <div className="sign-form__actions">
          <button type="button" className="btn btn--sm" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn--sm"
            onClick={() => void submit()}
            disabled={busy || nameMissing || fileMissing}
          >
            {busy ? 'Uploading…' : 'Upload'}
          </button>
        </div>
      </div>
    </div>
  )
}
