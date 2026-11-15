# Talus GIS — Security Overview

**Audience:** government and enterprise security teams, procurement, and prospective customers evaluating
Talus GIS. This is the buyer-facing summary of how Talus is secured. It is written to answer the security
questionnaire most municipal and public-sector RFPs include, in plain terms, with the same discipline the
project holds itself to internally: **confident about the architecture, honest about what is not yet
certified.**

**Why that discipline matters:** overstating security to a public-sector buyer destroys trust the first time
someone asks for evidence. Trust is one of this product's core promises, so this document does not claim
controls Talus does not have. Where there is a gap, it is named here (see *What Talus does not have yet*).

*This document was last reviewed against the codebase for release 1.0.0, on 2026-11-14. Not every statement
was re-tested on that date. The platform evolves; a detailed, point-in-time control mapping is available to
evaluators on request (see the closing section).*

---

## How this applies across the two tiers

Talus ships in two tiers, and either can be deployed air-gapped. Both run the same platform: the sign-in,
authorization, audit and network posture described below is **the same code in each**. Talus+ adds modules on
top of it — dashboards, story maps, scheduled reports, the Talus Field app and the optional AI assists — and
support. Those modules use the platform's sign-in and authorization.

| Tier | Licensing | AI module | This document |
|---|---|---|---|
| **Talus Community** | Open source (AGPLv3) | Not installed at all — no AI provider, no outbound connectivity required | Fully covered here; this is the public, open-core baseline |
| **Talus+** | Commercial subscription | The AI assists (a documentation assistant any signed-in user can ask, and a diagnose mode for administrators that reads a snapshot the administrator previews before it is sent — the latter built, not yet field-tested — and, on the roadmap and not yet built, authoring helpers; informational, not actuating); **absent in an air-gapped Talus+**, which keeps every non-AI module | Platform posture identical; see *AI module data handling* |

**Air-gapped is a deployment mode, not a tier.** Once installed, either tier runs with no outbound internet
connectivity and no relationship with any AI provider — first-class, not a degraded mode. Installing and
upgrading are the exception: the installer pulls container images from the internet, so a site with no
connection at all has to bring those images in by another route. For the most regulated environments, running
air-gapped is the recommended posture. Wherever a statement below depends on the tier or on air-gapping, it says
so.

---

## One-paragraph posture

Talus is **secure by default.** Every request is authenticated with a modern credential (Keycloak / OIDC
tokens for people, hashed and revocable access tokens for desktop GIS clients — a password never signs a request
in to the API), authorized by a fine-grained, **fail-closed** permission model (you cannot accidentally leak a
layer), and every data change is recorded in an audit trail that the application's own database role is
structurally unable to alter or delete. The services that hold your data are not reachable from the
internet; the only internet-facing component in the reference production deployment is a TLS-terminating
reverse proxy. Your data stays on infrastructure you control, and the few connections a deployment can make
to the internet are listed below, each one yours to switch off. In short: Talus removes the "someone forgot to lock it down" foot-guns that plague
self-assembled GIS stacks.

---

## The security questionnaire → Talus's answer

### Authentication

- **The API never signs a request in with a password.** People sign in through **Keycloak** and the browser carries an
  OIDC/OAuth2 bearer token (a JWT) tied to a provisioned account. Desktop GIS clients (QGIS, ArcGIS Pro) that
  can only speak HTTP Basic present a **personal access token** as the credential — never the account
  password; the server's sign-in challenge says exactly that. Tokens are stored as a SHA-256 hash (the
  plaintext is shown once and never kept), are revocable by their owner — and die with the account the moment an
  administrator disables it — and **never carry administrator rights**, whatever their owner's role — a leaked token cannot become an admin.
- **Sign-ins are recorded, and sessions end.** The sign-in server records sign-ins, failed sign-ins and
  administrative changes, and keeps them for 30 days (its admin console, **Events**). Repeated failures lock an
  account for a short time, never for good, on both the portal's realm and the sign-in server's own. No
  long-lived offline token is offered to any Talus client.
- **Anonymous access exists only where an administrator has explicitly published an item.** Everything else
  requires a credential, and an anonymous request for an unpublished item receives a `404`, the same status as
  a request for something that does not exist. The short message inside the response differs between the two
  cases on some endpoints (layer queries and metadata export among them), so a caller who compares messages
  can tell an unpublished item from a missing one.
