# Talus+ — install, upgrade from Community, and keep current

**What this is:** the procedure for adding Talus+ to a Talus deployment, whether the deployment
is brand new or has been running Talus Community for a year. It is the same procedure either
way, and it changes very little on your server. This runbook says exactly what.

**Read first:** `install-runbook.md` (a new server) or `upgrade-runbook.md` (keeping a
deployment current). Talus+ rides on top of both.

---

## 1. What you receive

Two things, each delivered to you directly — never fetched by the server:

| Item | Looks like | Where it goes |
|---|---|---|
| **The Talus+ bundle** | `talus-plus-<version>.tar.gz` — a signed archive with the Talus+ api image and web app | Copied to the server; applied by one command (§4) |
| **Your license file** | One `.json` file — your signed license, which names your organization, the modules, and the term. When your subscription includes the AI assists it is an **activation file**: the same signed license plus a one-time AI activation code | Uploaded once at Admin → Talus+ → **License**, or placed by the same command (§4). The license is stored on the storage volume and stays in place through upgrades. **It is not part of a backup set: keep the file Talus sent you**, because a server rebuilt from backups needs it applied again (`dr-restore-runbook.md`, "Talus+ after a restore"). The code is used once and never stored |

**About the activation code.** It works once. On upload, your server exchanges it with Talus's AI
service for this installation's own credential, which is stored encrypted on your server and
never shown. After that the code is dead, so the email it arrived in is worth nothing to anyone
who finds it later. The code expires seven days after it is issued (the date is in the file).
**The license inside does not expire with it**: a late upload still installs the license and turns
Talus+ on, and only the AI step tells you the code has expired. Ask Talus for a fresh activation
file and upload that; nothing else changes.

**The matching bundle.** A bundle is built on the api image of one Talus release, and it
installs only on a release that pins that same image. Normally the names agree:
`talus-plus-<version>` goes with release `<version>`. A release that changes no image, an
installer or documentation fix for example, keeps the previous bundle, and its `CHANGELOG.md`
entry says which one. You do not have to work this out: the installer compares the bundle's
image with the one your release pins and refuses a bundle built for a different release
before anything on the server changes. Keeping the two in step is §7.

Nothing in your license phones home. There is no license server and no remote switch; the
license is verified locally each time it is read, and Talus+ runs without any outside
connection. The one outbound connection Talus+ can make is the AI assists talking to Talus's AI
service (§6) — that is where the one-time code is exchanged — and an air-gapped deployment turns
it off entirely.

## 2. Two starting points, one procedure

- **A new server.** Install Talus Community first, exactly as `install-runbook.md` describes,
  through §5 (signing in for the first time). Then continue here at §3. There is no separate Talus+
  installer; every Talus+ deployment begins as a Community install.
- **An existing Community deployment.** Continue here at §3. Your layers, maps, users, and
  settings are not touched. The tables the Talus+ modules use already exist in every
  Community database, empty, waiting.

## 3. Before you apply — the pre-flight

1. **Back up.** Admin → Backups & recovery → **Back up now**; wait for the new `ok` row. This is
   rule zero for every change to a running deployment, and it makes §9 a two-minute act
   instead of a recovery.
2. **Check which bundle goes with your release:** the top entry of `CHANGELOG.md` in your
   install directory names it when it is not simply the bundle of the same version (§1). The
   installer enforces the match either way.
3. **Copy the bundle and the license to the server**, anywhere readable by the user who runs
   Talus (a home directory is fine). Free disk: a few gigabytes for the image.
4. **Dry-run the verification**, from your Talus install directory:
   ```
   ./scripts/talus-setup.sh --plus /path/to/talus-plus-<version>.tar.gz --check
   ```
   This verifies the bundle's signature and checksums and changes nothing. It must end with
   `bundle OK`. If it does not, §10.

## 4. Apply

From your Talus install directory:

```
./scripts/talus-setup.sh --plus /path/to/talus-plus-<version>.tar.gz --licence /path/to/licence.json
```

`--licence` is optional here: leave it off and add the file afterwards in the portal at
Admin → Talus+ → **License** (§5). Either way the license ends up in the same place. Given an
activation file, the command places the license and tells you to upload the same file in the
portal to turn on the AI assists (§6); the command itself never uses or stores the code.

A minute or two, mostly loading the image. In order, the command:

1. Verifies the bundle's **signature** against the signing key that ships in your release
   tree, then every member's **checksum**. Any mismatch stops it before anything changes.
2. Loads the Talus+ api image and confirms the loaded image is the one the signed bundle
   names.
3. Swaps in the Talus+ web app. **The previous web app is kept** beside it as
   `web/dist.prev-<timestamp>` — that is your rollback (§9).
4. Places `compose.plus.fragment.yml` beside your `docker-compose.yml` and adds one line
   (`COMPOSE_FILE=…`) to `.env` so plain `docker compose` commands include it from now on.
5. Places your license at `talus-licence/licence.json` on the storage volume.
6. Restarts the api, recreates the web edge so the new app is served, and prints the
   entitlement state. The line you want:
   ```
   Talus+ is installed.  Entitlement: LICENSED: ai,dashboards,field,reports,story_maps
   ```
   The list is the modules your license carries, in alphabetical order; yours may be shorter.

**What changed on the server — the complete list.** The api image tag, the web app directory
(previous kept), one environment variable naming the modules, one line in `.env`, and one
license file on the storage volume. **Nothing else:** no database change, no change to the
identity, authorization, tile, or proxy configuration (the web edge container is recreated so it
serves the new app, from the same configuration), no change to any volume that holds your
data.

