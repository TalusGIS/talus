# Talus — Disaster-recovery restore runbook

**What this is:** the documented, backup-gated procedure to restore Talus from a backup taken by its
DR subsystem. Restore is the one intentionally-destructive verb — it **overwrites live data** — so it
is a deliberate operator procedure, **not** an in-product button. Read the whole thing before you start.

> **HARD RULE: a fresh `pg_dump -Fc` of the CURRENT state must visibly complete BEFORE any
> restore.** Even when you're restoring *because* the current state is bad, capture it first — a
> restore that goes wrong with no fallback is how a bad day becomes a catastrophe. Confirm the dump file
> exists and is non-zero before proceeding.

---

## What a backup set contains

Each backup writes to the backup target (the `talus-backups` folder on the storage volume by default, or your
off-host S3 destination — set in **Admin → Backups & recovery → Destination**, or via `BACKUP_S3_*` in `.env`).

Talus keeps its state in three databases and on the storage volume, so a backup set contains **three database
dumps + the stored files**:

- **`db/talusgis-<stamp>.dump`** — the app DB: every layer, feature, user record, metadata row, **and the
  audit log**.
- **`db/keycloak-<stamp>.dump`** — the **Keycloak (identity)** DB: the actual user accounts/credentials people
  sign in with. *Without this, restored users can't log in.*
- **`db/openfga-<stamp>.dump`** — the **OpenFGA (authorization)** DB: every sharing relationship (who can see
  which layer/map, group membership, public flags). *Without this, the access graph is gone and — because
  Talus fails closed — nothing is reachable.*
- **`media/talus-media/…` and `media/talus-rasters/…`** — an **incremental, append-only** copy of the files on
  the storage volume: attachments, photos and the downloaded basemap under `talus-media`, imagery under
  `talus-rasters`. Each file is archived once and never edited there, so this archive is a **superset** of
  whatever any retained dump references. A field project's file gets a new name with each version, so every
  backup holds the version its database rows describe. **The one exception is the basemap**, which is replaced
  in place: a restore brings back the first basemap ever backed up. After a restore, fetch it again from
  **Admin → Basemap**; on an air-gapped server, upload the file you keep for it. A restore copies **both**
  folders back.

The **Admin → Backups & recovery** page lists each set with sizes. **Download** saves the set's database dumps
(up to three files: the Talus database, sign-in, and authorization). The stored files are not downloaded from the
page; they are copied from the backup target itself — a folder copy when the target is the default, your S3 client
when it is off-host (see "Restoring media"). **A `talusgis`-only restore loses logins and all sharing** — restore
**all three** databases for a real recovery.

**Not in a backup set** — keep these yourself:

- **`.env`**, unless you turned on the encrypted `.env` option (next box).
- **The Talus+ license file and the Talus+ bundle.** Keep the files Talus sent you; a rebuilt server needs them
  applied again ("Talus+ after a restore").
- **The edge's certificates and its built-in certificate authority.** A rebuilt server that uses the built-in
  authority creates a new one, and each device must trust the new root, as at first install.
- **The install directory** (the release files). Unpack the release again.

> **Your `.env` — the master key.** A restore needs it: the OpenFGA store ID in it (`OPENFGA_STORE_ID`) must
> match the restored OpenFGA data, and the database passwords and Keycloak secrets must match. **Restore the
> OpenFGA database before the installer runs.** The installer loads the authorization model on every run. With
> the restored database in place it finds your existing store and keeps its ID. Run against an empty database it
> creates a new store and writes that new ID into `.env`; once you then restore the OpenFGA database, that ID no
> longer exists and the api will not start until the installer is run again.
>
> By default `.env` is **NOT** in a backup — keep your own copy safe, separately. **If you turned on the
> `.env` "lifeboat"** (Admin → Backups & recovery → Edit settings → "Include the .env file (lifeboat)", which needs encryption to be on), the
> set *also* contains an **`age`-encrypted** `.env` (`env/.env-<stamp>.age`) — so one off-host secret (the
> `age` private key) recovers everything. Decrypt and use it as the master key — see *"Optional: use the
> backup's own `.env`"* below. (The `age` private key is never in a backup, so you still hold exactly one
> off-host secret — just one instead of two.)
>
> **`TALUS_ENCRYPTION_KEY` rides with the master key.** Admin-stored credentials (geocoder tokens,
> the off-host backup secret key) live in the DB as ciphertext only that key can open — that is
> deliberate (a backup alone never contains a usable credential). Restore with the **same** `.env` and
> they just work. Restore with a **different/lost** key and those locators show **"credential
> unavailable"** in Admin → Geocoders and are skipped from address search (everything else is
> unaffected) — the fix is to re-enter each token in the UI, not to touch the DB.