- **Standards-based SSO, mixed with local accounts.** Keycloak supports OIDC/OAuth2 and SAML. The sign-in
  runbook walks through Microsoft Entra ID step by step; Okta, Google Workspace, ADFS and any other SAML or OIDC
  provider connect the same way. A deployment can run **SSO and non-SSO users side by side** on one branded
  sign-in page (a local form plus a button per configured provider). SSO authenticates identity, but **a Talus
  account is admin-provisioned, not auto-created** — an authenticated-but-unprovisioned identity is denied.
  Identity is owned by Keycloak; Talus maintains no parallel password store.
- **Multi-factor authentication (MFA) is available out of the box, and enforcement is an operator choice.**
  The shipped realm carries a TOTP (authenticator-app) policy, so any user can enrol a second factor from day
  one. **Enforcement is not switched on by default**: the operator enables it per role or per user with
  Keycloak's standard conditional-OTP flow (the documented recipe enables it for administrators first, with a
  recovery path for a lost device). It composes with both local accounts (a time-based code after the
  password) and SSO (where MFA is normally enforced at the identity provider). For an **air-gapped deployment
  with no external identity provider, local account + TOTP delivers real two-factor entirely offline** — the
  time-based algorithm needs no network.
- **The API accepts no credential in a query string.** A browser session and a desktop client send their
  credential in the `Authorization` header, so tokens do not land in server access logs, browser history, or
  referrer headers. There is one deliberate exception outside the API itself: the Import URL that Talus builds
  for a QField field project, the QR code of that address, and a generated field project file carry an access
  token inside the address (`https://talus:<token>@…`), because that is the form QField imports. The field app
  sends it as an `Authorization` header. The address, a printed QR code and the project file are credentials
  and should be handled as such; revoking the token ends every copy at once.
- **Rate limiting is built in.** Anonymous traffic is limited per source address (separately for tiles,
  downloads, and reads), which bounds credential probing and bulk scraping of the public surface; desktop
  access-token traffic is limited per token, so rotating source addresses buys an attacker nothing. A request
  that presents a sign-in token which does not validate is limited per source address as well; only a
  signed-in session whose token the sign-in server issued, and which is still valid, is not limited by the
  API. This is the turnkey bound for a single-host install; an internet-facing deployment should still
  rate-limit at the reverse proxy, as with any web application.

### The identity server's bootstrap administrator (read this one)

An evaluator who signs in to the Keycloak administration console will see Keycloak's own banner: *"You are
logged in as a temporary admin user. To harden security, create a permanent admin account and delete the
temporary one."* This section says what that account is, why it exists, and what to do about it — so the
banner is not a surprise.

- **What it is.** Keycloak (the identity server Talus ships) requires one initial administrator in its own
  `master` realm. The installer supplies it from two values in the deployment's `.env` file
  (`KEYCLOAK_ADMIN_USER`, default `admin`, and `KEYCLOAK_ADMIN_PASSWORD`, a 192-bit random value the installer
  generates — there is no shipped default password). Keycloak 26 creates that account on its first start and
  flags it *temporary*, which is what the banner reports. This is the identity-server superuser: it can reset
  any user's password, remove a second factor, and register clients. It is a different account from the Talus
  portal administrator, which lives in the `talus` realm.
- **Why it is still there.** Talus's own installer and upgrade scripts authenticate as this account, from the
  `.env` values, to configure the `talus` realm — creating the first portal administrator, registering the
  portal's sign-in client, and re-pointing that client whenever the portal's address changes. The installer
  does not yet replace the account with a permanent one; that step is the operator's.
- **What to do about it.** Nothing is required. The account is protected by a 192-bit random password that is
  kept in the deployment's `.env` file, which is created with owner-only permissions (`0600`); no default
  password ships. One rule: **do not enrol a second factor on this account**, or on whichever account
  `KEYCLOAK_ADMIN_USER` names. The installer's realm-maintenance steps sign in as it with the user name and
  password alone, on every upgrade and every change of the portal's address. With a second factor on the account
  those sign-ins are refused and the upgrade stops at that step (loudly — it does not fail open). If you replace
  or delete the account, `KEYCLOAK_ADMIN_USER` and `KEYCLOAK_ADMIN_PASSWORD` in `.env` must name an
  administrator that exists, or the next upgrade stops at the same step. If the credentials are ever lost,
  Keycloak's `kc.sh bootstrap-admin` command recreates a temporary administrator from a shell on the host — host
  access, not the network, is the recovery path.
