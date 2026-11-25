# Talus — Upgrade runbook

**What this is:** the procedure for moving an existing Talus deployment to a newer release.
Upgrades are designed to be boring: back up, put the new version in place, re-run the
installer with the same flags. This document is that procedure plus the per-version notes
that occasionally matter. Read the target release's `CHANGELOG.md` entry **first** — when a
version needs anything beyond the standard procedure, it says so there.

> **Rule zero: a backup must visibly complete before any upgrade.** Use **Admin → Backups & recovery →
> "Back up now"** and wait for the new `ok` row — or take the dumps by hand (see
> `dr-restore-runbook.md`). If your host is a VM, a hypervisor snapshot on top costs one
> click and makes the whole upgrade trivially reversible. Do this even for "trivial"
> upgrades; the one you skip is the one that bites.

---

## The standard procedure

1. **Back up** (rule zero above). Confirm the `ok` row / non-zero dump before proceeding.
2. **Put the new release in place.** Your install directory is a checkout or unpacked
   tarball of the release; `.env` is the one file that is *yours*.
   - **Git checkout:** `git fetch && git checkout vX.Y.Z`. On a server with Talus+, put the community web
     app back first, or the checkout stops on changed files: `git checkout -- web/dist && git fetch && git
     checkout vX.Y.Z`. Step 5 applies the Talus+ bundle again.
   - **Tarball:** unpack the new release beside the old tree, then overlay it (the release
     tarball is named without the `v`, `talus-X.Y.Z.tar.gz`; `talus/` below is your install
     directory, whatever you called it):
     ```
     mkdir talus-X.Y.Z && tar xzf talus-X.Y.Z.tar.gz -C talus-X.Y.Z --strip-components 1
     rsync -a --delete --exclude '.env' --exclude 'docker-compose.override.yml' \
       --exclude 'compose.plus.fragment.yml' talus-X.Y.Z/ talus/
     ```
     `tar -C` needs the directory to exist, hence the `mkdir`.
     All three excludes are load-bearing. `.env` is your secrets and settings.
     `compose.plus.fragment.yml` exists on Talus+ installs: `.env` names it, so without the
     exclude this step deletes it and the installer in step 3 stops with "no such file" (the
     stack keeps running, and re-applying the plus bundle restores it — but the exclude avoids
     the failure altogether). Boxes without the file are unaffected.
     `docker-compose.override.yml` exists on IP-mode installs (the setup script writes it
     when you install without a DNS name, to publish the sign-in service on `:8443`) — it is *yours*, not the
     release's, and without the exclude this step deletes it and the next upgrade breaks
     sign-in. Boxes without the file are unaffected by the exclude.
3. **Re-run the installer with the same flags you installed with.** Forgotten which?
   `.env` records them: `grep TALUS_INSTALL_MODE .env` (e.g. `clean,edge,tls-internal` ⇒
   `./install.sh --edge --tls-internal`).
   The installer is an upgrader by design: it applies any new database migrations, recreates
   what changed, and **never rotates your admin credential or secrets**.
   The installer also **applies the release's realm settings** to your sign-in
   system (session lifetimes, login theme, password policy — users and clients untouched) and,
   on an IP-mode box, **refreshes the IP-mode overlay** (`docker-compose.override.yml`) from the
   release so it matches the release's Caddyfile. Both are idempotent. If either warns, the
   manual forms are `scripts/apply-realm-settings.sh` and `scripts/talus-setup.sh` (press Enter).
4. **Verify:**
   - `docker compose ps` — everything the release defines is up and healthy, and nothing
     the release *retired* is still running.
   - Sign in; open a layer, a map, and a record with a photo.
   - **Admin → Backups & recovery** loads and shows your existing backup history.
   - Optionally run the shipped verification suite: `scripts/verify-invariants/run.sh` —
     every leg should PASS (a loud SKIP with its reason printed is acceptable; a FAIL is not).

A normal upgrade takes **well under a minute** of installer time; the backup is the slow part.

---

## Per-version notes

### Upgrading to v1.1.2

- **From 1.1.0 or 1.1.1, by the standard procedure above.** No database migration; the upgrade pulls a new api
  image.
- **Talus+ needs the 1.1.2 bundle.** Apply it as step 2 of **Talus+ installs** below; an earlier bundle is refused.

### Upgrading to v1.1.1

