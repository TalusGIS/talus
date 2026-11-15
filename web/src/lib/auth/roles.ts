import type { User } from 'oidc-client-ts'
import { useAuth } from 'react-oidc-context'

interface RealmAccessClaims {
  realm_access?: { roles?: string[] }
}

/**
 * Decode a JWT payload WITHOUT verifying it. This is UI gating only — the api is
 * the real authority (it validates the token and enforces capabilities server-side); this
 * merely decides which affordances to render. A tampered token buys nothing: the
 * server rejects writes regardless of what the UI shows.
 */
function decodeJwtPayload(token: string): RealmAccessClaims | null {
  const segment = token.split('.')[1]
  if (!segment) return null
  try {
    const b64 = segment.replace(/-/g, '+').replace(/_/g, '/')
    const padded = b64 + '='.repeat((4 - (b64.length % 4)) % 4)
    return JSON.parse(atob(padded)) as RealmAccessClaims
  } catch {
    return null
  }
}

/** Keycloak realm roles from the access token (where `realm_access` lives). */
export function realmRoles(user: User | null | undefined): string[] {
  const token = user?.access_token
  if (!token) return []
  return decodeJwtPayload(token)?.realm_access?.roles ?? []
}

// The CREATE-LAYER capability (Talus 10). Mirrors the api's _has_publish_role
// ({talus-publisher, talus-admin}); this is UI gating ONLY — the POST /layers route
// enforces it for real, so a tampered token still can't claim a slot.
const PUBLISH_ROLES = ['talus-publisher', 'talus-admin']

/** Whether the user may create layers (publisher/admin). */
export function hasPublishRole(user: User | null | undefined): boolean {
  return realmRoles(user).some((r) => PUBLISH_ROLES.includes(r))
}

/** Hook form of hasPublishRole — gates the Create-a-layer affordance. */
export function useIsPublisher(): boolean {
  const auth = useAuth()
  return hasPublishRole(auth.user)
}

// The ADMIN capability (Talus 17). The realm role that backs the Admin surface (user + role
// management, spec §4). UI gating ONLY: this slice's Admin is a placeholder with nothing
// privileged behind it, and when real user-admin lands the api is the authority — a tampered
// token still can't provision users.
const ADMIN_ROLE = 'talus-admin'

/** Whether the user is a Talus admin (gates the Admin nav tab + /admin route). */
export function hasAdminRole(user: User | null | undefined): boolean {
  return realmRoles(user).includes(ADMIN_ROLE)
}

/** Hook form of hasAdminRole — gates the Admin tab and the Admin route guard. */
export function useIsAdmin(): boolean {
  const auth = useAuth()
  return hasAdminRole(auth.user)
}

// Friendly labels for the Talus realm roles, highest-capability first. Used for the nav's
// signed-in role context (display only — capability checks use the typed helpers above).
const ROLE_LABELS: ReadonlyArray<readonly [string, string]> = [
  ['talus-admin', 'Admin'],
  ['talus-publisher', 'Publisher'],
  ['talus-data-editor', 'Editor'],
  ['talus-viewer', 'Viewer'],
]

/** The user's highest Talus role as a display label, or null if they hold none. */
export function primaryRoleLabel(user: User | null | undefined): string | null {
  const roles = realmRoles(user)
  for (const [role, label] of ROLE_LABELS) {
    if (roles.includes(role)) return label
  }
  return null
}