- **Where the console is reachable.** In the reference production deployment the console is served under the
  sign-in address (`https://<sign-in-address>/admin`), so by default it is reachable from wherever the
  sign-in page is, and the generated password is the control. One optional setting narrows that. Set
  `KEYCLOAK_ADMIN_ALLOW_FROM` in `.env` to a space-separated list of addresses, in double quotes (for example
  `KEYCLOAK_ADMIN_ALLOW_FROM="203.0.113.7 198.51.100.0/24"`), then run
  `docker compose up -d caddy`: the reverse proxy then answers the sign-in server's admin console, its admin
  API and its `master` realm only to the listed addresses, and answers `404` to everyone else. The setting is
  off by default. List exact public IPv4 addresses, and do not add private address ranges on an
  internet-facing server: a caller from outside can reach the proxy from one of Docker's own private
  addresses (one arriving over IPv6, for example) and would be let in. Sign-in, tokens and single sign-on
  are not affected, and neither are Talus's own scripts, which reach the sign-in server on the host itself
  and not through the proxy. Two things to plan for: the console is where an administrator connects an
  identity provider, so list the address that work will be done from; and behind another reverse proxy
  every caller has that proxy's address, so restrict these paths there instead.

### Authorization (who can see and do what)

- **Fine-grained, item-level authorization** via **OpenFGA**, an open-source implementation of Google's
  Zanzibar relationship-based access-control model. Access is granted per item (a layer, a map) at the tiers
  *owner / shared-with-group / organization / public*.
- **Fail-closed by design.** If a permission cannot be confirmed — including if the authorization service is
  unavailable — the request is **denied**, never allowed. No request path on an installed system "fails open." A
  signed-in caller without access receives `403`; a signed-out caller receives `404`. When the authorization
  service cannot be reached, an access decision answers `503`, a listing shows fewer items, and some public
  addresses answer `404`; none of them serves the data. An administrator's read access comes from the
  administrator role and does not depend on that service. The server refuses to start if it cannot reach the
  authorization service. Refusal is covered by a standing suite of negative tests run against a running system;
  the outage case is a separate test in which the authorization service is stopped by hand.
- **You only see what you're entitled to.** Listing endpoints return only the items a user is permitted to
  view; they do not enumerate everything and filter in the browser.
- **Sanitized sharing without a second access model.** When a subset of a sensitive layer needs to be shared
  (e.g. a public-safe view of an internal dataset), the GIS team authors a filtered, field-limited **view** and
  shares that view with its own permissions — the original stays private.

### Audit and accountability

- **Every change to a record is audited, whatever door it came through.** Database triggers on every layer and
  table record each create, update and delete of a feature or row, of a feature's attachment, and of the layer's
  own registry entry, with the account that made it and a timestamp. The web editor, desktop clients, the field
  app, file imports, batch geocoding and database pulls all write through those triggers, and none can skip
  them. Three things are outside that trail. Sharing changes on layers, role and ownership changes and similar
  administrative actions are written to a separate activity log by the application, not by a trigger; the role
  that serves requests can add to that log and cannot change or delete what is in it. Changes to other items —
  web maps and how they are shared, imagery, field definitions, value lists — are recorded in the server log
  only. And deleting a layer deletes its change history with it; the deletion itself is recorded.
- **Actor attribution distinguishes people from automated integrations.** Each audited change records whether it
  came from a person or a service account (an API integration), and which one, so an external application
  writing data is attributable to *which* integration. The record also has a value for an AI inference; nothing
  in this release writes it. Reads are not part of this trail (see *What Talus does not have yet*).
- **A place for AI provenance is built into every record.** Every feature row and every audit entry has a field
  for AI provenance, written by the same triggers as the rest of the trail. No AI feature in this release writes
  or proposes data — the assistant only answers questions — so those fields are empty today.
- **The audit trail is structurally protected, not merely policed.** The database role that serves requests
  is not a superuser and holds no permission to delete or alter audit records or to disable the audit
  triggers; a test in continuous integration proves that on a real database on every change. Housekeeping and
  retention jobs are fenced to ephemeral cruft — they never touch authoritative data or the audit log. The
  same service also holds a separate owner connection, used for schema changes it makes at run time (view
  layers, imports) and for backups; that connection can do anything in the database, which is one reason the
  service's host and its administrators are trusted. (The audit trail is protected from the role that serves
  requests; the owner connection, and a database superuser on the host, can still alter it, which is why host
  access is an operator control.)

