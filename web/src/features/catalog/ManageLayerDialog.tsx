import { useEffect, useState } from 'react'

import {
  ApiError,
  fetchLayerShares,
  fetchOrgSettings,
  fetchShareableGroups,
  setLayerShares,
} from '@/lib/api'
import type { MyLayer, ShareGroup, ShareLevel } from '@/lib/api'
import { copyToClipboard } from '@/lib/clipboard'
import { useIsAdmin } from '@/lib/auth/roles'

import AttachmentsToggle from './AttachmentsToggle'
import CreateViewSection from './CreateViewSection'
import CurateSection from './CurateSection'
import CustomFieldsSection from './CustomFieldsSection'
import DeleteProtectionToggle from './DeleteProtectionToggle'
import EditingToggle from './EditingToggle'
import ManageTabs, { type ManageTab } from './ManageTabs'
import MetadataSection from './MetadataSection'
import RelatesSection from './RelatesSection'
import SharingSection, { shareBadgeLevel } from './SharingSection'
import TransferOwnershipSection from './TransferOwnershipSection'
import UsedInSection from './UsedInSection'
import { UseInContent } from './UseInWizard'
import CloseButton from '@/components/CloseButton'

/**
 * Talus 25 — the "Manage" dialog for a catalog layer: Link · Sharing · Delete protection — now
 * identical in shape to ManageMapDialog (the deferred Talus 24 F3, shipped here). A layer
 * owner-or-admin shares the hosted layer to the whole org ("Organization Only"), to specific groups
 * they belong to, and/or to the public ("Public Access"); the catalog row's share-status badge
 * updates live via `onSharesChange`.
 *
 * This dialog is only opened for an owner-or-admin (CatalogView.openManageLayer gates on
 * `layer.canManage`; a non-manager gets a notice), so it assumes management is permitted — the server
 * re-enforces every write regardless. Unlike the map dialog there is **no** Review-sharing cascade: a
 * layer is a leaf (it references nothing), so making it public is a single direct action (decision §F2).
 * The Public tier shows only when the deployment enables it (`publicAnonAccessEnabled`, the F6 switch)
 * and is interactive only for admins or, by org policy, non-admins (`nonadminCanSharePublic`).
 */
