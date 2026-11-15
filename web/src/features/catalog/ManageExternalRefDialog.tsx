import { useEffect, useState } from 'react'

import { EXT_KIND_LABEL, EXT_PALETTE, probeExternalUrl } from '@/features/map/addFromUrl'
import {
  ApiError,
  fetchExternalRefShares,
  fetchOrgSettings,
  fetchShareableGroups,
  setExternalRefShares,
  updateExternalRef,
} from '@/lib/api'
import type { ExternalRefSummary, ShareGroup, ShareLevel } from '@/lib/api'
import { useIsAdmin } from '@/lib/auth/roles'

import SharingSection, { shareBadgeLevel } from './SharingSection'
import TransferOwnershipSection from './TransferOwnershipSection'
import CloseButton from '@/components/CloseButton'

/**
 * Manage dialog for a reference item (decision-external-catalog-items.md): Details (title,
 * description, URL, where, color) · Sharing (org/group/public, the layer parity set) · Transfer.
 * Editing the URL/where is the POINT of the hybrid model — a central fix here propagates to every
 * referencing map on its next open (ops fall back to their snapshot only when this item is gone).
 * The kind is immutable (a different protocol is a different reference). No delete-protection —
 * deletion is already harmless (maps heal via snapshot), so the guard-rail would protect nothing.
 * Opened only for canManage rows (CatalogView gates); the server re-enforces every write.
 */