## 5. Verify

- Sign out and back in. The **New** menu in the top navigation gains **Dashboard**, **Story map**
  and **Report**, the navigation gains a **Field** tab, and Admin gains a **Talus+** group.
- `docker compose ps` shows the api running the Talus+ image named in the fragment.
- `bash scripts/verify-invariants/run.sh` — the security suite still passes on the upgraded
  deployment.

**`UNLICENSED` instead of `LICENSED`?** The bundle applied but no license is in place yet. Sign
in as an administrator: Admin now shows a **Talus+** group with one entry, **License**.
Its status card reads *No licence*; **Upload license or activation file…** takes the file you were
sent, checks the licence's signature on the server, and stores it. The card then names your organization,
the term, and the modules, and the Stories / Dashboards / Reports entries appear on the next page
load. No restart. (The console form works too: re-run the §4 command with `--licence`.) A file
that is malformed, unsigned, or signed by a different key is refused with the reason and nothing
changes. Talus+ features stay hidden until a valid license is present; everything else runs
normally.

Deployments that manage the license as a mounted file (`TALUS_LICENCE_PATH`) see the same status
card with no upload control; replace the mounted file instead.

## 6. The AI assists — on, off, and air-gapped

Talus+ deployments that include the AI assists talk to Talus's own AI service, never to a
model provider directly, and your server never holds a provider credential — only the
credential Talus's AI service issues to this installation.

- **Turn on:** upload your activation file at Admin → Talus+ → **License**. The page installs the
  license and turns on the AI assists in the same step; Admin → **AI** then reads **Connected**.
  Admin → **AI usage** shows percent of allowance and pace; it never shows currency.
  (By hand instead: open the file, copy the `tac_…` code, and paste it at Admin → **AI** →
  **Activate**.)
- **The code has expired, or was already used:** the page says which. Your license is installed
  either way. Ask Talus for a new activation file and upload it.
- **You rebuilt the server:** the credential belongs to the installation that activated it. A
  restore from your backup keeps working; a clean rebuild needs a new activation file from Talus.
- **Turn off:** Admin → **AI** → **Remove AI credential…** → Remove. The Helper is off for the
  deployment; nothing else changes.
- **Air-gapped deployment:** add `TALUS_AI_GATEWAY_URL=disabled` to `.env`, then
  ```
  docker compose up -d api
  ```
  (`docker compose restart` does not re-read `.env`; `up -d` does.) The AI page reads **Not
  available in this environment** and no AI code opens a connection. Every non-AI module runs
  exactly as before. Remove the line and repeat the command to re-enable.
- **Gateway unreachable** on the badge: outbound HTTPS from the server to the AI service is
  blocked or down; the Helper waits, nothing else is affected.

## 7. Keeping current — upgrades

Two steps, both re-runnable, always in this order:

1. **Core first** — `upgrade-runbook.md`, unchanged (back up, unpack the new release, re-run
   the installer with the same flags).
2. **Then the matching bundle:**
   ```
   ./scripts/talus-setup.sh --plus /path/to/talus-plus-<new-version>.tar.gz
   ```
   The license is already in place; do not pass it again unless it changed.

Between the two steps the api keeps running the previous Talus+ image. Do step 2 in the same
sitting.

## 8. Renewal and lapse

A license carries a term. When the term ends, **nothing installed is disabled**: every installed
module keeps running in the version you have. What ends is new releases (security patches
included), support, and the AI assists: the Helper stops until you renew. Administrators see one
plain banner on the Talus+ pages — *Talus+ entitlement expired <date> — installed features
continue to work; new releases and support require renewal* — with an **Upload renewal
license…** button and a link to the License page. Pick the renewed `licence.json` in either
place; the banner clears. No restart, no command.

## 9. Going back to Community

Everything Talus+ added can be undone in place. Your data, including anything created with the
Talus+ modules, stays in the database.

```
mv web/dist web/dist.plus
mv web/dist.prev-<timestamp> web/dist
rm compose.plus.fragment.yml
./scripts/talus-setup.sh
```

The last command asks its one hostname question — answer as you did before (or press Enter
for IP mode). It removes the `COMPOSE_FILE` line, brings the stack back up on the Community
image, and recreates the edge so the Community web app is served. Applying the bundle again
later (§4) restores the modules with their data.

## 10. If something goes wrong

- **`SIGNATURE INVALID`** — the bundle is not one Talus signed, or was altered in transit.
  Do not install it. Re-download from your delivery link; if it repeats, contact support with
  the delivery details.
- **`CHECKSUM MISMATCH`** — a member is corrupted. Re-download.
- **`this release tree is too old for Talus+ bundles`** — your install predates bundle
  signing. Upgrade the core first (§7 step 1).
- **`loaded image id does not match`** — the image tar inside the bundle is not the one the
  signed manifest describes. Treat as a bad download; re-download.
- **`api did not come up`** — `docker compose logs api`. Re-running the §4 command is safe.
- **The web app looks unchanged after applying** — sign out, clear the tab, and reload; if
  it persists, `docker compose --profile edge up -d --force-recreate caddy`.
- **AI page says `TALUS_ENCRYPTION_KEY is not set`** — the api stores the AI credential encrypted and
  needs that key in `.env`. Add it, then `docker compose up -d api` (not `restart`, which does
  not re-read `.env`).
- **`need write access to .env (run with sudo)`** — `.env` belongs to another user, which is how the
  installer leaves it when it was run as root. Run the same command with `sudo`. Nothing was changed.
- **Stuck** — write to `support@talusgis.com`; include the `talus-setup` output and
  `docker compose ps`.
