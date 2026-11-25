# Talus GIS — Frequently Asked Questions

Honest answers to the questions evaluators and new deployments ask most. Where Talus has a gap,
it is named here — that is deliberate.

## Editing, concurrency, and "do I need versioning?"

**Does Talus track who edited what?** Yes, comprehensively. Every edit stamps who and when, and
audit triggers keep a full change history with the actor's identity — including edits made from
ArcGIS Pro or QGIS through the interop doors. This is more than last-editor fields: it is a
queryable history.

**Can two people edit at the same time?** Yes, natively. Talus stores data in PostgreSQL, which
is fully transactional — two editors on different features never conflict, with no check-out,
no versioning apparatus, and nothing to reconcile.

**What if two people edit the same feature?** Talus uses optimistic concurrency: every feature
carries a version, every save asserts it, and a save against a stale version is refused with a
conflict (HTTP 409) rather than silently overwriting. The client refreshes and the second editor
sees the first editor's change before making their own.

**Where is reconcile-and-post / branch versioning?** Deliberately absent. The needs versioning
actually serves in most municipal shops — multi-editor safety and accountability — are covered
by real transactions, optimistic concurrency, and the audit trail. Isolated long-running edit
sessions are a heavy minority need Talus does not carry infrastructure for.

**Field collection?** QField (free, open source) works today through the standard doors —
collection, photos, and a documented round-trip recipe (`docs/connect-desktop.md`, Part 2). That
path stays in Talus Community and is the proven baseline, not a placeholder. **Talus Field**, a
web-first field app with offline packs and a browser install, is the Talus+ module: edits made
without a connection wait on the phone and sync when it is back. **ArcGIS Field Maps and Survey123 are not compatible** — they sign in to an Esri
portal and open only what lives there; the forms and offline areas built in them map onto Talus
Field.

## Security

**Is it secure?** Secure by default: standards-based sign-in (Keycloak — OIDC/OAuth2/SAML,
SSO and local accounts side by side), fine-grained fail-closed authorization (OpenFGA — an
authorization error denies, never allows), full write-audit with actor attribution, and a
negative-test suite you can run against your own install (`scripts/verify-invariants/`) that
proves the deny-by-default behavior rather than promising it. The full buyer-facing security
overview ships in this repository (`docs/security-overview.md`).

**Certifications?** None yet — no SOC 2, StateRAMP, FedRAMP, or CJIS. Talus is confident about
the architecture and honest about the certificates it has not earned. Do not use it to satisfy a
compliance checkbox it cannot back.

**Can the public see our data?** Only if an administrator deliberately turns public sharing on.
Every fresh install starts with the public tier off; enabling it is a portal setting behind a
confirmation that names the consequence, and every flip is recorded in the administrative audit
log. When it is off, none of your data is served anonymously — turning it off also stops serving anything
that was previously public, immediately. (The background map and the portal's default map view are not your
data and load without sign-in either way.) When it is on, only items explicitly shared to Public
Access are anonymously readable.

## "It's open source off GitHub — how do I know it's safe to install?"

A proprietary product asks you to trust it; open source lets you verify it. Concretely: the
install is a script you can read before you run it (`scripts/talus-setup.sh`, which runs
`install.sh`) and a `docker compose` file — no `curl | bash`, and nothing fetched from the
internet but the container images the compose file names.
Every change is gated by CI security scanners (static analysis, secret scanning, and dependency and
configuration scanning, failing on high/critical findings that have a fix). Every release ships with a CycloneDX
software bill of materials (source and image), and every container image in the shipped compose
file is pinned by content digest. Dependencies are kept current automatically in the private
repository where Talus is developed, and the third-party CI action the published workflows use is
pinned by commit hash.

There is also no untrusted-contributor path: Talus is solo-maintained by design (the SQLite
model — outside pull requests are declined automatically), so the most common real-world
open-source supply-chain compromise — a hijacked contributor — has no ingress.

The honest remaining gap: release images are not yet cryptographically signed (no
Sigstore/SLSA provenance attestation). That is on the roadmap; until then, the SBOM plus the
digest-pinned compose plus the readable source are the verification surface.

**Nothing phones home.** There is no telemetry, no update checker, and no license server. A
deployment *can* reach the internet for a short list of things, including certificate renewal, address search,
streamed map backgrounds, an off-host backup target, and in Talus+ the AI assists. Two of these are
on when you install, because a new portal is more useful with them: a public address search, and
street maps that each viewer's browser fetches straight from the publisher, OpenStreetMap by default.
An administrator can switch either off, and the rest exist only if you configure them. With those two
off and nothing else configured, the server makes no outbound connection and no viewer's browser is
sent anywhere but your portal, which you can confirm at your own network layer. The security overview
lists them one by one.

