# Talus — DR hardening guide (trustworthy off-host backups)

**What this is:** how to take Talus's built-in backups from "works on this host" to
"survives the host, and is safe to keep off-site." Companion to `docs/dr-restore-runbook.md` (how to restore).

A backup captures **all three of Talus's databases** — `talusgis` (data + audit), **Keycloak** (identity), and
**OpenFGA** (authorization) — **plus media** (attachments, photos, the downloaded basemap and imagery). So a
restore brings back logins and the sharing graph, not just GIS data. Your **`.env`** (secrets + the OpenFGA
store/model IDs) is **not** in a backup unless you turn on the encrypted lifeboat described below; it's the
master key a restore must match — **keep it safe separately** (see key custody below). A Talus+ license file
and the edge's certificates are never in a backup: `dr-restore-runbook.md` ("What a backup set contains") lists
what to keep yourself.

Out of the box, backups land on the **same** storage volume as your live data. That protects against bad
upgrades, app bugs, and accidental deletes — but **not host loss** (disk failure, the box destroyed/stolen,
ransomware encrypting the host). The **Admin → Backups & recovery** page warns that backups are stored on the
same host as your live data when you're in this state.
Everything below is about closing that gap.

---

## The target shape: 3-2-1, the thin way

The backup rule of thumb is **3-2-1** — *3 copies, on 2 kinds of media, 1 off-site.* Talus stays thin by
writing to **one** backup target and letting the object store make the second copy, rather than building a
multi-target replication engine:

- **One Talus target.** Backups go to a single destination: the local backups directory on the
  storage volume by default, or an off-host S3 bucket — set in **Admin → Backups & recovery → Destination**
  (no `.env` edit, no restart) or via `BACKUP_S3_*` env.
- **Second copy via the target's own replication.** For an off-host S3 target, enable the provider's
  replication — AWS S3 / Backblaze B2 / Wasabi cross-region/bucket replication, or your own store's
  bucket replication — to mirror the backup bucket to a second location. This is battle-tested, runs
  outside Talus, and keeps the app a thin backup producer. (For the local-default target, the second
  copy is simply an rsync/rclone of the backups directory — same principle, outside the app.)

**Picking the off-host target:**
- **Self-hosted / air-gapped:** an S3 endpoint you run **on another machine** — a NAS's S3 door, or
  Garage/SeaweedFS on a second server (a box in another building/rack). Open-source options exist,
  nothing leaves your control — the cleanest fit for an air-gapped deployment and data-sovereignty. For many
  small orgs, "another machine in another building" *is* adequate off-site DR.
- **Cloud:** **Backblaze B2** or **Wasabi** are the cost sweet spot for backups (cheap storage, simple S3
  API); AWS S3 / Cloudflare R2 / DigitalOcean Spaces also work. Enter the endpoint, bucket, and keys in
  **Admin → Backups & recovery → Destination** (use its **Test connection** before saving), or set
  `BACKUP_S3_ENDPOINT/_ACCESS_KEY/_SECRET` and `BACKUP_BUCKET` in `.env` — no code change either way.
  **Record the secret key in your own vault too:** Talus stores it encrypted and can never display it
  back, and a from-zero restore needs it (see `dr-restore-runbook.md`, "Where your backups are").

---

## Where the bytes live on the host

Media, rasters, and (by default) backups are plain files in the Docker volume `storagedata`
(`docker volume inspect <project>_storagedata` shows the host path — typically under
`/var/lib/docker/volumes/`). Two practical consequences:

- **Capacity**: the volume grows with your imagery and attachments and lives on the same
  filesystem as Docker itself by default — watch free space there, and size the host disk for
  your raster ambitions.
- **Dedicated-disk placement**: to put the storage volume on its own disk, standard Docker
  named-volume relocation applies; moving the *backup destination* off-host (this guide) is the
  higher-value step regardless of where the live volume sits.

## Harden the target

1. **Use an HTTPS endpoint.** `BACKUP_S3_ENDPOINT` should be `https://…` for any off-host/cloud target — the
   client uses the scheme to decide TLS, so backups (which include your whole DB + audit log) are encrypted in
   transit. (The image ships `ca-certificates`.)

2. **Give Talus a key scoped to the one backup bucket.** The `BACKUP_S3_*` credentials need **get, list, put
   and delete** on that bucket and nothing else, with no access to any other bucket. Talus reads backups back
   for **Test connection**, **Download**, the restore drill and single-item recovery, so a write-only key would
   stop all four. What stops a compromised server from destroying backups is item 3, object lock, not the key.

