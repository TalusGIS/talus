import { useOutletContext } from 'react-router'

import type { BasemapStatus, SystemHealth } from '@/lib/api'

/**
 * The admin layout's shared health feed (handoff-admin-1a §1.2/§2): AdminView owns the ONE
 * 30 s /system-health poll and passes it down as Outlet context — the rail's attention dots
 * and HealthView read the same data. In its own module so AdminView keeps only component
 * exports (react-refresh).
 */
export interface AdminStatus {
  health: SystemHealth | null
  healthError: string | null
  basemap: BasemapStatus | null
}

export function useAdminStatus(): AdminStatus {
  return useOutletContext<AdminStatus>()
}

/** The destination that owns a health issue — the rail dot and Health's callout links agree. */
export function issueDestination(key: string): '/admin/backups' | '/admin/basemap' | null {
  if (key.startsWith('backup_')) return '/admin/backups'
  if (key.startsWith('basemap')) return '/admin/basemap'
  return null
}