## "Talus is AI-built — isn't that a risk?"

Talus is developed with AI as the implementer, directed by a 25-year municipal GIS practitioner
under a decision-before-code discipline: every substantive capability gets a written design
decision before implementation, a security review, and negative tests for every new data path.
"AI slop" is un-owned code; this is the inverse — the judgment is front-loaded and human, the
implementation is reviewed, and the verification suite in this repository is the discipline's
public face. Judge the result by the running system: fail-closed, audited, and testable on your
own install.

## Deployment and scale

**Can I try it without installing?** Yes. [demo.talusgis.com](https://demo.talusgis.com/) is a live Talus Community
server with a Florida county's open data on it. Browse the maps without an account, or sign in with the shared login
on its front page to edit two practice layers. That login cannot upload data or create layers, so to try Talus with
your own data, install it. Edits are erased every night at 3 a.m. Eastern.

**What does it take to run?** One host with Docker. One script (`scripts/talus-setup.sh`) installs the whole
stack — the portal, database, identity, authorization, tiles, and TLS edge — where the closed
alternative needs several separately installed components. A modest machine — 4 vCPUs and 8 GB — serves a
small organization comfortably: measured at county scale (a 300,000-parcel layer), four vCPUs
carry about fifteen simultaneous requests, roughly 150 named users at typical duty cycles. Adding
CPU raises that ceiling; adding RAM does not.

**Are there credits or per-user fees?** No. Nothing in Talus Community is metered, and there are no
user licenses to buy: add the accounts you need. Talus+ is one flat price per deployment, never
counted by users or seats. What sets the practical limit is your hardware: on a 300,000-parcel
county layer, four vCPUs carry about fifteen simultaneous requests, roughly 150 named users at
typical duty cycles, and adding CPU raises that ceiling. The one allowance anywhere is the Talus+
AI assists, which pause at their ceiling and never bill on their own.

**Do we need Kubernetes?** No. The graceful ladder is: one host → externalize the database and
storage to managed services if you outgrow it → orchestration only if you genuinely reach that
scale. Talus does not push you up the ladder.

## Operations — "does it need babysitting?"

Talus keeps itself tidy. A built-in scheduler expires ephemeral cruft (transient keys, orphaned
uploads) on thresholds you control — and structurally cannot touch your layers, features, or
audit log; that is a design guarantee, not a setting. Backups run on a schedule with retention
floors, an admin-settable off-host destination (with a connection test button), optional
encryption, and written restore procedures with a restore drill you can run on your own server (`docs/dr-restore-runbook.md`). An Admin →
Maintenance panel shows what ran and what it freed. There is no separate maintenance utility to
learn.

**Will an upgrade break our maps or dashboards?** An upgrade happens when you run it, never on its
own. The upgrade runbook (`docs/upgrade-runbook.md`) starts with a backup that must visibly
complete, each release is rehearsed on a test server before it is published, and the restore from
that backup is rehearsed too.

## Common capability questions

- **Custom basemaps / aerial imagery?** Yes — self-hosted vector basemaps with no metering, and
  an imagery pipeline: a GeoTIFF, JPEG 2000 or ERDAS IMG raster converts to cloud-optimized GeoTIFF and serves
  as tiles, fully air-gapped.
- **"Can I take a map into the field on my phone — does it open in Avenza Maps?"** Yes. The map's
  **Export → Print** tab produces a one-page PDF (title, legend, scale bar, north arrow) that is
  **georeferenced** in the ISO 32000 geospatial-PDF form Avenza Maps, QGIS and Acrobat read, so it
  opens as a map, not a picture: your position shows on it in the field, and QGIS places it over the
  layers it was printed from. It is a single rendered page, not a map book or a layered PDF; a tilted
  (pitched) view exports as seen but is not georeferenced, and the Print tab says so.
- **Forms and validation?** Yes — owner-defined fields, reusable coded-value domains, constraint
  rules and defaults, all on PostgreSQL primitives. No proprietary expression language.
- **Scripting / automation?** A documented `/api/v1` with scoped service-account tokens
  (`docs/api-guide.md`) — script from your own environment, on your own compute.
- **Public input / crowdsourcing?** Not offered, and anonymous public writes are deliberately
  not accepted.
- **3D, routing, Utility Network, Parcel Fabric, heavy imagery analytics?** Non-goals. Talus
  deliberately sheds the exotic 20% — that is what keeps it one host and one install.
- **"Can I connect QGIS straight to the PostgreSQL database?"** No — deliberately, for everyone.
  Every read and write goes through the API doors, because the doors are where everything you
  run Talus for lives: sharing and authorization, audit and editor tracking, conflict
  detection, attachments, and backup coherence. A raw connection string would bypass all of it.
  QGIS connects through the standard doors instead (`docs/connect-desktop.md`), and attachments
  are managed objects — access-controlled, web- and Pro-visible, backed up with the database —
  not a path column pointing at a file share.
- **"Can I keep my token-secured ESRI geocoder? Does adding the World Geocoder log me into
  ArcGIS?"** Yes, and no. Add it in Admin → Geocoders as an ArcGIS GeocodeServer locator and paste
  a token you generated in your own ArcGIS Online or Location Platform organization — there is no
  ArcGIS sign-in and no OAuth prompt. The token is stored write-only and encrypted at rest, never
  echoed back (the form shows only its last four characters), and validated with a Test button.
  Talus never brokers an Esri sign-in and never sees your Esri password. For
  credit-consuming services, batch geocoding shows a credit estimate and requires an explicit
  acknowledgement before spending. Zero-credit alternatives ship in the box: an internal
  geocoder over your own address points, or self-hosted Nominatim.

- **"Can we use GeoLibre with Talus?"** Yes, read-only, today, with nothing to configure. GeoLibre
  is an open-source GIS that runs in a browser tab, on the desktop, and on a phone; it hosts no data
  and has no accounts, and its own documentation recommends pairing it with a server that provides
  those. In the GeoLibre desktop app, **Add Data → OGC API — Features** and paste the portal's
  `/api/v1/ogc` landing page: every publicly shared layer appears and pages through in full. Or
  **Add Data → ArcGIS layer** and paste one public layer's `/api/v1/esri/rest/services/<id>/FeatureServer/0`.
  Use the desktop app rather than the hosted web app: Talus keeps its doors closed to third-party
  browser origins on purpose. A private layer opens from a file (Use in… → Download a copy →
  GeoPackage). **Editing from GeoLibre does not work, and it is an authentication problem, not a
  capability one:** GeoLibre writes edits with the same Esri `applyEdits` call ArcGIS Pro uses on
  Talus, but its ArcGIS client can only sign in with a token in the URL (`?token=`), and Talus takes
  tokens only in a header, by decision (QGIS and Pro never needed a URL token; a token in a URL
  leaks into browser history and proxy logs). On an editing-enabled public layer GeoLibre will offer
  the save, Talus refuses it with a 401, and nothing is written.
- **Dashboards, scheduled reports, story maps?** Yes — all three are Talus+ modules: operational
  dashboards (indicators, charts, filtered map widgets with cross-filtering), scheduled and
  per-feature PDF reports with delivery to email or webhook targets, and story maps. The
  zero-build free path for per-feature documents is QGIS Atlas over the OGC door — a power-user
  recipe, not a placeholder.
- **"We're moving data out of ArcGIS, or anything else, into Talus. What should we do first?"**
  Back it up where it is. Export the geodatabase or the service, keep the copy somewhere Talus
  cannot touch, and leave the original system running until you have checked what came across.
  Imports are copies and the source is never modified, but the original is your way back.
- **"Can we get all our data out, photos included?"** It never leaves. Your layers live in the
  PostgreSQL database on your own server, and photos and attachments are stored beside them as
  managed files. Scheduled backups take every database plus those files, optionally encrypted to a
  key only you hold, to a destination you choose, and the restore is rehearsed. ArcGIS Pro and QGIS
  read every layer through the standard doors (`docs/connect-desktop.md`), so you can save a copy in
  any format they write, and datasets you publish download as CSV, GeoJSON, Shapefile, or
  GeoPackage.
- **"I'm importing a CSV — what happens to my ZIP codes, FIPS codes, and parcel IDs?"**
  Leading zeros are preserved. A CSV carries no type information, so Talus reads the header and
  the first 200 rows and picks a type per column: all whole numbers ⇒ integer, all decimals ⇒
  double, anything else ⇒ text. A value with a leading zero before more digits — `01234`, `007`,
  `-01` — is treated as an identifier, not a number, so the whole column comes in as text and the
  value is stored exactly as written. Plain `0` and `0.5` are still numbers. Dates and true/false
  are not guessed; they arrive as text. Rule of thumb: if a column is an identifier you would
  never do arithmetic on, Talus keeps it as text and keeps it intact. A field's type is fixed when
  the field is created, so if you want particular types, build the layer's fields first (Manage →
  Fields) and import the data into that existing layer rather than letting the import create one.

