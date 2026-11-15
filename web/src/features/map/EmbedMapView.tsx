import { Route, Routes } from 'react-router'

import AssetMap from '@/features/map/AssetMap'
import { SyncProvider } from '@/lib/offline/SyncProvider'

/**
 * The /embed/* mini-app (decision-map-embed.md F1/F2/F3). Rendered by App INSTEAD of the portal
 * shell for any /embed path — no NavBar, no auth wall, and critically NO login redirect: an
 * iframe on a third-party page must never show a Talus sign-in form. The map itself rides the
 * anon public-map path (F0) — a private/unknown map 404s server-side and the viewer shows a
 * neutral message through the frame.
 */
export default function EmbedApp() {
  return (
    // SyncProvider: AssetMap consumes useSync (T35). Embeds are view-only, but the provider is
    // the component contract — same wrap PortalShell gives it.
    <SyncProvider>
      <div className="embed-shell">
        <Routes>
          <Route path="/embed/map/:webMapId" element={<AssetMap embed />} />
          <Route
            path="*"
            element={<p className="embed-shell__msg">This map isn’t available.</p>}
          />
        </Routes>
      </div>
    </SyncProvider>
  )
}