3. **Make backups immutable (anti-ransomware).** Enable **object lock / versioning / a retention policy** on
   the backup bucket (S3 Object Lock in compliance/governance mode, B2/Wasabi object lock, or your
   store's retention). Then even an attacker holding the backup key **cannot delete or overwrite** existing backups —
   the single most valuable control for ransomware resilience. Talus's own retention
   (`prune_old_backups`) still works for normal aging; object-lock is the floor under it.

4. **Encrypt client-side.** Add `age` recipients so artifacts are `age`-encrypted
   **before** they leave the host — the target (especially cloud) never holds plaintext, and **Talus itself
   cannot decrypt** them (it holds only the public key). See key custody below. Recipients are set in
   **Admin → Backups & recovery → Edit settings → Encryption recipients**, without a restart — they're PUBLIC
   keys, so it's safe. `BACKUP_AGE_RECIPIENTS` in `.env` is the default; the page's setting wins, and falls
   back to the `.env` value when cleared.

   > **Encryption protects files archived AFTER you enable it.** The media archive is append-only, so
   > turning encryption on does not encrypt files already stored unencrypted — those stay as they are at the
   > backup target. This release ships no tool that removes them. Turn encryption on before the first backup
   > goes to an off-host target if that matters to you.

---

## Key custody (if you enable encryption)

- Generate a keypair: `age-keygen -o key.txt` → paste the `# public key: age1…` line into
  **Admin → Backups & recovery → Edit settings → Encryption recipients** (or set `BACKUP_AGE_RECIPIENTS` in `.env`
  as the deployment default — the panel setting wins).
- **Keep `key.txt` OFF the Talus host** — a password manager, a safe, an HSM. If the host is lost you restore
  *using the offline key*; if the key lives on the host, host loss takes it too.
- **Encrypt to ≥2 recipients** — a primary plus a **break-glass** key held by a second custodian
  (one public key per line in the panel). Either private key can decrypt.
- **Losing every private key = unrecoverable backups.** Confidentiality has this cost — write the custody plan
  down and test it.

---

## The `.env` "lifeboat" — one off-host secret instead of two

By default the `.env` is **not** in any backup: it holds *live, reusable* infra secrets (the OpenFGA store/model
IDs, every DB/Keycloak password, the API client secret) — the restore **master key** — so you safeguard it
off-host yourself, separately from the backups. That means recovering needs **two** off-host things: the `age`
private key **and** your copy of `.env`.

The lifeboat folds the `.env` **into the encrypted backup** so a set is self-contained — you then keep just
**one** off-host secret (the `age` private key), which opens everything:

- Turn on **Admin → Backups & recovery → Edit settings → "Include the .env file (lifeboat)"**. It is **gated on encryption** (fail-closed
  AND): with no `age` recipients the `.env` is **never** read or written, regardless of the toggle. The panel
  shows it "inactive until encryption is on."
- The `.env` is written **only** as `age` ciphertext (`env/.env-<stamp>.age`) — never plaintext, never logged.
  The api reads it from a **read-only** mount (`./.env:/secrets/.env:ro`, `BACKUP_ENV_PATH`); it cannot decrypt
  its own backups, and never edits `.env`.
- Point-in-time bonus: the `.env` and the DB dumps come from the **same snapshot**, so the OpenFGA IDs are
  guaranteed to match the restored tuples (more consistent than "old DBs + today's `.env`").

**Blast-radius honesty:** a leaked `.env` (vs a data dump) lets an attacker pivot into the *running* system. The
encryption + off-host private-key custody + opt-in are what contain that — so the lifeboat raises the stakes on
key custody. **Use ≥2 recipients (a break-glass key)** so the lifeboat isn't a single-key SPOF. The `age` private
key itself is **never** in a backup (that would defeat the encryption), so you still hold exactly one secret
off-host — just one instead of two. At restore, decrypt the backed-up `.env` and use it; see the runbook.

---

## Prove it: restore drills

A backup you have never restored is a hope, not a backup. Periodically (quarterly is a sane default):

- **Rehearse a restore** with `scripts/restore-drill.sh` — on the server itself, beside the running stack, it
  restores a chosen backup (use `--age-key` for encrypted sets) into a **second, isolated stack**, checks that
  the data, the sign-in accounts and the sharing came back and work together and that attachment files are
  present, then removes the drill stack. If you enabled the lifeboat, add `--env-from-backup` to use the
  backup's **own** encrypted `.env` instead of the server's. The drill needs the live server running, so it is
  not a rebuild on a second machine: what it does and does not prove is in `docs/dr-restore-runbook.md`
  ("Automated restore drill"), and that runbook's Scenario B is the rebuild to rehearse on a spare machine.
- Confirm your **off-host replication** is current (the second copy isn't silently stalled).
- Confirm the **break-glass** key actually decrypts (not just the primary).

---

## Trustworthy-off-host checklist

- [ ] The backup destination points **off-host** (the same-host warning on Admin → Backups & recovery is gone;
      the **Test connection** passed).
- [ ] Endpoint is **HTTPS**.
- [ ] Backup key is **least-privilege**, scoped to the backup bucket only.
- [ ] Backup bucket has **object lock / immutability** enabled (ransomware floor).
- [ ] **Object-store replication** makes a second copy (3-2-1).
- [ ] Encryption recipients set (panel or env); **private keys held off-host**, ≥2 recipients.
- [ ] *(optional)* `.env` **lifeboat** on (Admin → Backups & recovery) **with encryption** + the read-only `./.env` mount —
      so one off-host key recovers everything; verify with `restore-drill.sh --env-from-backup --age-key …`.
- [ ] A **restore drill** has actually succeeded from the off-host copy in the last quarter.

Hit all seven and "and your data is safe" is a claim you can stand behind — including for a government buyer.