---

## Where your backups are — record this BEFORE the disaster

The destination can be set two ways, and a restore on a dead or new host can't ask the portal which one
was live:

- **Portal-set:** Admin → Backups & recovery → Destination — stored in the database,
  wins over env. The panel shows the endpoint, bucket, and access key; destination changes are also in the
  admin audit log (`backup_destination_updated`).
- **Env-set:** `BACKUP_S3_*` in `.env` (the deployment default when no portal override exists).

**Write down, somewhere that survives the host: the endpoint URL, the bucket, the access key, and the
secret key.** Talus stores the secret encrypted and **can never display it back** — the copy in your own
password vault is the one a from-zero restore will use. Every catalog row also records the target it was
written to, so the Admin → Backups & recovery table tells you where each existing set lives.

If the destination was portal-set, the restore/drill scripts (which read env) need it **passed
explicitly** — export `BACKUP_S3_ENDPOINT` / `BACKUP_S3_ACCESS_KEY` / `BACKUP_S3_SECRET_KEY` (and
`BACKUP_BUCKET` if customized) in the shell before running them, using the values you recorded.

Changing the destination **starts fresh** at the new location: existing backups stay where they were
written (retention never prunes an old location), and their catalog rows keep the old target label.
If you ever need them, copy them from the old location yourself: with your S3 client if it was an S3
destination, or from the `talus-backups` folder on the storage volume if it was the default.

## If backups are encrypted — decrypt first

If encryption recipients were configured (the panel's **Encryption recipients**, or
`BACKUP_AGE_RECIPIENTS` in `.env`), every artifact is stored as **ciphertext** with a **`.age`** suffix
(the **Admin → Backups & recovery** page shows "encryption 🔒 on"). The running Talus system **cannot** decrypt
them — that's the point. Decryption happens here, with the **`age` private key you kept off-host**:

```
# the three dumps (downloaded as <name>-<stamp>.dump.age):
age -d -i /path/to/key.txt -o talusgis-<stamp>.dump talusgis-<stamp>.dump.age
age -d -i /path/to/key.txt -o keycloak-<stamp>.dump keycloak-<stamp>.dump.age
age -d -i /path/to/key.txt -o openfga-<stamp>.dump  openfga-<stamp>.dump.age
```

Every file in the media archive is stored the same way, as `<name>.age`. The loop that decrypts them is in
"Restoring media", step 2. Files archived before encryption was turned on are stored unencrypted and need no
decrypting.

Any one of the configured recipients' private keys works (you encrypted to a primary + a break-glass key).
**If every private key is lost, the backups are unrecoverable** — that is the cost of confidentiality.
Decrypt **all three** dumps (talusgis / keycloak / openfga) before restoring.

---

## Optional: use the backup's own `.env` (the lifeboat)

If you enabled the lifeboat, the set carries an encrypted `.env`. Use it as the master key instead of an
operator-preserved copy — it is point-in-time-consistent with the dumps (its OpenFGA IDs match the restored
tuples by construction). On a server that still runs, download it from the backup's row on
**Admin → Backups & recovery** (the **"🛟 .env (encrypted)"** button, separate from **Download**). Otherwise copy
`env/.env-<stamp>.age` from the backup target. Then decrypt it with the **same off-host private key**:

```
age -d -i /path/to/key.txt -o .env .env-<stamp>.age      # -> the exact .env from that snapshot
```

On a **new host**, put this `.env` in the install directory before you start Scenario B. The installer writes
the address settings for the new location; you do not edit them by hand — see *"New-host reconfiguration"*
below. To rehearse a restore that uses the backup's own `.env` in place of your current one, run
`./scripts/restore-drill.sh --env-from-backup --age-key key.txt`.

