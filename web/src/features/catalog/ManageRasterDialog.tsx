import { useEffect, useState } from 'react'

import {
  ApiError,
  fetchOrgSettings,
  fetchRasterSettings,
  fetchRasterShares,
  fetchShareableGroups,
  setRasterSettings,
  setRasterShares,
} from '@/lib/api'
import type { RasterSummary, ShareGroup, ShareLevel } from '@/lib/api'
import { useIsAdmin } from '@/lib/auth/roles'

import CurateSection from './CurateSection'
import DeleteProtectionToggle from './DeleteProtectionToggle'
import ManageTabs, { type ManageTab } from './ManageTabs'
import MetadataSection from './MetadataSection'
import SharingSection, { shareBadgeLevel } from './SharingSection'
import TransferOwnershipSection from './TransferOwnershipSection'
import UsedInSection from './UsedInSection'
import CloseButton from '@/components/CloseButton'

/**
 * Manage dialog for a raster (V045+): the FULL four-tier sharing model (org/group/PUBLIC —
 * raster-public slice), metadata, Open Data curation, plus (V050) download governance and delete
 * protection. Grew tabs (the map-dialog pattern) when public sharing + metadata + curation joined:
 * Sharing · Metadata · Open Data · Settings.
 */
export default function ManageRasterDialog({
  raster,
  accessToken,
  onSharesChange,
  onProtectionChange,
  onTransferred,
  onClose,
}: {
  raster: RasterSummary
  accessToken: string
  onSharesChange?: (level: ShareLevel) => void
  onProtectionChange?: (value: boolean) => void
  onTransferred?: () => void
  onClose: () => void
}) {
  const isAdmin = useIsAdmin()
  const [tab, setTab] = useState<ManageTab>('sharing')
  const [groups, setGroups] = useState<ShareGroup[] | null>(null)
  // Download governance (V050): may /export (and WCS values) serve this raster? Immediate-save.
  const [downloadable, setDownloadable] = useState<boolean | null>(null)
  const [dlSaving, setDlSaving] = useState(false)
  const [dlError, setDlError] = useState<string | null>(null)
  const [level, setLevel] = useState<'owner' | 'org' | 'public'>('owner')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [publicEnabled, setPublicEnabled] = useState(false) // F6 deploy switch
  const [nonadminMayPublic, setNonadminMayPublic] = useState(false) // org policy
  const [nonadminMayTransfer, setNonadminMayTransfer] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    let active = true
    Promise.all([
      fetchRasterShares(raster.id, accessToken),
      fetchShareableGroups(accessToken),
      fetchOrgSettings(accessToken),
      fetchRasterSettings(raster.id, accessToken),
    ])
      .then(([shares, available, org, settings]) => {
        if (!active) return
        // Broadest tier wins: public ⊃ organization ⊃ owner.
        setLevel(shares.publicAccess ? 'public' : shares.organizationOnly ? 'org' : 'owner')
        setSelected(new Set(shares.groups.map((g) => g.group_id)))
        setGroups(available)
        setPublicEnabled(org.publicAnonAccessEnabled)
        setNonadminMayPublic(org.nonadminCanSharePublic)
        setNonadminMayTransfer(org.nonadminCanTransferOwnership)
        setDownloadable(settings.downloadable)
      })
      .catch((e) => active && setError(e instanceof ApiError ? e.message : String(e)))
    return () => {
      active = false
    }
  }, [raster.id, accessToken])

  async function toggleDownloadable(next: boolean): Promise<void> {
    setDlSaving(true)
    setDlError(null)
    setDownloadable(next) // optimistic
    try {
      const saved = await setRasterSettings(raster.id, { downloadable: next }, accessToken)
      setDownloadable(saved.downloadable)
    } catch (e) {
      setDownloadable(!next) // revert
      setDlError(e instanceof ApiError ? e.message : String(e))
    } finally {
      setDlSaving(false)
    }
  }

  // Public toggle: admins always; a non-admin owner only when org policy allows (server re-enforces).
  const mayTogglePublic = isAdmin || nonadminMayPublic
  // Transfer: admins always; a non-admin owner only when org policy allows.
  const canTransfer = isAdmin || nonadminMayTransfer

  function toggleGroup(id: string): void {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  async function save(): Promise<void> {
    setSaving(true)
    setError(null)
    try {
      const updated = await setRasterShares(
        raster.id,
        {
          organizationOnly: level === 'org',
          groups: [...selected],
          publicAccess: level === 'public',
        },
        accessToken,
      )
      // Patch the catalog row's badge live from the authoritative response.
      onSharesChange?.(shareBadgeLevel(updated))
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
        aria-label="Manage raster"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal__header">
          <h2 className="modal__title">Manage “{raster.title}”</h2>
          <CloseButton onClick={onClose} />
        </div>

        <ManageTabs active={tab} onChange={setTab} showOpenData />

        {tab === 'sharing' && (
          <>
            <section className="modal__section">
              {groups === null && error === null ? (
                <p className="modal__muted">Loading sharing…</p>
              ) : (
                <SharingSection
                  itemNoun="raster"
                  groups={groups ?? []}
                  level={level}
                  onLevelChange={setLevel}
                  selected={selected}
                  onToggleGroup={toggleGroup}
                  publicEnabled={publicEnabled}
                  mayTogglePublic={mayTogglePublic}
                />
              )}
              {error && <p className="modal__field-error">{error}</p>}
            </section>

            <div className="sign-form__actions">
              <button type="button" className="btn btn--sm" onClick={onClose} disabled={saving}>
                Cancel
              </button>
              <button
                type="button"
                className="btn btn--sm"
                onClick={() => void save()}
                disabled={saving || groups === null}
              >
                {saving ? 'Saving…' : 'Save'}
              </button>
            </div>
          </>
        )}

        {/* Metadata — required before Open Data curation (T31 gate); type-generic editor. */}
        {tab === 'metadata' && (
          <MetadataSection
            itemType="raster"
            itemId={raster.id}
            accessToken={accessToken}
            // Gated on F6: with no public face there is no Open Data to publish to.
            onPublishToOpenData={publicEnabled ? () => setTab('opendata') : undefined}
          />
        )}

        {/* Open Data — the governed raster publish (raster-public slice). */}
        {tab === 'opendata' && (
          <CurateSection
            layerId={raster.id}
            itemKind="raster"
            accessToken={accessToken}
            publicEnabled={publicEnabled}
            onGoToMetadata={() => setTab('metadata')}
          />
        )}

        {tab === 'settings' && (
          <>
            <section className="modal__section">
              <p className="modal__section-title">Download</p>
              {downloadable === null && dlError === null ? (
                <p className="modal__muted">Loading…</p>
              ) : (
                <label className="share-dialog__row">
                  <input
                    type="checkbox"
                    checked={downloadable ?? true}
                    disabled={dlSaving || downloadable === null}
                    onChange={(e) => void toggleDownloadable(e.target.checked)}
                  />
                  <span className="share-dialog__text">
                    <span className="share-dialog__name">Allow download / export</span>
                    <span className="share-dialog__hint">
                      When off, this raster can still be viewed on the map (and streamed as WMTS) but
                      its pixels can’t be downloaded or read as WCS values — for licensed imagery you
                      may display but not redistribute.
                    </span>
                  </span>
                </label>
              )}
              {dlError && <p className="modal__field-error">{dlError}</p>}
            </section>

            <DeleteProtectionToggle
              itemType="raster"
              itemId={raster.id}
              itemNoun="raster"
              accessToken={accessToken}
              onChange={onProtectionChange}
            />

            {/* Where-used (decision-where-used.md §4.4) — maps referencing this raster. */}
            <UsedInSection kind="raster" itemId={raster.id} accessToken={accessToken} />

            {canTransfer && (
              <section className="modal__section">
                <TransferOwnershipSection
                  itemType="raster"
                  itemId={raster.id}
                  itemNoun="raster"
                  currentOwnerId={raster.ownerId}
                  accessToken={accessToken}
                  onTransferred={() => {
                    onTransferred?.()
                    onClose()
                  }}
                />
              </section>
            )}
          </>
        )}
      </div>
    </div>
  )
}
