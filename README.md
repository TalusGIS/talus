# Talus GIS

**An open-source platform that replaces ArcGIS Enterprise for the municipal departments that don't need exotic features.**

Talus GIS is an Open Core platform built for the ~80% of municipal GIS users whose workflows are field data collection, web maps with simple editing, and sharing — not Utility Network, Parcel Fabric, branch versioning, or heavy imagery analytics. It's designed to be installable in five minutes with sensible defaults, operable without a dedicated GIS admin, and simpler to run and cheaper to own than ArcGIS Enterprise for the workflows it covers.

The competitive frame is **operational simplicity and total cost of ownership** — not feature parity.

## Status

**Version 1.0.** The core is complete, validated against real municipal data (12,000+ signs, over 300,000 address points, 35,000 street segments, county-scale parcels and orthoimagery), and ships as versioned releases with digest-pinned images and per-release SBOMs — see the [releases](https://github.com/TalusGIS/talus/releases) and `CHANGELOG.md`. Highlights, by area:

**Portal & governance**
- Catalog of layers, standalone tables, rasters, and web maps — ownership, favorites, transfer, delete protection, at-a-glance sharing status, where-used cross-references
- Sharing model: private / group / organization / public, enforced fail-closed through OpenFGA; groups management UI; anonymous public access off until an administrator turns it on (Admin → Sharing & access)
- ISO 19115-cored metadata with a completeness gate, and a curated **Open Data portal** (anonymous downloads in CSV/GeoJSON/Shapefile, DCAT-US feed, ISO-19139/QMD export)
- Admin: user management (SSO + local, bulk CSV import, offboarding), service accounts, geocoder management, org settings, usage listings (content, sharing, members; CSV download), backups, maintenance

**Data & editing**
- Hosted point/line/polygon layers and geometry-less tables, provisioned on demand — no slot pools, no publish step
- Owner-defined fields, reusable coded-value domains, declarative constraint rules and defaults — all on PostgreSQL primitives, no proprietary expression engine
- Schema import/export (CSV, GeoPackage, Esri XML Workspace), bulk feature import through the portal (Shapefile/GeoPackage/GeoJSON/KML/CSV, or a one-time copy of an ArcGIS feature layer by URL; county-scale, background jobs) with dry-run preview and reprojection
- Working-CRS support with opt-in native projected storage (State Plane and friends), geometry calculation, feature attachments (photos/documents) on one storage authority
- Editing with optimistic concurrency (`row_version`/`If-Match`), per-row audit trails, per-layer editing toggle; view layers as sanitized shareable lenses

**Mapping**
- Web map authoring: basemaps (including self-hosted imagery), per-layer filters, labels, configurable popups, bookmarks, sketch/map notes, docked attribute tables
- Feature search (fuzzy/wildcard) and address geocoding — external locators plus a fully air-gapped internal geocoder over your own address points
- Map export: PNG screenshot and a one-page PDF print, client-side — the PDF is georeferenced (ISO 32000 geospatial PDF), so it opens as a map in Avenza Maps, QGIS and Acrobat
- Raster store & serve: GeoTIFF/COG, JPEG 2000 and ERDAS IMG uploads converted to COG, dynamic tiles, clip-and-download export

**Interoperability (no publish step — every surface is a façade over one authz-gated API)**
- OGC API Features, WMTS, and GeoServices (FeatureServer) emulation: **ArcGIS Pro and QGIS both read *and edit* Talus layers, including attachments**, with personal access tokens
- Deployment-level connection files (one credential-free file exposes the whole viewable catalog in Pro or QGIS), goal-first "Use in…" wizard, GeoPackage/Shapefile/CSV/GeoJSON exports
  — **Pro users: connect via the OGC/WMTS connection files or a pasted layer link, *not* Pro's "New ArcGIS Server Connection" dialog** (it requires Esri's proprietary Admin API and fails against any non-Esri server)
- Documented `/api/v1` with scoped service-account tokens for scripting — bring your own compute