### Network exposure and service isolation

- **The services that hold or serve data are not internet-reachable.** The vector-tile service and the
  authorization store (OpenFGA) have **no host-published ports**, and media/raster/backup bytes are plain
  files on a Docker volume only the API mounts — there is no object-store server at all, and the API is
  the only door to the bytes.
- **In the reference production deployment, the only internet-facing component is the TLS reverse proxy.** It
  terminates TLS and forwards to the API and to Keycloak behind it; the database is not exposed.
- **Developer conveniences bind to loopback.** The database, API, and Keycloak ports that a bare
  `docker compose up` publishes for local convenience bind to `127.0.0.1` by default, not to every
  interface — and a standing check in the product's verification suite fails if any service other than the
  TLS edge ever publishes on a public interface.
- **The API runs as a non-root user inside its container.**

### Encryption in transit, TLS, and HTTP hardening

- **TLS-secure out of the box.** The shipped production reference (a hardened Caddy reverse proxy) terminates
  TLS 1.2+ (negotiates TLS 1.3). The installer asks one question about certificates. By default the server
  issues them from its own built-in private CA — still TLS everywhere, with the CA distributed to clients by
  the operator — which is the right choice for a LAN or air-gapped deployment with no inbound internet. Answer
  yes to a public certificate (the server must be reachable from the internet on ports 80 and 443) and the
  proxy **obtains and renews** certificates automatically via ACME / Let's Encrypt. The secure path is the
  default documented bring-up, not an afterthought you have to assemble.
- **HTTP security headers are set**, including `X-Content-Type-Options: nosniff`, `X-Frame-Options` /
  `frame-ancestors` (clickjacking protection), a `Referrer-Policy`, an **enforced**
  Content-Security-Policy (the browser blocks violations — an injected script cannot run), and
  HTTP Strict Transport Security (HSTS) at the TLS edge.
- **Recon surface reduced.** The interactive API documentation endpoints are disabled outside development.

### Secrets management

- **No secret ships as a default on the production install path.** The installer generates every deployment
  secret, with OpenSSL's cryptographic random generator, at install time rather than
  baking them into an image — so there is no well-known default credential to forget to change. The resulting
  `.env` file is written with owner-only permissions.
- **Third-party credentials the platform stores are encrypted at rest.** Keys for external services an
  administrator configures (a commercial geocoder, for example) are held in the database as ciphertext under a
  per-deployment encryption key kept in `.env`, so a database dump or a query log never contains them in the
  clear.

### Software supply chain (can you trust what you install?)

A legitimate open-source concern is not only "is the application secure once running" but **"can I trust
installing it at all — could the code or one of its dependencies carry something harmful onto my
infrastructure?"** Talus answers this by being **inspectable, boring to install, and scanned on every
change**:

- **The install is the audit surface.** Talus is installed by a script you can read in full before running
  anything: `scripts/talus-setup.sh` asks its questions and then runs `install.sh`, which generates the
  secrets, starts the services named in `docker-compose.yml`, applies the database migrations, loads the
  authorization model and creates the first administrator account. The scripts and the compose file are plain
  text in the release. There is **no `curl … | bash` one-liner**, and the only thing the installer fetches from
  the internet is the container images the compose file names — the class of place supply-chain attacks most
  often hide is in front of you. What runs is what is in those files.
- **Every change is scanned before it merges.** Continuous-integration security gates run on every pull
  request and on the main branch: **Semgrep** (static analysis for insecure code patterns), **gitleaks**
  (secret scanning), and **Trivy** (vulnerability scanning of dependencies, checks of the container and
  compose configuration files, and secret scanning), configured to fail the build on HIGH/CRITICAL findings
  for which a fix is available. An accepted vulnerability finding must carry a written justification **and an
  expiry date** — after which it fails the build again — so an exception cannot quietly outlive its
  reasoning. A database-level invariant test also runs in CI to prove the least-privilege runtime role cannot
  tamper with the audit log.