## "What does the AI helper actually do — and what does it see?" (Talus+)

**What it is.** The **Talus Helper** answers questions from the product documentation and, for
administrators, from the deployment's own health state. It is informational: it explains, cites,
and suggests steps. **It never runs anything** — no restarts, no configuration edits, no data
changes.

**Docs mode (any signed-in user).** You ask; the answer comes from the Talus documentation only,
with the documents and sections it used, suggested next steps, and — when the docs do not cover
it — a short summary you can send to support. Every answer carries a provenance footer (model,
prompt version, docs version, request id): AI is always disclosed, never dressed as a person.

**Diagnose mode (administrators; included in Talus+, except in an air-gapped install — built, not
yet field-tested).** "Why isn't my service working?" The Helper reads a **snapshot** the portal
assembles from what it already records: live checks of the database, sign-in, permissions,
storage and the backup destination; the last ~200 warnings and errors; recent administrative
actions; backup, maintenance and migration state; license status. **You see the snapshot before
it is sent, section by section, and choose what goes.** The answer is a diagnosis, the steps
*you* perform (from the runbooks), and a support summary carrying the request id.

**Where does it run? What leaves my server?** Your portal talks to a small service Talus GIS LLC
operates, which talks to the model provider (Anthropic) under Talus's own account. What leaves:
your question; in diagnose mode, the snapshot sections you approved. What never leaves: your GIS
data, and your Talus token in the clear. The person asking is sent as an opaque per-installation
pseudonym, not a name. The snapshot sections are different: secrets are removed from them, but user
names and host names are kept, because they are what makes a diagnosis useful; leave a section out
if you do not want it sent. **What is kept:** usage metadata only (counts, timestamps, request ids and, when diagnostics were sent, which sections went and a hash of them) — no question text,
no answers, no snapshot content, on either side. The retention that applies at the model provider is set
out in the data-processing terms of your agreement.

