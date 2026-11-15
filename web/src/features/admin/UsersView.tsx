import { type ChangeEvent, useCallback, useEffect, useState } from 'react'
import { useAuth } from 'react-oidc-context'

import ConfirmDialog from '@/components/ConfirmDialog'
import {
  ApiError,
  bulkImportUsers,
  createUser,
  deleteUser,
  disableUser,
  enableUser,
  fetchGroups,
  fetchUserOwnedContent,
  fetchUsers,
  reassignUserContent,
  resetUserPassword,
  updateUserGroups,
  updateUserRole,
  type AdminUser,
  type ImportResult,
  type ImportRowStatus,
  type ReassignResult,
  type SaGroup,
} from '@/lib/api'
import CloseButton from '@/components/CloseButton'
import { FieldDevicesAction, fieldOffboardingNote } from '@paid'

/**
 * Admin → Users (Talus 27). The thin in-portal flow to onboard/offboard PEOPLE without the
 * Keycloak console: list · create · disable/re-enable · reset password · edit role & groups ·
 * offboard-reassign. Roles/groups are the EXISTING set (a single role tier + registered groups) —
 * deliberately NOT an identity/IAM console (North-Star drift guard). Keycloak issues the identity +
 * the one-time temporary password, shown ONCE here and never stored. Every endpoint is admin-gated
 * server-side; this screen sits behind RequireAdmin.
 */
const ROLE_OPTIONS: ReadonlyArray<{ value: string; label: string; hint: string }> = [
  { value: 'talus-viewer', label: 'Viewer', hint: 'read shared content' },
  { value: 'talus-data-editor', label: 'Editor', hint: 'edit features on layers they can reach' },
  { value: 'talus-publisher', label: 'Publisher', hint: 'create & own layers and maps' },
  { value: 'talus-admin', label: 'Administrator', hint: 'full administration' },
]

function roleLabel(role: string | null): string {
  if (role === null) return 'unknown'
  return ROLE_OPTIONS.find((r) => r.value === role)?.label ?? role
}