- **Dependencies are kept current, not frozen-and-forgotten.** Automated dependency updates (Dependabot)
  track Python, JavaScript, the API image's base image, and CI actions weekly, opening patch pull requests
  that the same scanners then gate. This runs in the private repository where Talus is developed, so no
  Dependabot configuration is published here; the updates arrive in this repository with each release. The
  third-party service images in the compose file are outside that automation and are moved to newer releases
  by hand. The third-party CI action the published workflows use is **pinned by commit hash**, not by a
  mutable tag, so a re-tagged action cannot silently change what runs. The three scanners are not actions.
  In the security checks they run from their publishers' container images at the `latest` tag — deliberately,
  so each run uses current rules and vulnerability data — and that is a mutable reference. The release
  workflow, which publishes each Release and its SBOMs, runs its scanner pinned by content digest.
- **What you install is pinned and inventoried.** Every container image in the shipped compose file — the
  Talus API image and each third-party service — is referenced **by content digest**, not by a mutable tag,
  and each release publishes a **Software Bill of Materials** (CycloneDX) for the image and the repository.
- **Open source is the verification mechanism, not a leap of faith.** Talus is AGPLv3 open core: you — or
  a third party you trust — can read, diff, and rebuild it, and copyleft guarantees the core *stays* open
  (no downstream vendor can take it closed). This is a control the closed alternative it replaces structurally
  cannot offer: with a proprietary product you are trusting a vendor's *assertion*; here you can verify the
  *artifact*.
- **No untrusted-contributor code path.** Talus is **solo-maintained by design and accepts no outside code
  contributions** (the SQLite model — pull requests are closed automatically); every change lands through a
  written design decision and a per-change security gate. The most common real-world open-source supply-chain
  compromise — a malicious or hijacked *contributor* getting code into the project — has no ingress here:
  there is a single accountable copyright holder, not an open commit bit. (Bug reports and private security
  disclosures are welcome; see `CONTRIBUTING.md` and *Reporting a vulnerability*.)
- **Nothing phones home — and the outbound connections that do exist are yours to see.** Talus contains no
  telemetry, no usage reporting, no update checker, and no license server; the Talus+ license is a signed
  file verified offline. The connections a deployment *can* make to the internet are listed here. Two are
  switched on at install (the address search and streamed map backgrounds); every one can be switched off or
  left unconfigured, and each is visible at the firewall:
  - certificate issuance and renewal (ACME) — only when a public certificate was chosen at install; none with
    the built-in certificate authority, which is the default;
  - the address search: Talus ships with the public OpenStreetMap geocoder pre-configured for convenience,
    which means a user's address lookup is sent to that public service; an administrator can remove or replace
    it in one screen, and an air-gapped deployment should;
  - map backgrounds: a portal lists streamed maps, downloaded maps, or both (**Admin → Basemap**). A *streamed*
    map (OpenStreetMap in three looks, and USGS Topo and Imagery from the U.S. Geological Survey) is not stored
    on the server and is not relayed by it: each viewer's browser fetches it directly from its publisher, which
    sees the viewer's internet address, the area being viewed, and the portal's address. That includes
    signed-out visitors to public and embedded maps. A new install lists streamed maps and opens new maps on
    OpenStreetMap; an install upgraded from an earlier release keeps what it listed before. The *downloaded*
    maps (Light, Dark and Gray) are drawn from a basemap file stored on the server, fonts and symbols included,
    nothing fetched; choosing **Downloaded** sends no viewer's browser to the internet for a map background.
    Fetching that file is a download from the public Protomaps builds, started by an administrator; the server
    contacts `build.protomaps.com`, and `build-metadata.protomaps.dev` to find the newest build. An air-gapped
    site replaces the download with an upload. In Talus+, the Talus Field app follows the same setting: a phone
    picks up a change the next time it reaches the server, and a phone that cannot reach it keeps the last
    setting it read for up to 7 days, then shows downloaded maps only;
  - field projects generated for QField draw an OpenStreetMap background, fetched by the phone from
    OpenStreetMap;
  - layers added from a web address: a map can carry a layer that lives on another organization's server (an
    ArcGIS service, a GeoJSON file, a tile address). Talus stores the address only; each viewer's browser
    fetches the layer from that server, which sees the viewer's internet address;
  - the home page picture, when an administrator sets one by web address: each visitor's browser fetches it
    from that address;
  - single sign-on, when an administrator connects an outside identity provider: sign-in then goes through
    that provider;
  - database pulls: a publisher or administrator can connect Talus to a SQL database your organization runs; the
    server then connects to the address they gave — on the schedule they set, and when they test the connection,
    browse its tables or run a pull by hand. Talus only reads: it sends the queries it builds itself and no text
    a person typed. On PostgreSQL, MySQL and MariaDB the session is also set read-only, so the database refuses
    a write. On SQL Server the connection only declares read-only intent, which a single server does not
    enforce, so give Talus a login that can only read;
  - an off-host backup target and any report-delivery destinations an administrator configures. A webhook
    address on a private range is refused unless an administrator has listed its host name and switched private
    addresses on; loopback and link-local addresses are refused always, and the address is checked again at
    every send. The mail relay for emailed reports is not held to that rule, because a relay is normally on the
    local network; it must use TLS and sign in;
  - **Talus+ with AI enabled only:** the AI module's calls (see *AI module data handling*).
  Switch off the address search and the streamed maps, configure none of the rest, and the server makes no
  outbound connection at all and sends no viewer's browser past itself — a property you can confirm at the
  network layer rather than take on trust.
