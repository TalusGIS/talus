import { useState } from 'react'
import { ArrowRight, Clock } from 'lucide-react'

import Caret from '@/components/Caret'
import { LICENSES_URL, SOURCE_URL } from '@/lib/release'
import HeroMotif from './HeroMotif'
import TalusLockup from './TalusLockup'

export type SignInState = 'loading' | 'signin' | 'expired' | 'error'

/**
 * The front door — every unauthenticated state the top-level gate can land on, in one shell
 * (handoff-auth-opendata-4 §2.1-2.2). It replaces the four ad-hoc `.placeholder` bodies in
 * App.tsx, which put "Talus GIS" above "Talus GIS Portal" and showed a raw OIDC exception
 * string as the only thing on screen.
 *
 * PRESENTATION ONLY. It decides nothing: App.tsx still owns every auth branch, and this
 * component just renders the state it is handed (§2.0 — if a change here would alter which
 * branch a given state lands in, it is out of scope).
 *
 * NO `publicFaceEnabled` PROP, and no "browse open data" footer row (§6.2). The handoff asked
 * for one gated on `config.publicAnonAccessEnabled`, but App.tsx returns PortalShell for the
 * public-face branch BEFORE it reaches this screen — so the row could never paint. On a
 * public-face deployment `signin` and `expired` never render at all; anon visitors get the
 * portal. Only `loading` and `error` render on every deployment.
 *
 * `inFlow` is the RequireAuth variant (§6.5): that guard renders INSIDE PortalShell, so a
 * full-viewport dark field there would paint over the portal chrome. In-flow drops the field,
 * the motif and the panel and leaves the lockup in the content column.
 */
export default function SignInScreen({
  state,
  errorDetail,
  onSignIn,
  inFlow = false,
}: {
  state: SignInState
  /** The raw OIDC exception string. Shown only in the `error` disclosure, never as the headline. */
  errorDetail?: string
  onSignIn: () => void
  inFlow?: boolean
}) {
  const [detailOpen, setDetailOpen] = useState(false)

  // `loading` is gated on two awaits (silent renew + /public/config). Lockup and nothing else:
  // a bare centred "Loading…" reads as a broken page on a slow link, and a spinner that arrives
  // and vanishes inside 200ms on a normal connection is worse than no spinner at all.
  const body =
    state === 'loading' ? null : (
      <>
        {state === 'expired' && (
          // Amber, never danger. A lapsed SSO session is not a fault and must not look like one —
          // the gate already separates it from real errors.
          <Clock className="signin__state-icon" size={18} aria-hidden="true" />
        )}
        <h1 className="signin__heading">
          {state === 'signin' && 'Sign in to Talus'}
          {state === 'expired' && 'Your session ended'}
          {state === 'error' && 'Couldn’t reach the sign-in service'}
        </h1>
        <p className="signin__sub">
          {state === 'signin' && 'You’ll continue to your organization’s identity provider.'}
          {state === 'expired' && 'Sign in again to pick up where you left off.'}
          {state === 'error' &&
            'The sign-in service didn’t respond. This is usually temporary — try again, and tell your administrator if it keeps happening.'}
        </p>
        <button type="button" className="signin__action" onClick={onSignIn}>
          {state === 'error' ? 'Try again' : 'Sign in'}
          <ArrowRight size={16} aria-hidden="true" />
        </button>
        {state === 'error' && errorDetail && (
          // The admin who can act on the OIDC string still gets it; the user who cannot is not
          // handed it as the entire content of the screen (which is what App.tsx used to do).
          <details
            className="signin__detail"
            onToggle={(e) => setDetailOpen((e.currentTarget as HTMLDetailsElement).open)}
          >
            <summary className="signin__detail-summary">
              <Caret open={detailOpen} size={13} />
              Technical detail
            </summary>
            <p className="signin__detail-body">{errorDetail}</p>
          </details>
        )}
        {/* Every recipient of the browser bundle can reach its licenses and the release's source —
            signed out included. Static files and an external link: nothing here decides anything. */}
        <p className="signin__legal">
          <a href={LICENSES_URL} target="_blank" rel="noopener noreferrer">
            Open-source licenses
          </a>
          {SOURCE_URL && (
            <>
              <span aria-hidden="true"> · </span>
              <a href={SOURCE_URL} target="_blank" rel="noopener noreferrer">
                Source code
              </a>
            </>
          )}
        </p>
      </>
    )

  if (inFlow) {
    return (
      <div className="signin signin--inflow">
        <div className="signin__panel">
          <TalusLockup />
          {body}
        </div>
      </div>
    )
  }

  return (
    <main className="signin">
      <HeroMotif />
      <div className="signin__scrim" />
      <div className="signin__panel">
        <TalusLockup />
        {body}
      </div>
    </main>
  )
}