**Operations & trust**
- One `docker compose` stack: PostGIS, FastAPI, Keycloak, OpenFGA, pg_tileserv, Caddy TLS edge — media/rasters/backups are plain files on a storage volume (no object-store server to run)
- Scheduled backups of all three databases + media, with optional client-side encryption and an admin-settable off-host destination with a connection test, retention floors, restore drills, single-item recovery, self-maintenance jobs — see [`docs/dr-restore-runbook.md`](docs/dr-restore-runbook.md) and [`docs/dr-hardening.md`](docs/dr-hardening.md)
- Security: fail-closed authorization everywhere, un-bypassable audit triggers, least-privilege runtime DB role, security headers, hardened realm defaults, and a **negative-test suite of security invariants** (`scripts/verify-invariants/`) you can run against your own install
- Every compose image digest-pinned; CycloneDX SBOMs (source + image) attached to each release

Not in the open-source core by design: the Talus+ modules (dashboards, reports, story maps and Talus Field) and the AI assists of Talus+ (see below). The core carries no AI features and depends on no service run by Talus.

## Quickstart

Requirements: a Linux host with Docker (Compose v2), plus the small tools the install scripts use — `curl`, `jq`, `python3`, `openssl` (`scripts/preflight.sh` names any that are missing, with the install command). Nothing is compiled on your machine: the web frontend ships prebuilt, and the api image is pulled digest-pinned from ghcr.

```bash
# Get the latest release (or download the release tarball from the Releases page)
git clone https://github.com/TalusGIS/talus.git
cd talus

# Install: asks who the administrator is and what address people will use, then does the rest
scripts/talus-setup.sh
```

