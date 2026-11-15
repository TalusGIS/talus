import { useEffect, useState } from 'react'

import {
  ApiError,
  fetchOrgSettings,
  fetchShareableGroups,
  fetchWebMapPublicLayerReview,
  fetchWebMapShares,
  setLayerPublicShare,
  setRasterPublicShare,
  setWebMapShares,
} from '@/lib/api'
import type {
  PublicLayerReviewItem,
  PublicRasterReviewItem,
  ShareGroup,
  ShareLevel,
  WebMapSummary,
} from '@/lib/api'
import { copyToClipboard } from '@/lib/clipboard'
import { useIsAdmin, useIsPublisher } from '@/lib/auth/roles'

import DeleteProtectionToggle from './DeleteProtectionToggle'
import MakeFieldProjectSection from './MakeFieldProjectSection'
import ManageTabs, { type ManageTab } from './ManageTabs'
import MetadataSection from './MetadataSection'
import SharingSection, { shareBadgeLevel } from './SharingSection'
import TransferOwnershipSection from './TransferOwnershipSection'
import CloseButton from '@/components/CloseButton'

/**
 * Talus 21 + 23 + 24 — the "Manage" dialog for a saved web map: Link · Sharing · Delete protection
 * (Talus 24 F2 consolidates an owner's controls into one surface). Sharing audiences: "Organization
 * Only" (everyone signed in to the org), specific groups the caller is a member of, and (Talus 23)
 * "Public Access" — anonymous, public-internet visitors.
 *
 * The Public Access tier is shown only when the deployment enables public access
 * (`publicAnonAccessEnabled`, the F6 switch) and is interactive only when the caller may use it
 * (admins always; non-admin owners only when `nonadminCanSharePublic` is on). The server is the
 * authority on all of this — the dialog is a faithful mirror, not the gate.
 *
 * Two phases:
 *  - "share": the main Manage surface — link, sharing audiences, delete protection.
 *  - "review" (the AGO Review-sharing cascade, c3b): if the saved map is public but some of its
 *    layers are NOT public, the map renders blank for the public — so we prompt to publish those
 *    layers too. Layers the caller can't manage (e.g. ownerless system layers, for a non-admin)
 *    are listed as needing an administrator. Each layer is published independently (F5).
 */
