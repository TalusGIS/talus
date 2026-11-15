import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router'
import { useAuth } from 'react-oidc-context'

import { useIsPublisher } from '@/lib/auth/roles'

import ConfirmDialog from '@/components/ConfirmDialog'
import {
  ApiError,
  cancelSqlPullRun,
  createSqlConnection,
  createSqlPull,
  deleteSqlConnection,
  deleteSqlPull,
  fetchSqlCatalog,
  fetchSqlCatalogColumns,
  fetchSqlConnections,
  fetchSqlDialects,
  fetchSqlPullRun,
  fetchSqlPulls,
  runSqlPull,
  setSqlPullSchedule,
  testSqlConnection,
  updateSqlConnection,
  type SqlCatalogColumn,
  type SqlCatalogTable,
  type SqlConnection,
  type SqlConnectionWrite,
  type SqlDialect,
  type SqlDialectInfo,
  type SqlPull,
  type SqlPullGeometry,
  type SqlPullRun,
  type SqlPullSchedule,
  type SqlTestResult,
  type SqlTlsMode,
} from '@/lib/api'

/**
 * Database sources (decision-sql-source-pull.md, s1; F11 amended 2026-10-01: PER-USER, publisher-gated).
 * `scope="mine"` is the Databases facet of the Catalog (`embedded`) — a publisher's own connections and pulls;
 * `scope="admin"` is the read-only overview under Admin — every connection and pull in the org with
 * its owner, never a credential, with Remove / Detach for oversight. Read-only copies of tables that
 * OTHER systems own — utility billing, permitting, work orders, the assessor, an SDE table —
 * pulled into ordinary Talus tables and refreshed from the source. Talus never writes to the
 * source; while a pull is attached its Talus copy is read-only — nobody edits it here; Detach makes it an
 * ordinary editable table (F3).
 *
 * Two halves: CONNECTIONS (where a pull points — host, database, a read-only login; the password
 * is write-only, the server never echoes it) and PULLS (one source table or view + the columns to
 * copy → one new Talus table, run now). The pull wizard asks in plain terms (which table, which
 * columns, which column identifies a row) — an admin never needs to know Talus's internals (F1).
 * Every door is admin-only server-side; this screen sits behind RequireAdmin.
 */

const TLS_MODES: ReadonlyArray<{ value: SqlTlsMode; label: string }> = [
  { value: 'verify-full', label: 'Encrypted, certificate verified (recommended)' },
  { value: 'require', label: 'Encrypted, host name not checked (PostgreSQL: certificate not verified; SQL Server: chain verified against the bundle)' },
  { value: 'disable', label: 'Not encrypted (LAN only, last resort)' },
]

interface ConnForm {
  name: string
  dialect: SqlDialect
  host: string
  port: string
  database: string
  username: string
  password: string
  clearPassword: boolean
  replacingPassword: boolean
  tlsMode: SqlTlsMode
  caPem: string
  clearCaBundle: boolean
  timezone: string
}

const EMPTY_CONN: ConnForm = {
  name: '', dialect: 'postgresql', host: '', port: '5432', database: '', username: '', password: '',
  clearPassword: false, replacingPassword: false, tlsMode: 'verify-full', caPem: '', clearCaBundle: false,
  timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC',
}

function formFromRow(r: SqlConnection): ConnForm {
  return {
    name: r.name, dialect: r.dialect, host: r.host, port: String(r.port), database: r.database,
    username: r.username, password: '', clearPassword: false, replacingPassword: false,
    tlsMode: r.tlsMode, caPem: '', clearCaBundle: false, timezone: r.timezone,
  }
}

type PullStep = { step: 'table' } | { step: 'columns'; table: SqlCatalogTable } | { step: 'done'; pull: SqlPull }

interface ColumnPick {
  selected: boolean
  utc: boolean
  toDate: boolean
}

function describeType(c: SqlCatalogColumn): string {
  if (c.dataType === null) return `skipped — ${c.unsupportedReason ?? 'unsupported'}`
  if (c.temporal) return 'date & time → text (ISO 8601)'
  if (c.dataType === 'number') return c.numericSubtype === 'integer' ? 'whole number' : 'number'
  return c.dataType
}

function buildSchedule(kind: 'manual' | 'nightly' | 'interval', hour: string, hours: string): SqlPullSchedule | null {
  if (kind === 'manual') return { kind: 'manual', hour: null, minutes: null }
  if (kind === 'nightly') {
    const h = Number(hour)
    return Number.isInteger(h) && h >= 0 && h <= 23 ? { kind: 'nightly', hour: h, minutes: null } : null
  }
  const n = Number(hours)
  return Number.isInteger(n) && n >= 1 ? { kind: 'interval', hour: null, minutes: n * 60 } : null
}