---

## Restoring all three databases (the core of any full recovery)

With the services stopped as your scenario says, restore each dump into **its own** Postgres container.
Each database is **dropped and created empty first**. Restoring over a database that still holds data leaves
it half today's and half the backup's, because a table made after the backup keeps the restore from replacing
the tables it depends on. (`--no-owner` tolerates differing role names.)

```
# 1) talusgis (data + audit) -> the app Postgres
docker compose cp talusgis-<stamp>.dump postgres:/tmp/r.dump
docker compose exec -T postgres dropdb --force -U talus talusgis
docker compose exec -T postgres createdb -U talus talusgis
docker compose exec -T postgres psql -U talus -d postgres -c 'ALTER DATABASE talusgis SET search_path = "$user", public, topology, tiger'
docker compose exec -T postgres pg_restore --clean --if-exists --no-owner -U talus -d talusgis /tmp/r.dump

# 2) Keycloak (identity/logins) -> keycloak-postgres
docker compose cp keycloak-<stamp>.dump keycloak-postgres:/tmp/r.dump
docker compose exec -T keycloak-postgres dropdb --force -U keycloak keycloak
docker compose exec -T keycloak-postgres createdb -U keycloak keycloak
docker compose exec -T keycloak-postgres pg_restore --clean --if-exists --no-owner -U keycloak -d keycloak /tmp/r.dump

# 3) OpenFGA (authorization/sharing) -> openfga-postgres
docker compose cp openfga-<stamp>.dump openfga-postgres:/tmp/r.dump
docker compose exec -T openfga-postgres dropdb --force -U openfga openfga
docker compose exec -T openfga-postgres createdb -U openfga openfga
docker compose exec -T openfga-postgres pg_restore --clean --if-exists --no-owner -U openfga -d openfga /tmp/r.dump

docker compose exec -T postgres rm -f /tmp/r.dump; docker compose exec -T keycloak-postgres rm -f /tmp/r.dump; docker compose exec -T openfga-postgres rm -f /tmp/r.dump
```

The user and database names above (`talus`/`talusgis`, `keycloak`/`keycloak`, `openfga`/`openfga`) are the ones
every install is created with. If your `.env` sets `POSTGRES_USER`, `POSTGRES_DB`, `KEYCLOAK_DB_USER`,
`KEYCLOAK_DB_NAME`, `OPENFGA_DB_USER` or `OPENFGA_DB_NAME` to something else, use your values.

**Read what each `pg_restore` prints.** Lines ending `does not exist, skipping` are expected on an empty
database. A line containing `ERROR`, or `errors ignored on restore`, means that database did not restore
completely, even though the command finished. Stop there and find the cause before you start any service.

Then continue with your scenario's next step.

---

## Restoring media (attachments, photos, the basemap, imagery)

The backup's `media/` folder holds two folders, one per storage area: `talus-media` and `talus-rasters`. Restore
**both**. The api must be running, because the files go into its storage volume.

**Short path — same host, default backup target, backups not encrypted.** One command, run as the api's own user:

```
docker compose exec api sh -c 'cp -R /storage/talus-backups/media/. /storage/'
```

**Every other case** (off-host target, encrypted backups, or a new host):

1. **Get the archive into a working folder on the host.**
   ```
   mkdir -p ~/talus-restore/media
   # default target, same host:
   docker cp talusgis-api:/storage/talus-backups/media/. ~/talus-restore/media/
   ```
   Off-host target: copy the bucket's whole `media/` prefix into `~/talus-restore/media/` with your S3 client.
   With the AWS CLI's container image, after setting `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` in your
   shell, run as your own user so the files are yours:
   ```
   docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
     -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_DEFAULT_REGION=us-east-1 \
     -v ~/talus-restore:/out amazon/aws-cli --endpoint-url <endpoint> \
     s3 sync s3://<bucket>/media/ /out/media/
   ```
2. **Decrypt, if the backups are encrypted** (needs `age` on this machine and your private key):
   ```
   find ~/talus-restore/media -type f -name '*.age' | while IFS= read -r f; do
     age -d -i /path/to/key.txt -o "${f%.age}" "$f" && rm -f "$f"
   done
   ```
