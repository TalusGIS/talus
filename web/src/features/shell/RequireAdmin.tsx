import type { ReactNode } from 'react'
import { Navigate } from 'react-router'

import { useIsAdmin } from '@/lib/auth/roles'

/**
 * Route guard for admin-only surfaces: a non-admin who reaches /admin by URL is redirected to
 * Home. UI gating only — the same stance as roles.ts. There's nothing privileged behind it in
 * this slice (Admin is a placeholder); when real user-admin lands, the api is the authority.
 */
export default function RequireAdmin({ children }: { children: ReactNode }) {
  const isAdmin = useIsAdmin()
  if (!isAdmin) return <Navigate to="/" replace />
  return <>{children}</>
}
