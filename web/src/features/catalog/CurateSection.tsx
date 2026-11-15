import { useEffect, useState } from 'react'

import {
  ApiError,
  curateLayer,
  curateRaster,
  fetchCuration,
  fetchRasterCuration,
  uncurateLayer,
  uncurateRaster,
  type CurationStatus,
} from '@/lib/api'

/**
 * Talus 33 c4b — the "Open Data" tab of the layer Manage dialog: the staff-facing publish control.
 *
 * Curating a layer into Open Data is ONE governed, audited action (metadata gate → make public →
 * share to group:open_data). This section reflects the server-computed readiness and walks the
 * publisher through it: a layer with incomplete T31 metadata is blocked with the missing fields + a
 * jump to the Metadata tab; an eligible layer publishes behind a confirm-heavy "this exposes it to
 * the public internet" warning. The server re-enforces every gate (owner-or-admin + Open Data
 * membership + metadata-complete + the F6/public-policy gate); this UI just makes them legible.
 *
 * Un-curate removes the layer from the public catalog but does NOT make it private (reducing public
 * exposure stays the Sharing tab's job) — stated plainly so a publisher isn't surprised.
 */
export default function CurateSection({
  layerId,
  itemKind = 'layer',
  accessToken,
  publicEnabled,
  onGoToMetadata,
  onCuratedChange,
}: {
  /** The item id — a layer slug or a raster uuid, per `itemKind`. */
  layerId: string
  /** Which curation seam to drive (raster-public slice added the raster twin). */
  itemKind?: 'layer' | 'raster'
  accessToken: string
  /** F6 deploy switch — Open Data publishing is only offered when the deployment has a public face. */
  publicEnabled: boolean
  /** Jump the dialog to the Metadata tab (the "complete metadata first" path). */
  onGoToMetadata: () => void
  /** Notify the parent when curation state flips (for any live catalog affordance). */
  onCuratedChange?: (curated: boolean) => void
}) {
  const [status, setStatus] = useState<CurationStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [confirming, setConfirming] = useState(false)

  const fetchStatus = itemKind === 'raster' ? fetchRasterCuration : fetchCuration
  const doCurate = itemKind === 'raster' ? curateRaster : curateLayer
  const doUncurate = itemKind === 'raster' ? uncurateRaster : uncurateLayer
  // The public-download copy differs: vectors export CSV/GeoJSON/Shapefile; a raster serves
  // GeoTIFF/COG (only while its downloadable flag is on — view-only rasters still stream).
  const downloadsCopy = itemKind === 'raster'
    ? 'GeoTIFF and Cloud-Optimized GeoTIFF (while downloads are allowed on this raster)'
    : 'CSV, GeoJSON, Shapefile, GeoPackage'

  useEffect(() => {
    let active = true
    fetchStatus(layerId, accessToken)
      .then((s) => active && setStatus(s))
      .catch((e) => active && setError(e instanceof ApiError ? e.message : String(e)))
    return () => {
      active = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- fetchStatus is derived from itemKind
  }, [layerId, itemKind, accessToken])

  async function run(action: 'curate' | 'uncurate'): Promise<void> {
    setBusy(true)
    setError(null)
    try {
      const s = action === 'curate'
        ? await doCurate(layerId, accessToken)
        : await doUncurate(layerId, accessToken)
      setStatus(s)
      setConfirming(false)
      onCuratedChange?.(s.curated)
    } catch (e) {
      // Re-sync status so the readiness (missing metadata / membership) re-renders accurately even
      // when the error body is opaque (the 422 metadata detail is a dict the client can't unwrap).
      try {
        setStatus(await fetchStatus(layerId, accessToken))
      } catch {
        /* keep the prior status */
      }
      setError(e instanceof ApiError && e.message ? e.message : 'Action failed. Please try again.')
    } finally {
      setBusy(false)
    }
  }

  const title = <p className="modal__section-title">Open Data</p>

  if (error && !status) {
    return (
      <section className="modal__section">
        {title}
        <p className="modal__field-error">{error}</p>
      </section>
    )
  }
  if (!status) {
    return (
      <section className="modal__section">
        {title}
        <p className="modal__muted">Loading…</p>
      </section>
    )
  }

  // F6 OFF: this deployment doesn't offer a public face, so Open Data publishing is unavailable.
  if (!publicEnabled) {
    return (
      <section className="modal__section">
        {title}
        <p className="modal__muted">
          Public access is turned off for this deployment, so Open Data publishing is unavailable.
        </p>
      </section>
    )
  }

  let body: React.ReactNode
  if (status.curated) {
    body = (
      <>
        <p className="curate__status curate__status--on">✓ Published to Open Data</p>
        <p className="share-dialog__hint">
          This dataset is publicly listed and downloadable. Removing it takes it out of the public
          catalog but does <strong>not</strong> make it private — change that under Sharing.
        </p>
        <button type="button" className="btn btn--sm" disabled={busy} onClick={() => void run('uncurate')}>
          {busy ? 'Removing…' : 'Remove from Open Data'}
        </button>
      </>
    )
  } else if (!status.canCurate) {
    body = (
      <p className="modal__muted">
        Only Open Data members or administrators can publish datasets. Ask an admin to add you to the
        Open Data group.
      </p>
    )
  } else if (!status.publicSharePermitted) {
    body = (
      <p className="modal__muted">
        Your organization doesn’t allow non-administrators to publish datasets publicly. Ask an
        administrator to publish this one, or to enable public sharing for non-admins.
      </p>
    )
  } else if (!status.metadataComplete) {
    body = (
      <>
        <p className="curate__status">Complete this layer’s metadata before publishing.</p>
        <p className="share-dialog__hint">Still needed: {status.missing.join(', ')}.</p>
        <button type="button" className="btn btn--sm" onClick={onGoToMetadata}>
          Complete metadata…
        </button>
      </>
    )
  } else if (confirming) {
    body = (
      <>
        <p className="curate__warn">
          ⚠ Publishing makes this dataset <strong>public on the internet</strong> — anyone can view
          and download it ({downloadsCopy}) with no sign-in. Editor names and internal
          references are stripped from public downloads. This is recorded in the audit log.
        </p>
        <div className="sign-form__actions">
          <button type="button" className="btn btn--sm" disabled={busy} onClick={() => setConfirming(false)}>
            Cancel
          </button>
          <button type="button" className="btn btn--sm" disabled={busy} onClick={() => void run('curate')}>
            {busy ? 'Publishing…' : 'Yes, publish publicly'}
          </button>
        </div>
      </>
    )
  } else {
    body = (
      <>
        <p className="share-dialog__hint">
          Publish this dataset to the public Open Data catalog — listed, previewable, and downloadable
          as {downloadsCopy}.
        </p>
        <button type="button" className="btn btn--sm" onClick={() => setConfirming(true)}>
          Curate into Open Data…
        </button>
      </>
    )
  }

  return (
    <section className="modal__section">
      {title}
      {body}
      {error && <p className="modal__field-error">{error}</p>}
    </section>
  )
}
