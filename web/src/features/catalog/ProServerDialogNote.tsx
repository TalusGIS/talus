import type { ReactNode } from 'react'

/**
 * The ArcGIS-Pro "New ArcGIS Server Connection" steer-away notice
 * (decision-pro-server-connection.md §5).
 *
 * Pro's server dialog validates a site by Esri's proprietary Admin API — it probes
 * `admin/generateToken` + `POST <site>/services` before ever consulting `/rest/info` — so against
 * Talus it fails with "We were unable to connect to: …" even when the token is valid (wire-verified
 * 2026-07-26; not auth — the same credentials go 401→200 on /rest/services mid-attempt). We can't
 * explain ourselves from the server side (Pro ignores HTTP error bodies on this path), so every
 * surface that hands a user Pro credentials or a Pro connection file carries this notice instead.
 *
 * One core sentence everywhere; `route` is the surface-specific "use this instead" clause.
 * Renders the Profile page's connect-note idiom (global.css) — icon + muted text, deliberately
 * NOT a warning banner: QGIS users share these surfaces and this caveat is Pro-only.
 */
export function ProServerDialogNote({ route }: { route: ReactNode }) {
  return (
    <p className="connect-note">
      <span className="connect-note__icon">
        <svg
          width="15"
          height="15"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden
        >
          <circle cx="12" cy="12" r="10" />
          <line x1="12" y1="16" x2="12" y2="12" />
          <line x1="12" y1="8" x2="12.01" y2="8" />
        </svg>
      </span>
      <span>
        <strong>ArcGIS Pro:</strong> skip Pro’s <em>New ArcGIS Server Connection</em> dialog — it
        requires Esri’s proprietary server-admin API, so against Talus it fails with “unable to
        connect” even when your token is valid. {route}
      </span>
    </p>
  )
}
