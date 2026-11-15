import { useCallback, useEffect, useState } from 'react'
import { NavLink, Outlet } from 'react-router'
import { PaidAdminRailItems } from '@paid'
import { useAuth } from 'react-oidc-context'
import {
  Activity,
  BrushCleaning,
  ChartNoAxesColumn,
  Database,
  DatabaseBackup,
  Globe,
  House,
  KeyRound,
  Layers,
  MapPin,
  Megaphone,
  Plug,
  Users,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'

import { basemapIsStale } from '@/features/admin/BasemapSection'
import { issueDestination, type AdminStatus } from '@/features/shell/adminStatus'
import {
  ApiError,
  fetchBasemapStatus,
  fetchServiceAccounts,
  fetchSystemHealth,
  fetchUsers,
  type BasemapStatus,
  type SystemHealth,
} from '@/lib/api'

/**
 * Admin layout (handoff-admin-1a §1): a grouped rail over nested, deep-linkable routes —
 * replacing the eight flat useState tabs, under which no admin screen was linkable and
 * cross-navigation needed a setSection relay (the deleted onOpenBackups prop). The routes
 * live in PortalShell under the existing RequireAuth + RequireAdmin wrapper; this renders
 * the rail and an Outlet. Role-gated there; every endpoint behind it is admin-gated
 * server-side.
 *
 * This layout owns the ONE /system-health poll (30 s, while any /admin/* page is mounted):
 * the rail's attention dots and HealthView both read it via Outlet context, so Health has
 * no fetch of its own and the dots work from every admin page — §1.2's "how Health's
 * issues become findable".
 */
const REFRESH_MS = 30_000

interface RailItem {
  to: string
  label: string
  icon: LucideIcon
}

const GROUPS: ReadonlyArray<{ label: string; items: RailItem[] }> = [
  {
    label: 'Operations',
    items: [
      { to: 'health', label: 'Health', icon: Activity },
      { to: 'backups', label: 'Backups & recovery', icon: DatabaseBackup },
      { to: 'maintenance', label: 'Maintenance', icon: BrushCleaning },
      { to: 'usage', label: 'Usage', icon: ChartNoAxesColumn },
    ],
  },
  {
    label: 'People',
    items: [
      { to: 'users', label: 'Users', icon: Users },
      { to: 'service-accounts', label: 'Service accounts', icon: KeyRound },
    ],
  },
  {
    label: 'Organization',
    items: [
      { to: 'notices', label: 'Notices', icon: Megaphone },
      { to: 'sharing', label: 'Sharing & access', icon: Globe },
      { to: 'home', label: 'Home page', icon: House },
      { to: 'connections', label: 'Connections', icon: Plug },
    ],
  },
  {
    label: 'Map data',
    items: [
      { to: 'basemap', label: 'Basemap', icon: Layers },
      { to: 'geocoders', label: 'Geocoders', icon: MapPin },
      { to: 'database-sources', label: 'Database sources', icon: Database },
    ],
  },
]

export default function AdminView() {
  const auth = useAuth()
  const token = auth.user?.access_token ?? ''

  const [health, setHealth] = useState<SystemHealth | null>(null)
  const [healthError, setHealthError] = useState<string | null>(null)
  const [basemap, setBasemap] = useState<BasemapStatus | null>(null)
  const [userCount, setUserCount] = useState<number | null>(null)
  const [saCount, setSaCount] = useState<number | null>(null)

  const reload = useCallback(async () => {
    if (!token) return
    try {
      setHealth(await fetchSystemHealth(token))
      setHealthError(null)
    } catch (e) {
      setHealthError(e instanceof ApiError ? e.message : 'Could not load system health.')
    }
    // Basemap staleness nudge (decision-basemap-provisioning.md) — a separate, anon read;
    // its failure never blanks the health page or the rail.
    try {
      setBasemap(await fetchBasemapStatus())
    } catch {
      setBasemap(null)
    }
  }, [token])

  useEffect(() => {
    void reload()
    const timer = window.setInterval(() => void reload(), REFRESH_MS)
    return () => window.clearInterval(timer)
  }, [reload])

  // Rail counts (§1.2) — loaded once per admin visit, a sense of scale rather than a live
  // gauge (creating a user updates it on the next visit, not the next tick).
  useEffect(() => {
    if (!token) return
    let cancelled = false
    void fetchUsers(token)
      .then((u) => {
        if (!cancelled) setUserCount(u.length)
      })
      .catch(() => {})
    void fetchServiceAccounts(token)
      .then((s) => {
        if (!cancelled) setSaCount(s.length)
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [token])

  // One right-aligned affordance per item at most (§1.2): the amber dot marks the
  // destination that owns an open issue; it is the only amber in the rail.
  const attention: Record<string, boolean> = {
    backups: health?.issues.some((i) => issueDestination(i.key) === '/admin/backups') ?? false,
    basemap:
      (health?.issues.some((i) => issueDestination(i.key) === '/admin/basemap') ?? false) ||
      (basemap != null && (!basemap.installed || basemapIsStale(basemap))),
  }
  const counts: Record<string, number | null> = {
    users: userCount,
    'service-accounts': saCount,
  }

  const status: AdminStatus = { health, healthError, basemap }

  return (
    <div className="admin-shell">
      <nav className="admin-rail" aria-label="Admin sections">
        <div className="admin-rail__brand">Administration</div>
        {GROUPS.map((g) => (
          <div key={g.label}>
            <div className="admin-rail__group">{g.label}</div>
            {g.items.map((it) => {
              const Icon = it.icon
              const count = counts[it.to]
              return (
                <NavLink
                  key={it.to}
                  to={it.to}
                  className={({ isActive }) => `admin-rail__item${isActive ? ' is-active' : ''}`}
                >
                  <Icon size={16} aria-hidden="true" />
                  <span className="admin-rail__label">{it.label}</span>
                  {attention[it.to] ? (
                    <span className="admin-rail__dot" role="img" aria-label="Needs attention" />
                  ) : count != null ? (
                    <span className="admin-rail__count">{count}</span>
                  ) : null}
                </NavLink>
              )
            })}
          </div>
        ))}
        {/* Talus+ group (F8 f5): entitlement-probed; the public-build stub renders nothing. */}
        <PaidAdminRailItems />
      </nav>
      <div className="admin-shell__body">
        <Outlet context={status} />
      </div>
    </div>
  )
}
