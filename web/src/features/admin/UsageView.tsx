import { useCallback, useEffect, useState } from 'react'
import { useAuth } from 'react-oidc-context'

import {
  ApiError,
  downloadReportCsv,
  fetchActivityReport,
  fetchContentReport,
  fetchGroupsReport,
  fetchMemberReport,
  fetchPublicContentReport,
  fetchServiceAccountReport,
  fetchStorageReport,
  fetchUsageReport,
  type ActivityReport,
  type ContentReport,
  type GroupsReport,
  type MemberReport,
  type PublicContentReport,
  type ServiceAccountReport,
  type StorageReport,
  type UsageReport,
} from '@/lib/api'

/**
 * Admin → Usage (Talus 29; renamed from ReportsView by handoff-admin-1a §1). Operational reporting: a content inventory + sharing breakdown, and the
 * member roster — each viewable as a table and downloadable as CSV. Counts + tables ONLY: no charts,
 * no dashboard builder, no BI over the GIS data (the drift fence). Admin-gated
 * server-side; this screen sits behind RequireAdmin.
 */
type ReportKind =
  | 'content'
  | 'members'
  | 'public'
  | 'usage'
  | 'activity'
  | 'storage'
  | 'groups'
  | 'service-accounts'

const SHARE_LABELS: Record<string, string> = {
  owner: 'Owner only',
  organization: 'Organization',
  groups: 'Groups',
  public: 'Public',
  unknown: 'Unknown',
}

const ROLE_LABELS: Record<string, string> = {
  'talus-admin': 'Administrator',
  'talus-publisher': 'Publisher',
  'talus-data-editor': 'Editor',
  'talus-viewer': 'Viewer',
  unassigned: 'Unassigned',
}

function fmtDate(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleDateString()
}

function fmtDateTime(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString()
}

// Human labels for the admin-audit action verbs (the raw event names are the CSV/JSON source of truth).
const ACTION_LABELS: Record<string, string> = {
  user_role_changed: 'Role changed',
  user_disabled: 'User disabled',
  user_enabled: 'User enabled',
  user_content_reassigned: 'Content reassigned',
  object_owner_transferred: 'Ownership transferred',
  layer_shares_set: 'Sharing changed',
  external_ref_shares_set: 'Sharing changed (reference)',
  group_items_added: 'Shared to group',
  group_item_removed: 'Unshared from group',
  open_data_curated: 'Curated to Open Data',
  open_data_uncurated: 'Removed from Open Data',
  layer_delete_protection_set: 'Delete protection changed',
  web_map_delete_protection_set: 'Delete protection changed (map)',
  raster_delete_protection_set: 'Delete protection changed (raster)',
  backup_settings_updated: 'Backup settings changed',
}

