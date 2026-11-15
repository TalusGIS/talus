/**
 * Runtime deployment config (backlog 9a): fetched from same-origin /config.json at boot,
 * BEFORE React mounts. This is what lets ONE built dist serve every deployment — the
 * appliance's one-image model depends on it.
 *
 * Serving contract:
 *   - edge (Caddy): install.sh --edge writes proxy/config/config.json from .env; served
 *     no-store, so an origin change takes effect on reload.
 *   - dev (vite): no /config.json exists → 404 → null → the VITE_ / localhost fallbacks
 *     in oidcConfig/client apply. Dev behavior is byte-identical to pre-9a.
 *
 * Fail-open ON PURPOSE: a missing/broken config.json must not blank the portal — the
 * fallbacks keep dev and legacy deploys working, and an edge misconfig surfaces as a
 * login failure (observable) rather than a white page (mystifying).
 */
export interface RuntimeConfig {
  /** API base for the SPA's fetches. Relative ("/api/v1") on purpose under edge. */
  apiBase?: string
  /** Full OIDC authority URL, e.g. https://auth.example.org/realms/talus */
  oidcAuthority?: string
}

/**
 * ★ Offline, a home-screen web app on iOS can leave this fetch PENDING FOREVER — neither a
 * rejection nor a timeout — and the shell never mounts (Talus Field f3f, airplane mode, 2026-09-16:
 * the boot log stopped at "starting"). Two guards, both harmless online:
 *   1. When the device says it is offline, read the last copy a service worker cached (Talus Field's
 *      worker keeps one under a NetworkFirst route) straight from the Cache API — no network call at
 *      all. No copy → fall through to the fetch, which then has the timeout.
 *   2. The fetch aborts after 4 s. The portal's own boot was never at risk (it has no worker cache
 *      to read and fails open), but a hung fetch would have blanked it exactly the same way.
 */
const CONFIG_TIMEOUT_MS = 4000

export async function loadRuntimeConfig(): Promise<RuntimeConfig | null> {
  try {
    if (typeof navigator !== 'undefined' && navigator.onLine === false && typeof caches !== 'undefined') {
      const hit = await caches.match('/config.json', { ignoreSearch: true })
      if (hit) return parse(await hit.json())
    }
    const ctrl = new AbortController()
    const timer = setTimeout(() => ctrl.abort(), CONFIG_TIMEOUT_MS)
    let res: Response
    try {
      res = await fetch('/config.json', { cache: 'no-store', signal: ctrl.signal })
    } finally {
      clearTimeout(timer)
    }
    if (!res.ok) return null
    return parse(await res.json())
  } catch {
    return null
  }
}

function parse(cfg: unknown): RuntimeConfig | null {
  if (typeof cfg !== 'object' || cfg === null) return null
  return sanitize(cfg as Record<string, unknown>)
}

/**
 * Whitelist the two values (security-review hardening, 2026-08-08): apiBase must be
 * SAME-ORIGIN-RELATIVE (a "/" path — never an absolute URL, so bearer-token'd fetches
 * can't be steered cross-origin even by a hypothetically compromised config), and
 * oidcAuthority must parse as an http(s) URL. Anything else is dropped field-wise —
 * the build-time fallbacks then apply, keeping a malformed config observable (login
 * fails against the wrong authority) rather than silently dangerous.
 */
function sanitize(cfg: Record<string, unknown>): RuntimeConfig {
  const out: RuntimeConfig = {}
  if (typeof cfg.apiBase === 'string' && cfg.apiBase.startsWith('/') && !cfg.apiBase.startsWith('//')) {
    out.apiBase = cfg.apiBase
  }
  if (typeof cfg.oidcAuthority === 'string') {
    try {
      const u = new URL(cfg.oidcAuthority)
      if (u.protocol === 'https:' || u.protocol === 'http:') out.oidcAuthority = cfg.oidcAuthority
    } catch {
      /* not a URL — drop the field */
    }
  }
  return out
}
