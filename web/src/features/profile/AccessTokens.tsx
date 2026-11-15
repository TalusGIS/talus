import { useCallback, useEffect, useState } from 'react'

import {
  ApiError,
  createMyToken,
  fetchMyTokens,
  purgeMyToken,
  revokeMyToken,
  type PatMinted,
  type PatSummary,
} from '@/lib/api'
import { useIsAdmin } from '@/lib/auth/roles'
import CloseButton from '@/components/CloseButton'
import useConfirm from '@/components/useConfirm'

/**
 * Profile → Access tokens (Interop I-1 frontend). The owner-facing PAT lifecycle over /me/tokens:
 * mint (plaintext shown ONCE — the ServiceAccountsView show-once pattern), list (metadata only),
 * revoke (confirm → immediate). The mint modal + reveal + copy-box are exported for the "Use in…"
 * wizard's credentials step, so minting is ONE flow everywhere. The plaintext lives only in
 * transient reveal state — never logged, never persisted.
 */

const dateFmt = (iso: string | null): string => (iso ? new Date(iso).toLocaleDateString() : '—')

// Same inline info glyph as ConnectComputer's — stroke = currentColor, sized for connect-note.
function IconInfo() {
  return (
    <svg
      width="15"
      height="15"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <circle cx="12" cy="12" r="10" />
      <line x1="12" y1="16" x2="12" y2="12" />
      <line x1="12" y1="8" x2="12.01" y2="8" />
    </svg>
  )
}

