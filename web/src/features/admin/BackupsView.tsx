import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { useAuth } from 'react-oidc-context'

import ConfirmDialog from '@/components/ConfirmDialog'
import {
  ApiError,
  clearBackupDestination,
  downloadBackup,
  fetchBackups,
  runBackupNow,
  testBackupDestination,
  updateBackupDestination,
  updateBackupSettings,
  type BackupArtifact,
  type BackupConfig,
  type BackupDestinationTestResult,
  type BackupDestinationUpdate,
  type BackupSettingsUpdate,
  type BackupsResponse,
} from '@/lib/api'

/**
 * Admin → Backups (Talus 40). The disaster-recovery catalog: one row per backup artifact (a pg_dump plus
 * the incremental media archive), with two SAFE actions — "Back up now" (additive) and Download (for the
 * runbook / an off-host copy). Talus 43 made the schedule/retention/encryption knobs editable here; the
 * DESTINATION is editable too (decision-backup-destination.md): its own card with a connection test, so a
 * non-technical admin can point backups off-host with no .env edit and no restart. The secret key is
 * write-only — the form never shows it, only "configured". There is intentionally NO restore button —
 * restore is a documented, backup-gated operator runbook (docs/dr-restore-runbook.md). Admin-gated
 * server-side; this screen sits behind RequireAdmin.
 */
function fmtDateTime(iso: string | null): string {
  if (!iso) return 'never'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString()
}

function fmtBytes(n: number | null): string {
  if (!n || n <= 0) return '—'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let v = n
  let u = 0
  while (v >= 1024 && u < units.length - 1) {
    v /= 1024
    u += 1
  }
  return `${v.toFixed(u === 0 ? 0 : 1)} ${units[u]}`
}

function fmtInterval(seconds: number): string {
  if (seconds % 86400 === 0) return `${seconds / 86400}d`
  if (seconds % 3600 === 0) return `${seconds / 3600}h`
  return `${seconds}s`
}

// --- editable settings (Talus 43) -------------------------------------------------------------------
// Friendly cadence presets -> seconds (decision-backup-settings.md frontend). The current effective value
// is added as an option if it isn't one of these (e.g. a custom env interval), so the select never loses it.
const CADENCE_PRESETS: { label: string; seconds: number }[] = [
  { label: 'Every 6 hours', seconds: 21600 },
  { label: 'Every 12 hours', seconds: 43200 },
  { label: 'Daily', seconds: 86400 },
  { label: 'Weekly', seconds: 604800 },
]

function cadenceOptions(current: number): { label: string; seconds: number }[] {
  const opts = [...CADENCE_PRESETS]
  if (!opts.some((o) => o.seconds === current)) {
    opts.unshift({ label: `Current (${fmtInterval(current)})`, seconds: current })
  }
  return opts
}

/** Plain-language cadence for the at-a-glance line, e.g. "daily" / "every 12 hours". */
function fmtCadence(seconds: number): string {
  const p = CADENCE_PRESETS.find((o) => o.seconds === seconds)
  return p ? p.label.toLowerCase() : `every ${fmtInterval(seconds)}`
}

/** Split the recipients textarea on whitespace/commas into trimmed, non-empty keys. */
function parseRecipients(text: string): string[] {
  return text
    .split(/[\s,]+/)
    .map((s) => s.trim())
    .filter(Boolean)
}

/** The editable-form state. Each knob carries a "use deployment default" flag (true = clear the override =
 * fall back to env) plus the custom value used when the flag is off. Initialized from the effective config:
 * useDefault = NOT overridden; the custom value starts at the current effective value (a sensible default if
 * the admin then unchecks "use default"). */
type SettingsForm = {
  intervalUseDefault: boolean
  intervalSeconds: number
  retentionUseDefault: boolean
  retentionDays: number
  minKeepUseDefault: boolean
  retentionMinKeep: number
  enabledUseDefault: boolean
  scheduledEnabled: boolean
  recipientsUseDefault: boolean
  ageRecipientsText: string
  includeEnvUseDefault: boolean
  includeEnv: boolean
}

/** Live count of recipients implied by the form (custom textarea, or the env default when "use default" is
 * on) — drives whether the `.env` lifeboat toggle is available (encryption must be on). */
function formRecipientCount(f: SettingsForm, cfg: BackupConfig): number {
  return f.recipientsUseDefault
    ? cfg.envDefaults.ageRecipients.length
    : parseRecipients(f.ageRecipientsText).length
}

