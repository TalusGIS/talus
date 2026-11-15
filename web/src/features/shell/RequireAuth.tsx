import { useEffect, useState, type ReactNode } from 'react'
import { useAuth } from 'react-oidc-context'

import { isConnectivityError } from '@/lib/auth/degraded'
import SignInScreen from './SignInScreen'

/**
 * Route guard for surfaces that REQUIRE a signed-in user (Talus 30). With the public shell, an
 * anonymous visitor can reach the app; protected routes (New Map authoring, the authenticated
 * catalog, editing) wrap their element in this guard, which sends the visitor to Keycloak sign-in.
 * UI gating only — every protected endpoint is enforced server-side; this just routes the anon user
 * to login instead of rendering a surface that would 401.
 *
 * Connectivity-aware (Talus 35 §5e + §11): NEVER bounce to Keycloak when offline OR when a token
 * renewal just failed on the network (the access token expires after minutes; a sign-in redirect is
 * then a doomed call that would tear down the surface and strand the user). `navigator.onLine` alone
 * is unreliable (it reports online when the server is unreachable), so we also treat a
 * connectivity-class auth error as degraded. While degraded we keep rendering the children so cached
 * viewing survives; auth recovers on reconnect. The real security boundary is the server, not this guard.
 */
export default function RequireAuth({ children }: { children: ReactNode }) {
  const auth = useAuth()
  const [online, setOnline] = useState(() => (typeof navigator === 'undefined' ? true : navigator.onLine))

  useEffect(() => {
    const goOnline = () => setOnline(true)
    const goOffline = () => setOnline(false)
    window.addEventListener('online', goOnline)
    window.addEventListener('offline', goOffline)
    return () => {
      window.removeEventListener('online', goOnline)
      window.removeEventListener('offline', goOffline)
    }
  }, [])

  const degraded = !online || isConnectivityError(auth.error)

  useEffect(() => {
    // Only redirect to sign-in when truly online + connected — otherwise it can't succeed and would
    // collapse the surface.
    if (!auth.isLoading && !auth.isAuthenticated && !degraded) {
      void auth.signinRedirect()
    }
  }, [auth.isLoading, auth.isAuthenticated, auth, degraded])

  // Render children when authenticated OR when degraded (so an offline/expired token doesn't strand
  // the user mid-session); only the connected-and-unauthenticated case shows the redirect notice.
  if (!auth.isAuthenticated && !degraded) {
    // The redirect hop. `inFlow` because this guard renders INSIDE PortalShell — the full-viewport
    // dark field SignInScreen normally paints would cover the portal chrome (handoff §6.5). The
    // `loading` state is lockup-only, which is what a hop should look like: not a wall, not
    // unstyled text (it used to flash a bare `.sa__muted` paragraph).
    return <SignInScreen state="loading" inFlow onSignIn={() => void auth.signinRedirect()} />
  }
  return <>{children}</>
}
