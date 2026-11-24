# Changelog

All notable changes to Talus GIS are documented here. Versions follow semantic
versioning; entries describe the product surface, not internal development history.

## v1.1.0 (2026-11-24)

Layers can be set to draw only at the zooms you choose. This release also tightens two picklist permissions and
fixes the map's scale readout. **A new api image. No database migration.**

### ★ Read before upgrading

- **Upgrade by the standard procedure** in `docs/upgrade-runbook.md`, from 1.0.0. Take the `pg_dump -Fc` its first
  step asks for.
- **Talus+ needs the 1.1.0 bundle.** The 1.0.0 bundle is refused.
- **Creating a picklist now needs the publisher or administrator role** (see Changed).

### Added

- **Visible range.** A layer can be drawn only between two zooms. Its owner or an administrator sets a default on the
  layer; anyone who can edit a map can give that map its own range, which replaces the layer's. Set it from the new
  **Visibility** tab in the layer inspector or **Visible range** in the layer's ⋯ menu, with named steps from Region to
  Buildings and the approximate scale at the map's centre. Outside its range a layer draws nothing and asks for none of
  its tiles, so a zoomed-out view of a large layer, such as a county's parcels, stays quick. The Layers panel greys
  the layer and says "Shown from zoom 14"; click that line to zoom in. **Zoom to** lands where the layer is drawn,
  and the printed legend leaves out layers that are not drawn. The range changes the drawing only: the table, export
  and the data are the same at any zoom, and it is not an access control.
- **Raster layers have the layer menu**, with **Zoom to** and **Visible range**.

### Changed

- **Creating a picklist (a domain) needs the publisher or administrator role**, like creating any other item.
  Before, any signed-in account could create one. Using existing picklists is unchanged.
- **A picklist's Used by list names only the layers you can view**; an administrator still sees all of them. Before,
  any signed-in account could see the name of every layer that used a picklist.
- **Questions, bug reports and suggestions** can go to GitHub Discussions as well as by email. Security reports still
  go to `security@talusgis.com`, never to Discussions.
- **Talus+ browser files are kept apart from the core app** and are fetched only when a page uses them. Search
  engines are asked not to list them or the Talus Field app.

### Fixed

- **The map's status bar shows the true scale.** It read twice the real figure.
- **The Sketch toolbar's Polygon button is no longer cut off.**

## v1.0.0 (2026-11-15)

Talus 1.0. The core is complete and validated against real municipal data; it ships as versioned releases with
digest-pinned images and per-release SBOMs. **1.0.0 is installed fresh**, and in-place upgrades, by the same procedure
as every release before it, begin here. This release adds streamed basemaps, a simpler Import data dialog and dates
that read as dates; moves every service to a current release; ships the U.S. datum correction files; and carries a
round of security fixes. **A new api image. Four database migrations.**

### ★ Read before installing

- **1.0.0 is installed fresh.** A server that ran a 0.9.0 beta is not upgraded in place: install 1.0.0 on a clean
  server (`docs/install-runbook.md`). In-place upgrades, by the upgrade runbook's standard procedure, begin with 1.0.0.
- **Why:** 1.0.0 moves the database, the sign-in server, the authorization service and the edge to current
  releases. The database's move changes the operating system under it, and a database created under the old one
  cannot simply be started on the new one.
- **Four database migrations, V115 to V118.** A new install applies them with the rest.
- **Talus+ needs the 1.0.0 bundle.** Its `BASE_IMAGE_REF` names this release's image; an older bundle is refused.

### Security

- A raster the server opens, whether just uploaded or already stored, is read with one named driver and never
  follows a reference inside the file to another file or address.
- A raster upload that names its own overview file is refused whatever case the reference is written in (the
  check compared one case only).
- A very long or deeply nested filter sent to the ArcGIS-compatible service can no longer tie up the server.
- A personal access token no longer counts as holding every role on the doors that also serve signed-out visitors.
  Before, a viewer's token could read the metadata of layers, maps and rasters the viewer could not open, and some
  doors told it it could manage items it did not own.
- A Talus Field device credential cannot edit through the ArcGIS-compatible service.
- A generated field project's credential edits only that project's layers.
- Only a source layer's owner or an administrator can change what a view of it exposes.
- A ranged read of the basemap file is streamed in pieces instead of being read whole into memory.
- A request is exempt from the rate limit only after its token has been validated.
- Every request body has a size ceiling: 32 MiB, with upload doors keeping their own.
- Uploaded GeoPackages and zipped Shapefiles are read within time and size limits; exports and attachment
  thumbnails have memory ceilings; backups copy large files without holding them in memory.
- Deleting a group removes its shares on every kind of item that can be shared with one.
- `/health` no longer returns the database's own error text.
- The administrative audit log cannot be changed or deleted by the database role that serves requests. (V117)
- New optional setting, off by default: `KEYCLOAK_ADMIN_ALLOW_FROM` answers the sign-in server's own
  administration pages only to the addresses listed.
- With the Keycloak admin restriction on, the sign-in host also refuses encoded-slash and dot-segment paths.
- No long-lived offline tokens; sign-in and admin events are recorded for 30 days; a person cannot change their
  own email.
- The installer writes the realm file readable only by its owner where that can be done safely.
- A page shown inside a frame is the embed view or nothing.
- The map library is MapLibre 5.24.1, the upstream patch release whose sanitizer for the text a map shows is an
  allow-list of tags, attributes and link protocols.
- The database, the sign-in server, the authorization service and the web edge are on current releases
  (PostgreSQL 16.16 with PostGIS 3.7.0, Keycloak 26.8.2, OpenFGA 1.22.1, Caddy 2.11.9).

### Added

- **Streamed basemaps.** Admin → Basemap → **What this portal lists** has three choices: **Streamed**,
  **Downloaded**, or **Streamed and downloaded**. Streamed maps are OpenStreetMap (standard, gray and dark), USGS Topo
  and Imagery. Nothing is stored on your server: each viewer's browser fetches them from the publisher, which sees the
  viewer's internet address and the area viewed. A new installation starts on Streamed, with new maps opening on
  OpenStreetMap, so there is a street map from the first sign-in. Choose **Downloaded** and no viewer's browser is sent
  to the internet for a map background.
- **Talus Field follows the same setting (Talus+).** Online, a crew sees the streamed maps the organization
  offers; from a pack with no signal, Light, Dark and Gray draw as before.
- **Import data is three short steps:** Source, Target, Options.
- **The map says when you are outside the downloaded basemap,** and where an administrator can fix it.
- **A file too large for the map's Add from file says why** and offers Import data, which takes far larger files.
- **A support page.** `docs/support.md` says where to write, what each edition gets, and what is not promised.
- **The U.S. National Geodetic Survey's datum correction files ship with the product** (NAD27 to NAD83, and NAD83
  to NAD83(HARN); public domain), in the database and in the api image alike.

### Changed

- **Layers on a NAD27 or a plain NAD83 coordinate system are corrected the same way in every state** when drawn
  with other data. Their positions can differ from the betas' by up to about 3 ft, and by about 2 m in Hawaii; NAD27
  imagery, which the betas converted with no correction file, by metres more. Stored coordinates are unchanged. Pick
  the coordinate system the data is really on.
- **Layers on a NAD83(HARN) State Plane or UTM zone are drawn with** layers on NAD27, original NAD83, NAD83(2011)
  and GPS coordinates of the same place. In the betas they were drawn about 0.6 to 1.3 m (2 to 4 ft) away, depending
  on the state; layers on HARN latitude and longitude were not affected.