function formFromConfig(cfg: BackupConfig): SettingsForm {
  return {
    intervalUseDefault: !cfg.overridden.intervalSeconds,
    intervalSeconds: cfg.intervalSeconds,
    retentionUseDefault: !cfg.overridden.retentionDays,
    retentionDays: cfg.retentionDays,
    minKeepUseDefault: !cfg.overridden.retentionMinKeep,
    retentionMinKeep: cfg.retentionMinKeep,
    enabledUseDefault: !cfg.overridden.scheduledEnabled,
    scheduledEnabled: cfg.enabled,
    recipientsUseDefault: !cfg.overridden.ageRecipients,
    ageRecipientsText: cfg.ageRecipients.join('\n'),
    includeEnvUseDefault: !cfg.overridden.includeEnv,
    includeEnv: cfg.includeEnv,
  }
}

/** Build the PUT body: a knob set to "use default" sends null (clears the override -> env). */
function bodyFromForm(f: SettingsForm): BackupSettingsUpdate {
  return {
    intervalSeconds: f.intervalUseDefault ? null : f.intervalSeconds,
    retentionDays: f.retentionUseDefault ? null : f.retentionDays,
    retentionMinKeep: f.minKeepUseDefault ? null : f.retentionMinKeep,
    scheduledEnabled: f.enabledUseDefault ? null : f.scheduledEnabled,
    ageRecipients: f.recipientsUseDefault ? null : parseRecipients(f.ageRecipientsText),
    includeEnv: f.includeEnvUseDefault ? null : f.includeEnv,
  }
}

// --- editable destination (decision-backup-destination.md) -------------------------------------------

/** The destination form. secretKey is WRITE-ONLY: '' means "keep the stored secret" when one exists
 * (the PUT sends null), and is simply not-yet-entered otherwise (Save/Test stay disabled). */
type DestForm = {
  endpoint: string
  bucket: string
  accessKey: string
  secretKey: string
}

/** PUT/test body from the form: blank bucket -> null (deployment default); blank secret -> null (keep). */
function destBody(f: DestForm): BackupDestinationUpdate {
  return {
    endpoint: f.endpoint.trim(),
    bucket: f.bucket.trim() ? f.bucket.trim() : null,
    accessKey: f.accessKey.trim(),
    secretKey: f.secretKey.trim() ? f.secretKey.trim() : null,
  }
}

/** One editable knob: label + a "use deployment default" checkbox (clears the override) + the custom
 * control shown only when not using the default. */
function Knob({
  label,
  useDefault,
  onUseDefault,
  defaultText,
  children,
}: {
  label: string
  useDefault: boolean
  onUseDefault: (v: boolean) => void
  defaultText: string
  children: ReactNode
}) {
  return (
    <div style={{ marginBottom: '0.9rem' }}>
      <div style={{ fontWeight: 600, marginBottom: '0.25rem' }}>{label}</div>
      <label style={{ display: 'inline-flex', gap: '0.4rem', alignItems: 'center' }}>
        <input type="checkbox" checked={useDefault} onChange={(e) => onUseDefault(e.target.checked)} />
        Use deployment default <span className="sa__muted">({defaultText})</span>
      </label>
      {!useDefault && <div style={{ marginTop: '0.4rem' }}>{children}</div>}
    </div>
  )
}

function StatusPill({ status }: { status: string | null }) {
  if (!status) return <span className="sa__muted">—</span>
  const ok = status === 'ok'
  return (
    <span style={{ color: ok ? 'var(--success-600)' : 'var(--danger-600)', fontWeight: 600 }}>
      {ok ? 'OK' : 'Error'}
    </span>
  )
}