export default function ManageExternalRefDialog({
  refItem,
  accessToken,
  onUpdated,
  onSharesChange,
  onTransferred,
  onClose,
}: {
  refItem: ExternalRefSummary
  accessToken: string
  /** Details saved — patch the catalog row live (title/description/color/url/where). */
  onUpdated?: (r: ExternalRefSummary) => void
  onSharesChange?: (level: ShareLevel) => void
  onTransferred?: () => void
  onClose: () => void
}) {
  const isAdmin = useIsAdmin()
  const isVector = refItem.externalKind === 'arcgis-feature' || refItem.externalKind === 'geojson'

  // Details (the mutable spec surface).
  const [title, setTitle] = useState(refItem.title)
  const [description, setDescription] = useState(refItem.description ?? '')
  const [url, setUrl] = useState(refItem.url)
  const [where, setWhere] = useState(refItem.where ?? '')
  const [color, setColor] = useState(refItem.color ?? EXT_PALETTE[0])
  const [detailsSaving, setDetailsSaving] = useState(false)
  const [detailsError, setDetailsError] = useState<string | null>(null)

  // Sharing (the ManageLayerDialog pattern: level radio + additive groups + gated public tier).
  const [groups, setGroups] = useState<ShareGroup[] | null>(null)
  const [level, setLevel] = useState<'owner' | 'org' | 'public'>('owner')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [publicEnabled, setPublicEnabled] = useState(false) // F6 deploy switch
  const [nonadminMayPublic, setNonadminMayPublic] = useState(false)
  const [nonadminMayTransfer, setNonadminMayTransfer] = useState(false)
  const [shareError, setShareError] = useState<string | null>(null)
  const [shareSaving, setShareSaving] = useState(false)

  useEffect(() => {
    let active = true
    Promise.all([
      fetchExternalRefShares(refItem.id, accessToken),
      fetchShareableGroups(accessToken),
      fetchOrgSettings(accessToken),
    ])
      .then(([shares, available, org]) => {
        if (!active) return
        setLevel(shares.publicAccess ? 'public' : shares.organizationOnly ? 'org' : 'owner')
        setSelected(new Set(shares.groups.map((g) => g.group_id)))
        setGroups(available)
        setPublicEnabled(org.publicAnonAccessEnabled)
        setNonadminMayPublic(org.nonadminCanSharePublic)
        setNonadminMayTransfer(org.nonadminCanTransferOwnership)
      })
      .catch((e) => active && setShareError(e instanceof ApiError ? e.message : String(e)))
    return () => {
      active = false
    }
  }, [refItem.id, accessToken])

  const mayTogglePublic = isAdmin || nonadminMayPublic
  const canTransfer = isAdmin || nonadminMayTransfer

  function toggleGroup(id: string): void {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  async function saveDetails(): Promise<void> {
    setDetailsSaving(true)
    setDetailsError(null)
    const nextUrl = url.trim()
    const nextWhere = where.trim()
    let geometryType = refItem.geometryType ?? null
    try {
      // Re-probe when the CONNECTION spec changed — the same honest paste-time check create runs
      // (browser-side only, the no-proxy fence), so a typo'd URL/where fails HERE instead of
      // silently blanking every referencing map. The kind is immutable (a different protocol is a
      // different reference), so a URL that probes as another kind is an error, not a repoint.
      // For arcgis-feature the where rides the probe as a /query URL, validating the PAIR against
      // the live service; geometryType refreshes from the probe (a repointed layer may differ).
      if (nextUrl !== refItem.url || nextWhere !== (refItem.where ?? '')) {
        const probeInput =
          refItem.externalKind === 'arcgis-feature' && nextWhere
            ? `${nextUrl}/query?where=${encodeURIComponent(nextWhere)}`
            : nextUrl
        const probe = await probeExternalUrl(probeInput)
        if (probe.externalKind !== refItem.externalKind) {
          throw new Error(
            `That URL is a ${EXT_KIND_LABEL[probe.externalKind]}, but this reference is a ` +
              `${EXT_KIND_LABEL[refItem.externalKind]} — create a new reference for a different service kind.`,
          )
        }
        if (probe.geometryType) geometryType = probe.geometryType
      }
      const updated = await updateExternalRef(
        refItem.id,
        {
          title: title.trim(),
          description: description.trim() || null,
          url: nextUrl,
          geometryType,
          ...(isVector ? { color } : {}),
          ...(nextWhere ? { where: nextWhere } : {}),
        },
        accessToken,
      )
      onUpdated?.({ ...refItem, ...updated })
      onClose()
    } catch (e) {
      setDetailsError(e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e))
      setDetailsSaving(false)
    }
  }

  async function saveShares(): Promise<void> {
    setShareSaving(true)
    setShareError(null)
    try {
      const updated = await setExternalRefShares(
        refItem.id,
        {
          organizationOnly: level === 'org',
          groups: [...selected],
          publicAccess: level === 'public',
        },
        accessToken,
      )
      onSharesChange?.(shareBadgeLevel(updated))
      onClose()
    } catch (e) {
      setShareError(e instanceof ApiError ? e.message : String(e))
      setShareSaving(false)
    }
  }

  return (
    <div className="modal__backdrop" role="presentation" onClick={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label="Manage reference"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal__header">
          <h2 className="modal__title">Manage “{refItem.title}”</h2>
          <CloseButton onClick={onClose} />
        </div>

        <section className="modal__section">
          <p className="modal__section-title">Details</p>
          <p className="modal__muted">
            {EXT_KIND_LABEL[refItem.externalKind]} — a read-only pointer to a remote service.
            Fixing the URL here updates every map that references this item.
          </p>
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
            placeholder="Description (optional)"
            onChange={(e) => setDescription(e.target.value)}
            aria-label="Description"
          />
          <input
            type="url"
            value={url}
            maxLength={2000}
            onChange={(e) => setUrl(e.target.value)}
            aria-label="Service URL"
          />
          {refItem.externalKind === 'arcgis-feature' && (
            <input
              type="text"
              value={where}
              maxLength={2000}
              placeholder="Filter (ArcGIS where-clause, optional)"
              onChange={(e) => setWhere(e.target.value)}
              aria-label="Where-clause filter"
            />
          )}
          {isVector && (
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
          {detailsError && <p className="modal__field-error">{detailsError}</p>}
          <div className="sign-form__actions">
            <button
              type="button"
              className="btn btn--sm"
              disabled={detailsSaving || !title.trim() || !url.trim()}
              onClick={() => void saveDetails()}
            >
              {detailsSaving ? 'Saving…' : 'Save details'}
            </button>
          </div>
        </section>

        <section className="modal__section">
          {groups === null && shareError === null ? (
            <p className="modal__muted">Loading sharing…</p>
          ) : (
            <SharingSection
              itemNoun="reference"
              groups={groups ?? []}
              level={level}
              onLevelChange={setLevel}
              selected={selected}
              onToggleGroup={toggleGroup}
              publicEnabled={publicEnabled}
              mayTogglePublic={mayTogglePublic}
            />
          )}
          {shareError && <p className="modal__field-error">{shareError}</p>}
          <div className="sign-form__actions">
            <button type="button" className="btn btn--sm" onClick={onClose} disabled={shareSaving}>
              Cancel
            </button>
            <button
              type="button"
              className="btn btn--sm"
              onClick={() => void saveShares()}
              disabled={shareSaving || groups === null}
            >
              {shareSaving ? 'Saving…' : 'Save sharing'}
            </button>
          </div>
        </section>

        {canTransfer && (
          <section className="modal__section">
            <TransferOwnershipSection
              itemType="external"
              itemId={refItem.id}
              itemNoun="reference"
              currentOwnerId={refItem.ownerId}
              accessToken={accessToken}
              onTransferred={() => {
                onTransferred?.()
                onClose()
              }}
            />
          </section>
        )}
      </div>
    </div>
  )
}
