import type { User } from 'oidc-client-ts'

/** Best-available human label for the signed-in user. */
export function userDisplayName(user: User | null | undefined): string {
  const profile = user?.profile
  return profile?.preferred_username ?? profile?.name ?? profile?.sub ?? 'unknown'
}
