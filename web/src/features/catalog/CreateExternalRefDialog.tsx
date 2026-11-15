import { useState } from 'react'

import { EXT_KIND_LABEL, EXT_PALETTE, probeExternalUrl } from '@/features/map/addFromUrl'
import type { ExternalProbe } from '@/features/map/addFromUrl'
import { ApiError, createExternalRef } from '@/lib/api'
import type { ExternalRef } from '@/lib/api'
import CloseButton from '@/components/CloseButton'

/**
 * "+ New reference" (decision-external-catalog-items.md §2.4): the Catalog's create form for a
 * referenced external service — the Add-from-URL detect → probe → confirm flow relocated here,
 * ending in a catalog ITEM instead of a map op. The probe runs in the BROWSER (the no-proxy fence:
 * the server never fetches the URL — a bad/CORS-blocked service fails honestly right here), then
 * POST /external-refs persists the validated spec. Publisher-only (the button is publisher-gated
 * in CatalogView; the server re-enforces).
 */
export default function CreateExternalRefDialog({
  accessToken,
  onCreated,
  onClose,
}: {
  accessToken: string
  onCreated: (ref: ExternalRef) => void
  onClose: () => void
}) {
  const [urlText, setUrlText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [probe, setProbe] = useState<ExternalProbe | null>(null)
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [color, setColor] = useState(EXT_PALETTE[0])
  const [saving, setSaving] = useState(false)

  const runProbe = (): void => {
    setBusy(true)
    setError(null)
    setProbe(null)
    void probeExternalUrl(urlText)
      .then((p) => {
        setProbe(p)
        setTitle(p.title.slice(0, 80))
      })
      .catch((e) => setError(e instanceof Error ? e.message : 'Could not read that URL'))
      .finally(() => setBusy(false))
  }

  async function create(): Promise<void> {
    if (!probe) return
    const isVector = probe.externalKind === 'arcgis-feature' || probe.externalKind === 'geojson'
    setSaving(true)
    setError(null)
    try {
      const ref = await createExternalRef(
        {
          title: title.trim() || probe.title.slice(0, 80) || 'External reference',
          description: description.trim() || null,
          externalKind: probe.externalKind,
          url: probe.url,
          ...(isVector ? { geometryType: probe.geometryType, color } : {}),
          ...(probe.where ? { where: probe.where } : {}),
        },
        accessToken,
      )
      onCreated(ref)
      onClose()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
      setSaving(false)
    }
  }

  return (
    <div className="modal__backdrop" role="presentation" onClick={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label="New reference"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal__header">
          <h2 className="modal__title">New reference</h2>
          <CloseButton onClick={onClose} />
        </div>

        <section className="modal__section">
          <p className="modal__muted">
            Reference a service another organization hosts — an ArcGIS feature or image service, a
            GeoJSON URL, or an XYZ tile template. Nothing is copied: the layer stays on the remote
            server, and this item is a shareable pointer map authors can add from Browse.
          </p>
          <div className="add-layers__url-row">
            <input
              type="url"
              placeholder="Paste a service or file URL…"
              value={urlText}
              onChange={(e) => setUrlText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && urlText.trim() && !busy) runProbe()
              }}
              aria-label="Service URL"
            />
            <button type="button" className="btn btn--xs" disabled={busy || !urlText.trim()} onClick={runProbe}>
              {busy ? 'Checking…' : 'Check'}
            </button>
          </div>
          {error && <p className="sign-form__error">{error}</p>}
        </section>

        {probe && (
          <section className="modal__section">
            <div className="modal__muted">{EXT_KIND_LABEL[probe.externalKind]}</div>
            <input
              type="text"
              value={title}
              maxLength={80}
              onChange={(e) => setTitle(e.target.value)}
              aria-label="Reference title"
            />
            <textarea
              value={description}
              maxLength={2000}
              rows={2}
              placeholder="Description (optional) — what this service is and who publishes it"
              onChange={(e) => setDescription(e.target.value)}
              aria-label="Description"
            />
            {(probe.externalKind === 'arcgis-feature' || probe.externalKind === 'geojson') && (
              <div className="modal__swatches">
                {EXT_PALETTE.map((c) => (
                  <button
                    key={c}
                    type="button"
                    className={`modal__swatch${color === c ? ' is-selected' : ''}`}
                    style={{ background: c }}
                    aria-label={`Color ${c}`}
                    aria-pressed={color === c}
                    onClick={() => setColor(c)}
                  />
                ))}
              </div>
            )}
            <div className="modal__muted">
              {probe.featureCount !== null && <>{probe.featureCount.toLocaleString()} features</>}
              {probe.where && <> · filtered: <code>{probe.where}</code></>}
            </div>
          </section>
        )}

        <div className="sign-form__actions">
          <button type="button" className="btn btn--sm" onClick={onClose} disabled={saving}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn--sm"
            disabled={!probe || saving}
            onClick={() => void create()}
          >
            {saving ? 'Creating…' : 'Create reference'}
          </button>
        </div>
      </div>
    </div>
  )
}
