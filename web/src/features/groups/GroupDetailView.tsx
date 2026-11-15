import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import { useAuth } from 'react-oidc-context'
import { ArrowLeft } from 'lucide-react'

import {
  ApiError,
  addGroupItems,
  addGroupMembers,
  deleteGroup,
  fetchGroupDetail,
  fetchMyLayers,
  fetchMyRasters,
  fetchMyWebMaps,
  fetchTransferTargets,
  removeGroupItem,
  removeGroupMember,
  transferGroup,
  updateGroup,
} from '@/lib/api'
import type { GroupContentItem, GroupDetail, ShareableItemType } from '@/lib/api'

type Tab = 'overview' | 'members' | 'content'

/**
 * A single group's page (Groups management UI, decision-groups-ui.md). Overview / Members / Content
 * tabs, AGO-shaped but the on-thesis subset. Manage affordances (edit, delete, add/remove members,
 * transfer, add/remove content) render only for the owner or an admin — `myRole` is server-given and
 * every action is re-enforced server-side. The Content tab shows each layer's edit legibility for the
 * viewing member (the three-key model), so "editors + viewers in one group" is visible, not hidden.
 */
export default function GroupDetailView() {
  const { groupId = '' } = useParams()
  const auth = useAuth()
  const token = auth.user?.access_token ?? ''
  const navigate = useNavigate()

  const [group, setGroup] = useState<GroupDetail | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [tab, setTab] = useState<Tab>('overview')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  function refresh(): void {
    fetchGroupDetail(groupId, token)
      .then(setGroup)
      .catch((e) =>
        setLoadError(
          e instanceof ApiError
            ? e.status === 404
              ? 'This group doesn’t exist, or you don’t have access to it.'
              : `Couldn’t load the group (HTTP ${e.status})`
            : 'Couldn’t load the group',
        ),
      )
  }
  useEffect(() => {
    refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, groupId])

  /** Run a mutation, surface errors, refresh on success. */
  async function run(fn: () => Promise<unknown>): Promise<void> {
    setBusy(true)
    setError(null)
    try {
      await fn()
      refresh()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Action failed')
    } finally {
      setBusy(false)
    }
  }

  if (loadError !== null)
    return (
      <div className="page">
        <p>
          <Link className="backlink" to="/groups">
            <ArrowLeft size={13} aria-hidden="true" />
            Groups
          </Link>
        </p>
        <p className="sign-form__error">{loadError}</p>
      </div>
    )
  if (group === null) return <div className="page"><p className="modal__muted">Loading…</p></div>

  const canManage = group.myRole === 'owner' || group.myRole === 'admin'

  return (
    <div className="page">
      <p>
          <Link className="backlink" to="/groups">
            <ArrowLeft size={13} aria-hidden="true" />
            Groups
          </Link>
        </p>
      <div className="page__head">
        <h1>
          {group.displayName}
          {group.system && <span className="modal__muted"> · system group</span>}
        </h1>
      </div>
      <p className="modal__muted">
        {group.owner ? <>Owned by {group.owner.displayName}</> : 'Administrator-managed'}
        {' · '}{group.members.length} member{group.members.length === 1 ? '' : 's'}
        {group.myRole && <> · you are {group.myRole === 'admin' ? 'an administrator' : group.myRole === 'owner' ? 'the owner' : 'a member'}</>}
        {group.deleteProtected && <> · 🔒 protected</>}
      </p>

      <div className="modal__tabs" role="tablist">
        {(['overview', 'members', 'content'] as Tab[]).map((t) => (
          <button
            key={t}
            type="button"
            role="tab"
            aria-selected={tab === t}
            className={`modal__tab${tab === t ? ' modal__tab--active' : ''}`}
            onClick={() => setTab(t)}
          >
            {t === 'overview' ? 'Overview' : t === 'members' ? `Members (${group.members.length})` : 'Content'}
          </button>
        ))}
      </div>

      {error !== null && <p className="sign-form__error">{error}</p>}

      {tab === 'overview' && (
        <OverviewTab group={group} canManage={canManage} busy={busy} token={token} run={run} onDeleted={() => navigate('/groups')} />
      )}
      {tab === 'members' && (
        <MembersTab group={group} canManage={canManage} busy={busy} token={token} run={run} />
      )}
      {tab === 'content' && (
        <ContentTab group={group} canManage={canManage} busy={busy} token={token} run={run} />
      )}
    </div>
  )
}