- **Private vulnerability disclosure.** Suspected issues have a documented private reporting path (see
  *Reporting a vulnerability*, below); the repository does not use a public issue tracker.

**Honest — planned, not in hand today.** Release images are not yet **cryptographically signed with
build-provenance attestation** (e.g. SLSA / Sigstore). That is the artifact that lets a security team verify
*who built* a release, as distinct from *what is in it* (the SBOM and digest pins already answer the latter).
Until then, provenance verification is by reading and rebuilding the open source. See *What Talus does not
have yet*.

### Backups, retention, and disaster recovery

- **Complete backups; encryption and an off-host destination are the operator's to switch on.** Scheduled
  backups capture all three databases — application data, identity (Keycloak), and authorization (OpenFGA) —
  plus media objects, so a restore brings back logins and the sharing graph, not just the GIS data. A new
  install takes them daily, keeps them on the same host as the live data, and does not encrypt them; the next
  two points are what an administrator sets, in **Admin → Backups & recovery**, to change that.
- **Client-side encryption to keys the server does not hold.** With recipient keys configured, every backup
  artifact written from then on is encrypted (asymmetric `age` encryption) before it leaves the host. The
  running system holds only the public half and **cannot decrypt its own backups**; the private key is kept
  off-host and used only at restore time. Media files copied to the backup archive before keys were added are
  not re-encrypted. A portal administrator can include the server's settings file (`.env`) in an encrypted
  backup and chooses the keys backups are encrypted to, so a portal administrator can obtain the server's
  secrets.
- **Off-host targets and honest DR guidance.** Backups can target off-host S3-compatible storage; the
  documentation is explicit that same-host backups do not survive host loss, and that off-host storage is the
  real disaster-recovery posture.
- **Retention is floored.** Old backups are pruned by age but never below a configured minimum count, so a
  misconfiguration cannot prune backups down to zero.
- **Tested restore.** Disaster recovery includes a documented, repeatable restore drill that stands up an
  isolated copy of the stack beside the running one, restores a backup into it, and verifies logins, the sharing
  graph, data, and media all come back — and recovery of a single deleted layer, table or map from a backup
  without rolling the whole system back. That recovery is a script an operator runs on the server. It brings
  back a layer's or a table's rows, field definitions, settings and sharing, or a map with its sharing. It does
  not bring back a layer's attachments, its relates to other tables, or its change history. The drill runs
  on the live server; it does not rehearse a rebuild on a second machine.

### Data sovereignty and telemetry