export default function AccessTokensSection({ accessToken }: { accessToken: string }) {
  const isAdmin = useIsAdmin()
  const { confirm, confirmDialog } = useConfirm()
  const [tokens, setTokens] = useState<PatSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [showMint, setShowMint] = useState(false)
  const [minted, setMinted] = useState<PatMinted | null>(null)
  const [actingOn, setActingOn] = useState<string | null>(null)

  const reload = useCallback(async () => {
    if (!accessToken) return
    try {
      setTokens(await fetchMyTokens(accessToken))
      setError(null)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to load tokens')
      setTokens([])
    }
  }, [accessToken])

  useEffect(() => {
    void reload()
  }, [reload])

  function revoke(t: PatSummary): void {
    confirm({
      title: `Revoke “${t.label}”?`,
      consequence:
        'Any desktop or script connection using this token stops working immediately — including copies you shared. Revoking cannot be undone; a replacement has to be minted.',
      confirmLabel: 'Revoke token',
      onConfirm: async () => {
        setActingOn(t.id)
        setError(null)
        try {
          await revokeMyToken(t.id, accessToken)
          await reload()
        } catch (err) {
          setError(err instanceof ApiError ? err.message : 'Revoke failed')
        } finally {
          setActingOn(null)
        }
      },
    })
  }

  // Permanently remove an already-revoked/expired token from the list (cleanup). The record is
  // dead — the mint/revoke events persist in the audit log regardless.
  function remove(t: PatSummary): void {
    confirm({
      title: `Remove “${t.label}” from your list?`,
      consequence:
        'The row disappears for good. The token is already dead, and the mint and revoke events stay in the audit log either way — this only tidies the list.',
      confirmLabel: 'Remove from list',
      onConfirm: async () => {
        setActingOn(t.id)
        setError(null)
        try {
          await purgeMyToken(t.id, accessToken)
          await reload()
        } catch (err) {
          setError(err instanceof ApiError ? err.message : 'Remove failed')
        } finally {
          setActingOn(null)
        }
      },
    })
  }

  return (
    <>
      <div className="tokpanel">
        <div className="tokpanel__head">
          <span className="tokpanel__head-title">Your tokens</span>
          <button type="button" className="btn btn--sm" onClick={() => setShowMint(true)}>
            + Create token
          </button>
        </div>

        {error !== null && <div className="tokpanel__error">{error}</div>}

        {tokens === null ? (
          <div className="tokpanel__empty">Loading…</div>
        ) : tokens.length === 0 ? (
          <div className="tokpanel__empty">
            No tokens yet — create one the first time you connect ArcGIS Pro or QGIS.
          </div>
        ) : (
          <div className="tokpanel__table">
            <div className="tokpanel__row tokpanel__row--head">
              <span>Label</span>
              <span>Access</span>
              <span>Created</span>
              <span>Last used</span>
              <span>Expires</span>
              <span>Status</span>
              <span aria-hidden />
            </div>
            {tokens.map((t) => {
              const revoked = t.revoked_at !== null
              const expired =
                !revoked && t.expires_at !== null && new Date(t.expires_at).getTime() < Date.now()
              const dead = revoked || expired
              return (
                <div key={t.id} className={`tokpanel__row${dead ? ' is-disabled' : ''}`}>
                  <span className="tokpanel__label" title={t.label}>
                    {t.label}
                  </span>
                  <span>
                    <span className={`sa-badge sa-badge--${t.scope === 'read_write' ? 'rw' : 'ro'}`}>
                      {t.scope === 'read_write' ? 'read-write' : 'read-only'}
                    </span>
                  </span>
                  <span className="tokpanel__lastused">{dateFmt(t.created_at)}</span>
                  <span className="tokpanel__lastused">{dateFmt(t.last_used_at)}</span>
                  <span className="tokpanel__lastused">
                    {t.expires_at === null ? 'never' : dateFmt(t.expires_at)}
                  </span>
                  <span>
                    {revoked ? (
                      <span className="sa-badge sa-badge--off">revoked</span>
                    ) : expired ? (
                      <span className="sa-badge sa-badge--off">expired</span>
                    ) : (
                      <span className="sa-badge sa-badge--on">active</span>
                    )}
                  </span>
                  <span className="tokpanel__action">
                    {!dead ? (
                      <button
                        type="button"
                        className="tokpanel__actionbtn"
                        disabled={actingOn === t.id}
                        onClick={() => revoke(t)}
                      >
                        Revoke
                      </button>
                    ) : (
                      <button
                        type="button"
                        className="tokpanel__actionbtn tokpanel__actionbtn--delete"
                        disabled={actingOn === t.id}
                        title="Permanently remove this dead token from your list"
                        onClick={() => remove(t)}
                      >
                        Delete
                      </button>
                    )}
                  </span>
                </div>
              )
            })}
          </div>
        )}
      </div>

      {/* Light-touch Pro steer (decision-pro-server-connection.md §5.2): minting a token here is
          the moment a Pro-first user detours through Talus — one line, no banner. */}
      <p className="connect-note">
        <span className="connect-note__icon">
          <IconInfo />
        </span>
        <span>
          Connecting <strong>ArcGIS Pro</strong>? Use the connection files under{' '}
          <strong>Connect this computer</strong> below — Pro’s <em>New ArcGIS Server Connection</em>{' '}
          dialog doesn’t work with Talus, even with a valid token.
        </span>
      </p>

      {isAdmin && (
        <details className="connect-adminnote">
          <summary>Note for administrators</summary>
          <p>
            Tokens deliberately carry no admin privileges. A desktop app connected with your token
            shows layers <em>owned by or shared with you</em> — not the platform-wide admin view.
            That keeps a lost laptop from becoming a master key; admin work stays in the portal.
          </p>
        </details>
      )}

      {showMint && (
        <MintTokenModal
          accessToken={accessToken}
          onClose={() => setShowMint(false)}
          onMinted={(m) => {
            setShowMint(false)
            setMinted(m)
            void reload()
          }}
        />
      )}

      {minted !== null && <TokenReveal minted={minted} onDone={() => setMinted(null)} />}
      {confirmDialog}
    </>
  )
}

// --- mint modal (also the wizard's credentials-step mint) -------------------------