- **Layers on Canada's original NAD83(CSRS), and on the zones built on it, are drawn with the others too.** In the
  betas they were drawn about 1.2 m (4 ft) away.
- **`NOTICES` names the EPSG dataset's owner and its terms of use,** and the three records Talus has marked
  deprecated in the coordinate library's reference database; the api image's own notices say the same.
- **The web edge accepts request headers up to 128 KiB,** as before; Caddy 2.11's own default is 16 KiB.
- **The database migration tool no longer checks for updates over the network.**
- **Dates and times read as dates and times** in the popup, the data table, the record drawer and dashboard lists,
  in the viewer's own time zone. Stored values are unchanged.
- **One typeface everywhere:** form controls, the map's own text, and dashboard charts and gauges.
- **psycopg 3.3.9** (from 3.3.6), which fixes a crash when fetching arrays after a connection closes.
- **Documentation:** the security overview, the FAQ, the runbooks and the user guides were read against the
  product and corrected where they had drifted; they describe streamed basemaps; the restore runbook's new-server
  scenario is rewritten; the Talus Field runbook says what a pack's basemap is.

### Fixed

- **A layer stored in its own coordinate system draws at the widest zooms,** and a box, a shape filter, an address
  search or a Talus Field pack extent reaching far outside that system's area is handled, instead of being converted to
  meaningless coordinates. (V118)
- **Single-item recovery brings back a layer or table with its field values, and a web map.**
- **The restore drill no longer stops on a server whose settings name optional modules.**
- **The restore drill reads stored files from the backup destination in effect**, set in Admin → Backups or in
  `.env`, and checks that it got as many as the server holds. It no longer needs a separate S3 client image, which
  could no longer be downloaded.