`talus-setup.sh` asks for your name, your email, a password of your own choosing (for the first administrator account, `admin` — your email signs in too), and the address your users will reach the portal at (a DNS name, or Enter for this machine's address). Nothing is installed until every question is answered; then it generates the secrets, brings up the stack with the web portal behind TLS, creates your administrator account and prints where to browse. It is safe to re-run. The full walk-through, including the first sign-in and the certificate prompt, is [`docs/install-runbook.md`](docs/install-runbook.md).

**Back up your source data before you move it into Talus.** An import is a copy and never changes the source, but the original, whether a file geodatabase, a shapefile or a service export, is your way back. Keep it, backed up, until you have checked what came across.

**To upgrade an existing deployment:** follow [`docs/upgrade-runbook.md`](docs/upgrade-runbook.md) — a backup first, the new release's files over the old, the installer re-run, then the version's notes. Read the release's `CHANGELOG.md` entry first — its *Read before upgrading* section says what that version needs (a migration, a new image, a bundle).

**Backups & disaster recovery:** start with [`docs/dr-hardening.md`](docs/dr-hardening.md) (getting backups off-host, trustworthily) and keep [`docs/dr-restore-runbook.md`](docs/dr-restore-runbook.md) where a disaster can't take it.

**Bugs and suggestions:** email support@talusgis.com. This repository does not use GitHub Issues. Every message is read by a person. Bugs are fixed in point releases, not parked; suggestions get an answer. How support works for each edition: [`docs/support.md`](docs/support.md).

## Architecture

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

**Database schemas** (four-schema layout):
- `platform` — users, layers registry, groups, settings, metadata, tokens, backups
- `assets` — hosted feature/table data + domain tables
- `media` — photos and feature attachments (bytes as files on the storage volume)
- `audit` — per-table shadow audit tables via a generic trigger function

**Tech stack:** PostGIS · Flyway · FastAPI · psycopg3 · Pydantic v2 · React + TypeScript · MapLibre GL JS · pg_tileserv · rio-tiler · Keycloak · OpenFGA · Caddy · Docker Compose · Talus Field (the Talus+ field app, a PWA served by the same portal) · Anthropic API (Talus+ AI assists only, never in Community).

## Two Tiers, Either One Air-Gapped

| Tier | Includes | Licensing |
|---|---|---|
| **Talus Community** | The full platform: hosted layers and tables, web maps, editing, sharing, desktop and field interop, geocoding, imagery, backups and restore. No AI. | Open source (AGPLv3) — this repository |
| **Talus+** | Everything in Community, plus support, the paid modules (dashboards, reports, story maps, and Talus Field — the offline-capable field collection app for phones), and the AI assists: a documentation assistant any signed-in user can ask, and a diagnose mode for administrators that reads a snapshot the administrator previews before it is sent (diagnose mode is built and not yet field-tested; informational, never actuating), and, on the roadmap and not yet built, authoring helpers with single-feature Vision capture (helpful, never authoritative) | Commercial subscription |

**Air-gapped is a deployment mode, not a tier.** Either tier runs with no outbound connectivity — first-class, not degraded. An air-gapped Talus+ keeps every non-AI module and simply has no AI. No AI feature in this release writes or proposes data; every answer from the Talus Helper is labeled as AI-generated and names the model and the prompt and documentation versions behind it.

## Repository Layout

```
talus/
├── api/                     # FastAPI portal backend (auth, authz, routers, façades)
├── web/                     # React + TypeScript SPA (source + the prebuilt dist that serves)
├── db/migrations/           # Flyway SQL migrations — the schema's full history
├── keycloak/                # Realm config + branded login theme
├── proxy/                   # Caddy edge profile (TLS, security headers)
├── scripts/                 # install helpers, backups/DR, talus-setup, verify-* sweeps
│   └── verify-invariants/   # Security invariant suite — negative tests you can run yourself
├── docs/                    # Operator guides (growing set — each published after review)
├── docker-compose.yml       # The whole stack, every image digest-pinned
├── NOTICES                  # Open-source notices for this release (see License below)
└── install.sh               # The installer that scripts/talus-setup.sh drives — start with talus-setup
```

## How this is built

Talus is developed with [Claude Code](https://docs.claude.com/en/docs/claude-code/overview) as the primary implementer, directed by the project lead, under a decision-doc-before-code discipline: every substantive capability gets a design conversation and a written decision document before implementation, plus a security review and negative tests for every new data path. The verification suite in this repository is that discipline's public face — deny-by-default proven on your install, not promised.

This repository is published one release at a time. Talus is developed in a private working repository, and each release lands here as a small number of commits, so the commit count is the release count, not the development history. `CHANGELOG.md` records every release since the first beta in August 2026. Every release passes the API test suite before it is published and a third-party licence check in the release pipeline, and is rehearsed as an upgrade on a running server before it is announced.

## License

Copyright © 2026 Talus GIS LLC. The core is licensed under the GNU Affero General Public License v3 (`LICENSE`).

- **Platform core** (this repository): [GNU AGPLv3](LICENSE) — genuinely open source, and it stays that way; copyleft means no one can absorb this code into a closed product. Commercial licensing is available for organizations whose policies preclude AGPL.
- **Third-party software:** [`NOTICES`](NOTICES) at the root of every release lists what the api image and the web app include and under which licenses; the image carries its own copy at `/usr/share/doc/talus/NOTICES`, the web app ships `third-party-licenses.txt` (linked from the sign-in screen and the account menu), and each GitHub Release attaches the complete source of the GPL, LGPL and MPL components and the Debian packages in the image. Replacing an LGPL component and rebuilding is documented in [`docs/replacing-lgpl-components.md`](docs/replacing-lgpl-components.md).
- **Talus+ modules and AI assists**: proprietary, operated by Talus GIS LLC
- **"Talus GIS" trademark**: U.S. trademark application pending (Serial No. 50129159), owned by Talus GIS LLC

Talus does not accept outside code contributions (solo-maintained by design — the SQLite model); bug reports, feature requests, and interop findings are genuinely welcome by email. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Project

Talus GIS is operated by **Talus GIS LLC**. Domain: [talusgis.com](https://talusgis.com).
