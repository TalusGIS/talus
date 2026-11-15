import { useEffect, useState } from 'react'
import { CloudOff, KeyRound } from 'lucide-react'
import { useAuth } from 'react-oidc-context'

import { isConnectivityError, portalReachable, signinUnreachableText } from '@/lib/auth/degraded'
import { useSync } from '@/lib/offline/SyncProvider'

/**
 * The NavBar's connectivity indicator (Talus 35). Editing is online-only, so this is purely
 * informational: a cue that the map is limited to already-cached areas and edits are paused
 * until reconnect. Nothing renders when connected.
 *
 * WIDENED (handoff-auth-opendata-4 §6.1). The handoff asked for a NEW offline strip in
 * PortalShell's top bar on the premise that a degraded session shows "no indication anywhere in
 * the UI". That premise was false — this component already existed and was already mounted — but
 * it was reading a NARROWER signal than the gate:
 *
 *   SyncProvider.online = navigator.onLine
 *   App.tsx degraded    = !online || isConnectivityError(auth.error)
 *
 * so the case App.tsx exists to handle — the token endpoint unreachable while `navigator.onLine`
 * still reports true, which is the common "server is down / captive portal / VPN dropped" shape —
 * left the portal rendering from cache with this pill SILENT. A field editor was looking at cached
 * data believing it live. It now reports the same `degraded` the gate computes.
 *
 * `online` itself is deliberately untouched: SyncProvider gates the online-only WRITE path on it,
 * and widening that would block writes on a stale auth error. Write-path behaviour while offline is
 * decision-disconnected-editing.md's problem; this only reports state (§2.4's scope fence).
 *
 * The copy is visible text, not a `title` tooltip — a tooltip is unreachable on touch, which is
 * where a field editor actually is.
 */
export default function SyncIndicator({ signinFailed = false }: { signinFailed?: boolean }) {
  const { online } = useSync()
  const auth = useAuth()
  const authError = isConnectivityError(auth.error)
  const degraded = !online || authError || signinFailed
  // First-touch finding: the portal answered but its sign-in service did not
  // (an untrusted certificate on the auth address) and this pill said "Offline". When the browser
  // says it is online and the auth error is connectivity-class, ask THIS origin once: if it answers,
  // the network is fine and the sign-in service is the thing to name — with the address to open.
  const [portalUp, setPortalUp] = useState<boolean | null>(null)
  useEffect(() => {
    if (!online || (!authError && !signinFailed)) {
      setPortalUp(null)
      return
    }
    let cancelled = false
    void portalReachable().then((ok) => !cancelled && setPortalUp(ok))
    return () => {
      cancelled = true
    }
  }, [online, authError, signinFailed])
  if (!degraded) return null
  if (online && (signinFailed || portalUp === true)) {
    return (
      <div className="syncind" aria-live="polite">
        <span className="syncind__pill syncind__pill--offline syncind__pill--auth">
          <KeyRound size={13} aria-hidden="true" />
          {signinUnreachableText(auth.settings.authority)}
        </span>
      </div>
    )
  }
  return (
    <div className="syncind" aria-live="polite">
      <span className="syncind__pill syncind__pill--offline">
        <CloudOff size={13} aria-hidden="true" />
        Offline — showing cached data
      </span>
    </div>
  )
}