- **Restoring into the existing stack drops and recreates each database first, then restores the stored files**
  (the runbook's commands could leave a database half restored, and a file replaced since the backup missing).
- **Each version of a field project's file is backed up;** Admin → Backups & recovery shows each backup's ID, and
  `restore-item.sh --list` shows what a backup holds.
- **An install or upgrade never changes an existing administrator account.**
- **A documented two-address `KEYCLOAK_ADMIN_ALLOW_FROM` no longer stops later installs;** the preflight names a
  `.env` line the shell cannot read.
- **The public on/off switch on a layer is recorded in the audit log.**
- **Talus Field (Talus+): deleting a feature from a pack works when the crew member is signed in,** and a delete
  that cannot be sent no longer blocks the rest of the queue.
- **Talus Field (Talus+): deleting online never discards queued edits;** a renewal whose reply is lost no longer
  wipes the phone.
- **A generated QField project opens on its own layers,** not on one fixed place.
- **A raster opened from the catalog is drawn on the map.**
- **An untouched new map no longer asks "Leave this map?".**
- **"No basemap installed yet" no longer shows over a streamed map.**
- **A double-click on Next in Import data no longer runs the import.**
- **Five messages name the page that exists** (Admin → Sharing & access, Admin → Connections), and the public
  download line names GeoPackage.
- **The report editor's two notes about maps on sheets say what the server does (Talus+).**
- **Reports (Talus+): scheduled documents are kept separately from requested ones.**
- **A successful basemap upload no longer ends on an error screen.**
- **The "No basemap installed yet" notice names Admin → Basemap,** where the page is.

## v0.9.0-beta.20 (2026-11-08)

The map's PDF print is georeferenced, its north arrow follows the map, and the doctor script's one false warning on
Linux is gone. **A new api image. No database migration.**

### ★ Read before upgrading

- **No migration.** The database schema is unchanged. Take the `pg_dump -Fc` the upgrade runbook's first step asks
  for anyway — it is the rule for every upgrade.
- **A new api image.** The upgrade pulls it. A server that pulls from the registry with a sign-in signs in for the
  pull and runs `docker logout ghcr.io` afterwards, as the runbook describes.
- **Talus+ needs the beta.20 bundle.** Its `BASE_IMAGE_REF` names this release's image; an older bundle is refused.
- **Talus+ servers coming from beta.18 or earlier:** if the upgrade's overlay step prints `Permission denied` under
  `web/dist` or `web/dist.prev-*`, run that `rsync` with `sudo`, this once. A server already on beta.19 needs no `sudo`.

### Added

- **The Print tab's PDF is georeferenced.** The one-page map PDF carries ISO 32000 geospatial registration — the form
  GDAL, QGIS, Avenza Maps and Acrobat read — so it opens as a map: Avenza shows your position on it in the field, and
  QGIS places it over the layers it was printed from. Always on; the Print tab says so. A tilted (pitched) view exports
  as seen and is not georeferenced; the Print tab says that too. No new dependency.

### Fixed

- **The PDF's north arrow turns with the map.** A rotated map printed an arrow that always pointed up the page; it
  now points to north.
- **`scripts/talus-doctor.sh` reads the `.env` file mode on Linux.** Every Linux server printed a garbled
  `WARN .env is mode …` line; the check now reports the mode and says ok when it is `600`.

### Changed

- **Documentation:** the README, the FAQ and `docs/guide-maps.md` describe the georeferenced PDF; the FAQ answers
  "can I take a map into the field on my phone?".

## v0.9.0-beta.19 (2026-11-06)

User guides for every part of the portal, a Helper that answers from them, geocoding from a table already in
Talus, working pop-ups for signed-out visitors on public maps, and an import fix for date fields. **A new
api image. No database migration.**

### ★ Read before upgrading

- **No migration.** The database schema is unchanged. Take the `pg_dump -Fc` the upgrade runbook's first step asks
  for anyway — it is the rule for every upgrade.
- **A new api image.** The upgrade pulls it. A server that pulls from the registry with a sign-in signs in for the
  pull and runs `docker logout ghcr.io` afterwards, as the runbook describes.
- **Talus+ needs the beta.19 bundle.** Its `BASE_IMAGE_REF` names this release's image; an older bundle is refused.
- **Talus+ servers coming from beta.18 or earlier:** if the upgrade's overlay step prints `Permission denied` under
  `web/dist`, run that `rsync` with `sudo`, this once. The beta.19 bundle step hands the web app to the install user
  afterwards.
- **Two things behave differently.** The coordinate-system picker's short list is now WGS 84, Web Mercator and NAD83
  on every deployment; search for your own zone by name or EPSG code. `scripts/basemap/extract-basemap-pmtiles.sh` has
  no default area any more: set `BASEMAP_BBOX=west,south,east,north`.

### Added

- **User guides.** Twelve documents in `docs/`, starting at `docs/user-guide.md`, which says which guide answers which
  question: the portal, layers and tables, maps, geocoding, imagery, administration, database pulls, dashboards,
  reports, story maps and the Helper. Each ends with what the feature deliberately does not do and what to do when
  something goes wrong.
- **Two runbooks the product already pointed at:** `docs/sso-runbook.md` (single sign-on and a second factor) and
  `docs/basemap-provisioning-runbook.md` (the street basemap, including on a network with no outbound access).
- **Geocode addresses takes a table or layer already in Talus.** The Input step asks where the addresses are: a file,
  as before, or a table or layer in the catalog that you can see. The result is a new point layer copied at that
  moment; it does not update when the table changes or a database pull refreshes it.
- **The Helper names the documents it cites in a form the product recognises** (Talus+). From the first public
  release a cited document opens at that release's tag; a beta build shows the document's name.
- **A shipped check for the new geocoding source:** `scripts/verify-invariants/inv50-batch-geocode-source-authz.sh` —
  a publisher who cannot view a table cannot geocode from it.

### Fixed

- **A signed-out visitor to a public map or an embedded map gets a working pop-up.** It read "Error loading feature"
  for every point, line and polygon layer. The pop-up lists the layer's active fields under the owner's labels.
- **The public attachment list no longer names the person who uploaded a file** to a signed-out caller. A signed-in
  viewer still sees it.
- **A feature layer copied from an ArcGIS URL keeps its date fields.** A layer with a date field refused every row.
  GeoJSON columns are now typed from a sample of the rows, not the first row alone.
- **Talus+: the web app is owned by the install user after the bundle step**, so the next core upgrade's overlay does
  not need `sudo`.
- **Talus+: the bundle no longer carries an 18 MB file that was never part of the product.**
- **The shipped suite's field-pack check no longer races the pack worker** on a fast server.

### Changed

- **No place, person or product is named in shipped code.** Error messages, placeholders and examples are neutral; the
  two behaviour changes are under *Read before upgrading*.
- **DOMPurify 3.4.17** in the web app, clearing two published advisories.
- **README:** bugs and suggestions go to `support@talusgis.com`; keep a backup of your source data before you move it
  into Talus.
- **`docs/tester-note-databases.md` is gone;** `docs/guide-databases.md` replaces it.
- **The release build's package-index signature check prints one line when it passes.**

## v0.9.0-beta.18 (2026-10-22)

A console report for when the portal cannot answer, the other services' error logs in the Helper's snapshot, a
snapshot that states the present before the history, bounded container logs, and the field app's fixes from a day
of phone testing. **A new api image. No database migration.**

### ★ Read before upgrading

- **No migration.** The database schema is unchanged. Take the `pg_dump -Fc` the upgrade runbook's first step asks
  for anyway — it is the rule for every upgrade, not only the ones with migrations.
- **A new api image.** The upgrade pulls it. A server that pulls from the registry with a sign-in signs in for the
  pull and runs `docker logout ghcr.io` afterwards, as the runbook describes.
- **Talus+ needs the beta.18 bundle.** Its `BASE_IMAGE_REF` names this release's image; an older bundle is refused.
- **Every container is recreated once at this upgrade's `up -d`.** Container logs are now bounded (below), and the
  setting lives on each long-running service, so Docker rebuilds each container the first time. Expect about a
  minute in which the portal does not answer; upgrade when that is acceptable. Later upgrades do not repeat it.
- **Two new pieces appear in the stack:** a one-shot `logs-init` service and a `servicelogs` volume (the error logs
  below). Nothing to configure.
- **`scripts/talus-doctor.sh` ships.** Run it from the install directory when the portal will not load; it prints a
  report with no secrets in it. The runbooks name it.
- **`scripts/apply-realm-settings.sh` is in the tarball from this release on.** The installer has called it on every
  run since beta.16, but the file itself was missing from the shipped tree — fresh installs lost nothing (the first
  boot carries the same settings); an upgraded server now receives a changed sign-in setting as intended.

### Added

- **A console report for when the portal cannot answer.** `scripts/talus-doctor.sh`, read-only, thirteen checks —
  services, host disk, memory and time sync, the edge and the api through it, the certificate's days and issuer,
  the configured name, clock agreement with the database, migration head, last backup, `.env` mode and key names,
  api log counts — every line scrubbed of secret shapes before it prints, so the report can be pasted into a
  ticket or the Helper's question box.
- **The other services' error logs in the Helper's snapshot** (Talus+). The database, the sign-in server and the
  edge write bounded error logs to a shared volume; the Helper's diagnostic snapshot carries the last fifty lines
  of each, errors and warnings only, with addresses, statements and identities masked — never an access log,
  never a statement. Previewed before it leaves the server, like every other section.
- **The snapshot states the present first** (Talus+). A `status_now` section — what is down, which issues are
  open, the newest backup, and that older records are history — leads the snapshot, so the Helper reads today's
  state before last night's.
- **Talus Field:** location starts when a map opens; the camera is back beside *More* on a feature's sheet; a
  search result says which field matched (*· Light type: Cobra head*) and finds coded values by their label; an
  address can be searched by pressing the keyboard's Search key or tapping *Search addresses for …*; when the
  server is down but the signal is up, Home says *Talus is not answering right now* and lists the maps with a pack
  on the phone; *Copy attributes* carries the Notes.
- **The guided setup's closing words** name the two first-hour steps nothing announced before — Admin → Basemap
  for the street basemap and Admin → Connections for the advertised address — and the Talus+ block says the AI
  assists need an activation file uploaded.

### Fixed

- **The certificate probe recognises the appliance's own certificate authority by the certificate's short lifetime**,
  not by its old name, so a server whose CA is named for the deployment no longer reports a certificate
  "expiring in 10 hours" every day.
- **A dead address locator says so.** A search whose every locator failed answers with an error code instead of an
  empty result; the map's search box shows it; type-ahead reports the locator unavailable.
- **Container logs are bounded** — 20 MB in five files per service. They grew without limit on every install.
- **Search in the field app never shows a record's internal identifier as its name**, and never matches on one.

### Verification

- The release's third-party source fetcher verifies the PostgreSQL repository's **signed** package index — the
  signing key committed beside it and pinned by fingerprint; a bad signature stops the build. This release's tag job
  is the check's first live run.
- The Helper's invariant gains a leg for the service-log section; the field app's pure tests number 96.

### Talus+ bundle `talus-plus-v0.9.0-beta.18`

- The field app changes above; the Helper's `service_logs` and `status_now` sections on the client side.
- Talus AI's matching prompt (helper-diagnose-v5) is already deployed.

## v0.9.0-beta.17 (2026-10-21)

Diagnostics an administrator can act on, open-source notices in the product and the release, a before/after
swipe step in story maps, and a field app that keeps its session. **A new api image** — the first since
beta.14 — and **one database migration.**

### ★ Read before upgrading

- **One migration (V114, a table for recent warnings).** Take a `pg_dump -Fc` before upgrading, as the
  upgrade runbook's first step says; the installer applies the migration.
- **A new api image.** The upgrade pulls it. A server that pulls from the registry with a sign-in signs in
  for the pull and runs `docker logout ghcr.io` afterwards, as the runbook describes.
- **Talus+ needs the beta.17 bundle.** Its `BASE_IMAGE_REF` names this release's image; an older bundle is
  refused by the installer's check.
- **Sessions now stay signed in for 4 hours of idle time** (was 30 minutes), so a field crew away from the
  phone is not signed out. The installer applies the release's realm settings on every run, so an upgraded
  server gets this without a separate step.
- **An IP-mode install's overlay is refreshed by the installer** on every run. Nothing to do.
- **The built-in certificate authority is now named for the deployment** — "Talus GIS (<portal address>) -
  <year> ECC Root" — on servers that **create** it from this release on. An upgraded server keeps the CA it
  already has; nobody re-trusts anything.
- **The Talus+ step touches only the services the bundle names** (today the api). Earlier releases restarted
  every service at that step, which on a fresh install briefly interrupted sign-in. Fixed; nothing to do.

### Added

- **Diagnostics an administrator can act on.** Admin → System gains a **certificate** probe (days until the
  portal certificate expires; amber inside two weeks for a public certificate) and a **clock** probe (the
  api against the database and the sign-in server; red past a minute, where sign-ins start failing).
  **Recent warnings survive a restart**: the "what went wrong recently" record is kept in the database as well
  as in memory, so the server that just restarted can still say why. **Failed pulls and imports** — the last
  ten of each — are a section of the Helper's diagnostic snapshot. **Every server error shows an error code**
  (the request id's first eight characters); paste it into the Helper with diagnostics on and the answer is
  about that request. The snapshot is still previewed, section by section, before it leaves the server.
- **Open-source notices, in the product and in the release.** The api image writes its own `NOTICES` at build
  (every component, its licence text, and where the source of each GPL/LGPL/MPL component is published);
  every web build ships `third-party-licenses.txt`; the sign-in screen, the account menu and — for anonymous
  visitors — a quiet **Licenses** link beside *Sign in* open them, and **Source code** points at this
  release's tag. Each GitHub Release carries the image's notices and the verified source archives of its
  copyleft components and Debian packages. An operator who replaces an LGPL component can rebuild with
  `TALUS_THIRD_PARTY_CHECK=report`; the notices then open with MODIFIED BUILD.
- **Story maps: a before/after swipe step and a full-pane image step** (Talus+). A step can compare two layer
  sets with a divider the reader drags; an image step can fill the pane; each step may set the map's
  bearing and pitch. Saved stories load unchanged.
- **Generate a record sheet from the identify popup** (Talus+): the popup's title row carries the sheet
  icon; one click queues the sheet and offers the report.
- **The field app:** an explicit tap on the map's title (or *Back to maps*) shows the map list even when
  there is one map; the session renews on the app's own cues (back online, back in the foreground, a
  token's expiry) and a refused refresh sends the crew to sign in once; a refused sign-in says which
  address to open — and, for the installed app, that the certificate authority must be trusted first.
- **A refused sign-in says which address to open** on the portal too (the navbar's *Sign in* and the sign-in
  card), instead of a button that does nothing.

### Fixed

- **Reports:** the objectid prints plain in the fields table and in a DOCX `{{field:objectid}}` — an
  identifier, not a quantity (Talus+).
- **The installer applies the release's realm settings on every run**, so an upgraded server receives a
  changed template (the 4-hour idle time above is the first such change).

### Verification

- Story-map schema checks: 54 legs (legacy defaults echoed; bearing, pitch and image-mode ranges refused with
  422; a swipe step naming a layer the map lacks refused; round-trip).
- The AI helper's invariant gains a leg for the per-request records; the throwaway licence the verify scripts
  mint now includes the field module, so running them no longer removes Talus Field from a development server.
- The release's third-party source fetcher refuses a Debian file name that is not a plain file name and any
  file the package's `.dsc` does not list.

### Talus+ bundle `talus-plus-v0.9.0-beta.17`

- Carries the api image's `NOTICES` as a signed member and says where the matching release publishes the source;
  refuses a base image without notices.
- The story-map swipe and full-pane steps, the popup sheet icon, the Helper's diagnostic sections above, the
  field app's session and first-touch changes.
- **The Helper answers a pasted error code from that request's own records** once Talus AI carries the
  matching prompt (helper-diagnose-v3).

## v0.9.0-beta.16 (2026-10-06)

A guided first install, and the first round of fixes from beta feedback: unique-value styling on your own
layers works again, saved dashboards, stories and reports are listed in the Catalog, a feature layer can be
imported from its URL, and the word is *license*. Same api image as beta.14 and beta.15; no migration.

### ★ Read before upgrading

- **Same api image as beta.14 and beta.15.** An install from either upgrades in place by re-running the
  installer over the new tree, as the upgrade runbook describes. No database migration.
- **Talus+ needs the beta.16 bundle.** Most of this release's dashboard fixes live in the bundle. Because the
  three releases share an api image, the installer cannot tell their bundles apart: applying an older bundle
  here would bring back the older web app. Use the bundle named for this release.
- **The installer makes a last name optional** in your sign-in realm on its next run (so an account named
  "Dispatch" works). Nothing to do; existing accounts are untouched.

### Added

- **A guided first install.** `scripts/talus-setup.sh` with no arguments, on a machine with nothing installed,
  asks for the administrator's name, email and password and the address people will type, verifies a Talus+
  bundle found beside the release (with your license or activation file), installs unattended and writes
  `talus-summary.txt`. The first administrator is the account **admin**; the email signs in too. A run that
  stops partway — the image pull needs a sign-in, say — is resumed by running the same command again. The
  install runbook and the README quickstart lead with it.
- **Import from an ArcGIS feature layer URL.** Catalog → Import data → *ArcGIS feature layer URL*: paste a
  public feature layer (`…/FeatureServer/0`, `…/MapServer/0`, a `/query?where=` URL) or a GeoJSON document,
  **Check** shows its title, geometry, count and an estimate of the copy's size, **Import** copies it once
  into a new Talus layer. Your browser does the fetching; the Talus server never calls the other server.
  Public services only — Talus holds no ArcGIS token. A **Reference** remains the way to draw a live service.
- **Saved items in the Catalog** (Talus+). Dashboards, Stories and Reports are Catalog facets beside Maps,
  where a saved map is found; `/dashboards`, `/stories` and `/reports` still open the full lists.
- **One-word account names.** A last name is optional in the Talus realm, so a shared counter sign-in named
  "Dispatch" is a complete account and meets no profile page at first sign-in.

### Fixed

- **Unique-value styles drew every feature grey on a hosted layer.** A layer styled by *Types (unique values)*
  showed the right legend and an all-grey map, because its tiles did not carry the styled field. They do now.
  (Reported by a user during the beta.)
- **"A pulled copy is locked for editing"** on the Databases tab now says what it means: while a pull is
  attached its copy in Talus is read-only, and *Detach* turns it into an ordinary table you can edit.
- **License, not licence**, everywhere a person reads it: the Admin page, the installer, the runbooks and
  the FAQ. Commands, flags and file names are unchanged.
- **Installer:** a password the realm would refuse is refused as soon as it is typed; a failed settings write
  stops the run instead of passing silently; when the guided setup is driving, every failure message names
  the guided setup as the way back; `.env` stays private after a guided run.

### Verification

- The Talus Field sync-token check's never-admin leg now asserts the property it names (a Bearer admin
  overriding editing rules is refused), instead of passing on an unrelated refusal.

### Talus+ bundle `talus-plus-v0.9.0-beta.16`

- **Editors keep your work.** The dashboard, story and report editors reloaded the saved copy every five
  minutes when the session token renewed, discarding unsaved widgets, steps and sections. They load once now.
  (Reported by a user during the beta.)
- **Dashboards:** the List widget pages through its rows and shows the count in a fixed footer; List and
  Filter widgets take a label; a Filter widget can start with its choice lists open; the Filter widget no
  longer overflows a narrow frame; the layer picker says why a Reference is not listed.
- **The Helper renders its answer's formatting** — code, bold, italic and lists — instead of showing the marks.

## v0.9.0-beta.15 (2026-10-04)

A shorter top bar with a **New** menu, and the security checks now run on real Talus+ servers. Same api
image as beta.14; no migration.

### ★ Read before upgrading

- **Same api image as beta.14.** An install from beta.14 upgrades in place by re-running the installer
  over the new tree, as the upgrade runbook describes.
- **Talus+ needs the beta.15 bundle.** The bundle carries the web app for Talus+ servers, including the
  new top bar. Because beta.14 and beta.15 share an api image, the installer cannot tell their bundles
  apart: applying the beta.14 bundle here would bring back the beta.14 web app. Use the bundle named for
  this release.

### Changed

- **A New menu, and three fewer tabs.** *New Map* becomes a **New** menu: **Map**; **Database pull** for
  publishers; and on Talus+ servers **Dashboard**, **Story map** and **Report**. (Where Map would be the
  only entry, it stays a plain *New map* link.) *Stories*, *Dashboards* and *Reports* leave the top bar;
  their lists are unchanged at `/stories`, `/dashboards` and `/reports`. The tabs no longer wrap onto
  two lines on a narrow window.
- The Talus+ runbook's verify step describes the New menu.

### Verification

- **The shipped security suite runs the Talus+ checks on real servers.** On a production install the
  Field and AI checks used to skip as a whole, because they looked for markers only a development setup
  has, and the suite still reported OK. They now ask the server itself what is installed and licensed,
  run every check they can, and name each one they cannot run. A new **PARTIAL** line in the summary
  lists checks that passed with some parts skipped. Parts that would change your server or use your AI
  service (replace the licence, paste or remove the AI token, redeem a code, make an AI request) run only
  on a development setup.
- **Three checks now confirm the refusal they test.** Each must see the request refused for the exact
  field it sends, not merely refused.

## v0.9.0-beta.14 (2026-10-03)

Talus+ licences can now arrive as an activation file. New api image; no migration.

### ★ Read before upgrading

- **New api image.** Upgrade in place by re-running the installer over the new tree, as the upgrade
  runbook describes.
- **Talus+ needs the beta.14 bundle.** The installer compares the bundle's api image with the one
  this release pins and refuses a bundle built for another release before anything changes.

### Added

- **Activation files.** A Talus+ subscription that includes the AI assists is delivered as one file:
  your signed licence plus a one-time activation code. Upload it once at Admin → Talus+ → **Licence**.
  The licence is verified and stored exactly as before; the code is used once and never stored. A
  bare licence file keeps working everywhere. The Talus+ runbook (§1, §6) describes it.
- **`talus-setup --licence` accepts an activation file** and places only the licence.

### Fixes

- **`talus-setup --plus` now really refuses a bundle built for a different release.** The check
  added in beta.13 compared image layers, which needs this release's api image on the server; on a
  Talus+ upgrade it is not there, so the check skipped itself and an older bundle was accepted.
  Bundles from beta.14 on name the api image they are built on, the installer compares that with the
  release's pin, and a bundle it cannot confirm is refused. Nothing changes on the server when it
  refuses.
- **Three invariant checks that look for secrets in the api log read the right time window.** On a
  server not set to UTC they had been reading an empty window, so they passed without looking.

## v0.9.0-beta.13 (2026-10-03)

Three install-path corrections from a line-by-line review of what a new administrator does first.
No image change and no migration.

### ★ Read before upgrading

- **Same api image as beta.11 and beta.12.** An install from either upgrades in place by re-running
  the installer over the new tree, as the upgrade runbook describes.
- **The Talus+ bundle for beta.11 is the matching bundle for this release** (it is built on the api
  image this release pins). The installer now checks that for you; see below.

### Fixes

- **The README's quickstart is now the install runbook's command.** The plain `./install.sh` it showed
  brings up the services without the web portal. The quickstart is `./install.sh --edge --tls-internal`
  followed by `scripts/talus-setup.sh`, and an install without the portal now says so plainly and
  prints the command that adds it.
- **`talus-setup --plus` refuses a bundle built for a different release, before anything changes.** It
  compares the bundle's api image with the one your release pins. Previously the runbook asked you to
  compare two version names by eye and nothing enforced it. A release that changes no image keeps the
  previous bundle, and its entry here names it.

## v0.9.0-beta.12 (2026-10-03)

Two fixes for Talus+ installs, both found installing and upgrading the first real bundle on the
project's fixture. Nothing else changes.

### ★ Read before upgrading

- **No database migration and no new image.** The api image is the one beta.11 pinned; this release
  changes the installer script only. An install from beta.11 upgrades in place by re-running the
  installer over the new tree, as the upgrade runbook describes. Rehearsed on the project's fixture
  with Talus+ installed: core first (16 seconds), then the plus bundle, ending licensed with every
  module and every service healthy.
- **The Talus+ bundle for beta.11 is the matching bundle for this release.** Its api image is built on
  the same base, so `talus-setup --plus talus-plus-v0.9.0-beta.11.tar.gz` installs on beta.12 unchanged.

### Fixes

- **`talus-setup --plus … --licence` now places the licence so the api can always read it.** The file
  was copied in with its original owner and permissions, so a licence kept private (mode 600) arrived
  unreadable to the api, which runs as its own non-root user, and Talus+ reported UNLICENSED. The
  licence is now written by the api's own user. Uploading a licence in the portal was never affected.
- **The upgrade runbook's overlay command now protects `compose.plus.fragment.yml`.** On a Talus+
  install the documented `rsync --delete` removed that file, which `.env` names, and the installer then
  stopped with "no such file". The stack kept running and re-applying the plus bundle restored it, but
  the command as written should not fail; it now excludes the file, like `.env` and the override.
- **The installer's closing line shows only the entitlement verdict** (`LICENSED: …` or `UNLICENSED`),
  never a log line.

## v0.9.0-beta.11 (2026-10-02)

The third database: MySQL and MariaDB join SQL Server and PostgreSQL as sources for a read-only,
scheduled copy of a table or view.

### ★ Read before upgrading

- **No database migration.** An install from beta.10 upgrades in place by re-running the installer
  over the new tree, as the upgrade runbook describes. Rehearsed on the project's fixture before this
  tag: a beta.10 install taken to this release in 27 seconds of installer time, the admin credential
  untouched, every service healthy, the api running at the pinned image, and the shipped verification
  suite green afterwards. The api image is rebuilt: it now carries the MySQL driver (pure Python, MIT).

### Your databases

- **MySQL and MariaDB** (Catalog → Databases → Add connection). One dialect serves both: the same
  per-user connections, read-only login, read-only session, never-list, catalog with keys, refresh
  and schedule as the other two sources. Keys come from the table's primary key or any single-column
  unique index; `TINYINT(1)` and `BIT(1)` arrive as booleans; unsigned integers widen honestly; `ENUM`,
  `SET` and `JSON` become text; binary and geometry columns are named as skipped.
- **Two sign-in plugins are refused in every TLS mode**, `mysql_clear_password` and `dialog`: a server
  asking for the password in clear text never gets it.
- **What each TLS mode means**, now stated in the tester note: `verify-full` checks the certificate
  chain and name; `require` encrypts and, with a pasted CA, checks the chain; `require` with nothing
  pasted does not check who answered, and MySQL's default sign-in sends the password through that
  tunnel on the first login, so treat it as a LAN-only setting; `disable` sends everything in clear.
  Pasting the server's own certificate as the CA is the recommended shape for a server that presents
  an auto-generated certificate.
- The remaining slot on the picker is Oracle.

### Fixes

- The administrator's recent-warnings panel no longer carries exception tracebacks.

## v0.9.0-beta.10 (2026-10-02)

The beta-window release: tables from the databases an organization already runs, per-layer
editing permissions, two more import doors, and the server side of Talus Field's offline work.

### ★ Read before upgrading

- **Ten additive migrations (V104 through V113)** run on install or re-install. Nothing is renamed
  or removed; the sign-in realm is unchanged from beta.9.
- **An install from beta.9 upgrades in place:** overlay the new tree with `.env` kept and re-run the
  installer with the flags you installed with, as the upgrade runbook describes. Rehearsed on the
  project's fixture before this tag: a beta.9 install taken to this release in 58 seconds of
  installer time, the ten migrations applied, the admin credential untouched, every service
  healthy, the api running at the pinned image, and the shipped verification suite green afterwards.
- **The api image carries a SQL Server driver** (pure Python, MIT) and a standard-library TLS channel
  for it. No new system packages, no new ports.

### Your databases

- **A read-only, scheduled copy of a table or view from SQL Server or PostgreSQL** (Catalog →
  Databases, publishers only). Connections are per user: a publisher's connection is invisible to
  every other publisher, the password is stored encrypted and never shown or logged again, and an
  administrator's overview lists hosts and owners, never secrets. Talus connects with a read-only
  login, opens a read-only session, and never writes to the source.
- **Points from two coordinate columns or from an address column**, the latter through the batch
  geocoder after the pull. A plain table needs neither.
- **Refresh writes only what changed.** Rows are matched by the source's primary key or unique
  index, or by content for a keyless view; a changed row keeps its Talus id. Nightly at an hour of
  your choosing or every few hours. A failed refresh keeps the last good copy and shows why; while a
  source is attached the copy is locked against editing in Talus, enforced in the database.
- **The pull follows its owner.** If the owner is disabled or set below publisher, the pull turns
  manual with a reason; a transferred layer arrives detached and editable; a removed user's
  connections go with them.
- **Refused by design:** Talus's own services and loopback as a source, by name and by address;
  free-text SQL and WHERE clauses (your DBA's view is the filter); MySQL and Oracle for now, listed
  but unavailable; Windows authentication to SQL Server for now.

### Editing

- **Per-layer edit permissions** (Manage → Settings → Editing): add, edit attributes, edit geometry,
  and delete are separate switches. Every door honours them: the web editor hides what a layer does
  not allow, the ArcGIS FeatureServer façade advertises and enforces them per request, and the
  attachment doors follow the attribute switch.
- **Draft autosave in the map editor:** unsaved changes are offered back on reopen (Restore / Discard).
- **Forms:** sections, per-field rules (visible-if, required-if, read-only), templates, and a saved-form
  preview on the layer's Form tab; the server enforces required-if on the write door.

### Data in

- **KML and KMZ join the import door** as the fifth format. An address-only Google My Maps export is
  named as such and routed to batch geocoding.
- **Batch geocoding at county scale:** work in chunks with honest progress (elapsed, rate), the
  output layer openable while running; house-number prefiltering and locality-aware scoring for the
  internal locator.
- **A bounding box on a native-CRS layer now filters correctly** across the feature door, export,
  statistics, the FeatureServer spatial filters, and the changes feed. Previously a 4326 envelope was
  compared against State Plane feet and matched nothing.

### Talus Field, the server side

The Field app itself ships in the Talus+ bundle; these are the doors it uses, in the core tree:

- **Device tokens** per pack (30 days, re-minted on refresh, revocable by the user or an
  administrator) that may create, update, and attach on the layers the pack names, never more.
- **A changes feed** (`GET /layers/{id}/changes`): rows changed since a watermark, gated like the
  feature door, so a phone refreshes a pack without re-downloading it.
- **The geocode read doors, the coordinate transform door, and the spatial-reference list accept a
  device token** as well as a session, so an offline capture in a State Plane layer converts at sync
  time with no sign-in.
- **A client that identifies itself as the field app** gets a plain 401 on an expired or revoked
  token, with a header naming which, instead of a browser sign-in sheet.

### Fixes

- The invariant suite reports an unavailable leg as a skip, not a failure.
- Minor: the layer Form tab's remove-field control uses the design-system icon.

### Talus+ (bundle)

Built from this version with `build-plus.sh`: Talus Field end to end (offline packs, GNSS metadata,
Bluetooth receivers on Android, the install card), dashboards, scheduled reports, story maps, the
AI assists, and the Field packs section of the portal.

## v0.9.0-beta.9 (2026-09-12)

The last release before the beta window opens: the install hardened, the pilot's scaffolding
gone, the operator documentation in the box, and the door for Talus+ built and keyed.

### ★ Read before upgrading

- **The sign-in realm is renamed, and this release is new-install only.** Sign-in now lives
  at `/realms/talus`; earlier releases used a development-era name. A deployment installed
  from beta.8 or earlier is **not** upgraded in place across this release: the installer
  retargets the sign-in addresses to the new realm and the identity service imports it
  fresh, so accounts from the old realm are not carried over. Install this release clean and
  bring your data in through the import doors. No deployment outside the project predates
  this release, which is why the rename happened now rather than after a customer had to
  migrate.
- **The api now runs as a non-root user.** On a re-run of the installer over an existing
  storage volume, ownership is adopted automatically — one logged step, once. `.env` is
  locked to mode 600 and, where the host allows it safely, owned by the api's user; the
  installer prints what it did and why if it could not.
- **The `--demo` flag is retired.** The artifact carries no demonstration data; a Talus
  install starts empty and fills with your own. `./install.sh --demo` exits with a message.
- **The database gains the tables the Talus+ modules use** (dashboards, scheduled reports,
  story maps, AI settings). On Talus Community they exist, stay empty, and nothing reads
  them; nothing to configure.
- **A production posture guard.** The api refuses to start in production with development
  settings in place, naming the setting. A clean install never trips it.

### The install, and what is no longer in it

- **The pilot's built-in layers are gone.** Every layer is now yours, provisioned on demand;
  the product ships no signs, lights, or county datasets of its own, and the catalog's empty
  state names the next step instead of ending in a blank page.
- **Quieter, harder install:** non-root api container, no development source mount in the
  shipped stack, `.env` locked down, storage ownership adopted, the identity realm kept
  coherent on every re-run, each step named in the install log.
- **Nine operator documents ship in `docs/`:** install, upgrade, Talus+, backups and hardening,
  restore, security overview, connecting QGIS / ArcGIS Pro / QField, the API guide, and the
  FAQ. The README describes the two tiers, Talus Community and Talus+, and air-gapped as a
  deployment mode of either.

### Data in

- **CSV joins the import door** alongside Shapefile, GeoPackage, and GeoJSON: coordinate
  columns are detected by family (a latitude/longitude pair or a projected easting/northing
  pair), identifiers with leading zeros survive as text, non-finite values never become
  coordinates, and a searchable coordinate-system picker replaces the code field. After an
  import you stay on the catalog list.
- **Owner-defined fields reach views.** A view exposes the fields its owner defined as typed
  columns on every door, filters on them, and the view builder offers them; a field a view
  depends on cannot be retired or deleted out from under it.
- **A layer-wide attachment listing** (`GET /layers/{id}/attachments`), authenticated and
  bounded, and a **bounded layer statistics door** for aggregates — both part of the
  documented API.

### Maps

- **Organization map defaults** (Admin → Settings → Basemap): a default extent and display
  units, drawn on a map or typed; new maps open there and the embed scale bar reads the
  units. A default basemap for the organization, separate from anyone's theme preference.
- **Point symbols:** an icon picker over a curated open icon set, point size, and a halo;
  icons appear in the legend, in map exports, and in field projects. Layers symbolize on
  their own fields, and owner-defined coded domains resolve in the Styles panel and the
  record drawer.
- **Unique-value styling caps a rebuilt class list at 200**, and descending sorts on
  owner-defined fields no longer lead with blanks.

### Talus+ becomes receivable

The subscription's modules are delivered as a signed bundle; this release ships the door.

- **`scripts/talus-setup.sh --plus <bundle> --licence <file>`** installs or upgrades Talus+ on
  a running deployment: the bundle's signature is verified against the key that ships in this
  tree before anything changes, the previous web app is kept for rollback, the licence lives
  on the storage volume and rides your backups, and the web edge is recreated so the new app
  is served. `docs/talus-plus-runbook.md` is the full procedure, including upgrading an
  existing Community deployment and going back.
- **The verifying key is real from this release on.** Licences and bundles are signed by
  Talus GIS LLC and verified locally; there is no activation server and no phone-home.
- **A licence status door for administrators** reports what is licensed and accepts a
  verified licence file; a file that is malformed, unsigned, or signed by another key is
  refused and nothing changes.

### Fixes

- Single-item restore no longer assumes a development administrator account exists.
- The identity realm imports from a directory mount, so the import file's name and the realm
  name can never disagree.
- CSV type detection makes one decision per column for both the preview and the load; a
  mixed column no longer refuses thousands of rows it had just previewed.
- Point layers with icons split into circle and icon layers by zoom, not by type, so points
  and their popups stop vanishing at the handover; a saved map holds its canvas until its
  basemap has landed.

## v0.9.0-beta.8 (2026-08-25)

Public sharing you can actually turn on.

### The switch is in the portal now
- **Admin → Settings gains the master switch for public access.** Sharing a map or layer to
  Public Access, embedding a public map in another website, and the Open Data catalog with its
  DCAT-US feed (what a site like data.gov harvests) all hang off one switch — and until now that
  switch was a configuration file on the server. Enabling a headline capability meant shell
  access, a text-file edit, and a container restart. Now an administrator turns it on where the
  rest of the organization's policy lives.
- **It takes effect immediately, in both directions — no restart.** Turning it on carries a
  deliberate confirmation that spells out what it means. Turning it off tells you how many items
  are currently shared publicly and stops serving them at once; each item keeps its sharing
  setting, so turning it back on restores them. (Map tiles a visitor's browser has already
  fetched can linger for up to a minute.)
- **Every flip is recorded** in the administrative activity report, with who and when.
- **Upgrades are safe by construction.** A deployment already serving public content keeps
  serving it across this upgrade; a fresh install stays closed until an administrator decides
  otherwise. Deployments that pin the server-side setting keep working unchanged — the portal
  setting simply takes precedence once used.
- **The share dialog now tells the truth about how to proceed.** When public sharing is off, it
  points at Admin → Settings instead of asking you to edit a server environment variable.

### Also in this release (fixes that just missed beta.7)
- **The share dialog explains an unavailable Public tier instead of hiding it.** The tier's row
  is always visible, greyed with the reason — previously it vanished entirely and left
  administrators hunting for a feature that was on screen the whole time in every other state.
- **Re-running the installer no longer promises a credential it will not print.** A re-run over
  an existing install says the administrator password is unchanged, rather than announcing a
  password reveal and then delivering none.

Database changes: one new column on the organization settings row (the public-sharing switch;
empty means "follow the server's configured default", which is what preserves existing
deployments across the upgrade).

## v0.9.0-beta.7 (2026-08-25)

Your first ten minutes, and the user list you live with afterwards.

### The install hands you a credential you can actually find
- **The administrator password is now the very last thing the installer prints.** It used to
  scroll out of view behind the closing notes — and it is the one string in that output you
  cannot recover.
- **Every installer failure names the exact command to resume with**, including the flags you
  originally used. Fixing a failed image pull no longer leaves you guessing what to type next.

### Change your own password, in the portal
- **Profile → Password.** Previously the only way to change a password was the administrator's
  reset dialog, which issues a temporary password and is written for resetting *someone else* —
  confusing when the person you were resetting was yourself.
- Your current password is required, so a stolen browser session cannot lock you out of your own
  account. Repeated wrong attempts warn you before the identity service's lockout engages.
- **The administrator reset dialog now adapts when you reset your own account**, and points you at
  Profile → Password if that is what you actually wanted.

### Users you can retire, and remove
- **Disable now works on every account.** Accounts created by a data import — the ones that exist
  so imported edits have an author, and that can never sign in — could not be disabled at all, and
  the failure reported "user not found" about a person visibly in the list.
- **You can now delete a departed user.** The user list no longer grows forever. Delete is offered
  once an account is disabled, and is refused while that person still owns maps or layers — it
  tells you what they own so you can transfer it first.
- **Deleting removes the sign-in account permanently.** If that person previously edited data,
  their record is kept so the edit history still reads correctly, and **their name is preserved on
  the work they did**. The portal tells you which of the two happened rather than implying an
  erasure that did not occur.
- **Accounts that cannot sign in are now labelled as such**, and are no longer offered password or
  role actions that could never work on them.

Database changes: one new column recording when an account was removed from the directory.

## v0.9.0-beta.6 (2026-08-24)

The first-contact release: everything here came from watching a first-time install fail, then
fixing what it hit.

### Reaching your portal — the whole chain, fixed
- **Renaming the portal now reconciles sign-in automatically.** The installer updates the
  identity service's client configuration to the current portal address on every run — the
  `Invalid parameter: redirect_uri` dead-end after a hostname change is gone.
- **`talus-setup`'s press-Enter option works on every install**, not just the appliance: the
  portal runs at the machine's network address (sign-in on port 8443) with the built-in
  certificate authority. LAN deployments with no DNS name now have a supported path.
- **An install that ends on the local-only default says so loudly** and names the one command
  that fixes it, instead of printing URLs that only work on the server itself.
- **Certificate choices report their outcome.** Choosing automatic public certificates on a
  host the internet can't reach now tells you issuance failed and how to switch to the
  built-in authority — instead of claiming success while serving nothing.
- **A failed image pull names the image and the way out**, and re-running the installer with
  the same flags resumes where it stopped.
- The sign-in page is titled **"Talus GIS"** (previously an internal realm label).

### First map — no more gray void
- **New maps open on USGS imagery until a local basemap is installed**, so a fresh portal has
  spatial context from the first second — and "use last map view" on the Basemap page has
  something to capture. Once your local basemap is installed, new maps open on the standard
  vector look; a map's own saved basemap always wins; air-gapped builds are unaffected.
- **Installing a basemap now reaches already-open map pages** — no reload required.

### Docs
- **`docs/install-runbook.md` ships with the release**: the full terminal-to-signed-in
  procedure — written, tested against a real first-time install, and rewritten from what
  that install taught us. Includes admin-password recovery and the certificate
  two-address gotcha.

No database changes.

## v0.9.0-beta.5 (2026-08-23)

### Upgrades
- **`docs/upgrade-runbook.md` ships with the release**: the standard back-up → overlay →
  re-run-the-installer procedure, per-version crossing notes (including the supervised
  object-store migration for pre-beta.3 installs), and the rules for hand-applied
  migrations. Written from a rehearsed beta.2 → beta.4 upgrade, not from theory.
- **The installer now removes retired service containers.** Upgrading across a release
  that retired a service (the object-store server, in beta.3) no longer leaves its old
  container running — the installer cleans it up by name, and is a no-op on fresh
  installs. Installs upgrading *onto* beta.3/beta.4 artifacts should still run
  `docker compose up -d --remove-orphans` once, per the runbook.

No other platform changes.

## v0.9.0-beta.4 (2026-08-23)

A documentation release: the artifact now carries what an operator needs *in the box*.

### The README is now the product's front door
- Quickstart is the real customer path — clone this repository (or download the release
  tarball) and run `./install.sh`; requirements are Docker and nothing else. Upgrade
  procedure inlined. Every link in the README resolves inside this repository.

### Operator docs ship with the release
- **`docs/dr-restore-runbook.md`** — the complete disaster-recovery restore procedure:
  decrypting encrypted sets, restoring all three databases + media, host-intact and
  host-loss scenarios, new-host reconfiguration, the automated from-zero restore drill,
  and single-item recovery. Includes *"Where your backups are — record this BEFORE the
  disaster."*
- **`docs/dr-hardening.md`** — taking backups from "works on this host" to "survives the
  host": choosing an off-host S3 target, least-privilege keys, immutability, encryption
  key custody (including generating `age` keys for the panel), the `.env` lifeboat, where
  your data lives on the host, and the trustworthy-off-host checklist.

No code changes; the platform is identical to v0.9.0-beta.3.

## v0.9.0-beta.3 (2026-08-23)

### Simpler storage — the bundled object-store server is gone
- **Media, rasters, and backups are now plain files** on the storage volume — one
  less service to run, one less credential set to hold, and your data is browsable
  and rsyncable with ordinary tools. Serving behavior is unchanged (including range
  requests for basemaps); an S3 client survives solely as the *off-host backup*
  path. Storage keys map 1:1 to file paths, so a later move to S3 is a plain copy.
- **⚠ Upgrade note (beta.1/beta.2 installs):** fresh installs are unaffected, but
  existing installs hold data in the removed object-store's volume and must migrate
  it to the storage volume **before** starting this version. The migration is a
  supervised one-time step — contact the project (see `SECURITY.md` for the
  channel) and we will walk it with you; it is backup-gated and verified by
  object-count comparison. Do not delete the old volume until the migration is
  confirmed.

### Choose where backups go — from the portal
- **Admin → Backups → Destination**: point backups at any S3-compatible store
  (Backblaze B2, AWS S3, Wasabi, MinIO, or a NAS with an S3 endpoint) — no
  configuration-file edit, no restart. A **Test connection** button proves reach,
  credentials, *and* write permission before you save; the secret key is stored
  encrypted and never displayed again. Reverting to the deployment default is one
  click, and backups already written stay where they are.
- **Honest-DR labeling throughout**: same-host backups say plainly they are not
  disaster recovery; endpoints require an explicit `https://` (or `http://` for a
  device on your own network, labeled as unencrypted transport).
- **Restore remains a deliberate, documented procedure** — the restore runbook
  gains *"Where your backups are — record this before the disaster."*

### Fixes
- **Raster backups could be silently empty** when the raster bucket name diverged
  from the backup job's default list — the backup now always follows the actual
  configured names. If you store rasters, take a fresh backup after upgrading.
- A fresh install's storage health probe no longer starts red before the first
  upload.
- Basemap archive replacement is now atomic — a reader sees old bytes or new,
  never neither.

### Verification suite
- Grows from 25 to **26 security invariants**: the backup-destination endpoints are
  deny-by-default proven (anonymous and non-admin refused; the secret never appears
  in any response), and the endpoint fence is asserted live. The restore drill and
  environment-artifact checks are fixed to be honest on filesystem storage.

## v0.9.0-beta.2 (2026-08-22)

### Bulk data import
- **Import wizard with background jobs**: load county-scale datasets (300k+ features)
  through the portal — streamed uploads at flat memory, per-job status, CRS selection,
  and attribute mapping. Import permissions are enforced server-side (publisher role
  required) and covered by the verification suite.

### Setup and hostname configuration
- **`talus-setup`**: one-question hostname configuration for appliance deployments —
  *"What hostname will users reach this portal at?"* A real name gives the portal and
  its sign-in sibling (`auth.<name>`) per-name TLS certificates on one address, with a
  choice of the built-in certificate authority (LAN default) or automatic public
  certificates. The command prints the two DNS records to create, DHCP-reservation
  guidance, and CA-trust steps; pressing Enter keeps (or returns to) IP mode at the
  machine's current address — also the supported path when the appliance's IP changes.
  Safe to re-run anytime. See `docs` for the operator guide.

### Network hardening
- **Internal service ports now bind to localhost by default** (database, API, sign-in
  service). The network sees only the web edge — ports 80/443 (plus 8443 in appliance
  IP mode). Local tools and diagnostics on the host are unaffected; deliberate
  re-exposure is a one-line override (`*_PUBLISH` in `.env`).

### Verification suite
- Grows from 23 to **25 security invariants**: import-job authorization gating and
  edge port exposure (no non-edge service published on a public interface) join the
  suite. A portability fix in the media-access check makes it honest on read-only
  installation trees.

### Supply chain
- **Every compose image is now digest-pinned** (the infrastructure images join the
  api image): identical bytes on every install, immune to tag drift.

## v0.9.0-beta.1 — first beta (2026-08-21)

The initial beta of Talus GIS: a self-hosted, open-source (AGPLv3) GIS platform for
municipal and small-organization use — a coherent portal over PostGIS, Keycloak, and
OpenFGA, installed with one command and operated without specialists.

### Platform
- **One-command install** (`./install.sh`): generated secrets, zero hand-edits, clean or
  `--demo` seeded modes, hardened `--edge` profile (TLS via Caddy, strict CSP enforced).
  The api image is pulled from ghcr, digest-pinned; no local build toolchain required.
- **Backups + disaster recovery**: scheduled encrypted backups to object storage,
  admin-editable cadence/retention, granular item restore, and a rehearsable
  restore drill (`scripts/restore-drill.sh`) that proves recovery on an isolated stack.
- **Verification suite** (`scripts/verify-invariants/`): 23 security invariants any
  operator can run against their own install — deny-by-default proven, not promised.
- **Upgrades**: installer-as-upgrader with pre-flight backup gating.

### Portal
- Catalog with search, filters, favorites, sharing tiers (private / groups / org /
  public), ownership transfer, delete protection, ISO metadata, and layer health.
- Web map authoring: multi-layer maps, bookmarks, saved viewpoints, identify popups
  (vector + raster band values), feature search, map export to PDF/PNG.
- Web editing: full toolbar (draw / vertex edit / delete), ArcMap-style snapping with
  exact coordinate preservation, select tools, per-feature edit history, optimistic
  concurrency.
- Open Data portal: anonymous browsing and downloads (CSV, GeoJSON, GeoPackage,
  Shapefile), DCAT-US catalog feed.

### Data
- Hosted vector layers in PostGIS (points, lines, polygons, nonspatial tables) with
  coded domains, contingent values, constraint rules, calculated geometry, join views,
  and filtered view layers.
- Bulk ingest through product doors: GeoJSON, CSV, Shapefile, GeoPackage, GeoParquet —
  county-scale proven (400k+ features).
- Raster store & serve: GDAL→COG ingest, dynamic tiles, WMTS, WCS values, export/clip.
- Feature attachments (photos/files) across web, desktop, and field clients.
- Geocoding: bundled Nominatim default, own-data locators, token-secured external
  locators, batch CSV geocoding to point layers.

### Interoperability
- ArcGIS-compatible REST doors: browse, query, edit, and append from ArcGIS Pro and
  Field Maps-compatible clients (GlobalID-aware applyEdits, attachments).
- QGIS first-class: direct layer access, attachment chute, generated field projects
  (QField) with crew tokens and QR import.
- OGC doors: WMTS, WCS; standard exports everywhere. Your data is never locked in.

### Security
- SSO via Keycloak (IdP brokering), relationship-based authorization via OpenFGA,
  API tokens with scoped service accounts, personal access tokens for field crews.
- Fail-closed by construction; anonymous access is a deliberate, per-item grant behind
  an org-level switch. Full write audit trail with actor attribution.
- Admin: user lifecycle without console access, bulk CSV import, MFA posture,
  system health page, org settings.

### Known limitations (beta)
- Bulk vector import UX is evolving; county-scale loads currently favor the documented
  scripted path.
- No 3D scenes, utility networks, parcel fabric, or heavy imagery analytics — these are
  deliberate non-goals; Talus targets the operational 80% of municipal GIS.
- Single Talus stack per host.