export default function ManageMapDialog({
  map,
  accessToken,
  onProtectionChange,
  onSharesChange,
  onTransferred,
  onFieldProjectGenerated,
  onClose,
}: {
  map: WebMapSummary
  accessToken: string
  onProtectionChange?: (value: boolean) => void
  onSharesChange?: (level: ShareLevel) => void
  onTransferred?: () => void
  onFieldProjectGenerated?: () => void
  onClose: () => void
}) {
  const isAdmin = useIsAdmin()
  const isPublisher = useIsPublisher()
  const [groups, setGroups] = useState<ShareGroup[] | null>(null)
  // Mutually-exclusive sharing level (AGO-style); Groups below are additive on top of it.
  // 'owner' = private to the owner (the explicit un-share).
  const [level, setLevel] = useState<'owner' | 'org' | 'public'>('owner')
  const [embedCopied, setEmbedCopied] = useState(false) // embed-snippet copy flash (decision-map-embed.md F5)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [publicEnabled, setPublicEnabled] = useState(false) // F6 deploy switch
  const [nonadminMayPublic, setNonadminMayPublic] = useState(false) // org policy
  const [nonadminMayTransfer, setNonadminMayTransfer] = useState(false) // org policy
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  // Review phase (the make-public cascade). Rasters joined the review (raster-public slice);
  // the checked set keys raster ids with a 'raster:' prefix so one set serves both lists.
  const [phase, setPhase] = useState<'share' | 'review'>('share')
  const [reviewLayers, setReviewLayers] = useState<PublicLayerReviewItem[]>([])
  const [reviewRasters, setReviewRasters] = useState<PublicRasterReviewItem[]>([])
  const [reviewChecked, setReviewChecked] = useState<Set<string>>(new Set())
  const [publishing, setPublishing] = useState(false)
  const [copied, setCopied] = useState(false)
  const [tab, setTab] = useState<ManageTab>('sharing')

  useEffect(() => {
    let active = true
    Promise.all([
      fetchWebMapShares(map.id, accessToken),
      fetchShareableGroups(accessToken),
      fetchOrgSettings(accessToken),
    ])
      .then(([shares, available, org]) => {
        if (!active) return
        // Broadest tier wins: public ⊃ organization ⊃ owner.
        setLevel(shares.publicAccess ? 'public' : shares.organizationOnly ? 'org' : 'owner')
        setSelected(new Set(shares.groups.map((g) => g.group_id)))
        setGroups(available)
        setPublicEnabled(org.publicAnonAccessEnabled)
        setNonadminMayPublic(org.nonadminCanSharePublic)
        setNonadminMayTransfer(org.nonadminCanTransferOwnership)
      })
      .catch((e) => active && setError(e instanceof ApiError ? e.message : String(e)))
    return () => {
      active = false
    }
  }, [map.id, accessToken])

  // Whether THIS caller may toggle Public Access (server re-enforces): admins always; a non-admin
  // owner only when org policy allows. The tier itself only appears when the deployment enables it.
  const mayTogglePublic = isAdmin || nonadminMayPublic
  // Offer Transfer ownership: admin always; a non-admin owner only when org policy allows (the dialog
  // is opened only by owner-or-admin, so a non-admin here IS the owner). Server enforces.
  const canTransfer = isAdmin || nonadminMayTransfer

  // A direct deep-link to open this map in the portal (respects the recipient's access — they open
  // it only if it's shared to them or public). Lets an owner drop the link into Teams/email.
  const mapUrl = `${window.location.origin}/map/${map.id}`
  async function copyLink(): Promise<void> {
    // copyToClipboard falls back to execCommand for non-secure (http://) contexts where
    // navigator.clipboard is unavailable; only flash "Copied" on a real success.
    if (await copyToClipboard(mapUrl)) {
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    }
  }

  function toggleGroup(id: string): void {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  function toggleReview(id: string): void {
    setReviewChecked((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  async function save(): Promise<void> {
    setSaving(true)
    setError(null)
    // The radio level maps onto the two independent backend tiers; Owner Only clears both.
    const organizationOnly = level === 'org'
    const publicAccess = level === 'public'
    try {
      const updated = await setWebMapShares(
        map.id,
        { organizationOnly, groups: [...selected], publicAccess },
        accessToken,
      )
      // Patch the catalog row's badge live from the authoritative response — no reload (mirrors the
      // delete-protection padlock). Fires before the cascade branch, since the share IS saved here.
      onSharesChange?.(shareBadgeLevel(updated))
      // Cascade: a public map whose layers aren't public renders blank for the public. Always
      // prompt when public + any non-public layer (decision §F3b — surface the gap every time).
      if (publicAccess) {
        try {
          const review = await fetchWebMapPublicLayerReview(map.id, accessToken)
          const nonPublic = review.layers.filter((l) => !l.isPublic)
          const nonPublicRasters = review.rasters.filter((r) => !r.isPublic)
          if (nonPublic.length > 0 || nonPublicRasters.length > 0) {
            setReviewLayers(nonPublic)
            setReviewRasters(nonPublicRasters)
            setReviewChecked(new Set([
              ...nonPublic.filter((l) => l.canManage).map((l) => l.layerId),
              ...nonPublicRasters.filter((r) => r.canManage).map((r) => `raster:${r.rasterId}`),
            ]))
            setPhase('review')
            setSaving(false)
            return
          }
        } catch {
          // The map IS saved/public; the cascade is best-effort. Fall through to close.
        }
      }
      onClose()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
      setSaving(false)
    }
  }

  async function updateLayerSharing(): Promise<void> {
    setPublishing(true)
    setError(null)
    try {
      for (const key of reviewChecked) {
        if (key.startsWith('raster:')) {
          await setRasterPublicShare(key.slice('raster:'.length), true, accessToken)
        } else {
          await setLayerPublicShare(key, true, accessToken)
        }
      }
      onClose()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
      setPublishing(false)
    }
  }

  const updatable = reviewLayers.filter((l) => l.canManage)
  const needsAdmin = reviewLayers.filter((l) => !l.canManage)
  const updatableRasters = reviewRasters.filter((r) => r.canManage)
  const needsAdminRasters = reviewRasters.filter((r) => !r.canManage)

  return (
    <div className="modal__backdrop" role="presentation" onClick={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label={phase === 'review' ? 'Review sharing' : 'Manage map'}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal__header">
          <h2 className="modal__title">
            {phase === 'review' ? 'Review sharing' : `Manage “${map.title}”`}
          </h2>
          <CloseButton onClick={onClose} />
        </div>

        {phase === 'share' ? (
          <>
            <ManageTabs active={tab} onChange={setTab} />

            {tab === 'sharing' && (
              <>
                <section className="modal__section">
                  <div className="share-dialog__link">
                    <div className="share-dialog__link-row">
                      <input
                        className="share-dialog__link-input"
                        type="text"
                        readOnly
                        value={mapUrl}
                        onFocus={(e) => e.currentTarget.select()}
                        aria-label="Link to this map"
                      />
                      <button type="button" className="btn btn--sm" onClick={() => void copyLink()}>
                        {copied ? 'Copied' : 'Copy'}
                      </button>
                    </div>
                  </div>

                  {groups === null && error === null ? (
                    <p className="modal__muted">Loading sharing…</p>
                  ) : (
                    <SharingSection
                      itemNoun="map"
                      groups={groups ?? []}
                      level={level}
                      onLevelChange={setLevel}
                      selected={selected}
                      onToggleGroup={toggleGroup}
                      publicEnabled={publicEnabled}
                      mayTogglePublic={mayTogglePublic}
                    />
                  )}
                  {/* Embed snippet (decision-map-embed.md F5): shown only at the Public tier. The
                      map's PUBLIC STATE is the whole gate — flipping it private kills every embed.
                      No tokens, no options; the /embed route is the chrome-less frameable viewer. */}
                  {level === 'public' && publicEnabled && (
                    <div className="modal__field" style={{ marginTop: '0.6rem' }}>
                      <span className="modal__label">Embed on another website</span>
                      <textarea
                        className="sign-form__input"
                        readOnly
                        rows={3}
                        style={{ fontFamily: 'var(--font-mono, monospace)', fontSize: '0.75rem' }}
                        value={`<iframe src="${window.location.origin}/embed/map/${map.id}" width="800" height="500" style="border:0" loading="lazy" allowfullscreen></iframe>`}
                        onFocus={(e) => e.currentTarget.select()}
                      />
                      <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                        <button
                          type="button"
                          className="btn btn--sm"
                          onClick={() => {
                            void copyToClipboard(
                              `<iframe src="${window.location.origin}/embed/map/${map.id}" width="800" height="500" style="border:0" loading="lazy" allowfullscreen></iframe>`,
                            ).then((ok) => {
                              setEmbedCopied(ok)
                              window.setTimeout(() => setEmbedCopied(false), 2000)
                            })
                          }}
                        >
                          Copy embed code
                        </button>
                        {embedCopied && <span className="modal__muted">Copied ✓</span>}
                      </div>
                      <p className="modal__muted">
                        Anyone can view this map while it is shared publicly (save sharing first if
                        you just changed it).
                      </p>
                    </div>
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

            {/* Metadata — ISO-cored required-core editor; its own fetch/PUT (Talus 31). */}
            {tab === 'metadata' && (
              <MetadataSection itemType="web_map" itemId={map.id} accessToken={accessToken} />
            )}

            {tab === 'settings' && (
              <>
                {/* Make field project (decision-generate-field-project.md F5) — publisher-gated;
                    the server re-enforces (_require_publisher), this is UI gating only. */}
                {isPublisher && (
                  <MakeFieldProjectSection
                    map={map}
                    accessToken={accessToken}
                    onGenerated={onFieldProjectGenerated}
                  />
                )}

                {/* Delete protection — independent of sharing; toggles immediately (own PUT). */}
                <DeleteProtectionToggle
                  itemType="web_map"
                  itemId={map.id}
                  itemNoun="map"
                  accessToken={accessToken}
                  onChange={onProtectionChange}
                />

                {canTransfer && (
                  <TransferOwnershipSection
                    itemType="web_map"
                    itemId={map.id}
                    itemNoun="map"
                    currentOwnerId={map.ownerId}
                    accessToken={accessToken}
                    onTransferred={() => {
                      onTransferred?.()
                      onClose()
                    }}
                  />
                )}
              </>
            )}
          </>
        ) : (
          <>
            <section className="modal__section">
              <p className="share-dialog__hint">
                “{map.title}” is now public, but these items aren’t — they won’t be visible to the
                public until they’re shared publicly too.
              </p>

              {(updatable.length > 0 || updatableRasters.length > 0) && (
                <>
                  <p className="modal__section-title share-dialog__groups-title">
                    Make these public too
                  </p>
                  {updatable.map((l) => (
                    <label key={l.layerId} className="share-dialog__row">
                      <input
                        type="checkbox"
                        checked={reviewChecked.has(l.layerId)}
                        onChange={() => toggleReview(l.layerId)}
                      />
                      <span className="share-dialog__text">
                        <span className="share-dialog__name">{l.title}</span>
                      </span>
                    </label>
                  ))}
                  {updatableRasters.map((r) => (
                    <label key={r.rasterId} className="share-dialog__row">
                      <input
                        type="checkbox"
                        checked={reviewChecked.has(`raster:${r.rasterId}`)}
                        onChange={() => toggleReview(`raster:${r.rasterId}`)}
                      />
                      <span className="share-dialog__text">
                        <span className="share-dialog__name">{r.title}</span>
                        <span className="share-dialog__hint">Raster</span>
                      </span>
                    </label>
                  ))}
                </>
              )}

              {(needsAdmin.length > 0 || needsAdminRasters.length > 0) && (
                <>
                  <p className="modal__section-title share-dialog__groups-title">
                    Needs an administrator
                  </p>
                  {needsAdmin.map((l) => (
                    <div key={l.layerId} className="share-dialog__row share-dialog__row--disabled">
                      <span className="share-dialog__text">
                        <span className="share-dialog__name">{l.title}</span>
                        <span className="share-dialog__hint">
                          You can’t make this layer public — ask an administrator.
                        </span>
                      </span>
                    </div>
                  ))}
                  {needsAdminRasters.map((r) => (
                    <div key={r.rasterId} className="share-dialog__row share-dialog__row--disabled">
                      <span className="share-dialog__text">
                        <span className="share-dialog__name">{r.title}</span>
                        <span className="share-dialog__hint">
                          You can’t make this raster public — ask an administrator.
                        </span>
                      </span>
                    </div>
                  ))}
                </>
              )}
              {error && <p className="modal__field-error">{error}</p>}
            </section>

            <div className="sign-form__actions">
              <button
                type="button"
                className="btn btn--sm"
                onClick={onClose}
                disabled={publishing}
              >
                Don’t update sharing
              </button>
              <button
                type="button"
                className="btn btn--sm"
                onClick={() => void updateLayerSharing()}
                disabled={publishing || reviewChecked.size === 0}
              >
                {publishing ? 'Updating…' : 'Update sharing'}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
