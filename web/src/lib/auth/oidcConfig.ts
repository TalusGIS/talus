import { WebStorageStateStore } from 'oidc-client-ts'
import type { AuthProviderProps } from 'react-oidc-context'

import type { RuntimeConfig } from '@/lib/runtimeConfig'

const origin = window.location.origin

/**
 * OIDC config for the Talus portal SPA against the Keycloak `talus` realm.
 *
 * Built as a FUNCTION since 9a: the authority resolves runtime /config.json first
 * (one dist, every deployment), then VITE_OIDC_AUTHORITY, then the localhost dev
 * realm. redirect URIs were always runtime (`window.location.origin`) — untouched.
 *
 * PKCE: oidc-client-ts uses PKCE with S256 by default for response_type=code —
 * what the talus-portal client requires. No extra config needed.
 */
export function buildOidcConfig(runtime?: RuntimeConfig | null): AuthProviderProps {
  return {
  authority:
    runtime?.oidcAuthority ??
    import.meta.env.VITE_OIDC_AUTHORITY ??
    'http://localhost:8080/realms/talus',
  client_id: import.meta.env.VITE_OIDC_CLIENT_ID ?? 'talus-portal',
  redirect_uri: import.meta.env.VITE_OIDC_REDIRECT_URI ?? `${origin}/`,
  post_logout_redirect_uri:
    import.meta.env.VITE_OIDC_POST_LOGOUT_REDIRECT_URI ?? `${origin}/`,
  response_type: 'code',
  scope: import.meta.env.VITE_OIDC_SCOPE ?? 'openid profile email',
  // Persist the session across tab/browser close (survives for repeat users).
  userStore: new WebStorageStateStore({ store: window.localStorage }),
  // Strip ?code & ?state from the URL once the redirect callback is processed. PRESERVE the current
  // history.state: react-router's data router keeps its history INDEX there ({usr, key, idx}), and
  // replacing it with {} breaks the index chain — which silently degrades useBlocker's back/forward
  // (POP) blocking into a full document navigation (decision-nav-blocker.md).
  onSigninCallback: () => {
    window.history.replaceState(window.history.state, document.title, window.location.pathname)
  },
  }
}