export function MintTokenModal({
  accessToken,
  onClose,
  onMinted,
  defaultScope = 'read_only',
  namePlaceholder = 'e.g. QGIS on my work laptop',
}: {
  accessToken: string
  onClose: () => void
  onMinted: (minted: PatMinted) => void
  /** The wizard's Edit recipes preselect read_write; everywhere else defaults to read-only. */
  defaultScope?: 'read_only' | 'read_write'
  /** Context-appropriate example (the wizard passes the client, e.g. "ArcGIS Pro on my laptop"). */
  namePlaceholder?: string
}) {
  const [label, setLabel] = useState('')
  const [touched, setTouched] = useState(false)
  const [expires, setExpires] = useState('') // days, blank = never
  const [scope, setScope] = useState<'read_only' | 'read_write'>(defaultScope)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const labelMissing = label.trim() === ''
  const days = expires.trim() === '' ? undefined : Number(expires)
  const daysInvalid = days !== undefined && (!Number.isInteger(days) || days < 1 || days > 3650)

  async function submit(): Promise<void> {
    if (labelMissing) {
      setTouched(true)
      return
    }
    if (daysInvalid) return
    setBusy(true)
    setError(null)
    try {
      onMinted(
        await createMyToken(
          { label: label.trim(), scope, ...(days !== undefined ? { expiresInDays: days } : {}) },
          accessToken,
        ),
      )
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Mint failed')
      setBusy(false)
    }
  }

  return (
    <div className="modal__backdrop" role="presentation" onClick={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label="New access token"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal__header">
          <h2 className="modal__title">New access token</h2>
          <CloseButton onClick={onClose} />
        </div>

        <section className="modal__section">
          <p className="modal__muted" style={{ marginTop: 0 }}>
            You’ll see the new token once, copy it, and paste it as the{' '}
            <strong>password</strong> when your desktop app asks you to sign in.
          </p>
          <label className="sign-form__field">
            <span>
              Name * <span className="modal__muted">(which computer or app is this for?)</span>
            </span>
            <input
              type="text"
              value={label}
              maxLength={80}
              placeholder={namePlaceholder}
              onChange={(e) => setLabel(e.target.value)}
              onBlur={() => setTouched(true)}
              autoFocus
            />
          </label>
          {touched && labelMissing && <p className="modal__field-error">A name is required</p>}
          <label className="sign-form__field">
            <span>
              Expires after <span className="modal__muted">(days, optional — blank = never)</span>
            </span>
            <input
              type="number"
              min={1}
              max={3650}
              value={expires}
              placeholder="e.g. 90"
              onChange={(e) => setExpires(e.target.value)}
            />
          </label>
          {daysInvalid && (
            <p className="modal__field-error">Expiry must be a whole number of days, 1–3650.</p>
          )}
          <fieldset className="sign-form__field" style={{ border: 0, padding: 0, margin: 0 }}>
            <span>Access</span>
            <label style={{ display: 'flex', gap: '0.4rem', alignItems: 'baseline' }}>
              <input
                type="radio"
                name="pat-scope"
                checked={scope === 'read_only'}
                onChange={() => setScope('read_only')}
              />
              <span>
                <strong>Read-only</strong>{' '}
                <span className="modal__muted">
                  — view data from desktop apps and scripts. The safe default.
                </span>
              </span>
            </label>
            <label style={{ display: 'flex', gap: '0.4rem', alignItems: 'baseline' }}>
              <input
                type="radio"
                name="pat-scope"
                checked={scope === 'read_write'}
                onChange={() => setScope('read_write')}
              />
              <span>
                <strong>Read &amp; edit</strong>{' '}
                <span className="modal__muted">
                  — desktop apps can also CHANGE data, but only on layers where you have edit
                  access and editing is on. Pick this only when setting up editing.
                </span>
              </span>
            </label>
          </fieldset>
          <p className="modal__muted">
            A token is your <strong>identity</strong>, not a key to any one layer — it sees
            whatever your account can see. Who can view or edit each layer is controlled in that
            layer’s Sharing settings.
          </p>
        </section>

        {error !== null && <p className="sign-form__error">{error}</p>}

        <div className="sign-form__actions">
          <button type="button" className="btn btn--sm btn--ghost" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn--sm"
            onClick={() => void submit()}
            disabled={busy || labelMissing || daysInvalid}
          >
            {busy ? 'Creating…' : 'Create token'}
          </button>
        </div>
      </div>
    </div>
  )
}

// --- token reveal (shown once) -----------------------------------------------------

export function TokenReveal({ minted, onDone }: { minted: PatMinted; onDone: () => void }) {
  return (
    <div className="modal__backdrop" role="presentation">
      <div className="modal" role="dialog" aria-modal="true" aria-label="Token created">
        <div className="modal__header">
          <h2 className="modal__title">Token created</h2>
        </div>
        <p className="sa-warn">
          You won’t see this again — copy it now. If it’s lost, revoke it and create a new one.
        </p>
        <section className="modal__section">
          <h3 className="modal__section-title">{minted.label}</h3>
          <CopyRow value={minted.token} mono />
          <ol className="use-in__path" style={{ marginTop: '0.6rem' }}>
            <li>
              <strong>Copy</strong> the token above.
            </li>
            <li>
              When ArcGIS Pro or QGIS asks you to sign in: <strong>username</strong> ={' '}
              <code className="sa-code">talus</code> (anything works).
            </li>
            <li>
              <strong>Password</strong> = paste the token. The app remembers it — you won’t be
              asked again on this computer.
            </li>
          </ol>
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

export function CopyRow({ value, mono }: { value: string; mono?: boolean }) {
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
