import { type ChangeEvent, useEffect, useState } from 'react'

import {
  ApiError,
  fetchFieldProjectShares,
  fetchOrgSettings,
  fetchShareableGroups,
  patchFieldProject,
  regenerateFieldProject,
  replaceFieldProject,
  setFieldProjectShares,
} from '@/lib/api'
import type { FieldProjectSummary, ShareGroup, ShareLevel } from '@/lib/api'
import { useIsAdmin } from '@/lib/auth/roles'

import DeleteProtectionToggle from './DeleteProtectionToggle'
import SharingSection, { shareBadgeLevel } from './SharingSection'
import TransferOwnershipSection from './TransferOwnershipSection'
import CloseButton from '@/components/CloseButton'

/**
 * Manage dialog for a field project (V085, decision-field-projects.md). Stacked sections (the
 * external-refs shape — no Metadata/Open-Data registration for this type in v1): Details
 * (rename/describe), Replace (the F4 replace-in-place update path), Sharing (org/groups — NO
 * public tier: the artifact may embed access credentials and the server refuses publicAccess),
 * delete protection, transfer.
 */
export default function ManageFieldProjectDialog({
  project,
  accessToken,
  onSharesChange,
  onProtectionChange,
  onChanged,
  onTransferred,
  onClose,
}: {
  project: FieldProjectSummary
  accessToken: string
  onSharesChange?: (level: ShareLevel) => void
  onProtectionChange?: (value: boolean) => void
  /** Details saved or artifact replaced — parent refetches the list. */
  onChanged?: () => void
  onTransferred?: () => void
  onClose: () => void
}) {
  const isAdmin = useIsAdmin()
  const [title, setTitle] = useState(project.title)
  const [description, setDescription] = useState(project.description ?? '')
  const [detailsSaving, setDetailsSaving] = useState(false)
  const [detailsError, setDetailsError] = useState<string | null>(null)

  const [replaceFile, setReplaceFile] = useState<File | null>(null)
  const [replaceBusy, setReplaceBusy] = useState(false)
  const [replaceError, setReplaceError] = useState<string | null>(null)
  const [replaceDone, setReplaceDone] = useState(false)

  const [regenBusy, setRegenBusy] = useState(false)
  const [regenError, setRegenError] = useState<string | null>(null)
  const [regenDone, setRegenDone] = useState(false)

  const [groups, setGroups] = useState<ShareGroup[] | null>(null)
  const [level, setLevel] = useState<'owner' | 'org' | 'public'>('owner')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [nonadminMayTransfer, setNonadminMayTransfer] = useState(false)
  const [shareError, setShareError] = useState<string | null>(null)
  const [shareSaving, setShareSaving] = useState(false)

  useEffect(() => {
    let active = true
    Promise.all([
      fetchFieldProjectShares(project.id, accessToken),
      fetchShareableGroups(accessToken),
      fetchOrgSettings(accessToken),
    ])
      .then(([shares, available, org]) => {
        if (!active) return
        setLevel(shares.organizationOnly ? 'org' : 'owner')
        setSelected(new Set(shares.groups.map((g) => g.group_id)))
        setGroups(available)
        setNonadminMayTransfer(org.nonadminCanTransferOwnership)
      })
      .catch((e) => active && setShareError(e instanceof ApiError ? e.message : String(e)))
    return () => {
      active = false
    }
  }, [project.id, accessToken])

  const canTransfer = isAdmin || nonadminMayTransfer

  async function saveDetails(): Promise<void> {
    setDetailsSaving(true)
    setDetailsError(null)
    try {
      await patchFieldProject(
        project.id,
        { title: title.trim(), description: description.trim() },
        accessToken,
      )
      onChanged?.()
    } catch (e) {
      setDetailsError(e instanceof ApiError ? e.message : String(e))
    } finally {
      setDetailsSaving(false)
    }
  }

  function onPickReplacement(e: ChangeEvent<HTMLInputElement>): void {
    setReplaceFile(e.target.files?.[0] ?? null)
    setReplaceDone(false)
    setReplaceError(null)
  }

  async function replaceArtifact(): Promise<void> {
    if (!replaceFile) return
    setReplaceBusy(true)
    setReplaceError(null)
    try {
      await replaceFieldProject(project.id, replaceFile, accessToken)
      setReplaceDone(true)
      setReplaceFile(null)
      onChanged?.()
    } catch (e) {
      setReplaceError(e instanceof ApiError ? e.message : String(e))
    } finally {
      setReplaceBusy(false)
    }
  }

  async function regenerate(): Promise<void> {
    setRegenBusy(true)
    setRegenError(null)
    setRegenDone(false)
    try {
      await regenerateFieldProject(project.id, accessToken)
      setRegenDone(true)
      onChanged?.()
    } catch (e) {
      setRegenError(e instanceof ApiError ? e.message : String(e))
    } finally {
      setRegenBusy(false)
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

  async function saveShares(): Promise<void> {
    setShareSaving(true)
    setShareError(null)
    try {
      const updated = await setFieldProjectShares(
        project.id,
        { organizationOnly: level === 'org', groups: [...selected] },
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
        aria-label="Manage field project"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal__header">
          <h2 className="modal__title">Manage “{project.title}”</h2>
          <CloseButton onClick={onClose} />
        </div>

        <section className="modal__section">
          <p className="modal__section-title">Details</p>
          {project.sourceWebMapId ? (
            <p className="modal__muted">
              Generated from a web map — the map is the source of truth. When the map changes,
              use Regenerate below (Talus never edits the .qgz; it recompiles it). Renaming does
              not change the served file.
            </p>
          ) : (
            <p className="modal__muted">
              A stored .qgz served to QField’s Import URL. Talus never opens or edits it — replace
              the file below to publish an update. Renaming does not change the served file.
            </p>
          )}
          <input
            type="text"
            value={title}
            maxLength={80}
            onChange={(e) => setTitle(e.target.value)}
            aria-label="Field project title"
          />
          <textarea
            value={description}
            maxLength={2000}
            rows={2}
            placeholder="Description (optional)"
            onChange={(e) => setDescription(e.target.value)}
            aria-label="Description"
          />
          {detailsError && <p className="modal__field-error">{detailsError}</p>}
          <div className="sign-form__actions">
            <button
              type="button"
              className="btn btn--sm"
              disabled={detailsSaving || !title.trim()}
              onClick={() => void saveDetails()}
            >
              {detailsSaving ? 'Saving…' : 'Save details'}
            </button>
          </div>
        </section>

        {project.sourceWebMapId && (
          <section className="modal__section">
            <p className="modal__section-title">Regenerate from the web map</p>
            <p className="modal__muted">
              Recompiles this project from the source map as it is now — new layers, colors and
              extent ride in; the embedded access token is reused, so already-imported copies
              keep working. Crews still delete the old copy on the phone and re-import to get
              the new version.
            </p>
            {regenDone && (
              <p className="modal__muted">Regenerated — crews get the new file on next import.</p>
            )}
            {regenError && <p className="modal__field-error">{regenError}</p>}
            <div className="sign-form__actions">
              <button
                type="button"
                className="btn btn--sm"
                disabled={regenBusy}
                onClick={() => void regenerate()}
              >
                {regenBusy ? 'Regenerating…' : 'Regenerate'}
              </button>
            </div>
          </section>
        )}

        <section className="modal__section">
          <p className="modal__section-title">Replace project file</p>
          <p className="modal__muted">
            Uploads a new .qgz in place — the Import URL stays the same, and crews pick up the
            new version on their next import (remind them: delete the old copy on the phone
            first, or the import lands as a duplicate).
          </p>
          <input type="file" accept=".qgz" onChange={onPickReplacement} />
          {replaceFile && (
            <p className="modal__muted">
              {replaceFile.name} · {(replaceFile.size / (1024 * 1024)).toFixed(1)} MB
            </p>
          )}
          {replaceDone && <p className="modal__muted">Replaced — crews get the new file on next import.</p>}
          {replaceError && <p className="modal__field-error">{replaceError}</p>}
          <div className="sign-form__actions">
            <button
              type="button"
              className="btn btn--sm"
              disabled={replaceBusy || replaceFile === null}
              onClick={() => void replaceArtifact()}
            >
              {replaceBusy ? 'Uploading…' : 'Replace'}
            </button>
          </div>
        </section>

        <section className="modal__section">
          {groups === null && shareError === null ? (
            <p className="modal__muted">Loading sharing…</p>
          ) : (
            <SharingSection
              itemNoun="field project"
              groups={groups ?? []}
              level={level}
              onLevelChange={setLevel}
              selected={selected}
              onToggleGroup={toggleGroup}
              publicEnabled={false} // no public tier: the artifact may embed access credentials (F3)
              mayTogglePublic={false}
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

        <DeleteProtectionToggle
          itemType="field_project"
          itemId={project.id}
          itemNoun="field project"
          accessToken={accessToken}
          onChange={onProtectionChange}
        />

        {canTransfer && (
          <section className="modal__section">
            <TransferOwnershipSection
              itemType="field_project"
              itemId={project.id}
              itemNoun="field project"
              currentOwnerId={project.ownerId}
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