function scheduleLabel(s: SqlPullSchedule, lastError?: string | null): string {
  if (s.kind === 'nightly') return `nightly at ${String(s.hour ?? 2).padStart(2, '0')}:00`
  if (s.kind === 'interval') return (s.minutes ?? 60) % 60 === 0 ? `every ${(s.minutes ?? 60) / 60} h` : `every ${s.minutes} min`
  // A lifecycle event (the owner disabled, or set below publisher) flips the pull to manual and leaves its reason
  // as the stale text; say why here, where the schedule is read (decision-sql-source-pull.md §11.2 L6).
  if (lastError && /^owner (disabled|role changed)/.test(lastError)) return `manual — ${lastError}`
  return 'manual'
}

function runSummary(run: SqlPullRun | null): string {
  if (!run) return 'never run'
  switch (run.status) {
    case 'pending': return 'queued'
    case 'running': return `running — ${run.processedRows}${run.totalRows != null ? ` of ${run.totalRows}` : ''} rows`
    case 'completed': {
      const parts: string[] = []
      if (run.insertedRows) parts.push(`${run.insertedRows} added`)
      if (run.updatedRows) parts.push(`${run.updatedRows} changed`)
      if (run.deletedRows) parts.push(`${run.deletedRows} removed`)
      if (run.unchangedRows) parts.push(`${run.unchangedRows} unchanged`)
      if (run.failedRows) parts.push(`${run.failedRows} refused`)
      return parts.length ? parts.join(', ') : 'nothing to copy'
    }
    case 'failed': return `failed — ${run.error ?? 'unknown'}`
    case 'canceled': return 'canceled — last good copy kept'
  }
}

