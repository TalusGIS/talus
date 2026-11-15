import { useEffect, useState } from 'react'
import { useAuth } from 'react-oidc-context'

import { fetchOrgSettings, type OrgSettings } from '@/lib/api'

/**
 * Talus 22 — the org-wide information / maintenance banner, rendered at the top of the portal for
 * every signed-in user. Admin-authored in Admin → Settings; this only displays it. Fail-safe: if
 * the settings read fails it renders nothing (a missing notice is the safe degrade) and never
 * blocks the shell. It reads once on mount, so an admin's change shows to others on their next
 * load (the authoring panel shows a live preview meanwhile).
 */
export default function InfoBanner() {
  const auth = useAuth()
  const token = auth.user?.access_token ?? ''
  const [settings, setSettings] = useState<OrgSettings | null>(null)

  useEffect(() => {
    if (!token) return
    let active = true
    fetchOrgSettings(token)
      .then((s) => active && setSettings(s))
      .catch(() => active && setSettings(null)) // fail-safe: no banner on read error
    return () => {
      active = false
    }
  }, [token])

  if (!settings || !settings.bannerEnabled || !settings.bannerMessage) return null
  return (
    <div
      className={`info-banner info-banner--${settings.bannerLevel}`}
      role="status"
      aria-live="polite"
    >
      <span className="info-banner__text">{settings.bannerMessage}</span>
    </div>
  )
}
