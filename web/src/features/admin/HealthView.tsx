import { Link } from 'react-router'

import { basemapIsStale } from '@/features/admin/BasemapSection'
import { issueDestination, useAdminStatus } from '@/features/shell/adminStatus'
import type { SystemProbe } from '@/lib/api'

/**
 * Admin → Health (decision-system-health.md; reworked per
 * handoff-admin-1a §2). The one-glance answer to "is my portal healthy?": issue callout,
 * four stat tiles, live service probes, and the operational state the platform already
 * records. STATUS, not a control plane — no remediation buttons, no alerting, no
 * history/graphs (fences §6); every issue links to the destination that owns the fix.
 * ★ The callout speaks for the SYSTEM, not just the processes (decision-system-health-issue-
 * escalation.md). It used to derive from PROBES alone, on the reasoning that "a dead service and an
 * old backup are different urgencies" — true, but it meant that from 2026-07-12 to 2026-07-26 the
 * page reported "All systems operational" in green while no backup had run for 14 days and the
 * amber/red evidence sat a few hundred pixels below. Green now requires no down services AND no
 * issues; severity is server-derived policy (this file renders the verdict, it does not compute
 * one). Data arrives via AdminView's layout-level 30 s poll (Outlet context) — the same feed
 * that lights the rail's attention dots. Admin-gated server-side; behind RequireAdmin.
 */
const SERVICE_LABELS: Record<string, string> = {
  postgres: 'Database (PostGIS)',
  storage: 'Media store (filesystem)',
  keycloak: 'Sign-in (Keycloak)',
  openfga: 'Permissions (OpenFGA)',
  pg_tileserv: 'Tile server',
  certificate: 'Portal certificate',
  clock: 'Clock',
  api: 'Portal API',
}

const PROBE_BADGE: Record<SystemProbe['status'], { cls: string; label: string }> = {
  up: { cls: 'sa-badge--success', label: 'Up' },
  slow: { cls: 'sa-badge--warning', label: 'Slow' },
  down: { cls: 'sa-badge--danger', label: 'Down' },
}

function fmtDateTime(iso: string | null): string {
  if (!iso) return 'never'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString()
}

function fmtAgo(iso: string | null): string {
  if (!iso) return 'never'
  const ms = Date.now() - new Date(iso).getTime()
  if (Number.isNaN(ms)) return '—'
  const days = Math.floor(ms / 86_400_000)
  if (days > 0) return `${days} day${days === 1 ? '' : 's'} ago`
  const hours = Math.floor(ms / 3_600_000)
  if (hours > 0) return `${hours} hour${hours === 1 ? '' : 's'} ago`
  const mins = Math.floor(ms / 60_000)
  return mins > 0 ? `${mins} min ago` : 'just now'
}

function fmtBytes(n: number | null): string {
  if (n == null) return '—'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let v = n
  let u = 0
  while (v >= 1024 && u < units.length - 1) {
    v /= 1024
    u += 1
  }
  return `${v.toFixed(u === 0 ? 0 : 1)} ${units[u]}`
}

