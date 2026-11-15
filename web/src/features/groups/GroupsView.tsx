import { useEffect, useState } from 'react'
import { Link } from 'react-router'
import { useAuth } from 'react-oidc-context'

import { useIsPublisher } from '@/lib/auth/roles'
import { ApiError, createGroup, fetchMyGroups } from '@/lib/api'
import type { GroupCard, MyGroups } from '@/lib/api'
import { Highlight } from '@/lib/highlight'

/**
 * The **Groups** browse page (Groups management UI, decision-groups-ui.md). Two buckets — groups I
 * own/belong to ("My groups") and other org-discoverable groups ("My organization's groups") —
 * mirroring AGO's tabs but as the on-thesis subset (browse + create; membership/content live on the
 * detail page). Creating a group is publisher/admin only (the API re-enforces); any user can browse.
 *
 * Refreshed (06-main-pages.md): the shared page-header + a NEW client-side name search (over the
 * already-fetched buckets, no API change) with a per-bucket "No groups matching …" empty state, and
 * bordered card-table buckets with role badges.
 */
function SearchGlyph() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <circle cx="11" cy="11" r="8" />
      <path d="m21 21-4.3-4.3" />
    </svg>
  )
}

export default function GroupsView() {
  const auth = useAuth()
  const token = auth.user?.access_token ?? ''
  const canCreate = useIsPublisher()

  const [groups, setGroups] = useState<MyGroups | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [showNew, setShowNew] = useState(false)
  const [newName, setNewName] = useState('')
  const [newSummary, setNewSummary] = useState('')
  const [query, setQuery] = useState('')

  function refresh(): void {
    fetchMyGroups(token)
      .then(setGroups)
      .catch((e) =>
        setLoadError(e instanceof ApiError ? `Couldn’t load groups (HTTP ${e.status})` : 'Couldn’t load groups'),
      )
  }
  useEffect(() => {
    refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token])

  async function create(): Promise<void> {
    if (newName.trim() === '') return
    setBusy(true)
    setError(null)
    try {
      await createGroup({ name: newName.trim(), summary: newSummary.trim() || null }, token)
      setNewName('')
      setNewSummary('')
      setShowNew(false)
      refresh()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Create failed')
    } finally {
      setBusy(false)
    }
  }

  if (loadError !== null) return <div className="mp-page"><p className="sign-form__error">{loadError}</p></div>

  return (
    <div className="mp-page">
      <div className="mp-head">
        <div>
          <p className="eyebrow">Sharing</p>
          <h1 className="mp-title">Groups</h1>
        </div>
        {canCreate && (
          <button type="button" className="btn btn--sm" onClick={() => setShowNew((s) => !s)} disabled={busy}>
            {showNew ? 'Cancel' : '+ Create group'}
          </button>
        )}
      </div>
      <p className="mp-intro">
        Groups organize people so you can share layers, maps, and rasters with a team at once. You can
        browse any group in your organization; {canCreate ? 'create the ones your team owns' : 'a publisher creates them'} and manage their members and content from each group’s page.
      </p>

      {showNew && canCreate && (
        <div className="grp-newform">
          <h3 className="grp-newform__title">Create a group</h3>
          <label className="grp-newform__field">
            <span>Name *</span>
            <input className="field" type="text" value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="Signs Team" />
          </label>
          <label className="grp-newform__field">
            <span>Summary <span className="grp-newform__opt">(optional)</span></span>
            <input className="field" type="text" value={newSummary} onChange={(e) => setNewSummary(e.target.value)} placeholder="What this group is for" />
          </label>
          {error !== null && <p className="sign-form__error">{error}</p>}
          <div>
            <button type="button" className="btn btn--sm" disabled={busy || newName.trim() === ''} onClick={() => void create()}>
              {busy ? 'Creating…' : 'Create group'}
            </button>
          </div>
          <p className="grp-newform__note">You’ll be the owner and first member.</p>
        </div>
      )}

      <div className="mp-search grp-search">
        <span className="mp-search__icon">
          <SearchGlyph />
        </span>
        <input
          className="field"
          type="search"
          placeholder="Search groups by name…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          aria-label="Search groups by name"
        />
      </div>

      <GroupBucket title="My groups" empty="You don’t own or belong to any groups yet." groups={groups?.mine ?? null} query={query} showRole />
      <GroupBucket title="My organization’s groups" empty="No other groups in your organization." groups={groups?.org ?? null} query={query} />
    </div>
  )
}

function GroupBucket({
  title,
  empty,
  groups,
  query,
  showRole = false,
}: {
  title: string
  empty: string
  groups: GroupCard[] | null
  query: string
  showRole?: boolean
}) {
  const q = query.trim().toLowerCase()
  const matched = groups === null ? null : q ? groups.filter((g) => g.displayName.toLowerCase().includes(q)) : groups
  return (
    <section className="grp-bucket">
      <h2 className="grp-bucket__title">{title}</h2>
      {matched === null ? (
        <p className="mp-intro" style={{ margin: 0 }}>Loading…</p>
      ) : (
        <div className="grp-table">
          <div className="grp-row grp-row--head">
            <span>Name</span>
            <span>Members</span>
            <span>{showRole ? 'My role' : 'Role'}</span>
          </div>
          {matched.length === 0 ? (
            <div className="grp-empty">{q ? `No groups matching “${query.trim()}”.` : empty}</div>
          ) : (
            matched.map((g) => (
              <div key={g.groupId} className="grp-row">
                <div>
                  <div className="grp-name">
                    <Link to={`/groups/${encodeURIComponent(g.groupId)}`}>
                      <Highlight text={g.displayName} query={query} />
                    </Link>
                    {g.system && <span className="chip">system</span>}
                  </div>
                  {g.summary && <div className="grp-summary">{g.summary}</div>}
                </div>
                <span className="grp-members">{g.memberCount}</span>
                <span>
                  {g.role === 'owner' ? (
                    <span className="grp-role grp-role--owner">Owner</span>
                  ) : g.role === 'member' ? (
                    <span className="grp-role grp-role--member">Member</span>
                  ) : (
                    <span className="grp-role--none">—</span>
                  )}
                </span>
              </div>
            ))
          )}
        </div>
      )}
    </section>
  )
}
