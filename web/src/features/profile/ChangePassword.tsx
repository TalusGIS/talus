import { useState, type FormEvent } from 'react'

import { ApiError, changeMyPassword } from '@/lib/api'

/**
 * Profile → Password (decision-first-ten-minutes.md F3). The self-service change a customer needs
 * in their first ten minutes: the installer hands them a generated credential, and until now the
 * only way to change it was the ADMIN reset dialog, whose generated-temporary copy is written for
 * resetting someone else.
 *
 * Deliberately in-portal rather than a link to Keycloak's account console: the console renders in
 * Keycloak's own branding (the shipped theme has a `login` sub-theme only), and sending a customer
 * to a differently-branded system to change the password they were handed ninety seconds earlier
 * is exactly the seam this product exists to remove.
 *
 * The current password is required because the server re-authenticates before writing — without
 * that, a lifted access token alone would set a new password and lock the real owner out.
 */

// Matches the shipped realm policy (length(12) + upper + lower + digit + notUsername + notEmail).
// Advisory only: the SERVER is the authority, and its 400 detail names the actual rule if an
// operator has changed the policy on their deployment.
const POLICY_HINT = 'At least 12 characters, including an uppercase letter, a lowercase letter, and a number.'

export default function ChangePasswordSection({ accessToken }: { accessToken: string }) {
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState(false)
  // Counted client-side purely to WARN before the realm's brute-force lockout (failureFactor 10)
  // catches a careful person mid-attempt. Not a security control — the server's counter is.
  const [wrongTries, setWrongTries] = useState(0)

  const mismatch = confirm.length > 0 && next !== confirm
  const sameAsCurrent = next.length > 0 && next === current
  const canSubmit =
    !busy && current.length > 0 && next.length > 0 && confirm.length > 0 && !mismatch && !sameAsCurrent

  async function submit(e: FormEvent): Promise<void> {
    e.preventDefault()
    if (!canSubmit) return
    setBusy(true)
    setError(null)
    try {
      await changeMyPassword(current, next, accessToken)
      setDone(true)
      setWrongTries(0)
      setCurrent('')
      setNext('')
      setConfirm('')
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) {
        setWrongTries((n) => n + 1)
        setError('That current password is not correct.')
      } else if (err instanceof ApiError) {
        // 400 (policy), 409 (SSO account), 429 (locked out), 503 (not configured) all carry a
        // server-written detail that says more than anything we could guess here.
        setError(err.message)
      } else {
        setError('Could not change the password.')
      }
    } finally {
      setBusy(false)
    }
  }

  if (done) {
    return (
      <div className="tokpanel">
        <div className="tokpanel__head">
          <span className="tokpanel__head-title">Password</span>
        </div>
        <div className="tokpanel__empty">
          Your password has been changed. Use the new one the next time you sign in.{' '}
          <button type="button" className="btn btn--sm" onClick={() => setDone(false)}>
            Change it again
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="tokpanel">
      <div className="tokpanel__head">
        <span className="tokpanel__head-title">Password</span>
      </div>

      <form onSubmit={(e) => void submit(e)} style={{ padding: '0.75rem 1rem 1rem' }}>
        <label className="sign-form__field">
          <span>Current password</span>
          <input
            type="password"
            autoComplete="current-password"
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
            disabled={busy}
          />
        </label>

        <label className="sign-form__field">
          <span>
            New password <span className="modal__muted">({POLICY_HINT})</span>
          </span>
          <input
            type="password"
            autoComplete="new-password"
            value={next}
            onChange={(e) => setNext(e.target.value)}
            disabled={busy}
          />
        </label>
        {sameAsCurrent && (
          <p className="modal__field-error">The new password must differ from the current one.</p>
        )}

        <label className="sign-form__field">
          <span>Confirm new password</span>
          <input
            type="password"
            autoComplete="new-password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            disabled={busy}
          />
        </label>
        {mismatch && <p className="modal__field-error">The two new passwords do not match.</p>}

        {error !== null && <p className="sign-form__error">{error}</p>}
        {wrongTries >= 3 && (
          <p className="sa-warn">
            Repeated incorrect attempts will temporarily lock this account out of signing in. If you
            are not sure of your current password, ask an administrator to reset it instead.
          </p>
        )}

        <button type="submit" className="btn btn--sm" disabled={!canSubmit}>
          {busy ? 'Changing…' : 'Change password'}
        </button>
      </form>
    </div>
  )
}
