import { useCallback, useEffect, useState } from 'react'
import { useAuth } from 'react-oidc-context'
import { useLocation } from 'react-router'

import EmbedApp from '@/features/map/EmbedMapView'
import PortalShell from '@/features/shell/PortalShell'
import SignInScreen from '@/features/shell/SignInScreen'
import { isEmbedPath, isFramed } from '@/lib/embedPath'
import { fetchPublicConfig, type PublicConfig } from '@/lib/api'
import { isConnectivityError, signinUnreachableText } from '@/lib/auth/degraded'

/**
 * Top-level gate (Talus 30). The SPA renders the portal shell for:
 *   - any AUTHENTICATED user (as before), OR
 *   - ANONYMOUS visitors WHEN this deployment offers a public face
 *     (`/public/config.publicAnonAccessEnabled`, the F6 deploy switch).
 * Otherwise the login wall stays — so internal/air-gapped deploys (public access OFF) are unchanged.
 *
 * Fail-safe: if /public/config can't be read, we treat the public face as OFF (login wall). The real
 * security boundary is the API (T23 fail-closed: anon sees only public_shared); this shell is cosmetic.
 */
export default function App() {
  const auth = useAuth()
  // first-touch finding: a refused redirect (the sign-in address unreachable, or
  // its certificate not trusted) left the card inert — the card now names the address to open once
  const [signinProblem, setSigninProblem] = useState<string | null>(null)
  const signIn = useCallback(() => {
    setSigninProblem(null)
    auth.signinRedirect().catch(() => setSigninProblem(signinUnreachableText(auth.settings.authority)))
  }, [auth])
  const location = useLocation()
  const [config, setConfig] = useState<PublicConfig | null>(null)
  const [configError, setConfigError] = useState(false)
  // Talus 35 §5e: track connectivity so an OFFLINE silent-renew failure doesn't replace the whole
  // app with the auth-error wall (which would strand a field editor mid-session).
  const [online, setOnline] = useState(() => (typeof navigator === 'undefined' ? true : navigator.onLine))

  useEffect(() => {
    fetchPublicConfig()
      .then(setConfig)
      .catch(() => setConfigError(true)) // unreachable => treat public face as OFF (fail-safe)
  }, [])

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

  // /embed/* (decision-map-embed.md F1/F3): the chrome-less iframe mini-app, BEFORE every auth
  // branch — an embed must never show a login wall or redirect inside someone else's page. The
  // real gate is the API's public-map check (anon 404 on private); this branch is routing only.
  // The path is read the way the EDGE reads it (decoded, any case, slashes merged — lib/embedPath):
  // the edge serves every such spelling as frameable, so every one of them must be the embed view.
  if (isEmbedPath(location.pathname)) {
    return <EmbedApp />
  }
  // The second lock (once-over S4): inside a frame, on any other path, render nothing. The edge
  // already forbids framing these paths; this is for the day the two disagree again.
  if (isFramed(window)) {
    return null
  }

  if (auth.isLoading || (config === null && !configError)) {
    return <SignInScreen state="loading" onSignIn={signIn} />
  }

  // react-oidc-context surfaces a failed silent (prompt=none) renew as `auth.error`. The OIDC
  // "interactive login required" signals — login_required / interaction_required / consent_required /
  // account_selection_required — are NOT failures: they mean the SSO session simply lapsed (a returning
  // user whose Keycloak session timed out, or a stale persisted token). Those drop to the normal sign-in
  // prompt below, NOT the scary red error wall (which previously showed "Authentication error:
  // login_required" to anyone returning after their session expired).
  const oidcErr = auth.error as (Error & { error?: string }) | undefined
  const needsInteractiveLogin =
    !!oidcErr &&
    /login_required|interaction_required|consent_required|account_selection_required/i.test(
      `${oidcErr.error ?? ''} ${oidcErr.message ?? ''}`,
    )
  // A token renewal that failed because the NETWORK was unreachable (the access token expired while
  // offline → `automaticSilentRenew` POSTs to the token endpoint → "Failed to fetch"). This is the
  // common offline case, and `navigator.onLine` is an unreliable guard (it reports online when the
  // server is unreachable). So treat a connectivity-class auth error like being offline: keep the app
  // rendering (cached viewing survives) rather than collapsing to the error wall; auth recovers on reconnect.
  const connectivityError = isConnectivityError(oidcErr)
  const degraded = !online || connectivityError

  // The hard auth-error wall is reserved for a GENUINE, unexpected error while ONLINE and connected.
  // Offline/degraded, a renewal failure must NOT replace the running app; an expired SSO session
  // (login_required) falls through to the sign-in prompt below.
  if (auth.error && online && !needsInteractiveLogin && !connectivityError) {
    return (
      <SignInScreen
        state="error"
        errorDetail={auth.error.message}
        onSignIn={signIn}
      />
    )
  }

  if (auth.isAuthenticated) {
    return <PortalShell publicConfig={config} />
  }

  // Degraded connectivity with no live session (the token lapsed offline, or a renewal hit the
  // network and failed): still render the shell so the cached map stays viewable. RequireAuth is
  // likewise degraded-aware, and the server remains the real auth boundary. Auth recovers on reconnect.
  if (degraded) {
    return <PortalShell publicConfig={config} />
  }

  // Anonymous: render the public shell ONLY when this deployment offers a public face.
  if (config?.publicAnonAccessEnabled) {
    return <PortalShell publicConfig={config} />
  }

  // `expired` is the lapsed-SSO-session case separated above (needsInteractiveLogin) — presented
  // calmly, with no danger colour, because it is not a fault. Both states offer the same action.
  return (
    <SignInScreen
      state={signinProblem ? 'error' : needsInteractiveLogin ? 'expired' : 'signin'}
      errorDetail={signinProblem ?? undefined}
      onSignIn={signIn}
    />
  )
}