3. **Copy both folders into the storage volume, then hand them to the api's user:**
   ```
   docker compose cp ~/talus-restore/media/. api:/storage/
   docker compose run --rm --no-deps --user 0 --entrypoint sh api -c 'chown -R 10001:10001 /storage'
   ```
   Do not skip the second command. Files copied in from the host belong to root, and the api does not run as
   root: without it the portal can show the restored files, but new uploads into the restored folders can fail
   while every service still shows as running.
4. **Remove `~/talus-restore`** once the restore is verified. If you decrypted, it holds readable copies of
   your files.

---

## Scenario A — restore into the existing stack (host intact)

Use when the database is corrupt or data was wrongly deleted/changed, but the host + storage volume are fine.

1. **Capture the current state first — all three databases:**
   ```
   mkdir -p ~/talusgis-backups
   STAMP=$(date '+%Y%m%d-%H%M%S')
   docker exec talusgis-postgres pg_dump -Fc -U talus -d talusgis > ~/talusgis-backups/pre-restore-talusgis-$STAMP.dump
   docker exec talusgis-keycloak-postgres pg_dump -Fc -U keycloak -d keycloak > ~/talusgis-backups/pre-restore-keycloak-$STAMP.dump
   docker exec talusgis-openfga-postgres pg_dump -Fc -U openfga -d openfga > ~/talusgis-backups/pre-restore-openfga-$STAMP.dump
   ls -l ~/talusgis-backups/pre-restore-*-$STAMP.dump   # confirm all three are non-zero
   ```
2. **Get the dumps** — in **Admin → Backups & recovery**, **Download** on the row saves its three dumps (or copy
   `db/talusgis-…`, `db/keycloak-…`, `db/openfga-…` from the backup target). Decrypt them if encrypted (above).
3. **Stop everything that uses the databases**, so nothing writes or holds old data during the restore:
   ```
   docker compose stop api pg_tileserv keycloak openfga
   ```
4. **Restore all three databases** — see **"Restoring all three databases"** above (talusgis + keycloak +
   openfga). A talusgis-only restore here would bring data back but leave you with no logins and no sharing.
5. **Start the services again:**
   ```
   docker compose up -d keycloak openfga api pg_tileserv
   ```
6. **Restore the stored files** — only if the storage volume also lost data. See **"Restoring media"** above; it
   needs the api running, which is why it comes after step 5.
7. **Verify** — see below.

---

## Scenario B — rebuild on a new host (host loss)

Use when the original host is gone. **This only works if your backups were stored OFF-HOST** (the backup
destination pointed at external S3 / another machine). Same-host backups die with the host — this is exactly
why the panel warns when you're same-host.

You need on the new host: Docker (`install-runbook.md` §1), the Talus release you were running when the backup
was taken, your `.env`, the backup set, and — if your backups are encrypted — `age` and your private key.

**The order matters: the databases are restored before the installer runs.**

1. **Unpack the release and put your `.env` in it.** Unpack as in `install-runbook.md` §2, then copy your
   `.env` into that folder: the copy you kept, or the backup's own (`env/.env-<stamp>.age` in the backup target,
   decrypted with your private key). **Do not run `scripts/talus-setup.sh` yet.** Without your `.env` in place it
   would begin a new install, with new secrets that match nothing in your backup.
   *Talus+ servers:* open `.env` and delete the line that begins `COMPOSE_FILE=`. It names a file that is not on
   this machine yet, and every `docker compose` command stops until the line is gone. Step 7 writes it back.
2. **Fetch the backup set** from your off-host target into a working folder, with your S3 client: the three
   dumps that share one `<stamp>` — `db/talusgis-<stamp>.dump`, `db/keycloak-<stamp>.dump`,
   `db/openfga-<stamp>.dump` (each ends `.age` if encrypted) — and the whole `media/` prefix. Decrypt the dumps if
   they are encrypted (above).
3. **Start the databases only, empty:**
   ```
   docker compose up -d postgres roles-init keycloak-postgres openfga-postgres
   docker compose ps -a
   ```
   Wait until the three database services show `healthy` and `roles-init` shows `Exited (0)`.