**Do I hold an AI vendor key?** No. You upload the activation file Talus sends you on the License page;
your server exchanges its one-time code for a **Talus-issued token**, which is stored encrypted and
validated once against a fixed destination that no setting in the product
can change. Talus holds the only provider credential. If you remove the token, the Helper is off;
if your term lapses, the Helper stops and everything else keeps running.

**Can it run up a bill?** No. Usage counts against an included allowance; you see percent used
and pace, never a meter. At the ceiling the AI pauses and we talk — a top-up or a larger allowance
is a quote you accept, never an automatic charge. A daily cap stops a runaway in hours.

**Talus Community?** No AI at all, structurally: the AI code is not in the public build; there is
no token, no route, and no provider key anywhere in a Community install. An air-gapped Talus+
likewise reports "not available in this environment" and keeps every non-AI module; the
documentation is the manual path.

**Which model?** Currently Claude Haiku. The model is chosen on the Talus side per operation, so
upgrades reach every customer without a product update — which is also why the feature is called
the Talus Helper, not after a model.

**Limits, honestly.** It answers from the docs, not the internet, and says so when the docs do
not cover a question. In diagnose mode it sees what the portal records plus the last error lines of the
database, the sign-in service and the web edge — not their full logs or the host — so some problems still end in "here is what to tell support,"
with a request id that lets us pick up exactly where it left off.

## Licensing — "AGPL? Our policy has questions."

- **Does using Talus obligate us to publish anything?** No. AGPL obligations trigger on
  modifying and distributing/serving the software, not on use. A municipality running Talus —
  even public-facing — has zero disclosure obligations. You almost certainly run GPL-family
  software already: QGIS and PostGIS are GPL-family.
- **Why AGPL and not permissive?** So the open core stays open: copyleft means no vendor can
  absorb Talus into a closed product and sell it back to you. It protects your exit rights.
- **Our procurement policy bans AGPL outright.** A commercial license for the identical code is
  available directly from the project — single copyright holder, which is what makes
  dual-licensing possible. Same software, different paper.
- **Can our staff modify it?** Yes, freely, for your own use. Only redistributing or offering
  your modified version to others triggers the share-back clause.