// --- Overview -------------------------------------------------------------------

function OverviewTab({
  group, canManage, busy, token, run, onDeleted,
}: {
  group: GroupDetail; canManage: boolean; busy: boolean; token: string
  run: (fn: () => Promise<unknown>) => Promise<void>; onDeleted: () => void
}) {
  const [editing, setEditing] = useState(false)
  const [name, setName] = useState(group.displayName)
  const [summary, setSummary] = useState(group.summary ?? '')
  const [protect, setProtect] = useState(group.deleteProtected)
  const [confirmDelete, setConfirmDelete] = useState(false)

  return (
    <section className="modal__section">
      <h3 className="modal__section-title">Summary</h3>
      {!editing ? (
        <p className={group.summary ? '' : 'modal__muted'}>{group.summary || 'No summary.'}</p>
      ) : (
        <>
          <label className="sign-form__field">
            <span>Name *</span>
            <input type="text" value={name} onChange={(e) => setName(e.target.value)} />
          </label>
          <label className="sign-form__field">
            <span>Summary</span>
            <input type="text" value={summary} onChange={(e) => setSummary(e.target.value)} />
          </label>
          <label className="share-dialog__row">
            <input type="checkbox" checked={protect} onChange={(e) => setProtect(e.target.checked)} />
            <span className="share-dialog__text">
              <span className="share-dialog__name">Protect from deletion</span>
              <span className="share-dialog__hint">
                While on, this group can’t be deleted by anyone — including administrators — until you turn protection off.
              </span>
            </span>
          </label>
          <div className="sign-form__actions">
            <button type="button" className="btn btn--sm" disabled={busy || name.trim() === ''}
              onClick={() => void run(() => updateGroup(group.groupId, { displayName: name.trim(), summary: summary.trim(), deleteProtected: protect }, token)).then(() => setEditing(false))}>
              {busy ? 'Saving…' : 'Save'}
            </button>
            <button type="button" className="btn btn--ghost btn--sm" onClick={() => setEditing(false)}>Cancel</button>
          </div>
        </>
      )}

      {canManage && !editing && (
        <div className="sign-form__actions" style={{ marginTop: '1rem' }}>
          {!group.system && (
            <button type="button" className="btn btn--sm" onClick={() => { setName(group.displayName); setSummary(group.summary ?? ''); setProtect(group.deleteProtected); setEditing(true) }}>
              Edit details
            </button>
          )}
          {!group.system && !confirmDelete && (
            group.deleteProtected ? (
              <span className="modal__muted">🔒 Protected from deletion — uncheck it in Edit details to delete.</span>
            ) : (
              <button type="button" className="btn btn--danger btn--sm" onClick={() => setConfirmDelete(true)}>Delete group</button>
            )
          )}
          {confirmDelete && (
            <>
              <span className="modal__muted">Delete this group? Members lose access to its shared content.</span>
              <button type="button" className="btn btn--danger btn--sm" disabled={busy}
                onClick={() => void run(() => deleteGroup(group.groupId, token)).then(onDeleted)}>
                {busy ? 'Deleting…' : 'Confirm delete'}
              </button>
              <button type="button" className="btn btn--ghost btn--sm" onClick={() => setConfirmDelete(false)}>Cancel</button>
            </>
          )}
        </div>
      )}
      {group.system && <p className="modal__muted">System groups are managed by Talus and can’t be renamed or deleted here.</p>}
    </section>
  )
}

// --- Members --------------------------------------------------------------------