4. **Restore all three databases** — "Restoring all three databases" above.
5. **Run the installer:**
   ```
   scripts/talus-setup.sh
   ```
   Because your `.env` is already there, it asks only where the portal lives (and, for a DNS name, whether to
   request a public certificate). Same name or address as before: answer as you did at first install. A new name
   or address: give the new one, and read "New-host reconfiguration" below. It keeps the secrets in your `.env`,
   starts sign-in, authorization, the api, the tile server and the edge on the restored databases, applies any
   database migrations newer than the backup, and points the portal's sign-in at the address you gave. Existing
   accounts and passwords are not changed.
6. **Restore the stored files** — "Restoring media" above. Put the `media/` folder you fetched in step 2 at
   `~/talus-restore/media/` and continue from that section's step 2.
7. **Talus+ servers:** apply the bundle and your license — "Talus+ after a restore" below.
8. **Verify** — below.

> **What has been rehearsed and what has not.** Steps 3, 4 and 6 are the steps `scripts/restore-drill.sh` runs,
> in this order, into empty databases. Step 5 is not: the drill starts the services directly, on the same machine
> as your live server, without the installer, the edge or a hostname. What the installer does in step 5 is what
> it does on every upgrade, but this whole sequence on a second machine is not run by any script Talus ships.
> Rehearse it on a spare machine before you depend on it.

---

## New-host reconfiguration — migrate (new identity) vs rebuild-in-place (same identity)

Scenario B above covers two situations that differ in **one** thing: whether the server *identity* changes. The
`.env` you restore with splits accordingly:

- **Carried as it is (the master key)** — `OPENFGA_STORE_ID`, every database password,
  `TALUS_API_CLIENT_SECRET` and the other client secrets, `TALUS_ENCRYPTION_KEY`, the off-host `BACKUP_S3_*`
  settings if you use them, and `BACKUP_AGE_RECIPIENTS`. You edit none of them.

- **Rebuild in place (same hostname or address):** at the installer's question in Scenario B step 5, answer
  as you did at first install. Nothing else changes.

- **Migrate (new hostname, address or domain):** at that question, give the new name (or press Enter on the new
  machine to use its network address). **Do not edit the address settings in `.env` by hand.** The installer
  writes every one of them — `TALUS_HOSTNAME`, `KEYCLOAK_HOSTNAME`, `PORTAL_ORIGIN`, `KEYCLOAK_ISSUER`,
  `CORS_ALLOW_ORIGINS`, `KC_PUBLIC_URL` — recreates the services that read them, and resets the portal's sign-in
  redirect addresses, which came back from the restore still naming the old host. What is left for you:
  - **DNS:** create the two records the installer prints (the portal name and `auth.<name>`, same address).
  - **Certificates:** if you asked for public certificates, they are requested for the new names. If the server
    uses its built-in certificate authority, the new machine has a new root; export it as the installer's
    closing text shows and install it on each device again.
  - **Admin → Connections → Advertised base URL:** set it to the new address. It is stored in the database, so
    it comes back from the restore naming the old one. Desktop clients, Talus Field devices and QR links use it.
  - **Single sign-on:** if people sign in through your own identity provider, the redirect address registered
    there names the old sign-in address; update it at the provider (`sso-runbook.md`).
  - **People and devices:** everyone is signed out once, because the sign-in address moved. Desktop and field
    connections saved with the old address need the new one.

> **What the drill shows of this:** `scripts/restore-drill.sh` proves that a token from the restored sign-in
> system is accepted by the restored api after the sign-in address has moved — to a test address on the same
> machine. It does not run the installer, the edge, DNS, certificates or a browser sign-in at a new hostname.
> See "Automated restore drill" below.

---

## Talus+ after a restore

A backup set does not contain your Talus+ license or the Talus+ bundle. What you do depends on what survived.

- **Scenario A, storage volume intact:** nothing. The license is still on the storage volume, and the Talus+
  image and web app are still installed.
