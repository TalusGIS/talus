import { type ChangeEvent, useEffect, useState } from 'react'

import { ApiError, uploadRaster } from '@/lib/api'
import CloseButton from '@/components/CloseButton'

interface UploadRasterDialogProps {
  open: boolean
  onClose: () => void
  onUploaded: (rasterId: string) => void // parent refreshes /me/rasters
  accessToken: string
}

/**
 * Upload-a-raster modal (V045+). The portal stores & serves user-produced rasters. Ingest accepts
 * GeoTIFF/COG, JPEG 2000 and ERDAS IMG, and nothing else, and converts it to a Cloud-Optimized GeoTIFF
 * (COG) server-side — COG is the internal serve format, not an upload requirement. The file bytes are
 * POSTed as the request body (the store is never browser-exposed); this dialog collects the file + title
 * and surfaces the api's validation errors. Publisher/admin only (enforced server-side).
 */
export default function UploadRasterDialog({ open, onClose, onUploaded, accessToken }: UploadRasterDialogProps) {
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
    // Default the title to the file's base name (sans extension) the first time, for convenience.
    if (picked && title.trim() === '') {
      setTitle(picked.name.replace(/\.(tif|tiff|cog|jp2|img)$/i, ''))
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
      const resp = await uploadRaster(file, title.trim(), description.trim(), accessToken)
      onUploaded(resp.id)
      onClose()
    } catch (err) {
      // 400 "not a valid COG", 413 over size limit, 403 etc. surface readably via ApiError.message.
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
        aria-label="Upload raster"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal__header">
          <h2 className="modal__title">Upload raster</h2>
          <CloseButton onClick={onClose} />
        </div>

        {/* ① Type — a fixed fact: served as dynamic tiles; stored internally as a COG. */}
        <section className="modal__section">
          <h3 className="modal__section-title">Type</h3>
          <p className="modal__muted">Raster · GeoTIFF, COG, or JPEG 2000</p>
        </section>

        {/* ② File — the raster to ingest (converted to a COG server-side if it isn't one already). */}
        <section className="modal__section">
          <h3 className="modal__section-title">File</h3>
          <input type="file" accept=".tif,.tiff,.cog,.jp2,.img,image/tiff,image/jp2" onChange={onPickFile} />
          {file && <p className="modal__muted">{file.name} · {(file.size / (1024 * 1024)).toFixed(1)} MB</p>}
          {touched && fileMissing && <p className="modal__field-error">Choose a raster file to upload</p>}
          <p className="modal__muted">
            GeoTIFF, COG, or JPEG 2000 from QGIS/ArcGIS. It’s stored and served as a Cloud-Optimized
            GeoTIFF (COG) for fast tiling — non-COG uploads are converted automatically.
          </p>
        </section>

        {/* ③ Details — title + optional description. */}
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
