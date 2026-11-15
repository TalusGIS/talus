import { useAuth } from 'react-oidc-context'

import { primaryRoleLabel } from '@/lib/auth/roles'
import { userDisplayName } from '@/lib/auth/user'

import AccessTokensSection from './AccessTokens'
import ChangePasswordSection from './ChangePassword'
import ConnectComputerCard from './ConnectComputer'

/**
 * /profile — the signed-in user's own page: an Account section (change your password,
 * decision-first-ten-minutes.md F3) above the Interop I-1 "Connect a desktop GIS" surface.
 * The connect spine below is unchanged. Refreshed (02-profile.md)
 * into ONE two-step spine (00-shared-conventions §1): an identity header, a framing line, then
 * Step 1 (create an access token — the token panel rejoins the step) and Step 2 (download a
 * connection file — a Vector/Imagery card matrix). The current step's number badge is brand blue;
 * the upcoming step's is neutral sunken. Every value is a DS alias, so dark mode reskins for free.
 */
export default function ProfileView() {
  const auth = useAuth()
  const token = auth.user?.access_token ?? ''
  const profile = auth.user?.profile
  const role = primaryRoleLabel(auth.user)

  return (
    <div className="page">
      <div className="page__head">
        <div>
          <h1 className="admin__title">{userDisplayName(auth.user)}</h1>
          <p className="admin__note">
            {profile?.email ?? ''}
            {role && (
              <>
                {profile?.email ? ' · ' : ''}
                {role}
              </>
            )}
          </p>
        </div>
      </div>

      {/* Account — identity concerns before the desktop-connect wizard. A customer who just
          installed Talus and was handed a generated credential comes here to change it
          (decision-first-ten-minutes.md F3), not to connect ArcGIS Pro. */}
      <div className="connect-framing" style={{ marginBottom: '1rem' }}>
        <div className="connect-framing__eyebrow">Your account</div>
      </div>
      <ChangePasswordSection accessToken={token} />

      <div className="connect-spine">
        <div className="connect-framing">
          <div className="connect-framing__eyebrow">Connect a desktop GIS</div>
          <p className="connect-framing__lede">
            Bring Talus layers into ArcGIS Pro or QGIS in two steps — once per computer. Create a
            token, download a connection file, and sign in with the token when the app asks.
          </p>
        </div>

        {/* STEP 1 — create an access token */}
        <div className="connect-step">
          <div className="connect-step__rail">
            <div className="connect-step__badge">1</div>
            <div className="connect-step__line" />
          </div>
          <div className="connect-step__body">
            <h2 className="connect-step__title">Create an access token</h2>
            <p className="connect-step__lede">
              A token is your <strong>app password</strong> for desktop GIS — sign in with username{' '}
              <span className="connect-chip">talus</span> and the token as the password. It sees the
              same layers you can; make one per computer and turn any off without affecting the
              others.
            </p>
            <AccessTokensSection accessToken={token} />
          </div>
        </div>

        {/* STEP 2 — download a connection file */}
        <div className="connect-step connect-step--last">
          <div className="connect-step__rail">
            <div className="connect-step__badge connect-step__badge--upcoming">2</div>
          </div>
          <div className="connect-step__body">
            <h2 className="connect-step__title">Download a connection file</h2>
            <p className="connect-step__lede">
              One credential-free file per deployment — no password, no layer list. Import it once;
              every layer you can view appears, including ones shared to you later. The same file
              works for the whole organization.
            </p>
            <ConnectComputerCard accessToken={token} />
          </div>
        </div>
      </div>
    </div>
  )
}