- **Scenario B (a new host), or any restore where the storage volume was lost:** once the portal is up and you
  can sign in, apply the bundle that matches your release together with your license file, from the install
  directory:
  ```
  ./scripts/talus-setup.sh --plus /path/to/talus-plus-<version>.tar.gz --licence /path/to/licence.json
  ```
  It should end with `Talus+ is installed.  Entitlement: LICENSED: …` (`talus-plus-runbook.md` §4). If the
  bundle is in place and only the license is missing, sign in as an administrator and add it at
  **Admin → Talus+ → License → Upload license or activation file…** instead.
- **What you made with Talus+** (dashboards, story maps, reports, Field settings) is in the database and came
  back with the restore. Until the bundle and license are back it is not shown; nothing is lost.
- **The AI assists:** the server's AI credential is stored encrypted in the database and came back with the
  restore, as long as your `.env` carries the same `TALUS_ENCRYPTION_KEY`. Check **Admin → AI** afterwards. If it
  does not read Connected, ask Talus for a new activation file and upload it on the License page.

Keep the license file and the bundle where you keep your `.env` copy and your backup key: off the server.

---

## Verify the restore

- Row counts match what you expect on a layer you know
  (`docker compose exec -T postgres psql -U talus -d talusgis -c "SELECT count(*) FROM assets.<your_layer_table>;"`).
- Sign in to the portal; open a map and a layer; open a record that has a photo — the **image renders**
  (proves attachments restored).
- If you publish imagery, open a map that shows one of your imagery layers — **it draws** (proves imagery
  restored).
- **Admin → Usage** and **Admin → Backups & recovery** load; the audit history is present and was **restored,
  not reset** — the per-table audit tables come back with the dump
  (`SELECT count(*) FROM audit.<your_layer_table>_audit;` the same way).
- On a Talus+ server: **Admin → Talus+ → License** shows your organization and modules.
- **Delete drill throwaways.** A backup taken while a restore drill ran contains that drill's throwaway
  verification admin (`tverify-admin-<hex>` — effectively locked: random password, never persisted). After
  a real restore, remove any `tverify-*` account: in **Admin → Users**, **Disable** it, then **Delete** it
  (which deletes its sign-in account too).

---

## Notes & gotchas

- **Version compatibility:** the backup records its `pg_version`; restore into the same major version (16).
  `pg_restore` from a newer client into the same/older server is fine; the Talus image ships
  `postgresql-client-16`.
- **A backup taken before an upgrade** holds the database as that older release left it. After restoring it
  into a newer release, run the installer again as in `upgrade-runbook.md` step 3, so the newer migrations are
  applied; that release's per-version notes apply as they did at the upgrade.
- **The three pre-restore dumps are your undo.** If a restore turns out wrong, restore all three back with the same commands (each database is dropped and created empty first).
- **Off-host is the real protection.** If **Admin → Backups & recovery** warns that backups are on the same host, you are protected against bad
  data / bad upgrades but **not** host loss — set an off-host destination in **Admin → Backups & recovery →
  Destination** (test it before you save; see `dr-hardening.md` for choosing and hardening a target).
- **Why no in-product restore button:** restoring the live database from inside the running api (its own
  connection pool live, restoring its own database) is a footgun. The portal deliberately keeps restore as
  this audited, deliberate operator procedure.

---

## Automated restore drill

A restore procedure is only as good as its last rehearsal. `scripts/restore-drill.sh` rehearses the core of one
automatically: on the machine your server runs on, beside the running stack, it builds a **second, isolated
stack that shares no volume, container or port with the live one**, restores a backup into it, and checks that
the data, the sign-in accounts and the sharing came back and work together.

```
./scripts/restore-drill.sh                    # take a fresh backup, drill it, check recovery, tear down
./scripts/restore-drill.sh --backup-id 42     # drill a specific existing backup set
./scripts/restore-drill.sh --age-key key.txt  # decrypt age-encrypted backups with the off-host private key
./scripts/restore-drill.sh --env-file /safe/.env                 # use the .env copy you keep, not the live one
./scripts/restore-drill.sh --env-from-backup --age-key key.txt   # use the backup's own .env
./scripts/restore-drill.sh --keep             # leave the drill stack up for inspection
```