- **From 1.1.0, by the standard procedure above.** No database migration.
- **Talus+ needs the 1.1.1 bundle.** Apply it as step 2 of **Talus+ installs** below; the 1.1.0 bundle is refused.
- **No manual edge step.** From this release the installer recreates the edge itself, so the extra command in the
  v1.1.0 note is not needed.

### Upgrading to v1.1.0

- **From 1.0.0, by the standard procedure above.** No database migration; the upgrade pulls a new api image.
- **Talus+ needs the 1.1.0 bundle.** Apply it as step 2 of **Talus+ installs** below. Its `BASE_IMAGE_REF` names this
  release's image; the 1.0.0 bundle is refused.
- **Creating a picklist now needs the publisher or administrator role.** Accounts without it keep using existing
  picklists, and nothing already created changes.
- **Recreate the edge after step 3.** The 1.1.0 installer leaves the running edge on the old Caddyfile, so on a
  server with the portal (`--edge`) run this once after the installer finishes:
  ```
  docker compose --profile edge up -d --force-recreate caddy
  ```
  Talus+ servers can skip it, because applying the bundle recreates the edge. If you skip it on a Community server
  the portal works as before, and only the new header that asks search engines not to list two paths is missing.

### Upgrading to v1.0.0 or later

- **1.0.0 is installed fresh.** A server that ran a 0.9.0 beta is not upgraded in place: install 1.0.0 on a
  clean server (`install-runbook.md`). Upgrades in place, by the standard procedure above, begin with 1.0.0.
- **Why.** 1.0.0 moves the database, the sign-in server, the authorization service and the edge to current
  releases. The database's move changes the operating system under it, and a database created under the old
  one cannot simply be started on the new one.
- **Talus+ needs the 1.0.0 bundle.** Its `BASE_IMAGE_REF` names this release's image; an older bundle is
  refused.

### General notes

- **`.env` changes and the api:** if an upgrade's notes ever direct you to edit `.env`,
  recreate the api afterwards (`docker compose up -d --force-recreate api`) — a plain
  `restart` does not re-read `.env`.
- **New `.env` keys:** a new version may need settings your `.env` predates. The installer
  appends the ones it can safely generate, and **prints a warning with remediation steps**
  for any it must not invent (a value that has to match existing state). Read the
  installer's output — a printed warning is an action item, not noise.
- **Downgrades are not supported.** The path backwards is your pre-upgrade backup (and
  hypervisor snapshot, if you took one) — which is why rule zero exists.
- **Before and after:** `scripts/talus-doctor.sh` (read-only) prints a one-page report — services,
  disk, clock, edge, certificate, migration head, last backup. Run it before you start, so you know
  the install was healthy going in, and again at the end; a FAIL line says what to do.

---

## If you apply database migrations by hand

The installer applies migrations for you and recreates the services that need it — prefer
it. If you instead run `docker compose run --rm flyway migrate` directly, two rules:

1. **Back up first** (rule zero), and confirm the new version row in `flyway_schema_history`
   shows `success = t` afterwards.
2. **★ If a migration adds new layer slots** (new `assets.tal_…` tables and their tile
   functions), **restart the tile server**: `docker compose restart pg_tileserv`. It
   discovers its function catalog at startup and never re-scans — brand-new tile functions
   it has never seen return 404s, so features on the new slots **silently never render**
   (no error anywhere; the map is just empty). The installer path is immune — it recreates
   pg_tileserv on every run — but a hand-applied migration is not. (Slots merely changing
   geometry type or CRS are fine; only *new* functions need the re-scan.)

## Talus+ installs

A Talus+ install upgrades in two steps, both re-runnable:

1. **Core first** — the standard upgrade above, unchanged. (The release tree carries the
   migrations and compose; your plus fragment pins the api image, so the api keeps running
   the previous Talus+ image until step 2.)
2. **Then the matching plus bundle** — every Talus+ bundle targets exactly one core release
   (`<core-version>-plus`); apply the one that matches the release you just upgraded to:

       ./scripts/talus-setup.sh --plus /path/to/talus-plus-<version>.tar.gz

   Checksums are verified before anything changes; the previous web app is kept beside the
   new one (`web/dist.prev-<stamp>`) for a two-`mv` rollback; your license and data are not
   touched by either step. `--check` dry-runs the verification alone.

**To confirm:** step 2 ends with `Talus+ is installed.  Entitlement: LICENSED: …`, followed by the
modules your license carries (`talus-plus-runbook.md` §4).