export default function HealthView() {
  const { health: data, healthError: error, basemap } = useAdminStatus()

  if (error) return <p className="sa__error">{error}</p>
  if (!data) return <p className="sa__muted">Checking…</p>

  const down = data.probes.filter((p) => p.status === 'down')
  const up = data.probes.filter((p) => p.status === 'up')
  const issues = data.issues ?? []
  const critical = issues.filter((i) => i.severity === 'critical')

  // Three states, worst-wins. Green is an assertion about operational state too, and it
  // requires BOTH no down services AND no issues (see the header comment for why).
  const level: 'ok' | 'warn' | 'bad' =
    down.length > 0 || critical.length > 0 ? 'bad' : issues.length > 0 ? 'warn' : 'ok'
  const tone = { ok: 'success', warn: 'warning', bad: 'danger' }[level]

  // The tile's callout follows the server's verdict rather than a second, client-side
  // staleness rule — one source of truth, and it is interval-aware (a hardcoded 2 days was
  // wrong for both weekly and hourly schedules).
  const backupFlagged = issues.some((i) => i.key.startsWith('backup_'))

  const headline =
    down.length > 0
      ? `${down.length} service${down.length === 1 ? '' : 's'} down` +
        (issues.length > 0 ? ` · ${issues.length} issue${issues.length === 1 ? '' : 's'}` : '')
      : issues.length > 0
        ? `${issues.length} issue${issues.length === 1 ? '' : 's'} need${issues.length === 1 ? 's' : ''} attention`
        : 'All systems operational'

  return (
    <div className="admin">
      <div className="sa__head">
        <div>
          <h1 className="admin__title">Health</h1>
          <p className="admin__note">
            Live service checks and the operational state your portal already records. Status
            only — each issue links to the page that fixes it.
          </p>
        </div>
        <p className="health-meta">
          checked {fmtAgo(data.generatedAt)} · refreshes every 30 s
        </p>
      </div>

      <div role="status" className={`callout callout--${tone}`}>
        <p className="callout__headline">{headline}</p>
        {issues.length > 0 ? (
          // Named, not just counted — a number tells you to go hunting, a sentence tells you what
          // is wrong. Summaries are server-composed from fixed strings (never failure detail).
          <ul className="callout__list">
            {issues.map((i) => {
              const dest = issueDestination(i.key)
              return (
                <li key={i.key}>
                  {i.severity === 'critical' ? <strong>{i.summary}</strong> : i.summary}
                  {dest ? (
                    <>
                      {' '}
                      <Link to={dest}>
                        {dest === '/admin/backups' ? 'Open Backups & recovery' : 'Open Basemap'}
                      </Link>
                    </>
                  ) : null}
                </li>
              )
            })}
          </ul>
        ) : null}
      </div>

      <div className="stat-tiles">
        <div className="stat-tile">
          <div className="stat-tile__label">Services up</div>
          <div className="stat-tile__value">
            {up.length} of {data.probes.length}
          </div>
        </div>
        <div className="stat-tile">
          <div className="stat-tile__label">Database</div>
          <div className="stat-tile__value">{fmtBytes(data.storage.dbBytes)}</div>
        </div>
        <div className="stat-tile">
          <div className="stat-tile__label">Media store</div>
          <div className="stat-tile__value">{fmtBytes(data.storage.mediaBytes)}</div>
        </div>
        <div className="stat-tile">
          <div className="stat-tile__label">Last backup</div>
          <div className="stat-tile__value">{fmtAgo(data.backup.lastOkAt)}</div>
          {backupFlagged ? (
            <div className="stat-tile__delta stat-tile__delta--down">needs attention</div>
          ) : null}
        </div>
      </div>

      <h3>Services</h3>
      <table className="data-table">
        <thead>
          <tr>
            <th>Service</th>
            <th>Status</th>
            <th>Latency</th>
            <th>Detail</th>
          </tr>
        </thead>
        <tbody>
          {data.probes.map((p) => (
            <tr key={p.name}>
              <td>{SERVICE_LABELS[p.name] ?? p.name}</td>
              <td>
                <span className={`sa-badge sa-badge--dot ${PROBE_BADGE[p.status].cls}`}>
                  {PROBE_BADGE[p.status].label}
                </span>
              </td>
              <td className="mono-num">{p.latencyMs != null ? `${p.latencyMs} ms` : '—'}</td>
              <td className="sa__muted">{p.error ?? p.detail ?? ''}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <h3>Operational state</h3>
      <section className="health-state">
        <div className="health-state__key">Last successful backup</div>
        <div className="health-state__val">
          {fmtAgo(data.backup.lastOkAt)}
          {data.backup.target ? <span className="sa__muted"> → {data.backup.target}</span> : null}
          {backupFlagged ? (
            // Links into Admin → Backups so "needs attention" starts the remediation instead of
            // dead-ending (the Health page itself stays STATUS-only — the control lives there).
            <span className="health-flag">
              {' · '}
              <Link to="/admin/backups">needs attention</Link>
            </span>
          ) : null}
        </div>

        <div className="health-state__key">Scheduled backups</div>
        <div className="health-state__val">
          {data.backup.scheduledEnabled == null ? '—' : data.backup.scheduledEnabled ? 'On' : 'Off'}
        </div>

        <div className="health-state__key">Basemap</div>
        <div className="health-state__val">
          {basemap === null ? (
            '—'
          ) : basemap.installed ? (
            <>
              {basemap.sourceDate ? `build ${basemap.sourceDate}` : 'uploaded archive'}
              {basemap.fetchedAt ? (
                <span className="sa__muted"> · installed {fmtAgo(basemap.fetchedAt)}</span>
              ) : null}
              {basemapIsStale(basemap) ? (
                // Amber nudge, not automation (the T39/T43 tone): the Update button lives at
                // /admin/basemap (§3.5 promotion); this row just says it's time.
                <span className="health-flag">
                  {' '}
                  · over 6 months old — <Link to="/admin/basemap">update from Basemap</Link>
                </span>
              ) : null}
            </>
          ) : (
            <span className="health-flag">
              not installed — <Link to="/admin/basemap">set one up in Basemap</Link>
            </span>
          )}
        </div>

        <div className="health-state__key">Last restore drill</div>
        <div className="health-state__val">
          {fmtDateTime(data.restoreDrillLastOkAt)}
          {data.restoreDrillLastOkAt == null ? (
            <span className="health-flag"> · recovery unproven</span>
          ) : null}
        </div>

        <div className="health-state__key">Storage — database</div>
        <div className="health-state__val">{fmtBytes(data.storage.dbBytes)}</div>

        <div className="health-state__key">Storage — media</div>
        <div className="health-state__val">
          {fmtBytes(data.storage.mediaBytes)}
          {data.storage.mediaObjects != null ? (
            <span className="sa__muted"> ({data.storage.mediaObjects} files)</span>
          ) : null}
          {data.storage.mediaAsOf ? (
            <span className="sa__muted"> · as of {fmtDateTime(data.storage.mediaAsOf)}</span>
          ) : (
            <span className="sa__muted"> · first measurement pending</span>
          )}
        </div>

        <div className="health-state__key">Running</div>
        <div className="health-state__val">
          Talus {data.version.service}
          {data.version.migrationHead ? (
            <span className="sa__muted"> · schema {data.version.migrationHead}</span>
          ) : null}
        </div>
      </section>

      <p className="admin__note">
        Housekeeping jobs and their run history live in{' '}
        <Link to="/admin/maintenance">Maintenance</Link>.
      </p>
    </div>
  )
}