// Compact one-line detail rendering — the raw JSON is preserved in the CSV/JSON download.
function fmtDetail(detail: Record<string, unknown>): string {
  const entries = Object.entries(detail)
  if (entries.length === 0) return '—'
  return entries.map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(', ') || '∅' : String(v)}`).join('; ')
}

function fmtBytes(n: number | null): string {
  if (n === null || Number.isNaN(n)) return '—'
  if (n === 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)))
  return `${(n / 1024 ** i).toFixed(i === 0 ? 0 : 1)} ${units[i]}`
}

function fmtNum(n: number): string {
  return n.toLocaleString()
}

export default function UsageView() {
  const auth = useAuth()
  const token = auth.user?.access_token ?? ''

  const [kind, setKind] = useState<ReportKind>('content')
  const [content, setContent] = useState<ContentReport | null>(null)
  const [members, setMembers] = useState<MemberReport | null>(null)
  const [publicContent, setPublicContent] = useState<PublicContentReport | null>(null)
  const [usage, setUsage] = useState<UsageReport | null>(null)
  const [activity, setActivity] = useState<ActivityReport | null>(null)
  const [storage, setStorage] = useState<StorageReport | null>(null)
  const [groups, setGroups] = useState<GroupsReport | null>(null)
  const [serviceAccounts, setServiceAccounts] = useState<ServiceAccountReport | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [downloading, setDownloading] = useState(false)

  const reload = useCallback(async () => {
    if (!token) return
    setError(null)
    try {
      if (kind === 'content') setContent(await fetchContentReport(token))
      else if (kind === 'members') setMembers(await fetchMemberReport(token))
      else if (kind === 'usage') setUsage(await fetchUsageReport(token))
      else if (kind === 'activity') setActivity(await fetchActivityReport(token))
      else if (kind === 'storage') setStorage(await fetchStorageReport(token))
      else if (kind === 'groups') setGroups(await fetchGroupsReport(token))
      else if (kind === 'service-accounts') setServiceAccounts(await fetchServiceAccountReport(token))
      else setPublicContent(await fetchPublicContentReport(token))
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to load report')
    }
  }, [token, kind])

  useEffect(() => {
    void reload()
  }, [reload])

  async function download(): Promise<void> {
    setDownloading(true)
    setError(null)
    try {
      await downloadReportCsv(kind === 'public' ? 'public-content' : kind, token)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Download failed')
    } finally {
      setDownloading(false)
    }
  }

  return (
    <div className="admin">
      <div className="sa__head">
        <div>
          <h1 className="admin__title">Usage</h1>
          <p className="admin__note">
            Operational snapshots of your organization's content and members. View here or download as
            CSV for a spreadsheet.
          </p>
        </div>
        <button type="button" className="btn" onClick={() => void download()} disabled={downloading}>
          {downloading ? 'Preparing…' : 'Download CSV'}
        </button>
      </div>

      <div className="report-tabs" role="tablist" aria-label="Report type">
        <button
          type="button"
          role="tab"
          aria-selected={kind === 'content'}
          className={`report-tab${kind === 'content' ? ' report-tab--active' : ''}`}
          onClick={() => setKind('content')}
        >
          Content inventory
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={kind === 'members'}
          className={`report-tab${kind === 'members' ? ' report-tab--active' : ''}`}
          onClick={() => setKind('members')}
        >
          Members
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={kind === 'public'}
          className={`report-tab${kind === 'public' ? ' report-tab--active' : ''}`}
          onClick={() => setKind('public')}
        >
          Public content
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={kind === 'usage'}
          className={`report-tab${kind === 'usage' ? ' report-tab--active' : ''}`}
          onClick={() => setKind('usage')}
        >
          Cross-references
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={kind === 'activity'}
          className={`report-tab${kind === 'activity' ? ' report-tab--active' : ''}`}
          onClick={() => setKind('activity')}
        >
          Activity
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={kind === 'storage'}
          className={`report-tab${kind === 'storage' ? ' report-tab--active' : ''}`}
          onClick={() => setKind('storage')}
        >
          Storage
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={kind === 'groups'}
          className={`report-tab${kind === 'groups' ? ' report-tab--active' : ''}`}
          onClick={() => setKind('groups')}
        >
          Groups
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={kind === 'service-accounts'}
          className={`report-tab${kind === 'service-accounts' ? ' report-tab--active' : ''}`}
          onClick={() => setKind('service-accounts')}
        >
          Service accounts
        </button>
      </div>

      {error !== null && <p className="sa__error">{error}</p>}

      {kind === 'content' ? (
        <ContentReportView report={content} />
      ) : kind === 'members' ? (
        <MemberReportView report={members} />
      ) : kind === 'usage' ? (
        <UsageReportView report={usage} />
      ) : kind === 'activity' ? (
        <ActivityReportView report={activity} />
      ) : kind === 'storage' ? (
        <StorageReportView report={storage} />
      ) : kind === 'groups' ? (
        <GroupsReportView report={groups} />
      ) : kind === 'service-accounts' ? (
        <ServiceAccountReportView report={serviceAccounts} />
      ) : (
        <PublicContentReportView report={publicContent} />
      )}
    </div>
  )
}

// --- content -------------------------------------------------------------------

function ContentReportView({ report }: { report: ContentReport | null }) {
  if (report === null) return <p className="sa__muted">Loading…</p>
  const { summary, rows } = report
  return (
    <>
      <section className="members-summary" aria-label="Content summary">
        <div className="members-summary__stats">
          <div className="members-summary__stat">
            <span className="members-summary__num members-summary__num--accent">{summary.total}</span>
            <span className="members-summary__label">Total items</span>
          </div>
          <div className="members-summary__stat">
            <span className="members-summary__num members-summary__num--muted">
              {summary.byType.layer ?? 0}
            </span>
            <span className="members-summary__label">Layers</span>
          </div>
          <div className="members-summary__stat">
            <span className="members-summary__num members-summary__num--muted">
              {summary.byType.web_map ?? 0}
            </span>
            <span className="members-summary__label">Maps</span>
          </div>
        </div>
        <div className="members-summary__divider" />
        <p className="members-summary__subhead">Sharing breakdown</p>
        <div className="members-summary__roles">
          {(['owner', 'organization', 'groups', 'public'] as const).map((lvl) => (
            <div key={lvl} className="members-summary__role">
              <span className="members-summary__role-count">{summary.byShareLevel[lvl] ?? 0}</span>
              <span className="members-summary__role-name">{SHARE_LABELS[lvl]}</span>
            </div>
          ))}
        </div>
      </section>
      {summary.staleCount > 0 && (
        <p className="admin__note">
          {summary.staleCount} item{summary.staleCount === 1 ? '' : 's'} not accessed in the last 90 days
          (or never) — candidates to review or retire. “Last accessed” reflects API reads
          (features/export, map opens), not tile draws.
        </p>
      )}

      <table className="data-table">
        <thead>
          <tr>
            <th>Title</th>
            <th>Type</th>
            <th>Owner</th>
            <th>Share level</th>
            <th>Modified</th>
            <th>Last accessed</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={`${r.itemType}:${r.itemId}`}>
              <td>
                {r.title}
                {r.deleteProtected && <span className="sa__muted"> 🔒</span>}
              </td>
              <td>{r.itemType === 'web_map' ? 'Map' : 'Layer'}</td>
              <td>{r.ownerName ?? <span className="sa__muted">— (system)</span>}</td>
              <td>{SHARE_LABELS[r.shareLevel ?? 'unknown']}</td>
              <td>{fmtDate(r.updated_at)}</td>
              <td>{r.lastAccessed ? fmtDate(r.lastAccessed) : <span className="sa__muted">never</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}

// --- public content (governance lens, Talus 33) ---------------------------------

function PublicContentReportView({ report }: { report: PublicContentReport | null }) {
  if (report === null) return <p className="sa__muted">Loading…</p>
  const { summary, rows } = report
  return (
    <>
      <section className="members-summary" aria-label="Public content summary">
        <div className="members-summary__stats">
          <div className="members-summary__stat">
            <span className="members-summary__num members-summary__num--accent">{summary.total}</span>
            <span className="members-summary__label">Public items</span>
          </div>
          <div className="members-summary__stat">
            <span className="members-summary__num members-summary__num--muted">
              {summary.byType.layer ?? 0}
            </span>
            <span className="members-summary__label">Layers</span>
          </div>
          <div className="members-summary__stat">
            <span className="members-summary__num members-summary__num--muted">
              {summary.byType.web_map ?? 0}
            </span>
            <span className="members-summary__label">Maps</span>
          </div>
          <div className="members-summary__stat">
            <span className="members-summary__num members-summary__num--muted">{summary.notCurated}</span>
            <span className="members-summary__label">Not in Open Data</span>
          </div>
        </div>
      </section>
      <p className="admin__note">
        Everything currently shared publicly — broader than the Open Data catalog. Items marked “not in
        Open Data” are public but weren’t published through the governed Open Data flow; review them for
        unintended exposure.
      </p>

      {rows.length === 0 ? (
        <p className="sa__muted">Nothing is shared publicly.</p>
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th>Title</th>
              <th>Type</th>
              <th>Owner</th>
              <th>In Open Data</th>
              <th>Modified</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={`${r.itemType}:${r.itemId}`}>
                <td>{r.title}</td>
                <td>{r.itemType === 'web_map' ? 'Map' : 'Layer'}</td>
                <td>{r.ownerName ?? <span className="sa__muted">— (system)</span>}</td>
                <td>
                  {r.inOpenData ? (
                    <span className="sa-badge sa-badge--on">curated</span>
                  ) : (
                    <span className="sa-badge sa-badge--off">not curated</span>
                  )}
                </td>
                <td>{fmtDate(r.updated_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  )
}

// --- cross-references (where-used, decision-where-used.md) -----------------------

const ITEM_TYPE_LABELS: Record<string, string> = {
  layer: 'Layer',
  view: 'View',
  table: 'Table',
  raster: 'Raster',
  external: 'External ref',
  web_map: 'Map',
}

function UsageReportView({ report }: { report: UsageReport | null }) {
  if (report === null) return <p className="sa__muted">Loading…</p>
  const { summary } = report
  // Referenced items first (that's what the report is FOR), then the unreferenced tail by title.
  const rows = [...report.rows].sort((a, b) => b.total - a.total || a.title.localeCompare(b.title))
  return (
    <>
      <section className="members-summary" aria-label="Cross-reference summary">
        <div className="members-summary__stats">
          <div className="members-summary__stat">
            <span className="members-summary__num members-summary__num--accent">
              {summary.itemsReferenced}
            </span>
            <span className="members-summary__label">Items referenced</span>
          </div>
          <div className="members-summary__stat">
            <span className="members-summary__num members-summary__num--muted">{summary.items}</span>
            <span className="members-summary__label">Total items</span>
          </div>
          <div className="members-summary__stat">
            <span className="members-summary__num members-summary__num--muted">
              {summary.totalReferences}
            </span>
            <span className="members-summary__label">References</span>
          </div>
        </div>
      </section>
      <p className="admin__note">
        Where every item is referenced in-product: maps that draw it, views built on it, geocoders
        that search it, and Open Data / Home listings. An item with zero references is safe to delete
        without breaking anything else.
        {summary.degraded && ' (Open Data / Home columns unavailable right now — try a reload.)'}
      </p>

      <table className="data-table">
        <thead>
          <tr>
            <th>Title</th>
            <th>Type</th>
            <th>Maps</th>
            <th>Views</th>
            <th>Geocoders</th>
            <th>Open Data</th>
            <th>Home</th>
            <th>Total</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={`${r.itemType}:${r.itemId}`} className={r.total === 0 ? 'is-disabled' : undefined}>
              <td>{r.title}</td>
              <td>{ITEM_TYPE_LABELS[r.itemType] ?? r.itemType}</td>
              <td>{r.webMaps || '—'}</td>
              <td>{r.viewLayers || '—'}</td>
              <td>{r.geocoders || '—'}</td>
              <td>{r.openData ? 'yes' : '—'}</td>
              <td>{r.homeFeatured ? 'yes' : '—'}</td>
              <td>{r.total || '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}

// --- activity (governance-action log, decision-admin-reports-round2) --------------

function ActivityReportView({ report }: { report: ActivityReport | null }) {
  if (report === null) return <p className="sa__muted">Loading…</p>
  const { summary, rows } = report
  return (
    <>
      <section className="members-summary" aria-label="Activity summary">
        <div className="members-summary__stats">
          <div className="members-summary__stat">
            <span className="members-summary__num members-summary__num--accent">{summary.total}</span>
            <span className="members-summary__label">Actions</span>
          </div>
          <div className="members-summary__stat">
            <span className="members-summary__num members-summary__num--muted">
              {Object.keys(summary.byAction).length}
            </span>
            <span className="members-summary__label">Action types</span>
          </div>
        </div>
      </section>
      <p className="admin__note">
        Who changed sharing, ownership, roles, and other governance settings, and when. Includes actions
        by any member — an owner publishing their own layer appears here alongside admin changes.
      </p>

      {rows.length === 0 ? (
        <p className="sa__muted">No governance actions recorded yet.</p>
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th>Time</th>
              <th>Actor</th>
              <th>Action</th>
              <th>Target</th>
              <th>Details</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={`${r.at}:${i}`}>
                <td>{fmtDateTime(r.at)}</td>
                <td>{r.actorName ?? <code className="sa-code">{r.actorId}</code>}</td>
                <td>{ACTION_LABELS[r.action] ?? r.action}</td>
                <td>
                  {r.targetType ? (
                    <>
                      {r.targetName ?? r.targetId}
                      <div className="sa__muted">{r.targetType}</div>
                    </>
                  ) : (
                    <span className="sa__muted">—</span>
                  )}
                </td>
                <td className="sa__muted">{fmtDetail(r.detail)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  )
}

// --- storage / capacity ---------------------------------------------------------

function StorageReportView({ report }: { report: StorageReport | null }) {
  if (report === null) return <p className="sa__muted">Loading…</p>
  const { summary, rows } = report
  return (
    <>
      <section className="members-summary" aria-label="Storage summary">
        <div className="members-summary__stats">
          <div className="members-summary__stat">
            <span className="members-summary__num members-summary__num--accent">
              {fmtBytes(summary.databaseBytes)}
            </span>
            <span className="members-summary__label">Database</span>
          </div>
          <div className="members-summary__stat">
            <span className="members-summary__num members-summary__num--muted">
              {fmtBytes(summary.totalObjectBytes)}
            </span>
            <span className="members-summary__label">Object storage</span>
          </div>
          <div className="members-summary__stat">
            <span className="members-summary__num members-summary__num--muted">{summary.layerCount}</span>
            <span className="members-summary__label">Layers</span>
          </div>
          <div className="members-summary__stat">
            <span className="members-summary__num members-summary__num--muted">
              {fmtNum(summary.totalFeatures)}
            </span>
            <span className="members-summary__label">Features</span>
          </div>
        </div>
        <div className="members-summary__divider" />
        <p className="members-summary__subhead">Object storage by bucket</p>
        <div className="members-summary__roles">
          {Object.entries(summary.objectBuckets).map(([bucket, bytes]) => (
            <div key={bucket} className="members-summary__role">
              <span className="members-summary__role-count">{fmtBytes(bytes)}</span>
              <span className="members-summary__role-name">{bucket}</span>
            </div>
          ))}
        </div>
      </section>

      <table className="data-table">
        <thead>
          <tr>
            <th>Layer</th>
            <th>Layer ID</th>
            <th>Features</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.layerId}>
              <td>{r.title}</td>
              <td>
                <code className="sa-code">{r.layerId}</code>
              </td>
              <td>{r.featureCount === null ? <span className="sa__muted">—</span> : fmtNum(r.featureCount)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}

// --- groups inventory -----------------------------------------------------------

function GroupsReportView({ report }: { report: GroupsReport | null }) {
  if (report === null) return <p className="sa__muted">Loading…</p>
  const { summary, rows } = report
  return (
    <>
      <section className="members-summary" aria-label="Groups summary">
        <div className="members-summary__stats">
          <div className="members-summary__stat">
            <span className="members-summary__num members-summary__num--accent">{summary.total}</span>
            <span className="members-summary__label">Groups</span>
          </div>
        </div>
      </section>
      {summary.degraded && (
        <p className="admin__note">
          Some member/content counts are temporarily unavailable (an authorization read failed) — try a
          reload.
        </p>
      )}

      <table className="data-table">
        <thead>
          <tr>
            <th>Group</th>
            <th>Members</th>
            <th>Shared content</th>
            <th>Created</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.groupId}>
              <td>
                {r.displayName}
                {r.deleteProtected && <span className="sa__muted"> 🔒</span>}
                <div className="sa__muted">
                  <code className="sa-code">{r.groupId}</code>
                </div>
              </td>
              <td>{r.memberCount === null ? <span className="sa__muted">—</span> : r.memberCount}</td>
              <td>{r.contentCount === null ? <span className="sa__muted">—</span> : r.contentCount}</td>
              <td>{fmtDate(r.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}

// --- service-account inventory --------------------------------------------------

function ServiceAccountReportView({ report }: { report: ServiceAccountReport | null }) {
  if (report === null) return <p className="sa__muted">Loading…</p>
  const { summary, rows } = report
  return (
    <>
      <section className="members-summary" aria-label="Service-account summary">
        <div className="members-summary__stats">
          <div className="members-summary__stat">
            <span className="members-summary__num members-summary__num--accent">{summary.total}</span>
            <span className="members-summary__label">Service accounts</span>
          </div>
          <div className="members-summary__stat">
            <span className="members-summary__num members-summary__num--muted">{summary.enabled}</span>
            <span className="members-summary__label">Enabled</span>
          </div>
          <div className="members-summary__stat">
            <span className="members-summary__num members-summary__num--muted">{summary.disabled}</span>
            <span className="members-summary__label">Disabled</span>
          </div>
        </div>
      </section>

      {rows.length === 0 ? (
        <p className="sa__muted">No service accounts.</p>
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Client ID</th>
              <th>Status</th>
              <th>Access</th>
              <th>Last active</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td>{r.displayName}</td>
                <td>
                  <code className="sa-code">{r.clientId ?? '—'}</code>
                </td>
                <td>
                  {r.enabled === true ? (
                    <span className="sa-badge sa-badge--on">enabled</span>
                  ) : r.enabled === false ? (
                    <span className="sa-badge sa-badge--off">disabled</span>
                  ) : (
                    <span className="sa__muted">unknown</span>
                  )}
                </td>
                <td>{r.accessLevel === 'read_write' ? 'Read / write' : 'Read only'}</td>
                <td>{fmtDate(r.last_seen_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  )
}

// --- members -------------------------------------------------------------------

function MemberReportView({ report }: { report: MemberReport | null }) {
  if (report === null) return <p className="sa__muted">Loading…</p>
  const { summary, rows } = report
  return (
    <>
      <section className="members-summary" aria-label="Member summary">
        <div className="members-summary__stats">
          <div className="members-summary__stat">
            <span className="members-summary__num members-summary__num--accent">{summary.total}</span>
            <span className="members-summary__label">Total members</span>
          </div>
          <div className="members-summary__stat">
            <span className="members-summary__num members-summary__num--muted">{summary.pending}</span>
            <span className="members-summary__label">Pending</span>
          </div>
          <div className="members-summary__stat">
            <span className="members-summary__num members-summary__num--muted">{summary.disabled}</span>
            <span className="members-summary__label">Disabled</span>
          </div>
        </div>
      </section>

      <table className="data-table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Username</th>
            <th>Role</th>
            <th>Status</th>
            <th>Last seen</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((u) => (
            <tr key={u.id} className={u.active ? undefined : 'is-disabled'}>
              <td>
                {u.display_name}
                <div className="sa__muted">{u.email}</div>
              </td>
              <td>
                <code className="sa-code">{u.username}</code>
              </td>
              <td>{ROLE_LABELS[u.role ?? 'unassigned'] ?? u.role}</td>
              <td>
                {u.active ? (
                  <span className="sa-badge sa-badge--on">active</span>
                ) : (
                  <span className="sa-badge sa-badge--off">disabled</span>
                )}
              </td>
              <td>{fmtDate(u.last_seen_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}