**What it does.** It starts the **real** compose file under a separate project (`-p talus-restoredrill`,
overlay `docker-compose.drill.yml`), restores the three dumps into empty databases, starts sign-in,
authorization and the api on them, copies the media archive, read through the api from your backup destination
(set in Admin → Backups or in `.env`, on this machine or off-host), into the drill's storage volume, and checks:

- the counts of layers, sign-in accounts and sharing records equal the live counts (with a fresh backup) or
  are at least one (with `--backup-id`);
- with a fresh backup, every kind of stored file a backup copies (attachments and imagery) comes back at least
  as many as the live server held when the backup ran; with `--backup-id`, at least the files taken from the
  archive;
- with a fresh backup: a token issued by the **restored** sign-in system is accepted by the **restored** api,
  on an administrator-only request and on a request that checks sharing; and the same request with no token is
  refused.

Then it removes the drill stack **and its volumes**. A full pass is recorded, and **Admin → Health** shows when
the last one was.

**What it does not do.** Know these before you rely on it:

- It is not a rebuild on a second machine. It needs the live stack running and runs beside it.
- It does not run the installer, the edge, the tile server, certificates, DNS or a browser sign-in. The
  installer and hostname steps of Scenario B are not rehearsed by it.
- It does not restore over databases that already hold data, which is what Scenario A does.
- It counts the stored files; it does not open them or serve them through the restored api.
- With `--backup-id`, the sign-in check is skipped (an older backup cannot contain the drill's temporary
  account); the script says so when it skips.
- It uses the server's current `.env` unless you pass `--env-file` or `--env-from-backup`.
- It does not restore a Talus+ license or start the Talus+ image.

**Isolation (the hard constraint):** a fixed drill project, volumes that belong to that project, containers
named for it, and **never the live ports** (it publishes only `:18000` and `:18080`); every teardown is
guarded by the project name. It reads from the live server (the dumps, the media archive) and makes three
small writes to it: one extra backup; one temporary administrator account, created before that backup and
deleted at the end (the script fails loudly if the deletion does not succeed); and, on a full pass, one line
in the maintenance history. **The live project is never brought down.**

Run it on a cadence you would be comfortable defending after a real disaster, and rehearse Scenario B itself
on a spare machine at least once.

---

## Granular item recovery — un-delete ONE item

When someone deletes a single **layer** (a `tal_` slot) or **web_map** and a full restore would wrongly roll the
whole org back, use the surgical scaffold instead.

The backup's number is the **ID** column in **Admin → Backups & recovery**; pick one taken before the deletion.
A deleted layer's id (`tal_…`) is no longer in the catalog. To find it, list what the backup holds:
`./scripts/restore-item.sh --backup-id <N> --list` shows its layers and web maps and marks those no longer in
the portal. It changes nothing.

```
./scripts/restore-item.sh --backup-id <pre-deletion N> --item <layer_id | web_map uuid>          # DRY-RUN
./scripts/restore-item.sh --backup-id <pre-deletion N> --item <id> --age-key key.txt             # encrypted
./scripts/restore-item.sh --backup-id <pre-deletion N> --item <id> --apply                       # recover
```

It restores the backup's `talusgis`+`openfga` dumps into **throwaway scratch DBs** (live DBs only read),
classifies the item, and — **dry-run by default** — prints the exact **additive** merge. `--apply` first
captures a `pg_dump -Fc` of live (your undo), refuses to clobber (a re-claimed slot / a web_map that's back),
runs the merge in one transaction (`ON CONFLICT DO NOTHING`), replays only the **missing** OpenFGA tuples, and
verifies the item is back. Pick a backup from **before** the deletion (retention is your undo horizon). It is a
scaffold, **not** an in-product button.

**What comes back.** For a layer or a table: its rows, its field definitions (and a value list those fields use,
if that list no longer exists), its settings (name, style, form, display field, the editing switches,
attachments on or off), its metadata record and its sharing. For a web map: the map and its sharing.

**What does not.** A layer's attachments (a full restore includes them), its relates to other tables, and its
change history. The script says so when it finishes.

**If it cannot finish.** The merge is one transaction: on any error nothing is written, the database's own error
is printed, and no sharing is restored. Fix the cause and run it again. One known cause: a value list the layer
needs was deleted and a different list now has its name.
