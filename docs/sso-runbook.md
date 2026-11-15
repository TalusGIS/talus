# Talus — SSO sign-in runbook (adding an enterprise identity provider)

**Audience:** the Talus administrator. **Scope:** wire an organization's enterprise IdP (Microsoft Entra ID,
Okta, Google Workspace, ADFS, Ping, any SAML/OIDC provider) into Talus so **SSO users** sign in through it,
while **local (non-SSO) users** keep their username/password.

**The model in one paragraph.** Keycloak is Talus's identity layer and owns the sign-in page. You add an IdP
**in the Keycloak admin console** (Talus deliberately does *not* rebuild that config UI in the portal — that's
Keycloak's job). The branded **`talus` login theme** then shows the local username/password form **plus one
button per enabled IdP** automatically. Crucially, **authenticating via the IdP does not by itself create a
Talus account** — an admin must pre-provision the user first. This is enforced two ways: you configure the IdP
to link only to a pre-existing account, and — as a hard backstop — Talus's API rejects any token whose user has
no Talus user record (`401 no_platform_user_row`). So a stranger in your tenant cannot get in.

> **Prerequisite — on a current install, nothing.** The realm ships
> `loginTheme: "talus"`, brute-force protection, and a password policy (`keycloak/realm-export.template.json`), the
> compose file mounts the theme, and the installer applies those settings to the running sign-in system on every
> install and upgrade. The rest of this box is how to apply them by hand, and what not to do.
>
> **⚠ Do NOT apply these by re-importing the realm or `--force-recreate`-ing Keycloak.** The compose runs
> `start --import-realm`, which imports only on *first boot* and **skips an already-existing realm** — so a
> recreate changes nothing; and a forced *overwrite* import **RESETS the realm and deletes any users not in the
> export** (`realm-export.template.json` is a seed, not a full mirror). Instead:
> 1. **Settings** (theme selection, brute-force, password policy): run **`scripts/apply-realm-settings.sh`** — it
>    PATCHes only those realm fields via the Keycloak Admin REST API (users/clients are untouched; it asserts the
>    user count is unchanged). Back up Keycloak's DB first for a checkpoint
>    (`docker exec talusgis-keycloak-postgres pg_dump -Fc -U keycloak -d keycloak > kc.dump`).
> 2. **Theme files** (only if the running keycloak container predates the theme volume mount): run
>    `docker compose up -d keycloak`. Safe — the realm + settings persist in the Keycloak DB across a recreate
>    (`--import-realm` skips the existing realm), so no users are lost.
>
> Password policy note: it applies to password *changes*, not existing logins — existing users keep
> working.
>
> **Session lifetimes.** The realm template sets the idle timeout to four hours (`ssoSessionIdleTimeout` 14400), so a
> field crew is not signed out while working; the maximum session is ten hours (`ssoSessionMaxLifespan` 36000) and
> access tokens last five minutes (`accessTokenLifespan` 300). `apply-realm-settings.sh` carries both session fields,
> so an existing realm picks them up on its next run.

---

## Step 1 — Add the IdP in the Keycloak console

Sign in to the Keycloak admin console → realm **talus** → **Identity providers** → **Add provider**.

### Option A — SAML v2.0 (Entra ID worked example)

1. Choose **SAML v2.0**. Set **Alias** = `entra` (this becomes the URL slug) and **Display name** =
   `Sign in with Microsoft` (this is the button label on the sign-in page).
2. **Import from metadata** (easiest): paste the Entra **App Federation Metadata URL** —
   `https://login.microsoftonline.com/<tenant-id>/federationmetadata/2007-06/federationmetadata.xml` — into
   *Import config from URL* and **Import**. This fills the SSO URLs + signing certificate for you.
   *(If your environment can't reach Entra to import, enter the **Single Sign-On Service URL** and
   **Validating X509 certificate** by hand.)*
3. On the Microsoft side, register Talus as an Enterprise Application and set the **Reply URL (ACS)** /
   **Identifier (Entity ID)** to Keycloak's SP endpoints — get them from Keycloak's *Endpoints* /
   *SAML 2.0 Service Provider Metadata* link on this IdP page.

### Option B — OpenID Connect (Entra ID)

1. Choose **OpenID Connect v1.0**. **Alias** = `entra`, **Display name** = `Sign in with Microsoft`.
2. Paste the **OIDC discovery URL** —
   `https://login.microsoftonline.com/<tenant-id>/v2.0/.well-known/openid-configuration` — into *Import from
   URL*; it fills the authorization/token/JWKS endpoints.
3. Enter the **Client ID** + **Client secret** from your Entra app registration; set the redirect URI Entra
   needs to Keycloak's IdP endpoint (shown on the page).

*(Okta / Google / ADFS / Ping are the same shape — SAML metadata or OIDC discovery + a display name. No
Talus code changes; it's all IdP config.)*

---

## Step 2 — Make it admin-provisioned (link by trusted email, never auto-create)

This is the step that implements "Upon invitation from an administrator."

1. On the IdP → **Settings**, set **Trust Email = On** (Talus pre-creates users with `emailVerified=true`, so a
   matching verified email is a safe link key).
2. Configure the first-login flow for **auto-link-by-trusted-email + reject-unmatched**.
   **Authentication → Flows → first broker login → Duplicate** (name it `talus first broker login`), then
   in the copy:
   - **Review Profile** → **Disabled** (no profile prompt on first sign-in).
   - **Create User If Unique** → **Disabled** — an identity with *no* matching Talus account is
     **rejected**, never silently auto-created.
   - Under the **Handle Existing Account** sub-flow: set **Confirm link existing account** → **Disabled**
     and the **Account verification options** sub-flow → **Disabled**, then **+ Add step** twice to
     *Handle Existing Account*: **Detect Existing Broker User** (Required) and **Automatically Set
     Existing User** (Required).
3. Back on the IdP → **Settings → First login flow**, choose `talus first broker login`.

   > **Why the extra steps (not just "disable Create User If Unique"):** the *stock* flow, on an email
   > match, falls through to *"verify existing account by re-authentication,"* which asks for the
   > existing account's password. A **federated user has no local password**, so that path is a dead end
   > ("Authenticate to link your account…"). **Detect Existing Broker User → Automatically Set Existing
   > User**, with the IdP's **Trust Email** on, links by the verified email with no challenge. (Verified
   > against Keycloak 26.)

   Because an email is the link key, Talus makes email an administrator-only field: a person cannot change
   their own in the account console. Link by email only with an identity provider that verifies the addresses
   it asserts; if a provider sends no email, Keycloak falls back to matching the username.

> **Defense in depth:** even if you skip step 2, an un-provisioned SSO sign-in cannot reach Talus data —
> Keycloak would mint a new user that has no Talus user record, and the API returns
> `401 no_platform_user_row`. Step 2 just turns that into a clean "access denied" instead of an orphan
> Keycloak account.

---

## Step 3 — Button label & order

- The **Display name** (Step 1) is the button text.
- Set **Display order** (`guiOrder`) on each IdP to control top-to-bottom order on the sign-in page. Lower number = higher on the page.
- Toggle an IdP **Enabled** off to hide its button without deleting config.

---

## Step 4 — Pre-provision the SSO user in Talus

In the Talus portal: **Admin → Users → New user**, and tick **SSO user** (the API field
`federated: true` on `POST /admin/users`). This:

- creates the Keycloak user **with no local password** (SSO-only — they can't log in locally),
- creates the Talus user record, the role, and the organization and group memberships — exactly like a
  local user, so sharing/audit/role all behave identically,
- returns **no temp password** (there's nothing to hand out; they sign in via the IdP).

> Match the **email** to what the IdP asserts — that's the link key. (For an IdP that doesn't assert a trusted
> verified email, leave Trust Email off and link manually on first login instead.)

---

## Step 5 — Test the round trip

1. Open the Talus sign-in page → confirm the **Sign in with Microsoft** button renders (branded) alongside the
   local form.
2. Click it → authenticate at Microsoft → you land back in Talus **authenticated**, linked to the
   pre-provisioned account.
3. **Negative test (the important one):** sign in as a tenant user you did **not** pre-provision → you must be
   **denied** (no Talus account appears). Confirm `401 no_platform_user_row` in the API logs, not a silent
   default account.
4. **Offboarding:** disable that user (Admin → Users) → confirm they can no longer get in via the IdP.

`scripts/verify-invariants/inv01-users-row-gate.sh` proves the provision gate on your own install: a valid
token for an identity with no Talus user record is refused.

---

## MFA (TOTP two-factor) — enable, enforce for admins, recovery codes

MFA lives in **Keycloak, not Talus**, and it is a **second factor that composes with either sign-in method** —
you get to mix per customer:

| | No MFA | + MFA (TOTP) |
|---|---|---|
| **Local account (non-SSO)** | works today | Keycloak prompts for a 6-digit TOTP code **after** the password |
| **SSO (IdP)** | works today | **MFA is enforced at the IdP** (Entra / Okta run their own); Keycloak *can* add a second OTP after broker-login, but that double-prompts, so normally let the IdP own it |

> **Air-gapped note:** local account **+ TOTP is the strongest-auth path for an air-gapped deployment** — no
> corporate IdP required, and **TOTP works fully offline** (RFC 6238 time-based; the authenticator app needs
> no network). This is how a disconnected deployment gets real two-factor.

The realm ships an explicit **OTP policy** (`keycloak/realm-export.template.json` → `otpPolicy*`: TOTP, SHA1, 6 digits,
30 s, codes non-reusable — Keycloak's secure defaults, made explicit and tunable). MFA is therefore **capable
out of the box but forced on no one** — you scope enforcement per role or per user below.

> **⚠ Never force OTP realm-wide.** Making *Configure OTP* a realm **default** required action forces **every**
> account to enroll on next login, including accounts that a script signs in with a password. Scope it to
> real users/roles as below.

### Enforce TOTP for the `talus-admin` role (admins-first — the highest-value posture)

Prefer a **role-conditional step in the *browser* flow** over a per-user required action. It covers every
administrator, present and future, in one place, and it leaves no pending action on an account: until an
administrator has enrolled, a script can still sign in as them with a password (see the caveat below).

1. **Authentication → Flows → `browser` → Duplicate** → name it `talus browser`.
2. In `talus browser`, open the **Browser - Conditional 2FA** sub-flow (nested under the *forms* sub-flow). Out
   of the box it holds **Condition - user configured** (Required), **Condition - credential** (Required),
   **OTP Form** (Alternative), and two steps that are Disabled (**WebAuthn Authenticator** and **Recovery
   Authentication Code Form**) — i.e. a code is asked for *only if the user already set one up*, which never
   **forces** enrollment.
3. **Delete "Condition - user configured"** in that sub-flow, and **+ Add condition → Condition - user
   role**, **User role = `talus-admin`**, requirement **Required**. Leave **Condition - credential** as it is.
   (Conditions in a conditional sub-flow are AND-ed, so a leftover *user-configured* condition would let an
   admin who has no authenticator skip the sub-flow entirely — the exact enrollment you're trying to force.)
4. Set **OTP Form** to **Required**. Now any `talus-admin` with no authenticator is sent through
   **Mobile Authenticator Setup** at their next sign-in and asked for the code every time after; a role-holder
   who already has one is asked directly.
5. **Action → Bind flow → Browser flow** to make `talus browser` the realm's browser flow.

Non-admins are unaffected (the role condition fails → the sub-flow is skipped). *(Verified on Keycloak 26.8.2,
the version this release ships, on a newly created realm: an administrator with no authenticator was sent to
set-up and then asked for a code at each sign-in; a viewer was never asked.)*

*(Lighter alternative — per **individual** user, not role: **Users → (user) → Required actions →
Configure OTP**. Do **not** use this on any account that also authenticates via direct-grant/CLI — a pending
required action makes the password-grant token request fail with "Account is not fully set up." A script should
not sign in as a person at all: give it a service account (see the caveat below).)*

### Recovery path for a lost authenticator

**Out of the box (no extra config) — admin resets the OTP:** when an admin loses their authenticator,
another admin removes the stale factor — **Users → (locked-out user) → Credentials → delete the OTP
credential** (the `reset-otp` action). The role-conditional flow then sends them back through **Configure OTP**
at their next sign-in to re-enroll. This needs no feature flag and is the right fit for an air-gapped site (no
email/SMS dependency). Document *who* on your team can perform the reset.

**Self-service recovery codes (present, not switched on):** Keycloak can also issue a user a printable set of
one-time backup codes. In the Keycloak this release ships the feature is part of the server, and a new realm
has its sign-in step, **Recovery Authentication Code Form**, in the same sub-flow, set to Disabled. Talus has
not tested sign-in with recovery codes alongside the recipe above; the admin-reset path is the supported
default.

### Caveat — what a second factor does to a script's sign-in

**Service accounts are unaffected.** The client-credentials flow (`api-guide.md`) has no user in it and is
never asked for a code, so machine-to-machine API access keeps working.

**A script that signs in as a person with their password is affected once that person enrols.** The recipe
above changes only the sign-in page, but Keycloak's own password-grant flow asks for a code from any account
that has an authenticator. An administrator who has not enrolled yet can still be signed in by a script with
the password alone; after they enrol, the same request is refused ("Invalid user credentials") unless it sends
the current code too. So do not run scripts as a person's account: give the script a service account.

The verification suite is not affected: it signs in temporary accounts of its own with a password, an
administrator among them, and none of them has an authenticator. Run `scripts/verify-invariants/run.sh` after
any flow change; it fails if one of them can no longer get a token.

---

## Email verification (SMTP-dependent — default off, how to enable)

`verifyEmail` ships **off** (`keycloak/realm-export.template.json`) — deliberately, because verification requires the
realm to be able to **send email**, which is a **deployment variable** (an air-gapped site may have no SMTP at
all). Enabling it without SMTP configured would strand users at an un-sendable verification step.

- **For SSO users it is moot** — the IdP already asserts a verified email, and Talus links by that trusted
  email (Step 2); Talus pre-creates federated users with `emailVerified=true`.
- **For local accounts**, to require a verified email before first use: configure **Realm settings → Email**
  (SMTP host/from), then **Realm settings → Login → Verify email = On**. Keycloak then emails a verification
  link and assigns the *Verify Email* required action on new/changed addresses.
- Keep it **off** on any realm without working SMTP. It ships off.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| SSO button doesn't appear | IdP **Enabled** off, or the realm isn't using the `talus` theme. Check Realm settings → Themes → Login theme = `talus`, and that `./keycloak/themes/talus` is mounted. |
| Lands at Microsoft but bounces back to login | SP metadata (ACS/Entity ID) on the IdP side doesn't match Keycloak's endpoints (Step 1.3). |
| SSO user authenticates but Talus shows access denied / 401 | No Talus user record for them — pre-provision in Step 4, and check the **email matches** the IdP assertion. This is the gate working as designed. |
| New Keycloak users appearing without provisioning | First-login flow is auto-creating — apply Step 2 (Create-User-If-Unique → Disabled). |
| SSO user stuck at "Authenticate to link your account" (prompted for a password they don't have) | The first-login flow isn't auto-linking a passwordless federated user. Apply Step 2's **Detect Existing Broker User + Automatically Set Existing User**, and confirm the IdP's **Trust Email** is on and the user's email matches. |
| Login page looks unstyled after a Keycloak upgrade | The base theme's `styles` list changed; update `keycloak/themes/talus/login/theme.properties`. |
