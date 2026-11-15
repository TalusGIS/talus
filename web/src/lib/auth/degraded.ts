/**
 * The ONE connectivity-class auth-error test (handoff-auth-opendata-4 §6.1).
 *
 * `automaticSilentRenew` POSTs to the token endpoint when the access token expires. If the
 * network is unreachable that surfaces as `auth.error` with a "Failed to fetch"-class message —
 * which is NOT a real auth failure and must not collapse the running app (Talus 35 §5e). Both
 * App.tsx (the top-level gate) and RequireAuth (the route guard) branch on it.
 *
 * It lived as two byte-identical copies of this regex. A third consumer was about to be added —
 * SyncIndicator, so the offline pill reports the SAME state the gate computes rather than its own
 * narrower one — and three copies of a security-adjacent predicate is how they drift. Extracted
 * unchanged: the pattern is character-for-character what both call sites had.
 *
 * Note `navigator.onLine` alone is an unreliable guard: it reports online when the SERVER is
 * unreachable. That is exactly why this test exists alongside it, and why `degraded` is the OR of
 * the two, never either one on its own.
 */
export function isConnectivityError(err: { message?: string } | null | undefined): boolean {
  return (
    !!err &&
    /failed to fetch|load failed|networkerror|err_internet|err_network|err_connection|err_timed_out/i.test(
      err.message ?? '',
    )
  )
}

/**
 * The sign-in service's origin from the OIDC authority ("https://192.0.2.10:8443") — the thing to
 * name when the portal itself answers but sign-in does not (a first-touch finding:
 * an untrusted certificate on the auth address made Sign in do nothing, and the pill said "Offline").
 */
export function authOrigin(authority: string | undefined): string | null {
  if (!authority) return null
  try {
    return new URL(authority).origin
  } catch {
    return null
  }
}

/**
 * The installed (home-screen) app is a SEPARATE browser context: no address bar, no certificate
 * interstitial to accept, and none of the exceptions Safari remembered — so on a self-signed box the
 * only step that works from the icon is trusting the CA.
 */
export function isStandaloneDisplay(): boolean {
  try {
    if (typeof window === 'undefined') return false
    const nav = navigator as Navigator & { standalone?: boolean }
    return nav.standalone === true || !!window.matchMedia?.('(display-mode: standalone)').matches
  } catch {
    return false
  }
}

/** The one sentence a stuck person needs: in a browser, the address they must open once; from the
 *  installed app, the CA step — "open that address" is impossible there. */
export function signinUnreachableText(
  authority: string | undefined,
  standalone: boolean = isStandaloneDisplay(),
): string {
  const origin = authOrigin(authority)
  if (standalone) {
    return `Can’t reach the sign-in service${origin ? ` at ${origin}` : ''}. An installed app cannot accept a certificate itself: install this server’s CA on the phone and switch it on (iPhone: Settings → General → About → Certificate Trust Settings), then open the app again.`
  }
  return origin
    ? `Can’t reach the sign-in service at ${origin}. New installation? Open that address once and accept its certificate, or install the CA, then try again.`
    : 'Can’t reach the sign-in service. New installation? Open the sign-in address once and accept its certificate, then try again.'
}

/**
 * Is THIS origin answering? A same-origin, no-store GET of the runtime config (one small file Caddy
 * serves before anything else). True = the portal is reachable, so a connectivity-class auth error
 * is the sign-in service's problem, not the network's.
 */
export async function portalReachable(): Promise<boolean> {
  try {
    const res = await fetch('/config.json', { cache: 'no-store' })
    return res.ok
  } catch {
    return false
  }
}
