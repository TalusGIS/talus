import { useEffect, useState } from 'react'

import { ApiError, exportRaster, fetchRasterExportFormats } from '@/lib/api'
import type { RasterExportFormat } from '@/lib/api'

import type { DrawBbox } from './drawBox'
import CloseButton from '@/components/CloseButton'

/**
 * Clip-export dialog for a raster operational layer (decision-raster-export.md c6). Opens after the
 * user drags a box on the map (bbox != null) or chose "whole raster" (bbox == null). Lets them pick a
 * writable format (probed server-side) and optionally downsample, then downloads the file.
 *
 * Honest size handling: an over-cap clip returns 413 with a message explaining the cap + the downsample
 * opt-in; we surface it and reveal the maxDim field so the user can retry without guessing.
 */
function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

export default function ExportRasterDialog({
  rasterId,
  title,
  bbox,
  accessToken,
  onClose,
}: {
  rasterId: string
  title: string
  bbox: DrawBbox | null
  accessToken: string
  onClose: () => void
}) {
  const [formats, setFormats] = useState<RasterExportFormat[] | null>(null)
  const [format, setFormat] = useState<string>('cog')
  const [whole, setWhole] = useState<boolean>(bbox === null)
  const [maxDim, setMaxDim] = useState<string>('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    fetchRasterExportFormats(accessToken)
      .then((fmts) => {
        if (!active) return
        setFormats(fmts)
        if (fmts.length && !fmts.some((f) => f.key === 'cog')) setFormat(fmts[0].key)
      })
      .catch((e) => active && setError(e instanceof ApiError ? e.message : String(e)))
    return () => {
      active = false
    }
  }, [accessToken])

  async function download(): Promise<void> {
    setBusy(true)
    setError(null)
    try {
      const dim = maxDim.trim() === '' ? undefined : Number(maxDim)
      if (dim !== undefined && (!Number.isFinite(dim) || dim < 1)) {
        setError('Downsample size must be a positive number of pixels.')
        setBusy(false)
        return
      }
      const result = await exportRaster(
        rasterId,
        { format, bbox: whole ? undefined : (bbox ?? undefined), maxDim: dim },
        accessToken,
      )
      saveBlob(result.blob, result.filename)
      onClose()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
      setBusy(false)
    }
  }

  const areaLabel = whole || bbox === null
    ? 'Whole raster'
    : `Clip: ${bbox.map((n) => n.toFixed(4)).join(', ')}`

  return (
    <div className="modal__backdrop" role="presentation" onClick={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label="Export raster"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal__header">
          <h2 className="modal__title">Export “{title}”</h2>
          <CloseButton onClick={onClose} />
        </div>

        <section className="modal__section">
          <p className="modal__muted">{areaLabel}</p>
          {bbox !== null && (
            <label className="modal__check">
              <input type="checkbox" checked={whole} onChange={(e) => setWhole(e.target.checked)} />
              <span>Download the whole raster instead of the drawn box</span>
            </label>
          )}

          <label className="modal__label" htmlFor="raster-export-format">
            Format
          </label>
          <select
            id="raster-export-format"
            className="input"
            value={format}
            onChange={(e) => setFormat(e.target.value)}
            disabled={formats === null}
          >
            {(formats ?? []).map((f) => (
              <option key={f.key} value={f.key}>
                {f.label}
              </option>
            ))}
          </select>

          <label className="modal__label" htmlFor="raster-export-maxdim">
            Downsample longest side to (pixels) — optional
          </label>
          <input
            id="raster-export-maxdim"
            className="input"
            type="number"
            min={1}
            placeholder="native resolution"
            value={maxDim}
            onChange={(e) => setMaxDim(e.target.value)}
          />

          {error && <p className="modal__field-error">{error}</p>}
        </section>

        <div className="sign-form__actions">
          <button type="button" className="btn btn--sm" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn--sm"
            onClick={() => void download()}
            disabled={busy || formats === null}
          >
            {busy ? 'Exporting…' : 'Download'}
          </button>
        </div>
      </div>
    </div>
  )
}
