import { useEffect, useState } from 'react'
import { useAuth } from 'react-oidc-context'

import {
  ApiError,
  fetchOrgSettings,
  updateAdvertisedBaseUrl,
  type OrgSettings,
} from '@/lib/api'

/**
 * Admin → Connections (handoff-admin-1a §3.4, split out of the Settings catch-all). The
 * advertised base URL (Interop I-1) — the address desktop clients and scripts use to reach
 * this server, shown in every "Use in…" recipe. A wrong value here silently breaks every
 * recipe, so verification lives beside the field: Test connection probes the address from
 * THIS browser (an admin's machine is a fair proxy for "other machines"). The probe is a
 * no-cors fetch, so any network-level answer counts as reachable — a 404 still proves DNS,
 * TLS and the host, which is exactly the failure mode a typo produces. Admin-gated
 * server-side; behind RequireAdmin.
 */
type TestResult = { state: 'testing' } | { state: 'done'; ok: boolean } | null

export default function ConnectionsView() {
  const auth = useAuth()
  const token = auth.user?.access_token ?? ''

  const [saved, setSaved] = useState<OrgSettings | null>(null)
  const [url, setUrl] = useState('')
  const [test, setTest] = useState<TestResult>(null)
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
        setUrl(s.advertisedBaseUrl ?? '')
        setError(null)
      })
      .catch((e) => active && setError(e instanceof ApiError ? e.message : 'Failed to load settings'))
    return () => {
      active = false
    }
  }, [token])

  const trimmed = url.trim()
  const invalid =
    trimmed !== '' && !(trimmed.startsWith('http://') || trimmed.startsWith('https://'))
  const dirty = saved !== null && (trimmed || null) !== (saved.advertisedBaseUrl ?? null)

  function edit(next: string): void {
    setUrl(next)
    setJustSaved(false)
    setTest(null) // a result describes the value it tested — clear it the moment that changes
  }

  async function testConnection(): Promise<void> {
    setTest({ state: 'testing' })
    const ctrl = new AbortController()
    const timer = window.setTimeout(() => ctrl.abort(), 5000)
    try {
      await fetch(trimmed, { mode: 'no-cors', cache: 'no-store', signal: ctrl.signal })
      setTest({ state: 'done', ok: true })
    } catch {
      // Network failure, DNS, refused TLS, timeout — or mixed content (an https portal may
      // not probe an http URL), which is itself a real client-side problem worth surfacing.
      setTest({ state: 'done', ok: false })
    } finally {
      window.clearTimeout(timer)
    }
  }

  async function save(): Promise<void> {
    if (invalid) return
    setBusy(true)
    setError(null)
    try {
      const next = await updateAdvertisedBaseUrl(trimmed || null, token)
      setSaved(next)
      setUrl(next.advertisedBaseUrl ?? '')
      setJustSaved(true)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Save failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="admin">
      <h1 className="admin__title">Connections</h1>
      <p className="admin__note">
        The address desktop clients (ArcGIS Pro, QGIS) and scripts use to reach this server —
        shown in the connection recipes the <strong>Use in…</strong> assistant hands out. Set it
        to the URL <em>other machines</em> can reach (e.g. your public hostname, or this
        machine’s LAN address on a test rig). Leave blank to use the portal’s own API address.
      </p>

      {error !== null && <p className="sa__error">{error}</p>}

      {saved === null && error === null ? (
        <p className="sa__muted">Loading…</p>
      ) : (
        <section className="org-settings__card">
          <label className="sign-form__field">
            <span>Advertised base URL</span>
            <input
              type="url"
              value={url}
              maxLength={1000}
              placeholder="e.g. https://gis.example.gov"
              onChange={(e) => edit(e.target.value)}
            />
          </label>
          {invalid && <p className="modal__field-error">Must start with http:// or https://.</p>}

          <div className="connections__test">
            <button
              type="button"
              className="btn btn--sm btn--accent"
              onClick={() => void testConnection()}
              disabled={trimmed === '' || invalid || test?.state === 'testing'}
            >
              {test?.state === 'testing' ? 'Testing…' : 'Test connection'}
            </button>
            {test?.state === 'done' &&
              (test.ok ? (
                <>
                  <span className="sa-badge sa-badge--dot sa-badge--success">Reachable</span>
                  <span className="sa__muted">The address answered from this browser.</span>
                </>
              ) : (
                <>
                  <span className="sa-badge sa-badge--dot sa-badge--danger">Unreachable</span>
                  <span className="sa__muted">
                    No answer from this browser — check the address, DNS, and that the host is
                    up.
                  </span>
                </>
              ))}
          </div>

          <div className="sign-form__actions">
            {justSaved && !dirty && <span className="org-settings__saved">Saved.</span>}
            <button
              type="button"
              className="btn btn--sm"
              onClick={() => void save()}
              disabled={busy || !dirty || invalid}
            >
              {busy ? 'Saving…' : 'Save'}
            </button>
          </div>
        </section>
      )}
    </div>
  )
}
