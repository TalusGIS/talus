import { useCallback, useEffect, useState } from 'react'
import { useAuth } from 'react-oidc-context'

import { ApiError, fetchMaintenanceRuns, type MaintenanceRuns } from '@/lib/api'

/**
 * Admin → Maintenance (Talus 39). A READ-ONLY view of the self-maintenance scheduler: which cleanup
 * jobs exist, when each last ran, and what it freed — the self-tidying story
 * made legible. Cadence + retention are config-driven (MAINTENANCE_* env), so there is no editable
 * surface here (an editable Maintenance settings section is a deferred fast-follow). Admin-gated
 * server-side; this screen sits behind RequireAdmin.
 */
const JOB_LABELS: Record<string, string> = {
  prune_applied_op_keys: 'Prune idempotency keys',
  cleanup_orphaned_media: 'Clean orphaned media',
}

function jobLabel(name: string): string {
  return JOB_LABELS[name] ?? name
}

function fmtDateTime(iso: string | null): string {
  if (!iso) return 'never'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString()
}

function fmtBytes(n: number | null): string {
  if (!n || n <= 0) return '—'
  const units = ['B', 'KB', 'MB', 'GB']
  let v = n
  let u = 0
  while (v >= 1024 && u < units.length - 1) {
    v /= 1024
    u += 1
  }
  return `${v.toFixed(u === 0 ? 0 : 1)} ${units[u]}`
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

export default function MaintenanceView() {
  const auth = useAuth()
  const token = auth.user?.access_token ?? ''

  const [data, setData] = useState<MaintenanceRuns | null>(null)
  const [error, setError] = useState<string | null>(null)

  const reload = useCallback(async () => {
    if (!token) return
    setError(null)
    try {
      setData(await fetchMaintenanceRuns(token))
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to load maintenance history')
    }
  }, [token])

  useEffect(() => {
    void reload()
  }, [reload])

  return (
    <div className="admin">
      <div className="sa__head">
        <div>
          <h1 className="admin__title">Maintenance</h1>
          <p className="admin__note">
            Talus tidies itself automatically — these scheduled jobs expire only cruft (idempotency
            keys, orphaned media blobs), never your layers, features, or audit log. Cadence and
            retention are set per deployment; this view is read-only.
          </p>
        </div>
        <button type="button" className="btn" onClick={() => void reload()}>
          Refresh
        </button>
      </div>

      {error !== null && <p className="sa__error">{error}</p>}

      {data === null ? (
        <p className="sa__muted">Loading…</p>
      ) : (
        <>
          <table className="data-table">
            <thead>
              <tr>
                <th>Job</th>
                <th>Last run</th>
                <th>Status</th>
                <th>Items</th>
                <th>Freed</th>
                <th>Detail</th>
              </tr>
            </thead>
            <tbody>
              {data.jobs.map((j) => (
                <tr key={j.jobName}>
                  <td>{jobLabel(j.jobName)}</td>
                  <td>{fmtDateTime(j.lastRunAt)}</td>
                  <td>
                    <StatusPill status={j.lastStatus} />
                  </td>
                  <td>{j.lastItemsAffected ?? <span className="sa__muted">—</span>}</td>
                  <td>{fmtBytes(j.lastBytesFreed)}</td>
                  <td>{j.lastDetail ?? <span className="sa__muted">—</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>

          {data.recent.length > 0 && (
            <>
              <p className="admin__note" style={{ marginTop: '1.5rem' }}>
                Recent runs
              </p>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Job</th>
                    <th>Ran</th>
                    <th>Status</th>
                    <th>Items</th>
                    <th>Freed</th>
                    <th>Detail</th>
                  </tr>
                </thead>
                <tbody>
                  {data.recent.map((r) => (
                    <tr key={r.id}>
                      <td>{jobLabel(r.jobName)}</td>
                      <td>{fmtDateTime(r.startedAt)}</td>
                      <td>
                        <StatusPill status={r.status} />
                      </td>
                      <td>{r.itemsAffected}</td>
                      <td>{fmtBytes(r.bytesFreed)}</td>
                      <td>{r.detail ?? <span className="sa__muted">—</span>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </>
      )}
    </div>
  )
}