export default function UsersView() {
  const auth = useAuth()
  const token = auth.user?.access_token ?? ''
  const myId = auth.user?.profile?.sub ?? ''

  const [users, setUsers] = useState<AdminUser[] | null>(null)
  const [groups, setGroups] = useState<SaGroup[]>([])
  const [error, setError] = useState<string | null>(null)
  // A short-lived confirmation of what a delete actually did (F5) — distinct from `error`.
  const [notice, setNotice] = useState<string | null>(null)
  const [showCreate, setShowCreate] = useState(false)
  const [showImport, setShowImport] = useState(false)
  const [actingOn, setActingOn] = useState<string | null>(null)
  const [editUser, setEditUser] = useState<AdminUser | null>(null)
  const [reassignUser, setReassignUser] = useState<AdminUser | null>(null)
  // One-time temp-password reveal (from create or reset). Once dismissed it is gone forever.
  const [reveal, setReveal] = useState<{
    title: string
    username: string
    password: string | null
    // Is the reset target the signed-in admin? The generated-TEMPORARY copy below is written for
    // resetting SOMEONE ELSE and reads as nonsense when the target is yourself (F5 re-run finding
    // #3). The path is unchanged — only the words are (decision-first-ten-minutes.md F4).
    self?: boolean
  } | null>(null)

  const reload = useCallback(async () => {
    if (!token) return
    try {
      const [us, grps] = await Promise.all([fetchUsers(token), fetchGroups(token)])
      setUsers(us)
      setGroups(grps)
      setError(null)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to load users')
      setUsers([])
    }
  }, [token])

  useEffect(() => {
    void reload()
  }, [reload])

  // The pending row action awaiting its ConfirmDialog (§5). Re-enable never confirms.
  const [confirm, setConfirm] = useState<{
    kind: 'disable' | 'delete' | 'reset'
    user: AdminUser
  } | null>(null)

  async function toggleEnabled(u: AdminUser): Promise<void> {
    const verb = u.active ? 'Disable' : 'Re-enable'
    setActingOn(u.id)
    setError(null)
    try {
      if (u.active) await disableUser(u.id, token)
      else await enableUser(u.id, token)
      await reload()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : `${verb} failed`)
    } finally {
      setActingOn(null)
      setConfirm(null)
    }
  }

  async function remove(u: AdminUser): Promise<void> {
    // Deliberately not a one-click action: the button only exists on an already-disabled account
    // (the PAT model — revoke, then Delete), and the dialog spells out what survives. The server
    // is the real gate; the confirm is about informed consent, not enforcement.
    setActingOn(u.id)
    setError(null)
    try {
      const r = await deleteUser(u.id, token)
      // F5: say which of the two things happened. Never imply an erase that did not occur.
      setNotice(
        r.outcome === 'deleted'
          ? `${u.display_name} was deleted. Nothing referenced this account, so no record of it remains.`
          : `${u.display_name} was removed from the directory. Their sign-in account is gone; their name is kept on the edits they made so that history still reads correctly.`,
      )
      await reload()
    } catch (err) {
      // A 409 carries the actionable reason — most often "still owns N maps; transfer first".
      setError(err instanceof ApiError ? err.message : 'Delete failed')
    } finally {
      setActingOn(null)
      setConfirm(null)
    }
  }

  async function reset(u: AdminUser): Promise<void> {
    const isSelf = u.id === myId
    setActingOn(u.id)
    setError(null)
    try {
      const r = await resetUserPassword(u.id, token)
      setReveal({ title: 'Password reset', username: u.username, password: r.temp_password, self: isSelf })
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Reset failed')
    } finally {
      setActingOn(null)
      setConfirm(null)
    }
  }

  return (
    <div className="admin">
      <div className="sa__head">
        <div>
          <h1 className="admin__title">Users</h1>
          <p className="admin__note">
            People in your organization. Create an account, assign a role and groups, reset a
            password, or disable someone who has left — no Keycloak console needed. To remove a
            departed person for good: disable them, transfer anything they own, then delete.
          </p>
        </div>
        <div className="sa__head-actions">
          <button type="button" className="btn btn--ghost" onClick={() => setShowImport(true)}>
            Bulk import
          </button>
          <button type="button" className="btn" onClick={() => setShowCreate(true)}>
            New user
          </button>
        </div>
      </div>

      {error !== null && <p className="sa__error">{error}</p>}
      {notice !== null && (
        <p className="sa-warn" role="status">
          {notice}{' '}
          <button type="button" className="btn btn--sm" onClick={() => setNotice(null)}>
            Dismiss
          </button>
        </p>
      )}

      {users !== null && users.length > 0 && <MembersSummary users={users} />}

      {users === null ? (
        <p className="sa__muted">Loading…</p>
      ) : users.length === 0 ? (
        <p className="sa__muted">No users yet.</p>
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Username</th>
              <th>Role</th>
              <th>Groups</th>
              <th>Status</th>
              <th aria-label="Actions" />
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.id} className={u.active ? undefined : 'is-disabled'}>
                <td>
                  {u.display_name}
                  {u.id === myId && <span className="sa__muted"> (you)</span>}
                  <div className="sa__muted">{u.email}</div>
                </td>
                <td>
                  <code className="sa-code">{u.username}</code>
                </td>
                <td>{roleLabel(u.role)}</td>
                <td>
                  {u.groups.length === 0 ? (
                    <span className="sa__muted">—</span>
                  ) : (
                    <span className="sa-chips">
                      {u.groups.map((g) => (
                        <span key={g.group_id} className="sa-chip">
                          {g.display_name}
                        </span>
                      ))}
                    </span>
                  )}
                </td>
                <td>
                  {u.active ? (
                    <span className="sa-badge sa-badge--on">active</span>
                  ) : (
                    <span className="sa-badge sa-badge--off">disabled</span>
                  )}
                  {/* F1: this row is a record, not an account — it exists so imported edits have
                      an author. Saying so answers "why is this person here and why can't I reset
                      their password?" before it becomes a support question. */}
                  {!u.can_sign_in && (
                    <div className="sa__muted" title="No sign-in account — this record exists so imported edits have an author">
                      cannot sign in
                    </div>
                  )}
                </td>
                <td className="sa-actions">
                  <button
                    type="button"
                    className="btn btn--sm"
                    disabled={actingOn === u.id}
                    onClick={() => setEditUser(u)}
                  >
                    Edit
                  </button>
                  <button
                    type="button"
                    className="btn btn--sm"
                    disabled={actingOn === u.id}
                    onClick={() => setReassignUser(u)}
                  >
                    Transfer…
                  </button>
                  {/* No identity => no password to reset. Offering it would 502. */}
                  {u.can_sign_in && (
                    <button
                      type="button"
                      className="btn btn--sm"
                      disabled={actingOn === u.id}
                      onClick={() => setConfirm({ kind: 'reset', user: u })}
                    >
                      Reset password
                    </button>
                  )}
                  {/* Talus Field f5c: the phones holding this user's packs — the '@paid' seam's
                      action; inert in a public build. */}
                  {u.can_sign_in && <FieldDevicesAction userId={u.id} userLabel={u.display_name} />}
                  <button
                    type="button"
                    className={`btn btn--sm${u.active ? ' btn--danger' : ''}`}
                    disabled={actingOn === u.id}
                    onClick={() =>
                      u.active ? setConfirm({ kind: 'disable', user: u }) : void toggleEnabled(u)
                    }
                  >
                    {u.active ? 'Disable' : 'Re-enable'}
                  </button>
                  {/* Delete appears only once the account is disabled — the same shape as an
                      access token (revoke, then Delete). The server still refuses if they own
                      anything, and says what. */}
                  {!u.active && (
                    <button
                      type="button"
                      className="btn btn--sm btn--danger"
                      disabled={actingOn === u.id}
                      onClick={() => setConfirm({ kind: 'delete', user: u })}
                    >
                      Delete
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {confirm !== null &&
        (() => {
          const u = confirm.user
          const busy = actingOn === u.id
          const close = () => setConfirm(null)
          switch (confirm.kind) {
            case 'disable':
              return (
                <ConfirmDialog
                  title={`Disable ${u.display_name}?`}
                  confirmLabel="Disable"
                  busy={busy}
                  onConfirm={() => void toggleEnabled(u)}
                  onCancel={close}
                  consequence="They will be signed out and can't sign in again until re-enabled."
                >
                  <p>Their content and attribution are preserved.</p>
                  {fieldOffboardingNote() && <p>{fieldOffboardingNote()}</p>}
                </ConfirmDialog>
              )
            case 'delete':
              return (
                <ConfirmDialog
                  title={`Delete ${u.display_name}?`}
                  confirmLabel="Delete"
                  tone="danger"
                  busy={busy}
                  onConfirm={() => void remove(u)}
                  onCancel={close}
                  consequence="This cannot be undone."
                >
                  <p>
                    Their sign-in account is deleted permanently and they leave this list for
                    good. Anything they previously edited keeps their name in its history.
                  </p>
                </ConfirmDialog>
              )
            case 'reset':
              return u.id === myId ? (
                <ConfirmDialog
                  title="Reset your own password?"
                  confirmLabel="Reset password"
                  busy={busy}
                  onConfirm={() => void reset(u)}
                  onCancel={close}
                  consequence="Your current password stops working immediately and you must set a new one at your next sign-in."
                >
                  <p>
                    If you just want to choose a new password, cancel this and use Profile →
                    Password instead.
                  </p>
                </ConfirmDialog>
              ) : (
                <ConfirmDialog
                  title={`Reset ${u.display_name}'s password?`}
                  confirmLabel="Reset password"
                  busy={busy}
                  onConfirm={() => void reset(u)}
                  onCancel={close}
                  consequence="Their current password stops working and they must set a new one at next sign-in."
                />
              )
          }
        })()}

      {showCreate && (
        <CreateUserModal
          groups={groups}
          accessToken={token}
          onClose={() => setShowCreate(false)}
          onCreated={(username, password) => {
            setShowCreate(false)
            setReveal({ title: 'User created', username, password })
            void reload()
          }}
        />
      )}

      {editUser !== null && (
        <EditUserModal
          user={editUser}
          groups={groups}
          accessToken={token}
          onClose={() => setEditUser(null)}
          onSaved={() => {
            setEditUser(null)
            void reload()
          }}
        />
      )}

      {reassignUser !== null && (
        <ReassignModal
          user={reassignUser}
          candidates={(users ?? []).filter((u) => u.active && u.id !== reassignUser.id)}
          accessToken={token}
          onClose={() => setReassignUser(null)}
          onDone={() => {
            setReassignUser(null)
            void reload()
          }}
        />
      )}

      {showImport && (
        <BulkImportModal
          accessToken={token}
          onClose={() => setShowImport(false)}
          onImported={() => void reload()}
        />
      )}

      {reveal !== null && <TempPasswordReveal reveal={reveal} onDone={() => setReveal(null)} />}
    </div>
  )
}

// --- members summary card -------------------------------------------------------

/**
 * Members summary atop Admin → Users (Talus 28). Counts only — Talus has no per-seat licensing,
 * so unlike the AGO/HubSpot card there are no assigned/available bars. Derived entirely from the
 * already-loaded list, so the numbers can never disagree with the table below.
 */
function MembersSummary({ users }: { users: AdminUser[] }) {
  const total = users.length
  const pending = users.filter((u) => u.last_seen_at === null).length
  const disabled = users.filter((u) => !u.active).length

  const counts = new Map<string, number>()
  for (const u of users) {
    const key = u.role ?? 'unassigned'
    counts.set(key, (counts.get(key) ?? 0) + 1)
  }
  // The four tiers always show (even at 0); "Unassigned" only when some human holds no role.
  const roleRows = [
    ...ROLE_OPTIONS.map((r) => ({ label: r.label, count: counts.get(r.value) ?? 0 })),
    { label: 'Unassigned', count: counts.get('unassigned') ?? 0 },
  ].filter((r) => r.label !== 'Unassigned' || r.count > 0)

  return (
    <section className="members-summary" aria-label="Members summary">
      <div className="members-summary__stats">
        <div className="members-summary__stat">
          <span className="members-summary__num members-summary__num--accent">{total}</span>
          <span className="members-summary__label">Total members</span>
        </div>
        <div className="members-summary__stat">
          <span className="members-summary__num members-summary__num--muted">{pending}</span>
          <span className="members-summary__label">Pending (never signed in)</span>
        </div>
        <div className="members-summary__stat">
          <span className="members-summary__num members-summary__num--muted">{disabled}</span>
          <span className="members-summary__label">Disabled</span>
        </div>
      </div>
      <div className="members-summary__divider" />
      <p className="members-summary__subhead">Members per user type</p>
      <div className="members-summary__roles">
        {roleRows.map((r) => (
          <div key={r.label} className="members-summary__role">
            <span className="members-summary__role-count">{r.count}</span>
            <span className="members-summary__role-name">{r.label}</span>
          </div>
        ))}
      </div>
    </section>
  )
}

// --- create modal ---------------------------------------------------------------

function CreateUserModal({
  groups,
  accessToken,
  onClose,
  onCreated,
}: {
  groups: SaGroup[]
  accessToken: string
  onClose: () => void
  onCreated: (username: string, password: string | null) => void
}) {
  const [username, setUsername] = useState('')
  const [email, setEmail] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [role, setRole] = useState('talus-viewer')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [federated, setFederated] = useState(false)
  const [touched, setTouched] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const missing = username.trim() === '' || email.trim() === '' || displayName.trim() === ''

  function toggle(id: string): void {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  async function submit(): Promise<void> {
    if (missing) {
      setTouched(true)
      return
    }
    setBusy(true)
    setError(null)
    try {
      const created = await createUser(
        {
          username: username.trim(),
          email: email.trim(),
          display_name: displayName.trim(),
          role,
          groups: [...selected],
          federated,
        },
        accessToken,
      )
      onCreated(created.username, created.temp_password)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Create failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal__backdrop" role="presentation" onClick={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label="Create user"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal__header">
          <h2 className="modal__title">New user</h2>
          <CloseButton onClick={onClose} />
        </div>

        <section className="modal__section">
          <label className="sign-form__field">
            <span>Full name *</span>
            <input
              type="text"
              value={displayName}
              placeholder="e.g. Dana Lee"
              onChange={(e) => setDisplayName(e.target.value)}
              onBlur={() => setTouched(true)}
              autoFocus
            />
          </label>
          <label className="sign-form__field">
            <span>Username *</span>
            <input
              type="text"
              value={username}
              placeholder="e.g. dana.lee"
              onChange={(e) => setUsername(e.target.value)}
              onBlur={() => setTouched(true)}
            />
          </label>
          <label className="sign-form__field">
            <span>Email *</span>
            <input
              type="email"
              value={email}
              placeholder="e.g. dana.lee@example.org"
              onChange={(e) => setEmail(e.target.value)}
              onBlur={() => setTouched(true)}
            />
          </label>
          {touched && missing && <p className="modal__field-error">Name, username, and email are required</p>}
        </section>

        <section className="modal__section">
          <h3 className="modal__section-title">Role</h3>
          {ROLE_OPTIONS.map((r) => (
            <label key={r.value} className="sa-check">
              <input
                type="radio"
                name="new-user-role"
                checked={role === r.value}
                onChange={() => setRole(r.value)}
              />
              <span>
                {r.label} — {r.hint}
              </span>
            </label>
          ))}
        </section>

        <section className="modal__section">
          <h3 className="modal__section-title">Groups</h3>
          <p className="modal__muted">The user can reach content shared to these groups.</p>
          {groups.length === 0 ? (
            <p className="modal__muted">No groups exist yet — create one via the publisher tools first.</p>
          ) : (
            <div className="sa-checklist">
              {groups.map((g) => (
                <label key={g.group_id} className="sa-check">
                  <input
                    type="checkbox"
                    checked={selected.has(g.group_id)}
                    onChange={() => toggle(g.group_id)}
                  />
                  <span>{g.display_name}</span>
                </label>
              ))}
            </div>
          )}
        </section>

        <section className="modal__section">
          <h3 className="modal__section-title">Sign-in method</h3>
          <label className="sa-check">
            <input
              type="checkbox"
              checked={federated}
              onChange={(e) => setFederated(e.target.checked)}
            />
            <span>
              SSO user — signs in through your organization&rsquo;s identity provider. No local password
              is created; configure the provider in Keycloak (see the SSO runbook). Match the email above
              to what the provider asserts.
            </span>
          </label>
        </section>

        {error !== null && <p className="sign-form__error">{error}</p>}

        <div className="sign-form__actions">
          <button type="button" className="btn btn--sm" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="button" className="btn btn--sm" onClick={() => void submit()} disabled={busy || missing}>
            {busy ? 'Creating…' : 'Create'}
          </button>
        </div>
      </div>
    </div>
  )
}

// --- edit modal (role + groups) -------------------------------------------------

function EditUserModal({
  user,
  groups,
  accessToken,
  onClose,
  onSaved,
}: {
  user: AdminUser
  groups: SaGroup[]
  accessToken: string
  onClose: () => void
  onSaved: () => void
}) {
  const [role, setRole] = useState(user.role ?? 'talus-viewer')
  const [selected, setSelected] = useState<Set<string>>(
    new Set(user.groups.map((g) => g.group_id)),
  )
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  function toggle(id: string): void {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const roleChanged = role !== user.role
  const before = new Set(user.groups.map((g) => g.group_id))
  const groupsChanged = before.size !== selected.size || [...selected].some((g) => !before.has(g))

  async function save(): Promise<void> {
    setBusy(true)
    setError(null)
    try {
      // Role first (it carries the last-admin demote guard) — if it 409s, groups are left untouched.
      if (roleChanged) await updateUserRole(user.id, role, accessToken)
      if (groupsChanged) await updateUserGroups(user.id, [...selected], accessToken)
      onSaved()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Save failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal__backdrop" role="presentation" onClick={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label={`Edit ${user.display_name}`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal__header">
          <h2 className="modal__title">Edit {user.display_name}</h2>
          <CloseButton onClick={onClose} />
        </div>

        <section className="modal__section">
          <h3 className="modal__section-title">Role</h3>
          {ROLE_OPTIONS.map((r) => (
            <label key={r.value} className="sa-check">
              <input
                type="radio"
                name="edit-user-role"
                checked={role === r.value}
                onChange={() => setRole(r.value)}
              />
              <span>
                {r.label} — {r.hint}
              </span>
            </label>
          ))}
        </section>

        <section className="modal__section">
          <h3 className="modal__section-title">Groups</h3>
          {groups.length === 0 ? (
            <p className="modal__muted">No groups exist yet.</p>
          ) : (
            <div className="sa-checklist">
              {groups.map((g) => (
                <label key={g.group_id} className="sa-check">
                  <input
                    type="checkbox"
                    checked={selected.has(g.group_id)}
                    onChange={() => toggle(g.group_id)}
                  />
                  <span>{g.display_name}</span>
                </label>
              ))}
            </div>
          )}
        </section>

        {error !== null && <p className="sign-form__error">{error}</p>}

        <div className="sign-form__actions">
          <button type="button" className="btn btn--sm" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn--sm"
            onClick={() => void save()}
            disabled={busy || (!roleChanged && !groupsChanged)}
          >
            {busy ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  )
}

// --- reassign modal -------------------------------------------------------------

function ReassignModal({
  user,
  candidates,
  accessToken,
  onClose,
  onDone,
}: {
  user: AdminUser
  candidates: AdminUser[]
  accessToken: string
  onClose: () => void
  onDone: () => void
}) {
  const [counts, setCounts] = useState<{ maps: number; layers: number } | null>(null)
  const [successor, setSuccessor] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<ReassignResult | null>(null)

  useEffect(() => {
    let live = true
    fetchUserOwnedContent(user.id, accessToken)
      .then((c) => {
        if (live) setCounts(c)
      })
      .catch((err: unknown) => {
        if (live) setError(err instanceof ApiError ? err.message : 'Could not load owned content')
      })
    return () => {
      live = false
    }
  }, [user.id, accessToken])

  const total = counts ? counts.maps + counts.layers : 0

  async function submit(): Promise<void> {
    if (successor === '') return
    setBusy(true)
    setError(null)
    try {
      const r = await reassignUserContent(user.id, successor, accessToken)
      setResult(r)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Transfer failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal__backdrop" role="presentation" onClick={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label={`Transfer ${user.display_name}'s content`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal__header">
          <h2 className="modal__title">Transfer content</h2>
          <CloseButton onClick={onClose} />
        </div>

        {result !== null ? (
          <section className="modal__section">
            <p>
              Moved <strong>{result.moved_maps.length}</strong> map(s) and{' '}
              <strong>{result.moved_layers.length}</strong> layer(s) to the new owner.
            </p>
            {result.failed.length > 0 && (
              <p className="sign-form__error">
                {result.failed.length} item(s) failed and were left with {user.display_name}; try again
                to retry just those.
              </p>
            )}
            <div className="sign-form__actions">
              <button type="button" className="btn btn--sm" onClick={onDone}>
                Done
              </button>
            </div>
          </section>
        ) : (
          <>
            <section className="modal__section">
              <p className="modal__muted">
                Move <strong>all</strong> content owned by <strong>{user.display_name}</strong> to one
                successor. Shares and creator attribution are preserved.
              </p>
              <p>
                {counts === null ? (
                  <span className="sa__muted">Counting owned items…</span>
                ) : (
                  <>
                    {counts.maps} map(s) and {counts.layers} layer(s) will be transferred.
                  </>
                )}
              </p>
            </section>

            <section className="modal__section">
              <h3 className="modal__section-title">New owner</h3>
              {candidates.length === 0 ? (
                <p className="modal__muted">No other active users to receive the content.</p>
              ) : (
                <label className="sign-form__field">
                  <span>Successor</span>
                  <select value={successor} onChange={(e) => setSuccessor(e.target.value)}>
                    <option value="">Select a user…</option>
                    {candidates.map((c) => (
                      <option key={c.id} value={c.id}>
                        {c.display_name} ({c.username})
                      </option>
                    ))}
                  </select>
                </label>
              )}
            </section>

            {error !== null && <p className="sign-form__error">{error}</p>}

            <div className="sign-form__actions">
              <button type="button" className="btn btn--sm" onClick={onClose} disabled={busy}>
                Cancel
              </button>
              <button
                type="button"
                className="btn btn--sm"
                onClick={() => void submit()}
                disabled={busy || successor === '' || total === 0}
              >
                {busy ? 'Transferring…' : 'Transfer'}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}

// --- temp-password reveal (shown once) ------------------------------------------

function TempPasswordReveal({
  reveal,
  onDone,
}: {
  reveal: { title: string; username: string; password: string | null; self?: boolean }
  onDone: () => void
}) {
  const [copied, setCopied] = useState(false)
  const password = reveal.password
  return (
    <div className="modal__backdrop" role="presentation">
      <div className="modal" role="dialog" aria-modal="true" aria-label={reveal.title}>
        <div className="modal__header">
          <h2 className="modal__title">{reveal.title}</h2>
        </div>
        {password === null ? (
          // Federated/SSO user (Talus 48): there is no local password to reveal.
          <>
            <p className="sa-warn">
              This is an SSO user — they sign in through your organization&rsquo;s identity provider.
              There is no temporary password to share.
            </p>
            <section className="modal__section">
              <h3 className="modal__section-title">Username</h3>
              <div className="sa-copyrow">
                <code className="sa-code">{reveal.username}</code>
              </div>
            </section>
          </>
        ) : (
          <>
            <p className="sa-warn">
              {reveal.self === true
                ? 'Copy this temporary password now — it is shown once and cannot be retrieved. It is YOURS: your old password no longer works, and you will be asked to set a new one at your next sign-in. (To choose a password directly next time, use Profile → Password.)'
                : 'Copy the temporary password now — it is shown once and cannot be retrieved. The user must change it at first sign-in. If lost, reset it.'}
            </p>
            <section className="modal__section">
              <h3 className="modal__section-title">Username</h3>
              <div className="sa-copyrow">
                <code className="sa-code">{reveal.username}</code>
              </div>
              <h3 className="modal__section-title" style={{ marginTop: '0.75rem' }}>
                Temporary password
              </h3>
              <div className="sa-copyrow">
                <code className="sa-code sa-code--secret">{password}</code>
                <button
                  type="button"
                  className="btn btn--sm"
                  onClick={() => {
                    void navigator.clipboard?.writeText(password)
                    setCopied(true)
                    window.setTimeout(() => setCopied(false), 1500)
                  }}
                >
                  {copied ? 'Copied' : 'Copy'}
                </button>
              </div>
            </section>
          </>
        )}
        <div className="sign-form__actions">
          <button type="button" className="btn btn--sm" onClick={onDone}>
            Done
          </button>
        </div>
      </div>
    </div>
  )
}

// --- bulk CSV import (decision-bulk-user-import.md) ------------------------------

const IMPORT_TEMPLATE =
  'full_name,username,email,role,groups\n' +
  'Dana Lee,dana.lee,dana.lee@example.org,Editor,Public Works;Parks\n' +
  'Sam Rivers,sam.rivers,sam.rivers@example.org,Viewer,\n'

const STATUS_LABEL: Record<ImportRowStatus, string> = {
  ok: 'will create',
  created: 'created',
  skip_exists: 'skip — exists',
  error: 'error',
  failed: 'failed',
}

/** Map a row verdict onto the EXISTING badge tokens (no new colors): green for ok/created, blue
 *  for an idempotent skip, red for error/failed. */
function statusBadgeClass(s: ImportRowStatus): string {
  if (s === 'ok' || s === 'created') return 'sa-badge sa-badge--on'
  if (s === 'skip_exists') return 'sa-badge sa-badge--ro'
  return 'sa-badge sa-badge--off'
}

/** Trigger a client-side CSV download (the import template, or the one-time credentials file). */
function downloadCsv(filename: string, content: string): void {
  const blob = new Blob([content], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

function Stat({ num, label }: { num: number; label: string }) {
  return (
    <div className="import-stat">
      <span className="import-stat__num">{num}</span>
      <span className="import-stat__label">{label}</span>
    </div>
  )
}

/**
 * Admin → Users bulk import. A validate-first flow (decision-bulk-user-import.md): upload/paste a
 * CSV → Validate (dry run, no writes) → review the per-row preview → Create the valid rows → download
 * the one-time credentials CSV. The server is the authority both passes; this is the cosmetic driver.
 */
function BulkImportModal({
  accessToken,
  onClose,
  onImported,
}: {
  accessToken: string
  onClose: () => void
  onImported: () => void
}) {
  const [csvText, setCsvText] = useState('')
  const [fileName, setFileName] = useState<string | null>(null)
  const [report, setReport] = useState<ImportResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // input (no report) → preview (validated, not committed) → results (committed).
  const phase = report === null ? 'input' : report.committed ? 'results' : 'preview'

  async function onPickFile(e: ChangeEvent<HTMLInputElement>): Promise<void> {
    const file = e.target.files?.[0]
    if (!file) return
    setFileName(file.name)
    setReport(null)
    setError(null)
    setCsvText(await file.text())
  }

  async function run(commit: boolean): Promise<void> {
    if (csvText.trim() === '') {
      setError('Choose a CSV file first.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const r = await bulkImportUsers(csvText, commit, accessToken)
      setReport(r)
      if (commit && r.summary.created > 0) onImported()
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : `${commit ? 'Import' : 'Validation'} failed`,
      )
    } finally {
      setBusy(false)
    }
  }

  function downloadCredentials(rep: ImportResult): void {
    const lines = ['username,temporary_password']
    for (const c of rep.credentials) lines.push(`${c.username},${c.temp_password}`)
    downloadCsv('talus-new-user-credentials.csv', lines.join('\n') + '\n')
  }

  return (
    <div className="modal__backdrop" role="presentation" onClick={onClose}>
      <div
        className="modal modal--wide"
        role="dialog"
        aria-modal="true"
        aria-label="Bulk import users"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal__header">
          <h2 className="modal__title">Bulk import users</h2>
          <CloseButton onClick={onClose} />
        </div>

        {phase === 'input' && (
          <section className="modal__section">
            <p className="modal__muted">
              Upload a CSV with columns{' '}
              <code className="sa-code">full_name, username, email, role, groups</code>. Role is
              Viewer / Editor / Publisher / Administrator (blank = Viewer); groups are existing group
              names separated by “;”. Nothing is created until you confirm.
            </p>
            <label className="sign-form__field">
              <span>CSV file</span>
              <input type="file" accept=".csv,text/csv" onChange={(e) => void onPickFile(e)} />
            </label>
            <p className="modal__muted">
              {fileName ? `Loaded ${fileName}.` : 'No file chosen.'}{' '}
              <button
                type="button"
                className="linklike"
                onClick={() => downloadCsv('talus-user-import-template.csv', IMPORT_TEMPLATE)}
              >
                Download template
              </button>
            </p>
          </section>
        )}

        {report && (
          <section className="modal__section">
            <div className="import-stats">
              {report.committed ? (
                <Stat num={report.summary.created} label="Created" />
              ) : (
                <Stat num={report.summary.to_create} label="To create" />
              )}
              <Stat num={report.summary.to_skip} label="Already exist" />
              <Stat
                num={report.committed ? report.summary.failed : report.summary.errors}
                label={report.committed ? 'Failed' : 'Errors'}
              />
            </div>

            {report.committed && report.credentials.length > 0 && (
              <p className="sa-warn">
                Download the credentials now — each temporary password is shown once and cannot be
                retrieved. Every user must change it at first sign-in.
              </p>
            )}

            <div className="import-rows">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>#</th>
                    <th>User</th>
                    <th>Role</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {report.rows.map((row) => (
                    <tr key={row.line}>
                      <td>{row.line}</td>
                      <td>
                        {row.display_name ?? '—'}
                        <div className="sa__muted">
                          {row.username ?? '—'}
                          {row.email ? ` · ${row.email}` : ''}
                        </div>
                      </td>
                      <td>
                        {row.role ?? '—'}
                        {row.groups.length > 0 && (
                          <div className="sa__muted">{row.groups.join(', ')}</div>
                        )}
                      </td>
                      <td>
                        <span className={statusBadgeClass(row.status)}>
                          {STATUS_LABEL[row.status]}
                        </span>
                        {row.message && <div className="sa__muted">{row.message}</div>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        )}

        {error !== null && <p className="sign-form__error">{error}</p>}

        <div className="sign-form__actions">
          {phase === 'input' && (
            <>
              <button type="button" className="btn btn--sm" onClick={onClose} disabled={busy}>
                Cancel
              </button>
              <button
                type="button"
                className="btn btn--sm"
                onClick={() => void run(false)}
                disabled={busy || csvText.trim() === ''}
              >
                {busy ? 'Validating…' : 'Validate'}
              </button>
            </>
          )}

          {phase === 'preview' && report && (
            <>
              <button
                type="button"
                className="btn btn--sm"
                onClick={() => {
                  setReport(null)
                  setError(null)
                }}
                disabled={busy}
              >
                Back
              </button>
              <button
                type="button"
                className="btn btn--sm"
                onClick={() => void run(true)}
                disabled={busy || report.summary.to_create === 0}
              >
                {busy
                  ? 'Creating…'
                  : `Create ${report.summary.to_create} user${report.summary.to_create === 1 ? '' : 's'}`}
              </button>
            </>
          )}

          {phase === 'results' && report && (
            <>
              {report.credentials.length > 0 && (
                <button
                  type="button"
                  className="btn btn--sm btn--ghost"
                  onClick={() => downloadCredentials(report)}
                >
                  Download credentials CSV
                </button>
              )}
              <button type="button" className="btn btn--sm" onClick={onClose}>
                Done
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