- **Self-hosted; the customer holds their own data.** Talus runs on infrastructure the customer controls; data
  resides exactly where it is deployed. (Because it is self-hosted, data-residency questions are answered by the
  deployment location, not by a vendor's regional policy.)
- **There is no telemetry.** Not opt-out, not opt-in — Talus's own code contains none: no usage reporting, no
  update check, no license check over the network. Nothing leaves a deployment except through the connections
  listed under *Nothing phones home* above and, in Talus+ with AI enabled, what the AI module sends (next
  section). Each of those is started by someone in your organization and can be switched off or left unused.
  Most are an administrator's; a database connection or a layer from an outside address is a publisher's to add.

### AI module data handling (Talus+ with AI enabled)

- **The AI module is optional and physically separable.** Talus Community does not include it. A Talus+ server
  makes no AI connection until an administrator enters an AI credential. An air-gapped Talus+ sets
  `TALUS_AI_GATEWAY_URL=disabled`; with that setting the outbound client is never constructed and no socket
  opens.
- **Where AI is enabled, the data path is fixed and visible.** An AI request travels from your deployment to a
  gateway operated by Talus GIS LLC, and from there to Anthropic's commercial API under Talus's own account —
  your deployment never holds an AI-provider key. The gateway's address is pinned to a `talusgis.com` hostname
  over HTTPS. No setting in the portal and no input to the product can change it, and a production server
  refuses to start if its settings file names any other address.
- **What is sent, and what is kept.** The AI assistant answers from the product documentation and, when an
  administrator explicitly asks it to diagnose the system, from a diagnostic snapshot. The administrator chooses
  which sections go and can open each one to read it before sending. Passwords, keys and tokens are removed
  first; names of users, hosts and layers are kept, because a diagnosis needs them. The server assembles the
  snapshot again at the moment of sending, so a log line written in between can differ from what was shown. The
  gateway keeps **no content** — not the question, not the answer, not the snapshot; it records only which kind
  of diagnostic sections were sent, a hash, token counts, and a pseudonymous user reference (a keyed hash of the
  identity, not the identity). Anthropic processes the request under its commercial API terms, under which
  customer content is **not used to train models**; the retention that applies is set out in the data-processing
  terms of the applicable agreement rather than asserted here.
- **The AI is informational, not actuating.** In this release the only AI feature is the Talus Helper, an
  assistant that answers questions. It answers in text; the product does not act on an answer, and the assistant
  cannot change data or settings. Each call is recorded in your deployment with the model, the prompt and
  documentation versions, a request id and token counts — never the question or the answer.
- **The LLM provider is project-configured**, not customer-swappable — a deliberate quality and supply-chain
  control.

---

## Regulated data considerations (CJIS / HIPAA-adjacent)

*Draft — a starting point for the conversation, not a compliance attestation.* Some municipal deployments
touch data under regimes like the **FBI CJIS Security Policy** (law-enforcement / criminal-justice
information) or **HIPAA** (protected health information — e.g. EMS, public-health departments). Talus is **not
certified or authorized under any of these**, and using it does not by itself make a deployment compliant.
Compliance is a property of the **whole environment** — the host, network, operator procedures, and personnel
— not of the application alone. What Talus contributes, honestly framed:

**Where Talus's architecture helps**

- **Access control & least privilege** — Keycloak authentication (no password ever accepted by the API; MFA
  available, see below) and OpenFGA fail-closed, item-level authorization map onto the access-control
  requirements both regimes specify.
- **Audit of changes** — every change to a feature, a table row or an attachment is recorded with actor
  identity, timestamp, and source type (human / integration / AI), and the database role that serves requests
  cannot alter that record. Deleting a layer removes that layer's change history from the running system along
  with the layer; the deletion itself is recorded, and a backup taken before it still holds the history. This
  supports the *write*-side of audit/accountability requirements.
- **Encryption in transit** — TLS 1.2+ by default at the shipped edge.
- **Data sovereignty / no third-party processor** — self-hosted; in an **air-gapped deployment the data never
  leaves the customer's environment and there is no AI provider in the loop**. For regulated data this is the
  recommended posture, and it sidesteps the "is there a BAA / a CJIS-authorized cloud" question entirely.
- **Strong authentication / MFA** — a TOTP MFA policy ships in the realm by default; enforcing it per
  role/user is a documented Keycloak recipe the operator runs (administrators first), not a separate Talus
  feature to build. For regulated air-gapped data, local account + offline TOTP gives two-factor with no
  external dependency.

**Operator responsibilities (Talus ships hardened defaults; the environment around it is yours)**

- **Encryption at rest of the live system.** Talus encrypts stored third-party credentials, and encrypts
  *backups* client-side once an administrator has added recipient keys, but **at-rest encryption of the
  running database and file store is an infrastructure responsibility** (full-disk / volume encryption, or a
  managed encrypted store). CJIS and HIPAA both expect
  at-rest protection — provision it at the host/volume layer.
- **Physical, personnel, and network controls** — facility security, background-screening of personnel with
  access, network segmentation, and boundary protection are deployment-environment responsibilities the
  policies require and Talus cannot provide.
- **Incident response, contingency planning, and formal policy documentation** — organizational requirements
  the customer owns; Talus's DR tooling and audit trail are inputs to them, not substitutes.

**The honest gaps that matter most here**

- **Read-access ("who *viewed* this record") auditing is not yet first-class.** Both CJIS and HIPAA care about
  logging *access*, not just modification. Talus's write-audit is strong. A view leaves only a line in the API's
  access log: the request and the time, not who made it, and those logs are kept to a fixed size and then
  overwritten. There is no record today of who viewed a record. **This is the single most likely gap to surface
  in a CJIS/HIPAA review, and it is on the roadmap to strengthen.**
- **No BAA, no CJIS-authorized managed offering, no formal attestation** for any regime. If a deployment needs
  one of these, that is a procurement conversation, not a checkbox Talus can tick today.

**Bottom line:** for regulated data, an **air-gapped deployment on customer-controlled, encrypted infrastructure**
is the posture to start from. Talus provides the auth, authorization, and change-audit foundations; the
operator provides the environment controls; and the read-access-audit gap should be discussed openly with the
buyer's security team rather than papered over.

---

## What Talus does not have yet (named honestly)

Leading with the gaps is part of how this project earns trust. As of this writing:

- **No formal compliance certifications.** Talus has **no SOC 2 report, and no FedRAMP, StateRAMP, CJIS, or
  HIPAA authorization.** The architecture is built to support the underlying controls (auth, fail-closed authz,
  full audit, encryption, data sovereignty), but **strong architecture is not a certification**, and Talus does
  not claim one. Formal attestations are a maturity-and-business investment the project will pursue as customer
  demand justifies.
- **Read access ("who viewed this") is not audited.** Changes to records are audited with actor attribution (see
  *Audit and accountability*). A successful read is not recorded against an account: the server's request log
  shows the address requested and when, not who asked. Only a refused request is logged with the account. This
  is a known item to strengthen, and matters most for CJIS- and HIPAA-adjacent data.
- **No independent third-party penetration test or formal security audit has been published yet.** Security is
  enforced and regression-tested internally; an external assessment is future work.
- **Release images are not yet signed with build-provenance attestation** (SLSA / Sigstore). Digest-pinned
  images and a per-release SBOM are published; a signature proving *who built* the image is the remaining
  piece, planned as a fast-follow to the release tooling.
- **The identity server's bootstrap administrator is not retired automatically.** Nothing requires retiring it
  (see *The identity server's bootstrap administrator*); automating that is on the roadmap. Its console is served
  under the sign-in hostname by default; narrowing who can reach it is an optional setting, described in the
  same section.
- **Per-organization data isolation (multi-tenancy)** is on the roadmap, not yet shipped. Today's recommended
  deployment is single-organization, which keeps the isolation boundary at the deployment.

If a control your procurement process requires is not listed above as present, assume Talus does not yet have
it and ask — an honest "not yet, here's the plan" is the answer you will get.

---

## Compliance and certifications

Talus's posture is **plain: there are no formal certifications today.** What Talus offers instead is a clear,
verifiable mapping from each common control to the architecture that implements it — auth via Keycloak,
fail-closed item-level authorization via OpenFGA, trigger-level audit of record changes with actor attribution,
encryption in transit, client-side encryption of backups when the operator supplies keys, and full data
sovereignty including air-gapped deployment.

For an evaluating security team, **the detailed, control-by-control mapping** — Talus measured against the
ArcGIS Enterprise Security Best Practices / Hardening Guide, control by control, with where-implemented
references — **is available on request** as part of a procurement engagement. This is the same internal
analysis the project maintains for itself; it is shared with evaluators rather than published broadly so it
stays current and accurate to the version you are evaluating.

---

## Reporting a vulnerability

Please report suspected security issues privately. Email **`security@talusgis.com`** (the address in the
repository's `SECURITY.md`), or use GitHub's **private vulnerability reporting** on the repository; the
repository does not use a public issue tracker.
Reports are acknowledged within three business days, assessed with a remediation plan within fourteen days of
acknowledgement, and disclosure is coordinated with the reporter. Good-faith research against **your own
deployment** is always in scope.

---

## Further reading

Published with each release: the **disaster-recovery hardening guide** (off-host targets, encryption keys,
the 3-2-1 posture) and the **restore runbook** (the tested restore drill). Available to evaluators on
request: the **Deployment Hardening Guide** (what ships hardened by default and the operator's
responsibilities baseline) and the **security control mapping** behind this summary.

---

*Talus GIS is a product of Talus GIS LLC. This document describes the platform's security architecture for
evaluation purposes and is not itself a contractual warranty; specific commitments are set out in the
applicable agreement.*