function MembersTab({
  group, canManage, busy, token, run,
}: {
  group: GroupDetail; canManage: boolean; busy: boolean; token: string
  run: (fn: () => Promise<unknown>) => Promise<void>
}) {
  const [adding, setAdding] = useState(false)
  const [candidates, setCandidates] = useState<{ id: string; display_name: string }[] | null>(null)
  const memberIds = useMemo(() => new Set(group.members.map((m) => m.userId)), [group.members])

  function openAdd(): void {
    setAdding(true)
    if (candidates === null)
      fetchTransferTargets(token).then(setCandidates).catch(() => setCandidates([]))
  }
  const addable = (candidates ?? []).filter((c) => !memberIds.has(c.id))

  return (
    <section className="modal__section">
      <h3 className="modal__section-title">Members</h3>
      <ul className="cf-fieldlist">
        {group.members.map((m) => (
          <li key={m.userId} className="cf-field" style={{ opacity: m.disabled ? 0.55 : 1 }}>
            <div className="cf-field__info">
              <strong>{m.displayName}</strong>
              {m.isOwner && <span className="modal__muted"> · owner</span>}
              {m.disabled && <span className="modal__muted"> · disabled</span>}
              {m.username && <div className="modal__muted"><code>{m.username}</code></div>}
            </div>
            {canManage && !m.isOwner && (
              <div className="cf-field__actions">
                <button type="button" className="btn btn--xs" disabled={busy}
                  onClick={() => void run(() => transferGroup(group.groupId, m.userId, token))}>
                  Make owner
                </button>
                <button type="button" className="btn btn--danger btn--xs" disabled={busy}
                  onClick={() => void run(() => removeGroupMember(group.groupId, m.userId, token))}>
                  Remove
                </button>
              </div>
            )}
          </li>
        ))}
      </ul>

      {canManage && (
        adding ? (
          <div style={{ marginTop: '0.75rem' }}>
            <h4 className="modal__section-title">Add a member</h4>
            {candidates === null ? (
              <p className="modal__muted">Loading users…</p>
            ) : addable.length === 0 ? (
              <p className="modal__muted">Everyone is already a member.</p>
            ) : (
              <ul className="cf-fieldlist">
                {addable.map((c) => (
                  <li key={c.id} className="cf-field">
                    <div className="cf-field__info"><strong>{c.display_name}</strong></div>
                    <div className="cf-field__actions">
                      <button type="button" className="btn btn--xs" disabled={busy}
                        onClick={() => void run(() => addGroupMembers(group.groupId, [c.id], token))}>
                        + Add
                      </button>
                    </div>
                  </li>
                ))}
              </ul>
            )}
            <div className="sign-form__actions">
              <button type="button" className="btn btn--ghost btn--sm" onClick={() => setAdding(false)}>Done</button>
              <button type="button" className="btn btn--danger btn--sm" onClick={() => setAdding(false)}>Cancel</button>
            </div>
          </div>
        ) : (
          <div className="sign-form__actions" style={{ marginTop: '0.75rem' }}>
            <button type="button" className="btn btn--success btn--sm" onClick={openAdd}>+ Add members</button>
          </div>
        )
      )}
    </section>
  )
}

// --- Content --------------------------------------------------------------------

interface PickItem { itemType: ShareableItemType; id: string; title: string }