export default function ManageLayerDialog({
  layer,
  accessToken,
  defaultTab,
  openImport,
  onProtectionChange,
  onSharesChange,
  onTransferred,
  onViewCreated,
  onClose,
}: {
  layer: MyLayer
  accessToken: string
  /** Which tab to open on (default 'sharing'). The create flow opens on 'fields' so a new layer lands
   * straight on schema setup. */
  defaultTab?: ManageTab
  /** When opening on the Fields tab, auto-expand the schema import panel (the create→import path). */
  openImport?: boolean
  onProtectionChange?: (value: boolean) => void
  onSharesChange?: (level: ShareLevel) => void
  onTransferred?: () => void
  /** Talus 38 — a view was authored from this layer in the Views tab; refresh the catalog so the new
   * view row appears (the dialog stays open). */
  onViewCreated?: () => void
  onClose: () => void
}) {
  const isAdmin = useIsAdmin()
  const name = layer.displayName ?? layer.id
  // Owner-defined custom fields apply only to claimed publisher slots (assets.tal_<8hex>); view
  // layers can't carry them (their schema derives from the source slot). The API re-enforces (slot regex).
  const isSlot = /^tal_[0-9a-f]{8}$/.test(layer.id)
  // Relates originate from SPATIAL layers only (v1 one-way, layer → table); tables and views
  // (which carry no geomType) don't get the tab. The API re-enforces the same rule.
  const isSpatial = layer.geomType === 'point' || layer.geomType === 'line' || layer.geomType === 'polygon'
  // Deep-link to the layer's Data view (the layer analog of the map's /map/:id link).
  const layerUrl = `${window.location.origin}/catalog/${layer.id}`

  const [copied, setCopied] = useState(false)
  const [groups, setGroups] = useState<ShareGroup[] | null>(null)
  // Mutually-exclusive sharing level (AGO-style); Groups are additive on top of it. 'owner' = private.
  const [level, setLevel] = useState<'owner' | 'org' | 'public'>('owner')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [publicEnabled, setPublicEnabled] = useState(false) // F6 deploy switch
  const [nonadminMayPublic, setNonadminMayPublic] = useState(false) // org policy
  const [nonadminMayTransfer, setNonadminMayTransfer] = useState(false) // org policy
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  // The dialog mounts fresh per open (CatalogView renders it only while a layer is selected), so the
  // initial value honors defaultTab without needing to react to later prop changes.
  const [tab, setTab] = useState<ManageTab>(defaultTab ?? 'sharing')

  useEffect(() => {
    let active = true
    Promise.all([
      fetchLayerShares(layer.id, accessToken),
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
  }, [layer.id, accessToken])

  // Whether THIS caller may toggle Public Access (server re-enforces): admins always; a non-admin
  // owner only when org policy allows. The tier itself only appears when the deployment enables it.
  const mayTogglePublic = isAdmin || nonadminMayPublic
  // Whether to offer Transfer ownership: admin always; a non-admin owner only when org policy allows
  // (the dialog is opened only by owner-or-admin, so a non-admin here IS the owner). Server enforces.
  const canTransfer = isAdmin || nonadminMayTransfer

  async function copyLink(): Promise<void> {
    // copyToClipboard falls back to execCommand for non-secure (http://) contexts where
    // navigator.clipboard is unavailable; only flash "Copied" on a real success.
    if (await copyToClipboard(layerUrl)) {
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

  async function save(): Promise<void> {
    setSaving(true)
    setError(null)
    // The radio level maps onto the two independent backend tiers; Owner Only clears both.
    const organizationOnly = level === 'org'
    const publicAccess = level === 'public'
    try {
      const updated = await setLayerShares(
        layer.id,
        { organizationOnly, groups: [...selected], publicAccess },
        accessToken,
      )
      // Patch the catalog row's badge live from the authoritative response — no reload (mirrors the
      // delete-protection padlock). No cascade for layers (a leaf, §F2).
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
        aria-label="Manage layer"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal__header">
          <h2 className="modal__title">Manage “{name}”</h2>
          <CloseButton onClick={onClose} />
        </div>

        {/* Native-CRS storage indicator (decision-crs-native-slice1.md): geometry is stored byte-exact in the
            layer's own CRS. Only shown for native layers; default (canonical-4326) layers show nothing. */}
        {layer.nativeStorage && (
          <p className="modal__muted" style={{ marginTop: 0 }}>
            <span title="Geometry is stored exactly in this projected CRS (no datum round-trip) — survey/high-accuracy.">
              🎯 Native storage · <strong>EPSG:{layer.workingSrid}</strong>
            </span>
          </p>
        )}

        {/* Views is layer-only and hidden when the layer is ITSELF a view (no view-of-a-view in v1). */}
        <ManageTabs
          active={tab}
          onChange={setTab}
          showOpenData
          showViews={layer.renderMode !== 'features'}
          showFields={isSlot}
          showRelates={isSpatial}
          showUseIn
        />

        {tab === 'sharing' && (
          <>
            <section className="modal__section">
              <div className="share-dialog__link">
                <div className="share-dialog__link-row">
                  <input
                    className="share-dialog__link-input"
                    type="text"
                    readOnly
                    value={layerUrl}
                    onFocus={(e) => e.currentTarget.select()}
                    aria-label="Link to this layer"
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
                  itemNoun="layer"
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
              <button
                type="button"
                className="btn btn--sm btn--ghost"
                onClick={onClose}
                disabled={saving}
              >
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
          <MetadataSection
            itemType="layer"
            itemId={layer.id}
            accessToken={accessToken}
            // Gated on F6: with no public face there is no Open Data to publish to.
            onPublishToOpenData={publicEnabled ? () => setTab('opendata') : undefined}
          />
        )}

        {/* Fields — define owner-defined custom attribute fields for this slot layer; own fetch/CRUD. */}
        {tab === 'fields' && (
          <CustomFieldsSection layer={layer} accessToken={accessToken} openImport={openImport} />
        )}

        {/* Relates — connect this layer to a table on a key (decision-relates-and-join-views.md
            PR A); navigation metadata only, surfaced in the record-detail drawer. */}
        {tab === 'relates' && <RelatesSection layer={layer} accessToken={accessToken} />}

        {/* Views — author a sanitized lens of this layer; its own fetch/POST (Talus 38). */}
        {tab === 'views' && (
          <CreateViewSection
            source={layer}
            accessToken={accessToken}
            onCreated={() => onViewCreated?.()}
            onDone={onClose}
          />
        )}

        {/* Open Data — the governed curate/publish control; its own fetch/POST (Talus 33). The
            metadata-incomplete path jumps to the Metadata tab above. */}
        {tab === 'opendata' && (
          <CurateSection
            layerId={layer.id}
            accessToken={accessToken}
            publicEnabled={publicEnabled}
            onGoToMetadata={() => setTab('metadata')}
          />
        )}

        {/* Use in… — the desktop/script connection assistant (Interop I-1); shared with the
            catalog-row wizard entry point. */}
        {tab === 'usein' && (
          <UseInContent
            layer={layer}
            accessToken={accessToken}
            isTable={layer.geomType === 'none'}
            onClose={onClose}
          />
        )}

        {tab === 'settings' && (
          <>
            {/* Delete protection — independent of sharing; toggles immediately (own PUT). */}
            <DeleteProtectionToggle
              itemType="layer"
              itemId={layer.id}
              itemNoun="layer"
              accessToken={accessToken}
              onChange={onProtectionChange}
            />

            {/* Editing — the owner-controlled read-only switch (decision-editing-toggle.md); own PUT. When
                off, editors lose affordances + writes 403, while admins still edit via the existing notice. */}
            <EditingToggle layerId={layer.id} accessToken={accessToken} />

            {/* Attachments — the owner-controlled "allow attachments" switch (decision-attachments.md A-1);
                own PUT. Default off (opt-in); a HARD gate — attachment uploads 403 while off, no admin override. */}
            <AttachmentsToggle layerId={layer.id} accessToken={accessToken} />

            {/* Where-used (decision-where-used.md §4.4) — what depends on this layer, beside the
                delete-protection it motivates. Viewer-filtered server-side; disclaimer inside. */}
            <UsedInSection kind="layer" itemId={layer.id} accessToken={accessToken} />

            {canTransfer && (
              <TransferOwnershipSection
                itemType="layer"
                itemId={layer.id}
                itemNoun="layer"
                currentOwnerId={layer.ownerId}
                accessToken={accessToken}
                onTransferred={() => {
                  onTransferred?.()
                  onClose()
                }}
              />
            )}
          </>
        )}
      </div>
    </div>
  )
}