export default function BackupsView() {
  const auth = useAuth()
  const token = auth.user?.access_token ?? ''

  const [data, setData] = useState<BackupsResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const pollRef = useRef<number | null>(null)

  // Editable settings (Talus 43): an inline form opened by "Edit settings".
  const [editing, setEditing] = useState(false)
  const [form, setForm] = useState<SettingsForm | null>(null)
  const [saving, setSaving] = useState(false)

  // Editable destination (decision-backup-destination.md): its own card + connection test.
  const [destEditing, setDestEditing] = useState(false)
  const [destForm, setDestForm] = useState<DestForm | null>(null)
  const [destSaving, setDestSaving] = useState(false)
  const [destTesting, setDestTesting] = useState(false)
  const [destTest, setDestTest] = useState<BackupDestinationTestResult | null>(null)

  const load = useCallback(async (): Promise<BackupsResponse | null> => {
    if (!token) return null
    const next = await fetchBackups(token)
    setData(next)
    return next
  }, [token])

  const reload = useCallback(async () => {
    setError(null)
    try {
      await load()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to load backups')
    }
  }, [load])

  useEffect(() => {
    void reload()
    return () => {
      if (pollRef.current !== null) window.clearInterval(pollRef.current)
    }
  }, [reload])

  // "Back up now" runs in the background (202), so after triggering we poll the catalog until a NEWER row
  // than the one we had before the click appears (or a ~90s timeout), then stop the spinner.
  const backUpNow = useCallback(async () => {
    if (!token || busy) return
    setError(null)
    setNotice(null)
    const prevTopId = data?.backups[0]?.id ?? 0
    try {
      await runBackupNow(token)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to start backup')
      return
    }
    setBusy(true)
    setNotice('Backup started — this can take a moment for a large database…')
    let ticks = 0
    pollRef.current = window.setInterval(async () => {
      ticks += 1
      try {
        const next = await load()
        const top = next?.backups[0]
        if (top && top.id !== prevTopId) {
          if (pollRef.current !== null) window.clearInterval(pollRef.current)
          pollRef.current = null
          setBusy(false)
          setNotice(top.status === 'ok' ? 'Backup complete.' : 'Backup finished with an error — see the row below.')
        }
      } catch {
        /* transient; keep polling */
      }
      if (ticks >= 30 && pollRef.current !== null) {
        window.clearInterval(pollRef.current)
        pollRef.current = null
        setBusy(false)
        setNotice('Still running — use Refresh to check on it.')
      }
    }, 3000)
  }, [token, busy, data, load])

  // Download a backup as a set: the database dumps are a coherent unit (a restore needs data + logins +
  // sharing together), so one click fetches every dump present in the set. Each saves as its own file.
  const onDownload = useCallback(
    async (id: number, hasKeycloak: boolean, hasOpenfga: boolean) => {
      if (!token) return
      setError(null)
      // The DATABASE dumps only (data + identity + authz) — a coherent restore unit. The `.env` lifeboat is
      // deliberately NOT bundled here: it's live infra secrets, downloaded on its own at full DR (below).
      const artifacts: BackupArtifact[] = ['db']
      if (hasKeycloak) artifacts.push('keycloak')
      if (hasOpenfga) artifacts.push('openfga')
      try {
        for (const artifact of artifacts) {
          await downloadBackup(token, id, artifact)
        }
      } catch (err) {
        setError(err instanceof ApiError ? err.message : 'Failed to download backup')
      }
    },
    [token],
  )

  // The `.env` lifeboat (Talus 45) downloads on its OWN, separate from the bulk Download — it holds live,
  // reusable infra creds, so it shouldn't be grabbed casually; you only need it at a full from-zero restore.
  const onDownloadEnv = useCallback(
    async (id: number) => {
      if (!token) return
      setError(null)
      try {
        await downloadBackup(token, id, 'env')
      } catch (err) {
        setError(err instanceof ApiError ? err.message : 'Failed to download .env')
      }
    },
    [token],
  )

  const startEdit = useCallback(() => {
    const c = data?.config
    if (!c) return
    setForm(formFromConfig(c))
    setEditing(true)
    setError(null)
    setNotice(null)
  }, [data])

  const cancelEdit = useCallback(() => {
    setEditing(false)
    setForm(null)
  }, [])

  // Set every knob to "use deployment default" (a one-click revert-all; Save then clears all overrides).
  const useAllDefaults = useCallback(() => {
    setForm((f) =>
      f === null
        ? f
        : {
            ...f,
            intervalUseDefault: true,
            retentionUseDefault: true,
            minKeepUseDefault: true,
            enabledUseDefault: true,
            recipientsUseDefault: true,
            includeEnvUseDefault: true,
          },
    )
  }, [])

  const save = useCallback(async () => {
    if (!token || !form || saving) return
    setSaving(true)
    setError(null)
    try {
      const updated = await updateBackupSettings(token, bodyFromForm(form))
      setData(updated)
      setEditing(false)
      setForm(null)
      setNotice('Settings saved. They take effect automatically from the next scheduled backup — nothing to restart.')
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to save settings')
    } finally {
      setSaving(false)
    }
  }, [token, form, saving])

  // --- destination handlers (decision-backup-destination.md) ---------------------------------------

  const startDestEdit = useCallback(() => {
    const d = data?.config.destination
    if (!d) return
    setDestForm({
      // Editing an existing override starts from its values; starting fresh from an env/local
      // destination starts blank (env S3 credentials are never disclosed to the browser).
      endpoint: d.source === 'override' ? d.endpoint : '',
      bucket: d.source === 'override' ? d.bucket : '',
      accessKey: d.source === 'override' ? d.accessKey : '',
      secretKey: '',
    })
    setDestTest(null)
    setDestEditing(true)
    setError(null)
    setNotice(null)
  }, [data])

  const cancelDestEdit = useCallback(() => {
    setDestEditing(false)
    setDestForm(null)
    setDestTest(null)
  }, [])

  const testDest = useCallback(async () => {
    if (!token || !destForm || destTesting) return
    setDestTesting(true)
    setDestTest(null)
    setError(null)
    try {
      setDestTest(await testBackupDestination(token, destBody(destForm)))
    } catch (err) {
      // Validation (422) and auth errors surface here; a failed PROBE comes back as ok:false instead.
      setDestTest({ ok: false, detail: err instanceof ApiError ? err.message : 'Test failed to run' })
    } finally {
      setDestTesting(false)
    }
  }, [token, destForm, destTesting])

  const saveDest = useCallback(async () => {
    if (!token || !destForm || destSaving) return
    setDestSaving(true)
    setError(null)
    try {
      const updated = await updateBackupDestination(token, destBody(destForm))
      setData(updated)
      setDestEditing(false)
      setDestForm(null)
      setDestTest(null)
      setNotice(
        'Destination saved — the next backup (scheduled, or “Back up now” above) writes there. Nothing to restart.',
      )
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to save destination')
    } finally {
      setDestSaving(false)
    }
  }, [token, destForm, destSaving])

  // Reversible (re-enter the values to come back), but confirmed via ConfirmDialog (§5):
  // the next backup lands elsewhere.
  const [confirmRevert, setConfirmRevert] = useState(false)

  const revertDest = useCallback(async () => {
    if (!token || destSaving) return
    setDestSaving(true)
    setError(null)
    try {
      const updated = await clearBackupDestination(token)
      setData(updated)
      setDestEditing(false)
      setDestForm(null)
      setDestTest(null)
      setNotice('Destination reverted to the deployment default.')
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to revert destination')
    } finally {
      setDestSaving(false)
      setConfirmRevert(false)
    }
  }, [token, destSaving])

  const cfg = data?.config

  // Live availability of the `.env` lifeboat toggle within the edit form: encryption must be on (≥1 recipient,
  // reacting to edits in the same form) AND the api must be able to read the mounted `.env`.
  const lifeboat =
    cfg && form
      ? {
          encryptionOn: formRecipientCount(form, cfg) > 0,
          fileReadable: cfg.includeEnvFileReadable,
          available: formRecipientCount(form, cfg) > 0 && cfg.includeEnvFileReadable,
        }
      : null

  return (
    <div className="admin">
      <div className="sa__head">
        <div>
          <h1 className="admin__title">Backups</h1>
          <p className="admin__note">
            Scheduled disaster-recovery backups: a full copy of all your databases — your GIS data, your user
            logins, and your sharing and permissions — plus your attachments, written to object storage.
          </p>
        </div>
        <div style={{ display: 'flex', gap: '0.5rem' }}>
          <button type="button" className="btn" onClick={() => void backUpNow()} disabled={busy}>
            {busy ? 'Backing up…' : 'Back up now'}
          </button>
          <button type="button" className="btn" onClick={() => void reload()}>
            Refresh
          </button>
        </div>
      </div>

      {error !== null && <p className="sa__error">{error}</p>}
      {notice !== null && <p className="admin__note">{notice}</p>}

      {cfg && !cfg.offHost && (
        <p
          className="sa__error"
          style={{ background: 'var(--warning-50)', color: 'var(--warning-600)' }}
        >
          ⚠ Backups are stored on the same host as your live data — this protects against accidental deletes
          and bad upgrades, but <strong>not host loss</strong>. To send them off-host (e.g. Backblaze B2,
          AWS S3, Wasabi, or a NAS with an S3 endpoint), set the destination in the{' '}
          <strong>Destination</strong> section below — no <code>.env</code> edit, nothing to restart. See{' '}
          <code>docs/dr-hardening.md</code> for the full off-host, encryption, and ransomware-hardening
          guide (3-2-1, immutable buckets, key custody).
        </p>
      )}

      {cfg && (
        <section
          style={{
            border: '1px solid var(--border-default)',
            borderRadius: '8px',
            padding: '1rem',
            marginTop: '0.5rem',
            marginBottom: '1rem',
          }}
        >
          <div className="sa__head" style={{ alignItems: 'center' }}>
            <h2 className="admin__title" style={{ fontSize: '1.05rem', margin: 0 }}>
              Settings
            </h2>
            {!editing && (
              <button type="button" className="btn" onClick={startEdit}>
                Edit settings
              </button>
            )}
          </div>

          {!editing ? (
            <p className="admin__note" style={{ marginTop: '0.5rem' }}>
              Change how often backups run, how long they're kept, who can decrypt them, and whether scheduled
              backups are on. Each setting can follow the deployment default or use a custom value — changes
              take effect automatically, with nothing to restart. <em>Where</em> backups go is its own section
              below.
            </p>
          ) : (
            form && (
              <form
                onSubmit={(e) => {
                  e.preventDefault()
                  void save()
                }}
                style={{ marginTop: '0.75rem', maxWidth: '36rem' }}
              >
                <Knob
                  label="Scheduled backups"
                  useDefault={form.enabledUseDefault}
                  onUseDefault={(v) => setForm({ ...form, enabledUseDefault: v })}
                  defaultText={cfg.envDefaults.scheduledEnabled ? 'on' : 'off'}
                >
                  <label style={{ display: 'inline-flex', gap: '0.4rem', alignItems: 'center' }}>
                    <input
                      type="checkbox"
                      checked={form.scheduledEnabled}
                      onChange={(e) => setForm({ ...form, scheduledEnabled: e.target.checked })}
                    />
                    Run scheduled backups (“Back up now” always works regardless)
                  </label>
                </Knob>

                <Knob
                  label="Schedule cadence"
                  useDefault={form.intervalUseDefault}
                  onUseDefault={(v) => setForm({ ...form, intervalUseDefault: v })}
                  defaultText={fmtInterval(cfg.envDefaults.intervalSeconds)}
                >
                  <select
                    value={form.intervalSeconds}
                    onChange={(e) => setForm({ ...form, intervalSeconds: Number(e.target.value) })}
                  >
                    {cadenceOptions(form.intervalSeconds).map((o) => (
                      <option key={o.seconds} value={o.seconds}>
                        {o.label}
                      </option>
                    ))}
                  </select>
                </Knob>

                <Knob
                  label="Retention — keep backups for (days)"
                  useDefault={form.retentionUseDefault}
                  onUseDefault={(v) => setForm({ ...form, retentionUseDefault: v })}
                  defaultText={`${cfg.envDefaults.retentionDays} days`}
                >
                  <input
                    type="number"
                    min={0}
                    value={form.retentionDays}
                    onChange={(e) =>
                      setForm({ ...form, retentionDays: Math.max(0, Number(e.target.value)) })
                    }
                    style={{ width: '6rem' }}
                  />
                </Knob>

                <Knob
                  label="Retention — always keep at least (most-recent backups)"
                  useDefault={form.minKeepUseDefault}
                  onUseDefault={(v) => setForm({ ...form, minKeepUseDefault: v })}
                  defaultText={String(cfg.envDefaults.retentionMinKeep)}
                >
                  <input
                    type="number"
                    min={1}
                    value={form.retentionMinKeep}
                    onChange={(e) =>
                      setForm({ ...form, retentionMinKeep: Math.max(1, Number(e.target.value)) })
                    }
                    style={{ width: '6rem' }}
                  />
                  <p className="sa__muted" style={{ margin: '0.3rem 0 0' }}>
                    The floor — never pruned below this many, even past the retention age. Minimum 1.
                  </p>
                </Knob>

                <Knob
                  label="Encryption recipients (age public keys)"
                  useDefault={form.recipientsUseDefault}
                  onUseDefault={(v) => setForm({ ...form, recipientsUseDefault: v })}
                  defaultText={
                    cfg.envDefaults.ageRecipients.length
                      ? `${cfg.envDefaults.ageRecipients.length} recipient(s)`
                      : 'none — encryption off'
                  }
                >
                  <textarea
                    rows={3}
                    value={form.ageRecipientsText}
                    onChange={(e) => setForm({ ...form, ageRecipientsText: e.target.value })}
                    placeholder={'age1… (one PUBLIC key per line)'}
                    style={{ width: '100%', fontFamily: 'var(--font-mono)' }}
                  />
                  <p className="sa__muted" style={{ margin: '0.3rem 0 0' }}>
                    One <code>age1…</code> PUBLIC key per line. Leave empty to disable encryption. The matching
                    PRIVATE key is kept off-host and used only at restore — this system cannot decrypt its own
                    backups.
                  </p>
                </Knob>

                <Knob
                  label="Include the .env file (lifeboat) — requires encryption"
                  useDefault={form.includeEnvUseDefault}
                  onUseDefault={(v) => setForm({ ...form, includeEnvUseDefault: v })}
                  defaultText={cfg.envDefaults.includeEnv ? 'included' : 'not included'}
                >
                  <label
                    style={{
                      display: 'inline-flex',
                      gap: '0.4rem',
                      alignItems: 'center',
                      opacity: lifeboat?.available ? 1 : 0.6,
                    }}
                  >
                    <input
                      type="checkbox"
                      checked={form.includeEnv}
                      disabled={!lifeboat?.available}
                      onChange={(e) => setForm({ ...form, includeEnv: e.target.checked })}
                    />
                    Include the encrypted <code>.env</code> in each backup
                  </label>
                  {!lifeboat?.fileReadable ? (
                    <p className="sa__error" style={{ margin: '0.3rem 0 0' }}>
                      The API can’t read <code>.env</code> — mount it read-only
                      (<code>./.env:/secrets/.env:ro</code>) in your deployment before enabling.
                    </p>
                  ) : !lifeboat?.encryptionOn ? (
                    <p className="sa__muted" style={{ margin: '0.3rem 0 0' }}>
                      Inactive until encryption is on — add at least one <code>age</code> recipient above. The{' '}
                      <code>.env</code> is written <strong>only</strong> as ciphertext, never plaintext.
                    </p>
                  ) : (
                    <p className="sa__muted" style={{ margin: '0.3rem 0 0' }}>
                      Bundles the live <code>.env</code> (encrypted) into each backup so one set is
                      self-contained — you then safeguard only the <code>age</code> private key off-host, not the{' '}
                      <code>.env</code> too. It holds live infra secrets, so keep custody tight (a second
                      break-glass recipient is wise).
                    </p>
                  )}
                </Knob>

                <div
                  style={{ display: 'flex', gap: '0.5rem', marginTop: '0.5rem', alignItems: 'center' }}
                >
                  <button type="submit" className="btn" disabled={saving}>
                    {saving ? 'Saving…' : 'Save settings'}
                  </button>
                  <button type="button" className="btn" onClick={cancelEdit} disabled={saving}>
                    Cancel
                  </button>
                  <button
                    type="button"
                    className="btn"
                    onClick={useAllDefaults}
                    disabled={saving}
                    title="Set every knob back to the deployment default"
                    style={{ marginLeft: 'auto' }}
                  >
                    Use deployment defaults
                  </button>
                </div>
              </form>
            )
          )}
        </section>
      )}

      {cfg && (
        <section
          style={{
            border: '1px solid var(--border-default)',
            borderRadius: '8px',
            padding: '1rem',
            marginBottom: '1rem',
          }}
        >
          <div className="sa__head" style={{ alignItems: 'center' }}>
            <h2 className="admin__title" style={{ fontSize: '1.05rem', margin: 0 }}>
              Destination
            </h2>
            {!destEditing && (
              <div style={{ display: 'flex', gap: '0.5rem' }}>
                <button type="button" className="btn" onClick={startDestEdit}>
                  {cfg.destination.source === 'override' ? 'Edit destination' : 'Change destination'}
                </button>
                {cfg.destination.source === 'override' && (
                  <button
                    type="button"
                    className="btn"
                    onClick={() => setConfirmRevert(true)}
                    disabled={destSaving}
                    title={`Revert to the deployment default (${cfg.envDefaults.destinationTarget})`}
                  >
                    Use deployment default
                  </button>
                )}
              </div>
            )}
          </div>

          {!destEditing ? (
            <p className="admin__note" style={{ marginTop: '0.5rem' }}>
              Backups currently go to <code>{cfg.destination.target}</code>{' '}
              {cfg.destination.source === 'override' ? (
                <>— set here in the portal{cfg.destination.secretKeySet && ', credentials configured'}.</>
              ) : (
                <>— the deployment default.</>
              )}{' '}
              Point them at any S3-compatible store (Backblaze B2, AWS S3, Wasabi, MinIO, or a NAS with an
              S3 endpoint): changes take effect from the next backup, with nothing to restart. Backups
              already written stay where they are.
            </p>
          ) : (
            destForm && (
              <form
                onSubmit={(e) => {
                  e.preventDefault()
                  void saveDest()
                }}
                style={{ marginTop: '0.75rem', maxWidth: '36rem' }}
              >
                <div style={{ marginBottom: '0.9rem' }}>
                  <div style={{ fontWeight: 600, marginBottom: '0.25rem' }}>Endpoint URL</div>
                  <input
                    type="url"
                    value={destForm.endpoint}
                    onChange={(e) => setDestForm({ ...destForm, endpoint: e.target.value })}
                    placeholder="https://s3.us-west-004.backblazeb2.com"
                    style={{ width: '100%' }}
                  />
                  <p className="sa__muted" style={{ margin: '0.3rem 0 0' }}>
                    Must start with <code>https://</code> (or <code>http://</code> for a NAS on your own
                    network) — just the server, no bucket or path.
                  </p>
                  {destForm.endpoint.trim().startsWith('http://') && (
                    <p
                      className="sa__error"
                      style={{
                        background: 'var(--warning-50)',
                        color: 'var(--warning-600)',
                        margin: '0.3rem 0 0',
                      }}
                    >
                      ⚠ <code>http://</code> sends backups <strong>unencrypted in transit</strong> — use it
                      only for a device on your own trusted network.
                    </p>
                  )}
                </div>

                <div style={{ marginBottom: '0.9rem' }}>
                  <div style={{ fontWeight: 600, marginBottom: '0.25rem' }}>Bucket</div>
                  <input
                    type="text"
                    value={destForm.bucket}
                    onChange={(e) => setDestForm({ ...destForm, bucket: e.target.value })}
                    placeholder={`leave blank for the default (${cfg.destination.bucket})`}
                    style={{ width: '100%' }}
                  />
                </div>

                <div style={{ marginBottom: '0.9rem' }}>
                  <div style={{ fontWeight: 600, marginBottom: '0.25rem' }}>Access key ID</div>
                  <input
                    type="text"
                    value={destForm.accessKey}
                    onChange={(e) => setDestForm({ ...destForm, accessKey: e.target.value })}
                    autoComplete="off"
                    style={{ width: '100%', fontFamily: 'var(--font-mono)' }}
                  />
                </div>

                <div style={{ marginBottom: '0.9rem' }}>
                  <div style={{ fontWeight: 600, marginBottom: '0.25rem' }}>Secret key</div>
                  <input
                    type="password"
                    value={destForm.secretKey}
                    onChange={(e) => setDestForm({ ...destForm, secretKey: e.target.value })}
                    autoComplete="new-password"
                    placeholder={
                      cfg.destination.source === 'override' && cfg.destination.secretKeySet
                        ? 'configured — leave blank to keep the current secret'
                        : ''
                    }
                    style={{ width: '100%', fontFamily: 'var(--font-mono)' }}
                  />
                  <p className="sa__muted" style={{ margin: '0.3rem 0 0' }}>
                    Stored encrypted; never shown again after saving — the Test button is how you verify it.
                  </p>
                </div>

                {!cfg.encryptionEnabled && (
                  <p
                    className="sa__error"
                    style={{
                      background: 'var(--warning-50)',
                      color: 'var(--warning-600)',
                    }}
                  >
                    ⚠ Backup encryption is <strong>off</strong>, so backups arrive at this destination as
                    plaintext copies of your data. Consider adding <code>age</code> encryption recipients in
                    Settings above before sending backups off-site.
                  </p>
                )}

                {destTest && (
                  <p
                    className={destTest.ok ? 'admin__note' : 'sa__error'}
                    style={destTest.ok ? { color: 'var(--success-600)', fontWeight: 600 } : undefined}
                  >
                    {destTest.ok ? '✓ Connection test passed — ' : '✗ Connection test failed — '}
                    {destTest.detail}
                  </p>
                )}

                <div
                  style={{ display: 'flex', gap: '0.5rem', marginTop: '0.5rem', alignItems: 'center' }}
                >
                  <button
                    type="button"
                    className="btn"
                    onClick={() => void testDest()}
                    disabled={
                      destTesting ||
                      destSaving ||
                      !destForm.endpoint.trim() ||
                      !destForm.accessKey.trim() ||
                      (!destForm.secretKey.trim() &&
                        !(cfg.destination.source === 'override' && cfg.destination.secretKeySet))
                    }
                  >
                    {destTesting ? 'Testing…' : 'Test connection'}
                  </button>
                  <button
                    type="submit"
                    className="btn"
                    disabled={
                      destSaving ||
                      destTesting ||
                      !destForm.endpoint.trim() ||
                      !destForm.accessKey.trim() ||
                      (!destForm.secretKey.trim() &&
                        !(cfg.destination.source === 'override' && cfg.destination.secretKeySet))
                    }
                  >
                    {destSaving ? 'Saving…' : 'Save destination'}
                  </button>
                  <button type="button" className="btn" onClick={cancelDestEdit} disabled={destSaving}>
                    Cancel
                  </button>
                </div>
                <p className="sa__muted" style={{ margin: '0.5rem 0 0' }}>
                  The test writes one tiny probe file to the bucket and removes it — run it before saving,
                  then use “Back up now” to land a real backup there.
                </p>
              </form>
            )
          )}
        </section>
      )}

      {cfg && (
        <p className="admin__note">
          <strong>In effect now:</strong> backs up {fmtCadence(cfg.intervalSeconds)} · keeps backups{' '}
          {cfg.retentionDays} days (never fewer than {cfg.retentionMinKeep}) · stores to{' '}
          <code>{cfg.target}</code> · includes database
          {cfg.includesKeycloak && ' + identity'}
          {cfg.includesOpenfga && ' + authz'} + media ({cfg.objectBuckets.join(', ')}) · encryption{' '}
          {cfg.encryptionEnabled
            ? `🔒 on (${cfg.encryptionRecipients} recipient${cfg.encryptionRecipients === 1 ? '' : 's'})`
            : 'off'}
          {' · '}
          {cfg.includeEnvEffective
            ? '.env lifeboat 🛟 included (encrypted)'
            : cfg.includeEnv
              ? '.env lifeboat on but INACTIVE (encryption off)'
              : '.env not included'}
          {!cfg.enabled && ' · scheduled backups PAUSED'}
        </p>
      )}

      <h2
        className="admin__title"
        style={{ fontSize: '1.05rem', marginTop: '1.5rem', marginBottom: '0.25rem' }}
      >
        Recovery
      </h2>
      <p className="admin__note">
        Restoring from a backup is a deliberate, documented procedure (see{' '}
        <code>docs/dr-restore-runbook.md</code>) — there's intentionally <strong>no one-click restore
        button</strong>, so live data can never be overwritten by accident.
      </p>
      {cfg?.includeEnvEffective ? (
        <p className="admin__note">
          <strong>Lifeboat on 🛟</strong> — these backups also include your <code>.env</code> (the master key:
          secrets + IDs a restore must match), written <strong>only as <code>age</code> ciphertext</strong>. So
          one backup set is self-contained: keep just the <code>age</code> <strong>private key</strong> off-host
          (never in a backup) and it opens everything. Download <code>.env</code> on its own, below. See{' '}
          <code>docs/dr-restore-runbook.md</code>.
        </p>
      ) : (
        <p className="admin__note">
          Backups capture all of Talus's databases (data, identity, authz) + media — but <strong>not your{' '}
          <code>.env</code></strong> file, which holds the secrets and IDs a restore needs to match. Keep{' '}
          <code>.env</code> somewhere safe of its own; it's the master key. (Turn on the <em>“Include the .env
          file (lifeboat)”</em> setting above, with encryption, to fold an encrypted copy into each backup
          instead.) See <code>docs/dr-restore-runbook.md</code>.
        </p>
      )}
      {cfg?.encryptionEnabled && (
        <p className="admin__note">
          Encryption is on, so the files you download are <strong>ciphertext</strong>. Decrypt them with your{' '}
          <code>age</code> private key (kept off-host) per <code>docs/dr-restore-runbook.md</code>; this
          system cannot decrypt them itself.
        </p>
      )}

      {data === null ? (
        <p className="sa__muted">Loading…</p>
      ) : data.backups.length === 0 ? (
        <p className="sa__muted">No backups yet. The first scheduled backup runs on the configured cadence, or click “Back up now”.</p>
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th title="The backup's number — the restore drill and single-item recovery take it as --backup-id">ID</th>
              <th>Taken</th>
              <th>Kind</th>
              <th>Status</th>
              <th>DB size</th>
              <th>Media added</th>
              <th>Target</th>
              <th>Detail</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {data.backups.map((b) => (
              <tr key={b.id}>
                <td>{b.id}</td>
                <td>{fmtDateTime(b.createdAt)}</td>
                <td>{b.kind}</td>
                <td>
                  <StatusPill status={b.status} />
                </td>
                <td>
                  {fmtBytes(b.dbSizeBytes)}
                  {b.encrypted && (
                    <span title="Encrypted (age)" style={{ marginLeft: '0.35rem' }}>
                      🔒
                    </span>
                  )}
                  {b.envInBackup && (
                    <span title="Includes the .env lifeboat (encrypted)" style={{ marginLeft: '0.35rem' }}>
                      🛟
                    </span>
                  )}
                </td>
                <td>
                  {b.mediaObjectsCopied > 0
                    ? `${b.mediaObjectsCopied} (${fmtBytes(b.mediaBytesCopied)})`
                    : '—'}
                </td>
                <td>{b.target}</td>
                <td>{b.detail ?? <span className="sa__muted">—</span>}</td>
                <td>
                  {b.status === 'ok' && b.dbObjectKey ? (
                    <div style={{ display: 'flex', gap: '0.35rem', flexWrap: 'wrap' }}>
                      <button
                        type="button"
                        className="btn btn--sm"
                        title="Download this backup — the database dumps it contains (data, identity, authorization), as separate files"
                        onClick={() => void onDownload(b.id, b.keycloakSizeBytes > 0, b.openfgaSizeBytes > 0)}
                      >
                        Download
                      </button>
                      {b.envInBackup && (
                        <button
                          type="button"
                          className="btn btn--sm"
                          title="Download the encrypted .env lifeboat on its own — live infra secrets; decrypt off-host with your age private key. Only needed at a full from-zero restore."
                          onClick={() => void onDownloadEnv(b.id)}
                        >
                          🛟 .env (encrypted)
                        </button>
                      )}
                    </div>
                  ) : (
                    <span className="sa__muted">—</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {confirmRevert && (
        <ConfirmDialog
          title="Revert to the deployment default destination?"
          confirmLabel="Revert destination"
          busy={destSaving}
          onConfirm={() => void revertDest()}
          onCancel={() => setConfirmRevert(false)}
          consequence={
            <>
              Next backups write to <code>{cfg?.envDefaults.destinationTarget ?? 'the deployment default'}</code>.
            </>
          }
        >
          <p>Backups already at the current destination are not deleted.</p>
        </ConfirmDialog>
      )}
    </div>
  )
}