function ContentTab({
  group, canManage, busy, token, run,
}: {
  group: GroupDetail; canManage: boolean; busy: boolean; token: string
  run: (fn: () => Promise<unknown>) => Promise<void>
}) {
  const isContributor = group.myRole !== null // member, owner, or admin
  const [adding, setAdding] = useState(false)
  const [myItems, setMyItems] = useState<PickItem[] | null>(null)

  const sharedIds = useMemo(() => {
    const s = new Set<string>()
    group.content.layers.forEach((i) => s.add(`layer:${i.id}`))
    group.content.maps.forEach((i) => s.add(`web_map:${i.id}`))
    group.content.rasters.forEach((i) => s.add(`raster:${i.id}`))
    return s
  }, [group.content])

  function openAdd(): void {
    setAdding(true)
    if (myItems !== null) return
    Promise.all([
      fetchMyLayers(token).then((r) => r.layers.filter((l) => l.owner).map((l): PickItem => ({ itemType: 'layer', id: l.id, title: l.displayName ?? l.id }))).catch(() => []),
      fetchMyWebMaps(token).then((r) => r.owned.map((m): PickItem => ({ itemType: 'web_map', id: m.id, title: m.title }))).catch(() => []),
      fetchMyRasters(token).then((r) => r.owned.map((rs): PickItem => ({ itemType: 'raster', id: rs.id, title: rs.title }))).catch(() => []),
    ]).then((lists) => setMyItems(lists.flat()))
  }
  const addable = (myItems ?? []).filter((i) => !sharedIds.has(`${i.itemType}:${i.id}`))

  return (
    <section className="modal__section">
      <h3 className="modal__section-title">Shared with this group</h3>

      <ContentList label="Layers" items={group.content.layers} kind="layer" canManage={canManage} busy={busy} token={token} groupId={group.groupId} run={run} showEdit />
      <ContentList label="Maps" items={group.content.maps} kind="web_map" canManage={canManage} busy={busy} token={token} groupId={group.groupId} run={run} />
      <ContentList label="Rasters" items={group.content.rasters} kind="raster" canManage={canManage} busy={busy} token={token} groupId={group.groupId} run={run} />

      {isContributor && (
        adding ? (
          <div style={{ marginTop: '0.75rem' }}>
            <h4 className="modal__section-title">Add an item you own</h4>
            {myItems === null ? (
              <p className="modal__muted">Loading your items…</p>
            ) : addable.length === 0 ? (
              <p className="modal__muted">You have no other items to add.</p>
            ) : (
              <ul className="cf-fieldlist">
                {addable.map((i) => (
                  <li key={`${i.itemType}:${i.id}`} className="cf-field">
                    <div className="cf-field__info">
                      <strong>{i.title}</strong>
                      <span className="modal__muted"> · {i.itemType === 'web_map' ? 'map' : i.itemType}</span>
                    </div>
                    <div className="cf-field__actions">
                      <button type="button" className="btn btn--xs" disabled={busy}
                        onClick={() => void run(() => addGroupItems(group.groupId, [{ itemType: i.itemType, id: i.id }], token))}>
                        + Add
                      </button>
                    </div>
                  </li>
                ))}
              </ul>
            )}
            <div className="sign-form__actions">
              <button type="button" className="btn btn--ghost btn--sm" onClick={() => setAdding(false)}>Done</button>
              <button type="button" className="btn btn--danger btn--sm" onClick={() => setAdding(false)}>Cancel</button>
            </div>
          </div>
        ) : (
          <div className="sign-form__actions" style={{ marginTop: '0.75rem' }}>
            <button type="button" className="btn btn--success btn--sm" onClick={openAdd}>+ Add items</button>
          </div>
        )
      )}
    </section>
  )
}

function ContentList({
  label, items, kind, canManage, busy, token, groupId, run, showEdit = false,
}: {
  label: string; items: GroupContentItem[]; kind: ShareableItemType; canManage: boolean
  busy: boolean; token: string; groupId: string; run: (fn: () => Promise<unknown>) => Promise<void>
  showEdit?: boolean
}) {
  if (items.length === 0) return null
  return (
    <>
      <h4 className="modal__section-title" style={{ marginTop: '0.75rem' }}>{label}</h4>
      <ul className="cf-fieldlist">
        {items.map((i) => (
          <li key={i.id} className="cf-field">
            <div className="cf-field__info">
              <strong>{i.title}</strong>
              {showEdit && (
                i.editable
                  ? <span className="modal__muted"> · editable{i.canEdit ? ' · you can edit' : ' · view-only for you'}</span>
                  : <span className="modal__muted"> · read-only</span>
              )}
            </div>
            {canManage && (
              <div className="cf-field__actions">
                <button type="button" className="btn btn--danger btn--xs" disabled={busy}
                  onClick={() => void run(() => removeGroupItem(groupId, kind, i.id, token))}>
                  Remove
                </button>
              </div>
            )}
          </li>
        ))}
      </ul>
    </>
  )
}
