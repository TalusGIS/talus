import { useEffect, useState } from 'react'
import { useAuth } from 'react-oidc-context'

import {
  ApiError,
  fetchOrgSettings,
  updateOrgSettings,
  type BannerLevel,
  type OrgSettings,
} from '@/lib/api'

/**
 * Admin → Notices (handoff-admin-1a §3.1, split out of the Settings catch-all). The
 * information / maintenance banner — an admin-set message shown to everyone signed in
 * (maintenance window, read-only mode, status alert). PUT /org-settings is admin-gated
 * server-side; this screen sits behind RequireAdmin. The save sends ONLY the three banner
 * fields — the governance policies are optional in the PUT body and omitting them leaves
 * them unchanged, so this page can never clobber Sharing & access.
 */
const LEVELS: ReadonlyArray<{ value: BannerLevel; label: string; hint: string }> = [
  { value: 'info', label: 'Info', hint: 'general notice' },
  { value: 'warning', label: 'Warning', hint: 'maintenance window / read-only mode' },
  { value: 'critical', label: 'Critical', hint: 'urgent / classified-information alert' },
]

export default function NoticesView() {
  const auth = useAuth()
  const token = auth.user?.access_token ?? ''

  const [saved, setSaved] = useState<OrgSettings | null>(null)
  const [enabled, setEnabled] = useState(false)
  const [message, setMessage] = useState('')
  const [level, setLevel] = useState<BannerLevel>('info')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [justSaved, setJustSaved] = useState(false)

  useEffect(() => {
    if (!token) return
    let active = true
    fetchOrgSettings(token)
      .then((s) => {
        if (!active) return
        setSaved(s)
        setEnabled(s.bannerEnabled)
        setMessage(s.bannerMessage ?? '')
        setLevel(s.bannerLevel)
        setError(null)
      })
      .catch((e) => active && setError(e instanceof ApiError ? e.message : 'Failed to load settings'))
    return () => {
      active = false
    }
  }, [token])

  const trimmed = message.trim()
  const messageMissing = enabled && trimmed === ''
  const dirty =
    saved !== null &&
    (enabled !== saved.bannerEnabled ||
      (trimmed || null) !== (saved.bannerMessage ?? null) ||
      level !== saved.bannerLevel)

  function edit(fn: () => void): void {
    fn()
    setJustSaved(false)
  }

  async function save(): Promise<void> {
    if (messageMissing) return
    setBusy(true)
    setError(null)
    try {
      const next = await updateOrgSettings(
        { bannerEnabled: enabled, bannerMessage: trimmed || null, bannerLevel: level },
        token,
      )
      setSaved(next)
      setEnabled(next.bannerEnabled)
      setMessage(next.bannerMessage ?? '')
      setLevel(next.bannerLevel)
      setJustSaved(true)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Save failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="admin">
      <h1 className="admin__title">Notices</h1>
      <p className="admin__note">
        The information banner shown at the top of the portal to everyone signed in — for
        maintenance windows, read-only mode, or status alerts.
      </p>

      {error !== null && <p className="sa__error">{error}</p>}

      {saved === null && error === null ? (
        <p className="sa__muted">Loading…</p>
      ) : (
        <section className="org-settings__card">
          <label className="switch">
            <input
              type="checkbox"
              checked={enabled}
              onChange={(e) => edit(() => setEnabled(e.target.checked))}
            />
            <span className="switch__track" aria-hidden="true" />
            <span>Show a banner at the top of the portal for everyone signed in</span>
          </label>

          <label className="sign-form__field">
            <span>Message{enabled ? ' *' : ''}</span>
            <textarea
              value={message}
              rows={3}
              maxLength={500}
              placeholder="e.g. Scheduled maintenance Saturday 02:00–04:00 ET — the portal will be read-only."
              onChange={(e) => edit(() => setMessage(e.target.value))}
            />
          </label>
          {messageMissing && (
            <p className="modal__field-error">A message is required to show the banner.</p>
          )}

          <fieldset className="org-settings__levels">
            <legend>Level</legend>
            {LEVELS.map((l) => (
              <label key={l.value} className="sa-check">
                <input
                  type="radio"
                  name="banner-level"
                  checked={level === l.value}
                  onChange={() => edit(() => setLevel(l.value))}
                />
                <span>
                  <strong>{l.label}</strong> — {l.hint}
                </span>
              </label>
            ))}
          </fieldset>

          {enabled && trimmed !== '' && (
            <div className="org-settings__preview">
              <p className="org-settings__preview-label">Preview</p>
              <div className={`info-banner info-banner--${level}`} role="status">
                <span className="info-banner__text">{trimmed}</span>
              </div>
            </div>
          )}

          <div className="sign-form__actions">
            <span className="org-settings__saved">
              {justSaved && !dirty ? 'Saved.' : 'Live for everyone on save.'}
            </span>
            <button
              type="button"
              className="btn btn--sm"
              onClick={() => void save()}
              disabled={busy || !dirty || messageMissing}
            >
              {busy ? 'Saving…' : 'Save notice'}
            </button>
          </div>
        </section>
      )}
    </div>
  )
}
