import { useCallback, useEffect, useState } from 'react'
import { useAuth } from 'react-oidc-context'

import ConfirmDialog from '@/components/ConfirmDialog'
import {
  ApiError,
  createServiceAccount,
  disableServiceAccount,
  fetchGroups,
  fetchServiceAccounts,
  rotateServiceAccountSecret,
  type SaAccessMode,
  type SaGroup,
  type ServiceAccount,
} from '@/lib/api'
import CloseButton from '@/components/CloseButton'

/**
 * Admin → Service accounts (Talus 19 c4). The thin in-portal flow for non-interactive API
 * credentials: create + scope + rotate + disable. It does what the dev scripts do, server-side
 * — the portal orchestrates + records; Keycloak issues the secret, shown ONCE here and never
 * stored. Scope = group membership × read/write role (decision-19 Q3/Q4): deliberately NOT a
 * group editor or per-permission console (North-Star drift guard). Every endpoint is admin-
 * gated server-side; this screen sits behind RequireAdmin.
 */
export default function ServiceAccountsView() {
  const auth = useAuth()
  const token = auth.user?.access_token ?? ''

  const [accounts, setAccounts] = useState<ServiceAccount[] | null>(null)
  const [groups, setGroups] = useState<SaGroup[]>([])
  const [error, setError] = useState<string | null>(null)
  const [showCreate, setShowCreate] = useState(false)
  const [actingOn, setActingOn] = useState<string | null>(null)
  // The one-time secret reveal (from create or rotate). Once dismissed it is gone forever.
  const [reveal, setReveal] = useState<{ clientId: string; secret: string; title: string } | null>(
    null,
  )

  const reload = useCallback(async () => {
    if (!token) return
    try {
      const [accts, grps] = await Promise.all([
        fetchServiceAccounts(token),
        fetchGroups(token),
      ])
      setAccounts(accts)
      setGroups(grps)
      setError(null)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to load service accounts')
      setAccounts([])
    }
  }, [token])

  useEffect(() => {
    void reload()
  }, [reload])

  async function rotate(clientId: string): Promise<void> {
    setActingOn(clientId)
    setError(null)
    try {
      const r = await rotateServiceAccountSecret(clientId, token)
      setReveal({ clientId, secret: r.secret, title: 'Secret rotated' })
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Rotate failed')
    } finally {
      setActingOn(null)
    }
  }

  // The client awaiting its disable ConfirmDialog (§5). Revocation, not destruction: the
  // portal offers no re-enable, but the server keeps group tuples so a Keycloak-side
  // re-enable preserves scope — so the dialog is the warning tone, not danger.
  const [confirmDisable, setConfirmDisable] = useState<string | null>(null)

  async function disable(clientId: string): Promise<void> {
    setActingOn(clientId)
    setError(null)
    try {
      await disableServiceAccount(clientId, token)
      await reload()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Disable failed')
    } finally {
      setActingOn(null)
      setConfirmDisable(null)
    }
  }

  return (
    <div className="admin">
      <div className="sa__head">
        <div>
          <h1 className="admin__title">Service accounts</h1>
          <p className="admin__note">
            Non-interactive API credentials for scripts and integrations. Each is scoped to the
            groups you choose and is read-only unless granted read-write — never an admin.
          </p>
        </div>
        <button type="button" className="btn" onClick={() => setShowCreate(true)}>
          New service account
        </button>
      </div>

      {error !== null && <p className="sa__error">{error}</p>}

      {accounts === null ? (
        <p className="sa__muted">Loading…</p>
      ) : accounts.length === 0 ? (
        <p className="sa__muted">No service accounts yet. Create one to script against Talus.</p>
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Client ID</th>
              <th>Access</th>
              <th>Groups</th>
              <th>Status</th>
              <th aria-label="Actions" />
            </tr>
          </thead>
          <tbody>
            {accounts.map((sa) => (
              <tr key={sa.client_id} className={sa.enabled === false ? 'is-disabled' : undefined}>
                <td>{sa.name}</td>
                <td>
                  <code className="sa-code">{sa.client_id}</code>
                </td>
                <td>
                  <span className={`sa-badge sa-badge--${sa.access_mode === 'read-write' ? 'rw' : 'ro'}`}>
                    {sa.access_mode}
                  </span>
                </td>
                <td>
                  {sa.groups.length === 0 ? (
                    <span className="sa__muted">—</span>
                  ) : (
                    <span className="sa-chips">
                      {sa.groups.map((g) => (
                        <span key={g.group_id} className="sa-chip">
                          {g.display_name}
                        </span>
                      ))}
                    </span>
                  )}
                </td>
                <td>
                  {sa.enabled === false ? (
                    <span className="sa-badge sa-badge--off">disabled</span>
                  ) : sa.enabled === true ? (
                    <span className="sa-badge sa-badge--on">enabled</span>
                  ) : (
                    <span className="sa__muted">unknown</span>
                  )}
                </td>
                <td className="sa-actions">
                  <button
                    type="button"
                    className="btn btn--sm"
                    disabled={actingOn === sa.client_id || sa.enabled === false}
                    onClick={() => void rotate(sa.client_id)}
                  >
                    Rotate secret
                  </button>
                  <button
                    type="button"
                    className="btn btn--sm btn--danger"
                    disabled={actingOn === sa.client_id || sa.enabled === false}
                    onClick={() => setConfirmDisable(sa.client_id)}
                  >
                    Disable
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {confirmDisable !== null && (
        <ConfirmDialog
          title={`Disable ${confirmDisable}?`}
          confirmLabel="Disable"
          busy={actingOn === confirmDisable}
          onConfirm={() => void disable(confirmDisable)}
          onCancel={() => setConfirmDisable(null)}
          consequence="Existing tokens stop working within minutes and no new ones can be issued."
        />
      )}

      {showCreate && (
        <CreateServiceAccountModal
          groups={groups}
          accessToken={token}
          onClose={() => setShowCreate(false)}
          onCreated={(clientId, secret) => {
            setShowCreate(false)
            setReveal({ clientId, secret, title: 'Service account created' })
            void reload()
          }}
        />
      )}

      {reveal !== null && <SecretReveal reveal={reveal} onDone={() => setReveal(null)} />}
    </div>
  )
}

// --- create modal ---------------------------------------------------------------

function CreateServiceAccountModal({
  groups,
  accessToken,
  onClose,
  onCreated,
}: {
  groups: SaGroup[]
  accessToken: string
  onClose: () => void
  onCreated: (clientId: string, secret: string) => void
}) {
  const [name, setName] = useState('')
  const [touched, setTouched] = useState(false)
  const [mode, setMode] = useState<SaAccessMode>('read-only')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const nameMissing = name.trim() === ''

  function toggle(id: string): void {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  async function submit(): Promise<void> {
    if (nameMissing) {
      setTouched(true)
      return
    }
    setBusy(true)
    setError(null)
    try {
      const created = await createServiceAccount(
        { name: name.trim(), access_mode: mode, groups: [...selected] },
        accessToken,
      )
      onCreated(created.client_id, created.secret)
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
        aria-label="Create service account"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal__header">
          <h2 className="modal__title">New service account</h2>
          <CloseButton onClick={onClose} />
        </div>

        <section className="modal__section">
          <h3 className="modal__section-title">Name</h3>
          <label className="sign-form__field">
            <span>Name *</span>
            <input
              type="text"
              value={name}
              placeholder="e.g. Weather bot"
              onChange={(e) => setName(e.target.value)}
              onBlur={() => setTouched(true)}
              autoFocus
            />
          </label>
          {touched && nameMissing && <p className="modal__field-error">Name is required</p>}
        </section>

        <section className="modal__section">
          <h3 className="modal__section-title">Access</h3>
          <label className="sa-check">
            <input
              type="radio"
              name="sa-mode"
              checked={mode === 'read-only'}
              onChange={() => setMode('read-only')}
            />
            <span>Read-only — can read its granted layers</span>
          </label>
          <label className="sa-check">
            <input
              type="radio"
              name="sa-mode"
              checked={mode === 'read-write'}
              onChange={() => setMode('read-write')}
            />
            <span>Read-write — can also edit features (data-editor)</span>
          </label>
        </section>

        <section className="modal__section">
          <h3 className="modal__section-title">Groups</h3>
          <p className="modal__muted">The account can touch only layers shared to these groups.</p>
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

        {error !== null && <p className="sign-form__error">{error}</p>}

        <div className="sign-form__actions">
          <button type="button" className="btn btn--sm" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn--sm"
            onClick={() => void submit()}
            disabled={busy || nameMissing}
          >
            {busy ? 'Creating…' : 'Create'}
          </button>
        </div>
      </div>
    </div>
  )
}

// --- secret reveal (shown once) -------------------------------------------------

function SecretReveal({
  reveal,
  onDone,
}: {
  reveal: { clientId: string; secret: string; title: string }
  onDone: () => void
}) {
  return (
    <div className="modal__backdrop" role="presentation">
      <div className="modal" role="dialog" aria-modal="true" aria-label={reveal.title}>
        <div className="modal__header">
          <h2 className="modal__title">{reveal.title}</h2>
        </div>
        <p className="sa-warn">
          Copy the secret now — it is shown once and cannot be retrieved. If lost, rotate it.
        </p>
        <section className="modal__section">
          <h3 className="modal__section-title">Client ID</h3>
          <CopyRow value={reveal.clientId} />
          <h3 className="modal__section-title" style={{ marginTop: '0.75rem' }}>
            Client secret
          </h3>
          <CopyRow value={reveal.secret} mono />
        </section>
        <div className="sign-form__actions">
          <button type="button" className="btn btn--sm" onClick={onDone}>
            Done
          </button>
        </div>
      </div>
    </div>
  )
}

function CopyRow({ value, mono }: { value: string; mono?: boolean }) {
  const [copied, setCopied] = useState(false)
  return (
    <div className="sa-copyrow">
      <code className={mono ? 'sa-code sa-code--secret' : 'sa-code'}>{value}</code>
      <button
        type="button"
        className="btn btn--sm"
        onClick={() => {
          void navigator.clipboard?.writeText(value)
          setCopied(true)
          window.setTimeout(() => setCopied(false), 1500)
        }}
      >
        {copied ? 'Copied' : 'Copy'}
      </button>
    </div>
  )
}