export default function DatabaseSourcesView({ scope = 'mine', embedded = false }: { scope?: 'mine' | 'admin'; embedded?: boolean }) {
  const auth = useAuth()
  const token = auth.user?.access_token ?? ''
  const isPublisher = useIsPublisher()
  const adminView = scope === 'admin'

  const [dialects, setDialects] = useState<SqlDialectInfo[]>([])
  const [rows, setRows] = useState<SqlConnection[]>([])
  const [pulls, setPulls] = useState<SqlPull[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const [form, setForm] = useState<ConnForm | null>(null)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [tests, setTests] = useState<Record<string, SqlTestResult | 'testing'>>({})
  const [confirmDelete, setConfirmDelete] = useState<SqlConnection | null>(null)
  const [confirmDetach, setConfirmDetach] = useState<SqlPull | null>(null)

  // The pull wizard
  const [wizardConn, setWizardConn] = useState<SqlConnection | null>(null)
  const [wizard, setWizard] = useState<PullStep>({ step: 'table' })
  const [catalog, setCatalog] = useState<SqlCatalogTable[] | null>(null)
  const [filter, setFilter] = useState('')
  const [columns, setColumns] = useState<SqlCatalogColumn[]>([])
  const [picks, setPicks] = useState<Record<string, ColumnPick>>({})
  const [keyColumn, setKeyColumn] = useState('')
  const [pullName, setPullName] = useState('')
  // s2 — where the rows go on the map: nowhere (a table), a point from two coordinate columns, or
  // addresses (geocoded afterwards with the existing Geocode addresses door on the pulled table).
  const [location, setLocation] = useState<'none' | 'xy' | 'address'>('none')
  const [xCol, setXCol] = useState('')
  const [yCol, setYCol] = useState('')
  const [srid, setSrid] = useState('')
  const [sridDetected, setSridDetected] = useState(false)
  // s3 — the refresh schedule; nightly at 02:00 (in the connection's time zone) is the default
  const [schedKind, setSchedKind] = useState<'manual' | 'nightly' | 'interval'>('nightly')
  const [schedHour, setSchedHour] = useState('2')
  const [schedHours, setSchedHours] = useState('24')
  const [editingSchedule, setEditingSchedule] = useState<string | null>(null)
  const [wizardError, setWizardError] = useState<string | null>(null)
  const pollTimer = useRef<number | null>(null)

  const editingRow = editingId ? rows.find((r) => r.id === editingId) ?? null : null

  const reload = useCallback(async () => {
    if (!token) return
    try {
      const [d, c, p] = await Promise.all([fetchSqlDialects(token), fetchSqlConnections(token, adminView), fetchSqlPulls(token, adminView)])
      setDialects(d)
      setRows(c)
      setPulls(p)
      setError(null)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not load database sources.')
    }
  }, [token, adminView])

  useEffect(() => {
    void reload()
  }, [reload])


  // Poll while any run is live, so the list shows movement and the final verdict.
  useEffect(() => {
    const live = pulls.some((p) => p.lastRun && (p.lastRun.status === 'pending' || p.lastRun.status === 'running'))
    if (!live || !token) return
    pollTimer.current = window.setTimeout(() => void reload(), 2000)
    return () => {
      if (pollTimer.current) window.clearTimeout(pollTimer.current)
    }
  }, [pulls, token, reload])

  // ---- connections -------------------------------------------------------------------------
  const openAdd = () => {
    setEditingId(null)
    setForm({ ...EMPTY_CONN })
  }
  const openEdit = (r: SqlConnection) => {
    setEditingId(r.id)
    setForm(formFromRow(r))
  }
  const onDialect = (d: SqlDialect) => {
    if (!form) return
    const info = dialects.find((x) => x.name === d)
    setForm({ ...form, dialect: d, port: info ? String(info.defaultPort) : form.port })
  }

  const save = async () => {
    if (!form) return
    const port = Number(form.port)
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      setError('Port must be a whole number between 1 and 65535.')
      return
    }
    const body: SqlConnectionWrite = {
      name: form.name.trim(), dialect: form.dialect, host: form.host.trim(), port,
      database: form.database.trim(), username: form.username.trim(), tlsMode: form.tlsMode,
      timezone: form.timezone.trim() || 'UTC',
    }
    if (form.password) body.password = form.password
    else if (form.clearPassword) body.clearPassword = true
    if (form.caPem.trim()) body.caPem = form.caPem
    else if (form.clearCaBundle) body.clearCaBundle = true
    setBusy(true)
    try {
      if (editingId) await updateSqlConnection(editingId, body, token)
      else await createSqlConnection(body, token)
      setForm(null)
      setEditingId(null)
      setError(null)
      await reload()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not save the connection.')
    } finally {
      setBusy(false)
    }
  }

  const test = async (r: SqlConnection) => {
    setTests((t) => ({ ...t, [r.id]: 'testing' }))
    try {
      const out = await testSqlConnection(r.id, token)
      setTests((t) => ({ ...t, [r.id]: out }))
      await reload()
    } catch (e) {
      setTests((t) => ({ ...t, [r.id]: { ok: false, reason: 'error', message: e instanceof ApiError ? e.message : 'Test failed.', tables: null } }))
    }
  }

  const remove = async () => {
    if (!confirmDelete) return
    setBusy(true)
    try {
      await deleteSqlConnection(confirmDelete.id, token)
      setConfirmDelete(null)
      await reload()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not remove the connection.')
    } finally {
      setBusy(false)
    }
  }

  // ---- the pull wizard ---------------------------------------------------------------------
  const openWizard = async (r: SqlConnection) => {
    setWizardConn(r)
    setWizard({ step: 'table' })
    setCatalog(null)
    setFilter('')
    setWizardError(null)
    try {
      setCatalog(await fetchSqlCatalog(r.id, token))
    } catch (e) {
      setWizardError(e instanceof ApiError ? e.message : 'Could not read the database catalog.')
    }
  }

  const pickTable = async (t: SqlCatalogTable) => {
    if (!wizardConn) return
    setWizardError(null)
    try {
      const out = await fetchSqlCatalogColumns(wizardConn.id, t.schema, t.name, token)
      setColumns(out.columns)
      const init: Record<string, ColumnPick> = {}
      for (const c of out.columns) init[c.name] = { selected: c.dataType !== null, utc: false, toDate: false }
      setPicks(init)
      setKeyColumn(out.table.keyColumns[0] ?? '')
      setPullName(t.name.replace(/[_-]+/g, ' '))
      const xy = out.xyCandidates
      setLocation(xy ? 'xy' : 'none')
      setXCol(xy?.x ?? '')
      setYCol(xy?.y ?? '')
      setSrid(xy?.srid ? String(xy.srid) : '')
      setSridDetected(!!xy?.srid)
      setWizard({ step: 'columns', table: out.table })
    } catch (e) {
      setWizardError(e instanceof ApiError ? e.message : 'Could not read the table.')
    }
  }

  const createPull = async () => {
    if (!wizardConn || wizard.step !== 'columns') return
    const chosen = columns.filter((c) => picks[c.name]?.selected)
    if (chosen.length === 0) {
      setWizardError('Pick at least one column.')
      return
    }
    let geometry: SqlPullGeometry | null = null
    if (location === 'xy') {
      if (!xCol || !yCol || xCol === yCol) {
        setWizardError('Pick the X (or longitude) and Y (or latitude) columns.')
        return
      }
      const n = srid.trim() === '' ? null : Number(srid)
      if (n !== null && (!Number.isInteger(n) || n < 1)) {
        setWizardError('The EPSG code must be a whole number, e.g. 4326.')
        return
      }
      geometry = { kind: 'xy', x: xCol, y: yCol, srid: n }
    }
    const schedule = buildSchedule(schedKind, schedHour, schedHours)
    if (!schedule) {
      setWizardError('The schedule needs a valid hour (0–23) or a number of hours (1 or more).')
      return
    }
    setBusy(true)
    setWizardError(null)
    try {
      const pull = await createSqlPull(
        {
          connectionId: wizardConn.id, schema: wizard.table.schema, table: wizard.table.name,
          columns: chosen.map((c) => ({ name: c.name, utc: picks[c.name].utc, toDate: picks[c.name].toDate })),
          keyColumn: keyColumn || null, name: pullName.trim(), geometry, schedule,
        },
        token,
      )
      setWizard({ step: 'done', pull })
      await reload()
    } catch (e) {
      setWizardError(e instanceof ApiError ? e.message : 'Could not create the pull.')
    } finally {
      setBusy(false)
    }
  }

  const refreshNow = async (p: SqlPull) => {
    try {
      await runSqlPull(p.id, token)
      await reload()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not start the refresh.')
    }
  }

  const cancelRun = async (p: SqlPull) => {
    if (!p.lastRun) return
    try {
      await cancelSqlPullRun(p.lastRun.id, token)
      await reload()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not cancel the run.')
    }
  }

  const detach = async () => {
    if (!confirmDetach) return
    setBusy(true)
    try {
      await deleteSqlPull(confirmDetach.id, token)
      setConfirmDetach(null)
      await reload()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not detach the pull.')
    } finally {
      setBusy(false)
    }
  }

  const pollRun = useCallback(async (runId: string) => {
    try {
      const run = await fetchSqlPullRun(runId, token)
      setWizard((w) => (w.step === 'done' ? { step: 'done', pull: { ...w.pull, lastRun: run } } : w))
      if (run.status === 'pending' || run.status === 'running') window.setTimeout(() => void pollRun(runId), 1500)
      else await reload()
    } catch {
      /* the list poll will catch up */
    }
  }, [token, reload])

  useEffect(() => {
    if (wizard.step === 'done' && wizard.pull.lastRun) void pollRun(wizard.pull.lastRun.id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wizard.step])

  if (!isPublisher) {
    return (
      <div className={embedded ? 'catalog__databases' : 'admin'}>
        {!embedded && <h1 className="admin__title">Database sources</h1>}
        <p className="admin__note">Pulling data from a database creates tables, so this is for publishers. Ask an admin for the publisher role.</p>
      </div>
    )
  }

  const filteredCatalog = (catalog ?? []).filter((t) => {
    const q = filter.trim().toLowerCase()
    return !q || `${t.schema}.${t.name}`.toLowerCase().includes(q)
  })

  return (
    <div className={embedded ? 'catalog__databases' : 'admin'}>
      {!embedded && <h1 className="admin__title">Database sources</h1>}
      <p className="admin__note">
        Pull a table or view from a database another system owns — utility billing, permitting, work orders,
        the assessor, an SDE table — into a Talus table, and refresh it from the source. Talus only ever reads
        from your database: nothing is written back. While a pull is attached, its copy in Talus is read-only —
        nobody can edit it here, so it always matches the source; choose <em>Detach</em> to turn the copy into
        an ordinary table you can edit. Ask the DBA for a <strong>read-only login</strong>, ideally on a view
        made for Talus.
      </p>

      {error && <p className="sa__error" role="alert">{error}</p>}

      {adminView && (
        <p className="admin__note">
          <strong>Admin overview.</strong> Every connection and pull in the organization, with its owner. Passwords are never
          shown here or anywhere; each publisher manages their own under Catalog → Database sources. You can remove a connection
          or detach a pull for oversight.
        </p>
      )}
      <h2 className="org-settings__heading">Connections</h2>
      {!form && !adminView && (
        <div className="sign-form__actions">
          <button type="button" className="btn btn--sm" onClick={openAdd} disabled={busy}>+ Add connection</button>
        </div>
      )}

      {form && (
        <div className="org-settings__card">
          <h3 className="org-settings__heading">{editingId ? 'Edit connection' : 'Add connection'}</h3>
          <label className="sign-form__field">
            <span>Name</span>
            <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="e.g. Work orders" />
          </label>
          <label className="sign-form__field">
            <span>Database type</span>
            <select value={form.dialect} onChange={(e) => onDialect(e.target.value as SqlDialect)}>
              {dialects.map((d) => (
                <option key={d.name} value={d.name} disabled={!d.available}>
                  {d.name === 'postgresql' ? 'PostgreSQL / PostGIS' : d.name === 'sqlserver' ? 'Microsoft SQL Server' : d.name === 'mysql' ? 'MySQL / MariaDB' : 'Oracle'}
                  {d.available ? '' : ` — ${d.unavailableReason ?? 'not available yet'}`}
                </option>
              ))}
            </select>
          </label>
          <label className="sign-form__field">
            <span>Server (host name or IP address)</span>
            <input value={form.host} onChange={(e) => setForm({ ...form, host: e.target.value })} placeholder="db01.example.org" />
          </label>
          <label className="sign-form__field">
            <span>Port</span>
            <input value={form.port} onChange={(e) => setForm({ ...form, port: e.target.value })} inputMode="numeric" />
          </label>
          <label className="sign-form__field">
            <span>Database</span>
            <input value={form.database} onChange={(e) => setForm({ ...form, database: e.target.value })} />
          </label>
          <label className="sign-form__field">
            <span>User name</span>
            <input value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} autoComplete="off" />
          </label>
          <div className="sign-form__field">
            <span>Password</span>
            {editingRow?.hasPassword && !form.clearPassword && !form.replacingPassword ? (
              <p>
                <span className="sa-badge sa-badge--on">Password set</span>{' '}
                {editingRow.passwordUnavailable && (
                  <span className="sa-badge sa-badge--off" title="The server's encryption key can't open the stored password. Re-enter it.">
                    credential unavailable — re-enter it
                  </span>
                )}{' '}
                <button type="button" className="btn btn--sm" onClick={() => setForm({ ...form, replacingPassword: true })}>Replace</button>{' '}
                <button type="button" className="btn btn--sm btn--danger" onClick={() => setForm({ ...form, clearPassword: true, password: '' })}>Clear</button>
              </p>
            ) : form.clearPassword ? (
              <p>
                <span className="sa-badge sa-badge--off">Password will be removed when you save</span>{' '}
                <button type="button" className="btn btn--sm" onClick={() => setForm({ ...form, clearPassword: false })}>Undo</button>
              </p>
            ) : (
              <input type="password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} autoComplete="new-password" />
            )}
          </div>
          <label className="sign-form__field">
            <span>Encryption (TLS)</span>
            <select value={form.tlsMode} onChange={(e) => setForm({ ...form, tlsMode: e.target.value as SqlTlsMode })}>
              {TLS_MODES.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
            </select>
          </label>
          {form.tlsMode === 'verify-full' && (
            <div className="sign-form__field">
              <span>CA bundle (PEM, optional — for a certificate signed by your own CA; for SQL Server with a self-signed certificate, paste the server’s own certificate)</span>
              {editingRow?.hasCaBundle && !form.caPem && !form.clearCaBundle ? (
                <p>
                  <span className="sa-badge sa-badge--on">CA bundle set</span>{' '}
                  <button type="button" className="btn btn--sm btn--danger" onClick={() => setForm({ ...form, clearCaBundle: true })}>Clear</button>
                </p>
              ) : (
                <textarea rows={4} value={form.caPem} onChange={(e) => setForm({ ...form, caPem: e.target.value, clearCaBundle: false })} placeholder="-----BEGIN CERTIFICATE-----" />
              )}
            </div>
          )}
          <label className="sign-form__field">
            <span>Time zone of the server’s date-and-time values</span>
            <input value={form.timezone} onChange={(e) => setForm({ ...form, timezone: e.target.value })} placeholder="America/Chicago" />
          </label>
          <div className="sign-form__actions">
            <button type="button" className="btn btn--sm btn--primary" onClick={() => void save()} disabled={busy}>Save</button>{' '}
            <button type="button" className="btn btn--sm" onClick={() => { setForm(null); setEditingId(null) }} disabled={busy}>Cancel</button>
          </div>
        </div>
      )}

      {rows.length === 0 && !form ? (
        <p className="admin__note">{adminView ? 'No connections in the organization yet.' : 'No connections yet.'}</p>
      ) : rows.length > 0 && (
        <table className="sa-table">
          <thead>
            <tr><th>Name</th>{adminView && <th>Owner</th>}<th>Server</th><th>Login</th><th>Last test</th><th></th></tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const t = tests[r.id]
              return (
                <tr key={r.id}>
                  <td>{r.name}<br /><small>{r.dialect}{r.dialectAvailable ? '' : ' (not available yet)'}</small></td>
                  {adminView && <td>{r.ownerName ?? r.ownerId}</td>}
                  <td>{r.host}:{r.port}/{r.database}<br /><small>{TLS_MODES.find((m) => m.value === r.tlsMode)?.label}</small></td>
                  <td>{r.username}{' '}{r.hasPassword ? <span className="sa-badge sa-badge--on">password set</span> : <span className="sa-badge sa-badge--off">no password</span>}</td>
                  <td>
                    {t === 'testing' ? 'testing…' : t ? (t.ok ? `OK — ${t.tables} tables/views` : `failed — ${t.message}`)
                      : r.lastTestedAt ? (r.lastTestOk ? 'OK' : `failed — ${r.lastTestError}`) : 'never'}
                  </td>
                  <td>
                    {!adminView && (
                      <>
                        <button type="button" className="btn btn--sm" onClick={() => void test(r)} disabled={busy}>Test</button>{' '}
                        <button type="button" className="btn btn--sm" onClick={() => void openWizard(r)} disabled={busy || !r.dialectAvailable}>New pull</button>{' '}
                        <button type="button" className="btn btn--sm" onClick={() => openEdit(r)} disabled={busy}>Edit</button>{' '}
                      </>
                    )}
                    <button type="button" className="btn btn--sm btn--danger" onClick={() => setConfirmDelete(r)} disabled={busy}>Remove</button>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}

      {wizardConn && (
        <div className="org-settings__card">
          <h3 className="org-settings__heading">New pull from {wizardConn.name}</h3>
          {wizardError && <p className="sa__error" role="alert">{wizardError}</p>}
          {wizard.step === 'table' && (
            <>
              <p className="admin__note">Which table or view holds the rows you want in Talus?</p>
              <label className="sign-form__field">
                <span>Find</span>
                <input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="schema.table" />
              </label>
              {catalog === null ? (
                <p>Reading the catalog…</p>
              ) : (
                <table className="sa-table">
                  <thead><tr><th>Table or view</th><th>Identifies a row by</th><th></th></tr></thead>
                  <tbody>
                    {filteredCatalog.slice(0, 200).map((t) => (
                      <tr key={`${t.schema}.${t.name}`}>
                        <td>{t.schema}.{t.name} <small>({t.kind})</small></td>
                        <td>{t.keyColumns.length ? t.keyColumns.join(', ') : <small>no unique column</small>}</td>
                        <td><button type="button" className="btn btn--sm" onClick={() => void pickTable(t)}>Choose</button></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </>
          )}
          {wizard.step === 'columns' && (
            <>
              <p className="admin__note">
                Which columns should Talus copy from <strong>{wizard.table.schema}.{wizard.table.name}</strong>? Leave out
                anything sensitive — only the columns you tick ever leave the source.
              </p>
              <table className="sa-table">
                <thead><tr><th></th><th>Column</th><th>Source type</th><th>In Talus</th><th>Options</th></tr></thead>
                <tbody>
                  {columns.map((c) => (
                    <tr key={c.name}>
                      <td>
                        <input type="checkbox" checked={picks[c.name]?.selected ?? false} disabled={c.dataType === null}
                          onChange={(e) => setPicks({ ...picks, [c.name]: { ...picks[c.name], selected: e.target.checked } })} />
                      </td>
                      <td>{c.name}</td>
                      <td><small>{c.sourceType}</small></td>
                      <td>{picks[c.name]?.toDate ? 'date (time dropped)' : describeType(c)}</td>
                      <td>
                        {c.temporal && (
                          <>
                            <label><input type="checkbox" checked={picks[c.name]?.utc ?? false}
                              onChange={(e) => setPicks({ ...picks, [c.name]: { ...picks[c.name], utc: e.target.checked } })} /> stored in UTC</label>{' '}
                            <label><input type="checkbox" checked={picks[c.name]?.toDate ?? false}
                              onChange={(e) => setPicks({ ...picks, [c.name]: { ...picks[c.name], toDate: e.target.checked } })} /> keep the date only</label>
                          </>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className="sign-form__field">
                <span>Where do these rows go on the map?</span>
                <label><input type="radio" name="loc" checked={location === 'none'} onChange={() => setLocation('none')} /> Nowhere — a table (join or relate it to a layer later)</label>
                <label><input type="radio" name="loc" checked={location === 'xy'} onChange={() => setLocation('xy')} /> Points from two coordinate columns</label>
                <label><input type="radio" name="loc" checked={location === 'address'} onChange={() => setLocation('address')} /> Addresses — geocode the table after the pull</label>
              </div>
              {location === 'xy' && (
                <>
                  <label className="sign-form__field">
                    <span>X or longitude column</span>
                    <select value={xCol} onChange={(e) => { setXCol(e.target.value); setSridDetected(false) }}>
                      <option value="">choose…</option>
                      {columns.filter((c) => picks[c.name]?.selected && (c.dataType === 'number' || c.dataType === 'text')).map((c) => <option key={c.name} value={c.name}>{c.name}</option>)}
                    </select>
                  </label>
                  <label className="sign-form__field">
                    <span>Y or latitude column</span>
                    <select value={yCol} onChange={(e) => { setYCol(e.target.value); setSridDetected(false) }}>
                      <option value="">choose…</option>
                      {columns.filter((c) => picks[c.name]?.selected && (c.dataType === 'number' || c.dataType === 'text')).map((c) => <option key={c.name} value={c.name}>{c.name}</option>)}
                    </select>
                  </label>
                  <label className="sign-form__field">
                    <span>Coordinate system (EPSG code){sridDetected ? ' — detected from the column names' : ''}</span>
                    <input value={srid} onChange={(e) => { setSrid(e.target.value); setSridDetected(false) }} inputMode="numeric"
                      placeholder="4326 for longitude/latitude, or your projected system’s code" />
                  </label>
                </>
              )}
              {location === 'address' && (
                <p className="admin__note">
                  The pull creates a table. When it finishes, open <strong>Geocode addresses</strong> on the Catalog and
                  choose this table under <strong>A table or layer already in Talus</strong>. The result is a new point
                  layer, copied at that moment: it does not update when the pull refreshes. Unmatched rows stay in
                  the result for review.
                </p>
              )}
              <label className="sign-form__field">
                <span>Which column identifies a row? (used to detect changes on later refreshes)</span>
                <select value={keyColumn} onChange={(e) => setKeyColumn(e.target.value)}>
                  <option value="">none — replace the whole copy each refresh</option>
                  {wizard.table.keyColumns.filter((k) => picks[k]?.selected).map((k) => <option key={k} value={k}>{k}</option>)}
                </select>
              </label>
              <SchedulePicker kind={schedKind} hour={schedHour} hours={schedHours} onKind={setSchedKind} onHour={setSchedHour} onHours={setSchedHours} />
              <label className="sign-form__field">
                <span>Name of the new Talus table</span>
                <input value={pullName} onChange={(e) => setPullName(e.target.value)} />
              </label>
              <div className="sign-form__actions">
                <button type="button" className="btn btn--sm btn--primary" onClick={() => void createPull()} disabled={busy}>Create and pull now</button>{' '}
                <button type="button" className="btn btn--sm" onClick={() => setWizard({ step: 'table' })} disabled={busy}>Back</button>{' '}
                <button type="button" className="btn btn--sm" onClick={() => setWizardConn(null)} disabled={busy}>Cancel</button>
              </div>
            </>
          )}
          {wizard.step === 'done' && (
            <>
              <p>
                <strong>{wizard.pull.targetName}</strong> ({wizard.pull.geometry ? 'point layer' : 'table'}) — {runSummary(wizard.pull.lastRun)}.{' '}
                <Link to={`/catalog/${wizard.pull.targetLayerId}`}>Open it</Link>
              </p>
              <div className="sign-form__actions">
                <button type="button" className="btn btn--sm" onClick={() => setWizardConn(null)}>Close</button>
              </div>
            </>
          )}
        </div>
      )}

      <h2 className="org-settings__heading">Pulls</h2>
      {pulls.length === 0 ? (
        <p className="admin__note">{adminView ? 'No pulls in the organization yet.' : 'No pulls yet. Add a connection, test it, then choose “New pull”.'}</p>
      ) : (
        <table className="sa-table">
          <thead><tr><th>Talus table</th>{adminView && <th>Owner</th>}<th>Source</th><th>Columns</th><th>Refresh</th><th>Last run</th><th></th></tr></thead>
          <tbody>
            {pulls.map((p) => {
              const live = p.lastRun && (p.lastRun.status === 'pending' || p.lastRun.status === 'running')
              return (
                <tr key={p.id}>
                  <td><Link to={`/catalog/${p.targetLayerId}`}>{p.targetName}</Link></td>
                  {adminView && <td>{p.ownerName ?? p.ownerId}</td>}
                  <td>{p.connectionName}: {p.schema}.{p.table}{p.geometry ? <><br /><small>points from {p.geometry.x}/{p.geometry.y} (EPSG {p.geometry.srid})</small></> : null}</td>
                  <td>{p.columns.length}{p.keyColumn ? <small> · key {p.keyColumn}</small> : <small> · by content</small>}</td>
                  <td>
                    {editingSchedule === p.id ? (
                      <ScheduleEditor
                        initial={p.schedule}
                        onCancel={() => setEditingSchedule(null)}
                        onSave={async (sch) => {
                          try {
                            await setSqlPullSchedule(p.id, sch, token)
                            setEditingSchedule(null)
                            await reload()
                          } catch (e) {
                            setError(e instanceof ApiError ? e.message : 'Could not save the schedule.')
                          }
                        }}
                      />
                    ) : (
                      <>
                        {scheduleLabel(p.schedule, p.lastError)}
                        {p.nextRunAt && p.schedule.kind !== 'manual' && <><br /><small>next {new Date(p.nextRunAt).toLocaleString()}</small></>}
                        {!adminView && <>{' '}<button type="button" className="btn btn--sm" onClick={() => setEditingSchedule(p.id)}>Change</button></>}
                      </>
                    )}
                  </td>
                  <td>
                    {p.staleSince && (
                      <><span className="sa-badge sa-badge--off" title={p.lastError ?? ''}>stale since {new Date(p.staleSince).toLocaleString()}</span><br /></>
                    )}
                    {runSummary(p.lastRun)}
                    {p.lastRun?.finishedAt && <><br /><small>{new Date(p.lastRun.finishedAt).toLocaleString()}</small></>}
                  </td>
                  <td>
                    {live ? (
                      <button type="button" className="btn btn--sm" onClick={() => void cancelRun(p)}>Cancel</button>
                    ) : !adminView ? (
                      <button type="button" className="btn btn--sm" onClick={() => void refreshNow(p)} disabled={busy}>Refresh now</button>
                    ) : null}{' '}
                    <button type="button" className="btn btn--sm btn--danger" onClick={() => setConfirmDetach(p)} disabled={busy || !!live}>Detach</button>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}

      {confirmDelete && (
        <ConfirmDialog
          title="Remove this connection?"
          confirmLabel="Remove"
          tone="danger"
          busy={busy}
          onConfirm={() => void remove()}
          onCancel={() => setConfirmDelete(null)}
        >
          “{confirmDelete.name}” and its pulls will be removed. The Talus tables already copied stay, and become editable again.
        </ConfirmDialog>
      )}
      {confirmDetach && (
        <ConfirmDialog
          title="Detach this pull?"
          confirmLabel="Detach"
          tone="danger"
          busy={busy}
          onConfirm={() => void detach()}
          onCancel={() => setConfirmDetach(null)}
        >
          “{confirmDetach.targetName}” stops refreshing and becomes an ordinary table you can edit. The source is untouched.
        </ConfirmDialog>
      )}
    </div>
  )
}


function SchedulePicker({ kind, hour, hours, onKind, onHour, onHours }: {
  kind: 'manual' | 'nightly' | 'interval'; hour: string; hours: string
  onKind: (k: 'manual' | 'nightly' | 'interval') => void; onHour: (v: string) => void; onHours: (v: string) => void
}) {
  return (
    <div className="sign-form__field">
      <span>How often should Talus refresh it? (times are in the connection’s time zone)</span>
      <label><input type="radio" name="sched" checked={kind === 'nightly'} onChange={() => onKind('nightly')} /> Nightly at{' '}
        <input value={hour} onChange={(e) => onHour(e.target.value)} inputMode="numeric" style={{ width: '3em' }} disabled={kind !== 'nightly'} />:00</label>
      <label><input type="radio" name="sched" checked={kind === 'interval'} onChange={() => onKind('interval')} /> Every{' '}
        <input value={hours} onChange={(e) => onHours(e.target.value)} inputMode="numeric" style={{ width: '3em' }} disabled={kind !== 'interval'} /> hours</label>
      <label><input type="radio" name="sched" checked={kind === 'manual'} onChange={() => onKind('manual')} /> Only when I press Refresh now</label>
    </div>
  )
}

function ScheduleEditor({ initial, onSave, onCancel }: { initial: SqlPullSchedule; onSave: (s: SqlPullSchedule) => void; onCancel: () => void }) {
  const [kind, setKind] = useState<'manual' | 'nightly' | 'interval'>(initial.kind)
  const [hour, setHour] = useState(String(initial.hour ?? 2))
  const [hours, setHours] = useState(String(Math.max(1, Math.round((initial.minutes ?? 1440) / 60))))
  return (
    <div>
      <SchedulePicker kind={kind} hour={hour} hours={hours} onKind={setKind} onHour={setHour} onHours={setHours} />
      <button type="button" className="btn btn--sm btn--primary" onClick={() => { const s = buildSchedule(kind, hour, hours); if (s) onSave(s) }}>Save</button>{' '}
      <button type="button" className="btn btn--sm" onClick={onCancel}>Cancel</button>
    </div>
  )
}
