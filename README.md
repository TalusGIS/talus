# Talus GIS

**An open-source platform that replaces ArcGIS Enterprise for organizations that don't need exotic features.**

Talus GIS is a web GIS you install on your own server. It's built for organizations whose GIS work is collecting data in
the field, publishing web maps with simple editing, and sharing data with staff or the public. It doesn't do Utility
Network, Parcel Fabric, branch versioning or heavy imagery analysis, and it isn't trying to.

It's designed to install in five minutes with sensible defaults, to run without a dedicated GIS administrator, and to be
simpler to run and cheaper to own than ArcGIS Enterprise for the work it covers. It doesn't try to match ArcGIS feature
for feature. The goal is a GIS that is easy to run and costs little to own.

The core is free and open source. A paid edition, Talus+, adds support and a few extra modules (see
[the two editions](#the-two-editions) below).

## Try it first

There's a live demo at [demo.talusgis.com](https://demo.talusgis.com/). It's a real Talus Community server loaded with
a Florida county's open data. You can look at the maps without an account, or sign in with the shared login shown on
its front page to edit two practice layers. That login can't upload data or create layers, and everything you change is
erased at 3 a.m. Eastern each night. Talus+ isn't installed on it.

## Where it stands

This is version 1.0. It has been tested on real municipal data: more than 12,000 signs, over 300,000 address points,
35,000 street segments, and county-wide parcels and aerial imagery. Each release is on the
[Releases](https://github.com/TalusGIS/talus/releases) page, and `CHANGELOG.md` says what changed in each one.

## What it does

**Your data**
- Hosted point, line and polygon layers, and tables with no geometry. You create them when you need them; there's no
  separate publishing step and no fixed number of slots.
- Your own fields, reusable pick lists (coded-value domains), validation rules and default values. They're built on
  ordinary PostgreSQL, with no special expression language to learn.
- Import from Shapefile, GeoPackage, GeoJSON, KML or CSV, or make a one-time copy of an ArcGIS feature layer from its
  URL. Imports as big as a whole county run in the background, you can preview one before it runs, and data is
  reprojected on the way in. Layer designs can be exported and imported too (CSV, GeoPackage, or Esri XML Workspace).
- Keep your data in your local coordinate system, such as State Plane, if you choose.
- Calculate geometry, and attach photos and documents to features.
- Several people can edit at the same time. If two people change the same feature, the second save is stopped rather
  than quietly overwriting the first. Every change is recorded: who made it, when, and what it was.
- Turn editing on or off for each layer, and share a filtered or trimmed version of a layer as a view layer without
  copying the data.

**Maps**
- Build web maps with basemaps (including your own aerial imagery), filters, labels, pop-ups, bookmarks, sketches and
  map notes, and attribute tables.
- Search for features, with partial and approximate matching, and search addresses. Address search can use an outside geocoder or a
  built-in one that works from your own address points with no internet connection.
- Export a map as a PNG picture or a one-page PDF. The PDF is georeferenced, so it opens as a map in Avenza Maps, QGIS
  and Acrobat.
- Aerial imagery and other rasters: upload GeoTIFF, JPEG 2000 or ERDAS IMG files. Talus converts them to
  cloud-optimized GeoTIFF, draws them as map tiles, and lets people clip out an area and download it.

**The portal and sharing**
- One catalog of your layers, tables, imagery and web maps, showing who owns each item, whether it's shared and where
  it's used. Items can be marked as favorites, handed to a new owner, and protected from deletion.
- Share an item with no one, a group, your whole organization, or the public. Public access is off until an
  administrator turns it on (Admin → Sharing & access). If Talus can't confirm that someone is allowed to see
  something, the answer is no.
- Metadata based on ISO 19115, with a check that flags what's missing, and an Open Data page where the public can
  download data as CSV, GeoJSON or Shapefile. It publishes a DCAT-US feed for data catalogs and exports metadata as
  ISO 19139 or QGIS QMD.
- Admin pages for users (single sign-on and local accounts side by side, bulk import from a CSV file, and
  offboarding), service accounts, geocoders, organization settings, usage lists you can download as CSV, backups and
  maintenance.

**ArcGIS Pro and QGIS**
- ArcGIS Pro and QGIS can both read *and edit* Talus layers, attachments included, using a personal access token.
  Talus speaks OGC API Features, WMTS and the Esri FeatureServer format, so there is nothing to publish first.
- One connection file gives Pro or QGIS everything in the catalog that person is allowed to see. A "Use in…" helper
  walks people through it, and data downloads as GeoPackage, Shapefile, CSV or GeoJSON.
- **ArcGIS Pro users:** connect with the connection files or a pasted layer link. Pro's "New ArcGIS Server Connection"
  dialog won't work; it needs Esri's own administration interface, which no non-Esri server has.
- A documented API (`/api/v1`) with service-account tokens, for your own scripts on your own computers.

**Running it**
- Everything runs as one Docker Compose stack on one Linux machine. Photos, imagery and backups are ordinary files on
  disk, with no separate storage server to run.
- Scheduled backups of all three databases plus the photos and files, with optional encryption, an off-site backup
  destination you can test with one button, minimum retention, restore drills and recovery of single items.
  Housekeeping jobs clear out temporary files on their own. See [`docs/dr-restore-runbook.md`](docs/dr-restore-runbook.md) and
  [`docs/dr-hardening.md`](docs/dr-hardening.md).
- Every request is checked for permission, and when a check can't be made the request is refused. The audit trail
  can't be bypassed. Talus connects to its database with only the rights it needs, sends standard browser security
  headers, and starts with strict sign-in settings. A set of tests in `scripts/verify-invariants/` lets you check on
  your own server that access is refused where it should be.
- Every container image is pinned to an exact version, and each release comes with a software bill of materials
  (CycloneDX SBOMs for the source and the images).

**Not in the free version:** dashboards, reports, story maps, the Talus Field app and the AI assists. Those are part of
Talus+. The free version has no AI features and doesn't depend on any service run by Talus GIS.

## Install

You need a Linux machine with Docker and Docker Compose v2, plus four small tools the install uses: `curl`, `jq`,
`python3` and `openssl`. (`scripts/preflight.sh` checks for them and tells you how to install any that are missing.)
Nothing is compiled on your machine: the web app comes prebuilt, and the server image is downloaded from GitHub's
container registry at an exact, pinned version.

```bash
# Get the latest release (or download the release file from the Releases page)
git clone https://github.com/TalusGIS/talus.git
cd talus

# Install: asks who the administrator is and what address people will use, then does the rest
scripts/talus-setup.sh
```

`talus-setup.sh` asks for your name, your email, a password of your choosing for the first administrator account
(`admin`; your email address works to sign in too), and the address people will use to reach the portal (a DNS name,
or press Enter to use this machine's address). Nothing is installed until you've answered every question. Then it
creates the passwords and keys the system needs, starts everything with the portal behind HTTPS, creates your
administrator account and tells you where to browse. You can safely run it again. The full walk-through, including the
first sign-in and the certificate warning, is in [`docs/install-runbook.md`](docs/install-runbook.md).

**Back up your source data before you move it into Talus.** An import is a copy and never changes the original, but the
original (a file geodatabase, a shapefile or a service export) is your way back. Keep it, and keep it backed up, until
you've checked what came across.

**Upgrading:** follow [`docs/upgrade-runbook.md`](docs/upgrade-runbook.md): a backup first, the new release's files over
the old ones, the installer run again, then that version's notes. Read the release's `CHANGELOG.md` entry before you
start; its *Read before upgrading* section says what that version needs, such as a database change, a new image or a
new bundle.

**Backups and recovering from a disaster:** start with [`docs/dr-hardening.md`](docs/dr-hardening.md), which covers
getting backups off the server in a way you can trust, and keep [`docs/dr-restore-runbook.md`](docs/dr-restore-runbook.md)
somewhere a disaster can't reach.

**Questions, bugs and suggestions:** ask in [GitHub Discussions](https://github.com/TalusGIS/talus/discussions), or
email support@talusgis.com. This repository doesn't use GitHub Issues. Every message is read by a person. Bugs are fixed
in point releases rather than set aside, and suggestions get an answer. Never post a security problem in Discussions:
report it privately, as [`SECURITY.md`](SECURITY.md) describes. How support works
for each edition: [`docs/support.md`](docs/support.md).

## The two editions

| Edition | What you get | License |
|---|---|---|
| **Talus Community** | The full GIS: hosted layers and tables, web maps, editing, sharing, ArcGIS Pro, QGIS and QField connections, address search, imagery, backups and restore. No AI. | Open source (AGPLv3): this repository |
| **Talus+** | Everything in Community, plus support, the paid modules (dashboards, reports, story maps, and Talus Field, the field-collection app for phones that works offline), and the AI assists. The assists are a documentation assistant any signed-in user can ask, and a diagnose mode for administrators that reads a snapshot the administrator sees before it's sent. Diagnose mode is built but not yet field-tested; it explains, and never changes anything. Authoring helpers with single-feature Vision capture are on the roadmap and not built yet; they will suggest, never decide. | Commercial subscription |

**Either edition can run with no internet connection at all.** That's a way of installing it, not a separate edition.
A Talus+ server with no internet keeps every module and only loses the AI assists. No AI feature in this release changes
your data or suggests changes to it. Every answer from the Talus Helper is labeled as written by AI and
names the model and the versions of the prompt and documentation behind it.

## How this is built

Talus is built with [Claude Code](https://docs.claude.com/en/docs/claude-code/overview), an AI coding tool, doing the
programming, directed by the project lead. Every significant feature starts with a design discussion and a written
decision before any code is written, and gets a security review; every new way of reaching data also gets tests that
try to get past its access checks. Those tests are in this repository, so you can run them on your own server instead of
taking our word for it.

This repository is published one release at a time. Talus is developed in a private repository, and each release
arrives here as a few commits, so the number of commits here counts releases, not the development history.
`CHANGELOG.md` covers every release since the first beta in August 2026. Before a release is published it passes the
API test suite and a license check of the third-party software it includes, and it's rehearsed as an upgrade on a
running server before it's announced.

## License

Copyright © 2026 Talus GIS LLC. The core is licensed under the GNU Affero General Public License v3 (`LICENSE`).

- **The core** (this repository): [GNU AGPLv3](LICENSE). It's open source and will stay that way. Because the license
  is copyleft, no one can take this code into a closed product. A commercial license is available for organizations
  whose policies rule out the AGPL.
- **Third-party software:** [`NOTICES`](NOTICES), at the top of every release, lists what the server image and the web
  app include and under which licenses. The image carries its own copy at `/usr/share/doc/talus/NOTICES`, and the web
  app ships `third-party-licenses.txt` (linked from the sign-in screen and the account menu). Each GitHub Release
  attaches the complete source of the GPL, LGPL and MPL components and of the Debian packages in the image. How to
  replace an LGPL component and rebuild is in [`docs/replacing-lgpl-components.md`](docs/replacing-lgpl-components.md).
- **Talus+ modules and AI assists:** proprietary, run by Talus GIS LLC.
- **The "Talus GIS" name:** U.S. trademark application pending (Serial No. 50129159), owned by Talus GIS LLC.

Talus doesn't accept outside code contributions. It's maintained by one person on purpose, and like SQLite it doesn't take code
from outside. Bug reports, feature requests and notes on how Talus works with other software are welcome by email. See
[CONTRIBUTING.md](CONTRIBUTING.md).

## Under the hood

For the people who will run it, or want to read the code.

```
┌──────────────────┐   ┌────────────────────┐   ┌──────────────────┐
│ React SPA        │◄─►│ FastAPI portal API │◄─►│ PostGIS          │
│ (MapLibre GL)    │   │ /api/v1 · OGC ·    │   │ (Flyway-managed: │
│                  │   │ WMTS · GeoServices │   │  platform/assets/│
└──────────────────┘   └─────────┬──────────┘   │  media/audit)    │
         ▲                       │              └──────────────────┘
         │              ┌────────┼────────────┐
   ┌─────┴─────┐        ▼        ▼            ▼
   │ Caddy TLS │   ┌─────────┐ ┌────────┐ ┌────────────┐
   │ edge      │   │Keycloak │ │OpenFGA │ │storage vol │
   └───────────┘   │identity │ │authz   │ │media/COGs/ │
                   │SSO/OIDC │ │(fail-  │ │backups     │
                   └─────────┘ │closed) │ └────────────┘
                               └────────┘   + pg_tileserv (MVT)
```

Every way in (the portal, OGC API Features, WMTS, the FeatureServer emulation and `/api/v1`) goes through the same API
and the same permission checks.

**Database schemas:**
- `platform`: users, the layer registry, groups, settings, metadata, tokens, backups
- `assets`: hosted feature and table data, and domain tables
- `media`: photos and feature attachments (the files themselves sit on the storage volume)
- `audit`: a shadow audit table for each table, filled by one shared trigger function

**Built with:** PostGIS · Flyway · FastAPI · psycopg3 · Pydantic v2 · React + TypeScript · MapLibre GL JS · pg_tileserv ·
rio-tiler · Keycloak · OpenFGA · Caddy · Docker Compose · Talus Field (the Talus+ field app, a progressive web app served
by the same portal) · the Anthropic API (Talus+ AI assists only, never in Community).

**What's in this repository:**

```
talus/
├── api/                     # FastAPI portal backend (sign-in, permissions, the API and its OGC/Esri faces)
├── web/                     # React + TypeScript web app (source, plus the prebuilt copy that's served)
├── db/migrations/           # Flyway SQL migrations: the database's full history
├── keycloak/                # Sign-in configuration and the branded login page
├── proxy/                   # Caddy edge (HTTPS, security headers)
├── scripts/                 # install helpers, backups and recovery, talus-setup, verification checks
│   └── verify-invariants/   # Security tests you can run yourself: they try to get in where they shouldn't
├── docs/                    # Guides for people running Talus (more are added as each is reviewed)
├── docker-compose.yml       # The whole stack, every image pinned to an exact version
├── NOTICES                  # Open-source notices for this release (see License above)
└── install.sh               # The installer that scripts/talus-setup.sh runs; start with talus-setup
```

## Project

Talus GIS is operated by **Talus GIS LLC**. Website: [talusgis.com](https://talusgis.com).
