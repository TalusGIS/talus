"""Talus 40 — disaster recovery: backup / retention (the second consumer of the T39 scheduler seam).

Two jobs, assembled per tick by api/app/maintenance.py (no second scheduler, no lifespan change — the seam
was built to take a second consumer additively; Talus 43 made their cadence/enabled runtime-editable):

  - `run_backup` (job name `scheduled_backup`) — `pg_dump -Fc` of the database (vector/attribute/metadata/
    audit + DDL) streamed to a temp file, integrity-checked with `pg_restore --list`, uploaded to the backup
    target, THEN an INCREMENTAL APPEND-ONLY copy of the configured source object-bucket(s) (attachments now,
    the raster bucket later). One `platform.backups` catalog row per run; the run itself is logged in
    `platform.maintenance_runs` by the harness, like every other job.
  - `prune_old_backups` — retention: delete DB-dump artifacts (+ their catalog rows) older than N days, but
    NEVER below the M most-recent (a non-zero floor). The media archive is append-only and is NOT pruned here.

Talus 43: the effective recipients (encryption) + retention are threaded in from the COALESCE-over-env
reader (api/app/backup_settings.py) — the scheduler passes the per-tick snapshot; other callers pass None
and these resolve it on the same conn.

Design decisions (docs/decision-dr-backup-restore.md §4):
  - F-where: the api shells out to `pg_dump`/`pg_restore` (postgresql-client-16 baked into the image). The api
    stays unprivileged — it is just a libpq client to a DB it already talks to; no Docker socket.
  - F-scope: blobs are effectively immutable (a photo isn't edited; a deleted sign orphans its blob, swept by
    T39 after a grace window), so an APPEND-ONLY copy makes the media archive a superset of any retained DB
    dump's references => a point-in-time `pg_restore` is always consistent with it. This is a backup that
    includes blobs, NOT a multi-target replication manager (the drift fence).
  - F-target: the backup target is the V090 admin-set override when present, else BACKUP_S3_* env
    (all three set => off-host S3; default: the local `talus-backups` subtree on the data volume —
    a same-host copy, honestly labeled, R2 of decision-object-store-build.md). Resolved PER RUN
    (decision-backup-destination.md) — pointing it off-host needs no restart. The honest-DR seam.

HARD FENCE (decision §2): retention prunes OLD BACKUPS ONLY (rows here + their dump artifacts), never live
layers/features, never the audit log, and never below the floor. Restore is the one destructive verb and is a
documented operator runbook (docs/dr-restore-runbook.md), NOT a button — there is no restore code here.

Fail-safe by construction (inherited from the seam): a job that throws is rolled back, recorded `error` in
`maintenance_runs`, and retried next tick. `run_backup` additionally records an `error` row in
`platform.backups` (a visible alarm) before re-raising, and always unlinks its temp dump.
"""
from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import structlog
from fastapi.concurrency import run_in_threadpool
from psycopg import AsyncConnection
from psycopg.conninfo import conninfo_to_dict

from .backup_settings import (
    EffectiveBackupDestination,
    EffectiveBackupSettings,
    get_effective_backup_destination,
    get_effective_backup_settings,
)
from .config import settings
from .maintenance import MaintenanceResult
from .storage import FilesystemStorage, MediaObjectMissing, StorageBackend, StoredObject, build_backup_storage

log = structlog.get_logger(__name__)

_MEDIA_PREFIX = "media/"   # backup-target prefix the object-store half writes under
_DB_PREFIX = "db/"         # backup-target prefix the pg_dump artifacts live under
_ENV_PREFIX = "env/"       # backup-target prefix the `.env` lifeboat artifact lives under (Talus 45)
_CHUNK = 1 << 20           # 1 MiB streaming chunk for the dump
_AGE_SUFFIX = ".age"       # appended to artifact keys when client-side encryption is on (Talus 40.1)


# ---- Backup target (V090 DB override, else BACKUP_S3_* env; default: the local subtree) ----------
#
# The seam construction (storage.build_backup_storage, B6) keeps this path's OWN endpoint/creds —
# the honest-DR seam survives as the product's only S3 path. The destination is runtime-editable
# (decision-backup-destination.md), so every job/request RESOLVES the effective destination and
# constructs its own backend — nothing here caches one (the no-restart mechanism). These
# module-level helper names are the test seams (test_backup.py monkeypatches them) — keep them
# stable; each takes the run's resolved `store` first.

def _store_for(dest: EffectiveBackupDestination) -> StorageBackend:
    """The run's backend from a RESOLVED destination. Overridden => the V090 group verbatim
    (the CHECK guarantees it is never partial; a missing secret here means it was resolved
    with_secret=False — a code bug, not a config state). Env => the BACKUP_S3_* trio exactly as
    pre-V090, INCLUDING the partial-config loud raise (fail-open fence, B6)."""
    if dest.overridden:
        if not dest.secret_key:
            raise RuntimeError("backup destination resolved without its secret (code bug: "
                               "pass with_secret=True before building the backend)")
        return build_backup_storage(endpoint=dest.endpoint, access_key=dest.access_key,
                                    secret_key=dest.secret_key, bucket=dest.bucket)
    return build_backup_storage(endpoint=settings.backup_s3_endpoint,
                                access_key=settings.backup_s3_access_key,
                                secret_key=settings.backup_s3_secret_key,
                                bucket=dest.bucket)


async def resolve_backup_store(conn: AsyncConnection) -> StorageBackend:
    """Resolve the CURRENT effective destination (override-or-env, per call — never cached) and build
    its backend. The one-stop for request-path callers (the admin Download endpoint); the jobs resolve
    the destination themselves because they also need its label for catalog rows."""
    dest = await get_effective_backup_destination(conn, with_secret=True)
    return _store_for(dest)


def _ensure_backup_bucket(store: StorageBackend) -> None:
    """Create the backup target's root if absent (BLOCKING) — mkdir -p locally, make_bucket off-host.
    Idempotent, so the first backup never fails on a missing root."""
    store.ensure_root()


def _put_backup_file(store: StorageBackend, key: str, path: str) -> None:
    """Upload a local file to the backup target under `key` (BLOCKING). fsync'd on the filesystem
    target — backup artifacts are the DR story (B5)."""
    store.write_file(key, path, fsync=True)


def _put_backup_bytes(store: StorageBackend, key: str, data: bytes, content_type: str) -> None:
    """Upload in-memory bytes (a media blob) to the backup target under `key` (BLOCKING)."""
    store.write_bytes(key, data, content_type or "application/octet-stream", fsync=True)


def _list_backup_keys(store: StorageBackend, prefix: str) -> set[str]:
    """Every object key under `prefix` in the backup target (BLOCKING). Used for the incremental media diff
    AND the retention scan. A missing root => empty set (nothing backed up yet)."""
    return {o.key for o in store.list(prefix)}


def _stat_backup_size(store: StorageBackend, key: str) -> int:
    """Size of one backup artifact (BLOCKING) — for the bytes-freed tally on retention."""
    return store.stat(key).size


def _remove_backup_object(store: StorageBackend, key: str) -> None:
    """Delete one artifact from the backup target (BLOCKING). Used ONLY by retention, on a DB-dump key beyond
    the floor and past the age cutoff (HARD FENCE — old backups only)."""
    store.delete(key)


def open_backup_stream(store: StorageBackend, key: str):
    """Open a streaming read of a backup artifact from the CURRENT target (BLOCKING handshake). Returns a
    chunk iterator; open errors raise HERE, before the first chunk. Used by the admin Download endpoint —
    which resolves the effective destination per request. An artifact that lives only at a PREVIOUS
    destination raises MediaObjectMissing => a legible 404 (F2: destination changes start fresh)."""
    return store.iter_chunks(key)


# ---- Source stores (the LIVE media/raster subtrees being backed up) ------------------------------

def _source_store(bucket: str) -> FilesystemStorage:
    """The live filesystem store for one source "bucket" (B3: bucket names are the storage-root
    subtrees, so BACKUP_OBJECT_BUCKETS and the report/backup config keep working unchanged)."""
    return FilesystemStorage(Path(settings.storage_root) / bucket)


def _list_source_objects(bucket: str) -> list[StoredObject]:
    """Enumerate a live source store (BLOCKING). Missing/empty => empty list (e.g. no raster ever
    uploaded)."""
    return _source_store(bucket).list()


def _source_object_path(bucket: str, key: str) -> str:
    """The live blob's fenced local path (BLOCKING check that it exists). The media copy streams from it
    instead of reading the object into memory (once-over 2026-11-15 K4: a stored raster may be up to the
    5 GB upload limit, and the copy, twice that with encryption, made every later backup fail on the
    8 GB server the install runbook recommends)."""
    path = _source_store(bucket).path_for(key)
    if not path.is_file():
        raise MediaObjectMissing(key)
    return str(path)


def _put_backup_file_typed(store: StorageBackend, key: str, path: str, content_type: str) -> None:
    """As _put_backup_file, carrying a content type for an off-host S3 target (BLOCKING)."""
    store.write_file(key, path, content_type or "application/octet-stream", fsync=True)


def _read_source_object(bucket: str, key: str) -> tuple[bytes, str]:
    """Read a live blob's bytes + a guessed content-type (BLOCKING). The filesystem holds no MIME
    metadata (serving reads it from DB rows — build-stage §1.6); the extension guess only labels the
    archived copy for anyone browsing an off-host S3 target."""
    data = _source_store(bucket).read(key)
    return data, (mimetypes.guess_type(key)[0] or "application/octet-stream")


# ---- Pure helpers (unit-tested without any live DB / object store) -------------------------------

def _incremental_targets(
    source_objs: dict[str, list[StoredObject]], existing_dest_keys: set[str], dest_suffix: str = "",
) -> list[tuple[str, str, str, int]]:
    """The APPEND-ONLY incremental diff: for each (source bucket -> its objects), the blobs to copy. The
    backup dest key is namespaced by source bucket (`media/<bucket>/<key>`) so two source buckets can't
    collide; a NEW blob is stored in the CURRENT form, carrying `dest_suffix` (".age" when encrypting).

    The "already archived?" check is **suffix-agnostic** (Talus 47/B1): a blob already present in EITHER form
    — plaintext `<key>` OR encrypted `<key>.age` — is SKIPPED. Before this, the check compared the suffixed
    dest key, so toggling encryption re-copied EVERY (immutable) blob under the other suffix, doubling the
    archive (and giving a false sense of security — the old plaintext copy was still there). Now toggling
    creates no duplicate. Consequence (honest, documented): blobs archived BEFORE encryption was enabled stay
    plaintext — you can't retroactively encrypt an append-only/object-locked object in place; an operator who
    needs every archived blob encrypted starts a fresh destination. Nothing is ever deleted here.
    Returns (source_bucket, source_key, dest_key, size)."""
    out: list[tuple[str, str, str, int]] = []
    for bucket, objs in source_objs.items():
        for o in objs:
            base_key = f"{_MEDIA_PREFIX}{bucket}/{o.key}"
            if base_key in existing_dest_keys or f"{base_key}{_AGE_SUFFIX}" in existing_dest_keys:
                continue  # already archived in some form (plaintext or .age) => immutable, already safe
            out.append((bucket, o.key, f"{base_key}{dest_suffix}", o.size))
    return out


def _select_prunable(
    rows: list[tuple[int, str | None, datetime]], now: datetime, retention_days: int, min_keep: int,
) -> list[tuple[int, str | None]]:
    """The retention selection (HARD FENCE: old backups only, never below the floor). `rows` = (id,
    db_object_key, created_at) for status='ok' backups. Sort newest-first, ALWAYS keep the first `min_keep`
    (the floor), then among the rest select those older than `retention_days`. With retention_days=0 the age
    test passes for all, but the floor still protects the M most-recent => NEVER prunes to zero."""
    floor = max(min_keep, 0)
    ordered = sorted(rows, key=lambda r: r[2], reverse=True)
    cutoff = now - timedelta(days=retention_days)
    prunable: list[tuple[int, str | None]] = []
    for idx, (bid, key, created) in enumerate(ordered):
        if idx < floor:
            continue                       # within the floor — always kept
        if created < cutoff:
            prunable.append((bid, key))
    return prunable


# ---- pg_dump / archive verification (async subprocess; no event-loop blocking) -------------------

def _pg_dump_argv_env(dsn: str) -> tuple[list[str], dict[str, str]]:
    """Build the `pg_dump -Fc` argv + env from a libpq DSN (Talus 42: any of the three databases). The
    password goes in the subprocess ENV (PGPASSWORD), never argv (no leak in the container process list)."""
    p = conninfo_to_dict(dsn)
    argv = [
        "pg_dump", "-Fc", "--no-password",
        "-h", str(p.get("host", "postgres")),
        "-p", str(p.get("port", "5432")),
        "-U", str(p.get("user", "talus")),
        "-d", str(p.get("dbname", "talusgis")),
    ]
    env = {"PGPASSWORD": str(p.get("password", "")), "PATH": os.environ.get("PATH", "")}
    return argv, env


def _primary_dump_dsn() -> str:
    """The DSN the talusgis dump runs on: the OWNER role, never the least-privilege runtime role.

    A complete `pg_dump` must be able to read every object by definition, and `talus_app`
    deliberately cannot (S-1a, `decision-s1-least-privilege-db-role.md`). V070 moved the API's main
    pool to `talus_app` and this dump silently inherited it, so every scheduled backup from
    2026-07-12 died on `LOCK TABLE public.flyway_schema_history` — 119 consecutive failures, no
    backup for 14 days. Same `owner or main` fallback `db.py:34` uses for the owner pool, so a
    pre-cutover deploy (where both DSNs are the owner) is unaffected.
    """
    return settings.owner_database_url or settings.database_url


async def _pg_dump_to_tempfile(dsn: str) -> str:
    """Run `pg_dump -Fc` of `dsn` to a temp file (streamed, no memory load). Returns the path; the caller owns
    it (must unlink) and computes the stored-artifact digest via _file_digest after any encryption. Raises
    RuntimeError on a non-zero exit (the dump is unusable)."""
    argv, env = _pg_dump_argv_env(dsn)
    fd, path = tempfile.mkstemp(prefix="talus-backup-", suffix=".dump")
    os.close(fd)
    proc = await asyncio.create_subprocess_exec(
        *argv, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdout is not None and proc.stderr is not None
    with open(path, "wb") as f:
        while True:
            chunk = await proc.stdout.read(_CHUNK)
            if not chunk:
                break
            f.write(chunk)
    stderr = (await proc.stderr.read()).decode("utf-8", "replace")
    rc = await proc.wait()
    if rc != 0:
        os.unlink(path)
        raise RuntimeError(f"pg_dump exited {rc}: {stderr.strip()[:500]}")
    return path


async def _verify_archive(path: str) -> None:
    """Integrity gate: `pg_restore --list` must read the archive's TOC. A truncated/corrupt dump fails here,
    so we never record a broken backup as 'ok'. Cheap; reads the TOC only (no data restore)."""
    proc = await asyncio.create_subprocess_exec(
        "pg_restore", "--list", path,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stderr is not None
    stderr = (await proc.stderr.read()).decode("utf-8", "replace")
    rc = await proc.wait()
    if rc != 0:
        raise RuntimeError(f"pg_restore --list failed (corrupt/unreadable archive): {stderr.strip()[:500]}")


# ---- Client-side encryption (Talus 40.1 — `age`, asymmetric; the api can't decrypt) --------------

def _age_recipient_args(recipients: list[str]) -> list[str]:
    """Flatten age public keys into `-r <key>` argv pairs (ALL recipients; any one's private key can
    decrypt). Empty when there are no recipients. Talus 43: recipients are threaded in (the EFFECTIVE list,
    override-or-env) rather than read from `settings` here — keeps the leaf helpers settings-free + testable."""
    args: list[str] = []
    for r in recipients:
        args.extend(("-r", r))
    return args


async def _age_encrypt_file(src_path: str, recipients: list[str]) -> str:
    """Encrypt a file to `<src>.age` for all given recipients (streams via the `age` binary — no memory
    load). Returns the ciphertext path. Raises on a non-zero exit."""
    dst = src_path + _AGE_SUFFIX
    proc = await asyncio.create_subprocess_exec(
        "age", *_age_recipient_args(recipients), "-o", dst, src_path,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stderr is not None
    stderr = (await proc.stderr.read()).decode("utf-8", "replace")
    rc = await proc.wait()
    if rc != 0:
        raise RuntimeError(f"age encrypt failed (rc={rc}): {stderr.strip()[:500]}")
    return dst


async def _age_encrypt_file_to(src_path: str, dst_path: str, recipients: list[str]) -> None:
    """Encrypt a file to an explicit destination (streams via the `age` binary — no memory load). For a
    live media blob, whose own folder must never receive a `.age` beside it. Raises on a non-zero exit."""
    proc = await asyncio.create_subprocess_exec(
        "age", *_age_recipient_args(recipients), "-o", dst_path, src_path,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stderr is not None
    stderr = (await proc.stderr.read()).decode("utf-8", "replace")
    rc = await proc.wait()
    if rc != 0:
        raise RuntimeError(f"age encrypt(blob) failed (rc={rc}): {stderr.strip()[:300]}")


async def _copy_media_object(store: StorageBackend, src_bucket: str, src_key: str, dest_key: str,
                             recipients: list[str]) -> None:
    """Copy one live blob to the backup target without holding it in memory (K4): streamed from its
    path, or encrypted file to file into a temporary file first. Memory stays flat whatever its size;
    the temporary file is the object's size, one object at a time, and is always removed."""
    src = await run_in_threadpool(_source_object_path, src_bucket, src_key)
    if not recipients:
        ctype = mimetypes.guess_type(src_key)[0] or "application/octet-stream"
        await run_in_threadpool(_put_backup_file_typed, store, dest_key, src, ctype)
        return
    fd, tmp = tempfile.mkstemp(prefix="talus-media-", suffix=_AGE_SUFFIX)
    os.close(fd)
    try:
        await _age_encrypt_file_to(src, tmp, recipients)
        # ciphertext, not the original media type
        await run_in_threadpool(_put_backup_file_typed, store, dest_key, tmp, "application/octet-stream")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            log.warning("backup_tempfile_unlink_failed", path=tmp, exc_info=True)


async def _age_encrypt_bytes(data: bytes, recipients: list[str]) -> bytes:
    """Encrypt in-memory bytes (a media blob) to all given recipients via the `age` binary (stdin->stdout)."""
    proc = await asyncio.create_subprocess_exec(
        "age", *_age_recipient_args(recipients),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(data)
    if proc.returncode != 0:
        raise RuntimeError(f"age encrypt(blob) failed (rc={proc.returncode}): {err.decode('utf-8','replace')[:300]}")
    return out


def _file_digest(path: str) -> tuple[int, str]:
    """(size_bytes, sha256_hex) of a local file, read in chunks — computed on the ARTIFACT WE STORE (the
    ciphertext when encrypting), so the catalog's size/sha verify the stored object. BLOCKING."""
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
    return size, h.hexdigest()


@dataclass(frozen=True)
class _ArtifactMeta:
    """One stored dump artifact: its key in the backup target + the digest of the bytes we stored."""

    object_key: str
    size: int
    sha256: str


async def _dump_one(store: StorageBackend, name: str, dsn: str, stamp: str,
                    recipients: list[str]) -> _ArtifactMeta | None:
    """Dump ONE database to the backup target: pg_dump -> verify the plaintext archive -> (encrypt) ->
    digest -> upload. Returns the stored-artifact metadata, or **None** when the DSN is empty (that database
    is deliberately not backed up — e.g. a deployment running identity/authz externally). Each call owns and
    cleans its own temp files. A non-empty DSN that fails to dump RAISES — an incomplete set must never be
    recorded 'ok' (decision-dr-completeness.md §3 F-completeness-policy). Talus 43: encryption is keyed off
    the EFFECTIVE `recipients` (override-or-env), threaded in by the caller."""
    if not dsn.strip():
        return None
    encrypted = bool(recipients)
    suffix = _AGE_SUFFIX if encrypted else ""
    tmp_paths: list[str] = []
    try:
        path = await _pg_dump_to_tempfile(dsn)
        tmp_paths.append(path)
        await _verify_archive(path)  # the integrity gate runs on PLAINTEXT (can't list ciphertext)
        upload_path = path
        if encrypted:
            upload_path = await _age_encrypt_file(path, recipients)
            tmp_paths.append(upload_path)
        size, sha = await run_in_threadpool(_file_digest, upload_path)  # digest the STORED bytes
        key = f"{_DB_PREFIX}{name}-{stamp}.dump{suffix}"
        await run_in_threadpool(_ensure_backup_bucket, store)
        await run_in_threadpool(_put_backup_file, store, key, upload_path)
        return _ArtifactMeta(key, size, sha)
    finally:
        for p in tmp_paths:
            if os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    log.warning("backup_tempfile_unlink_failed", path=p, exc_info=True)


# ---- The `.env` lifeboat (Talus 45 — encrypt-only; the fail-closed AND lives in the caller) -------

def env_file_readable() -> bool:
    """True iff the mounted `.env` (settings.backup_env_path) exists and is readable. The PUT-time precondition
    for enabling include_env (caught at config time, not silently at backup time) AND the includeEnvEffective
    signal in the GET config. Opens for read (honest — os.R_OK can lie under ACLs) and closes immediately;
    NEVER reads or logs the contents."""
    try:
        with open(settings.backup_env_path, "rb"):
            return True
    except OSError:
        return False


async def _backup_env_file(store: StorageBackend, recipients: list[str],
                           stamp: str) -> _ArtifactMeta | None:
    """Capture the literal `.env` as an `age`-encrypted lifeboat artifact: copy the mounted file to a temp
    path -> encrypt to ALL recipients -> digest the CIPHERTEXT -> upload under `env/.env-<stamp>.age`. Returns
    the stored-artifact metadata, or **None** when the file is unreadable (the caller turns that into a loud
    error — never a silent partial).

    CALLED ONLY from inside the encrypted branch (recipients non-empty) AND only when include_env is on — the
    fail-closed AND. There is deliberately NO plaintext path: the `.env` is written to a backup ONLY
    age-encrypted. The `.env` bytes are never logged; an unreadable file logs the PATH only. We copy to a temp
    file first because the mount is read-only (`age` writes `<src>.age` beside the source) and to keep the
    digest/upload on a path we own + clean up."""
    src = settings.backup_env_path
    tmp_paths: list[str] = []
    try:
        fd, plain = tempfile.mkstemp(prefix="talus-env-", suffix=".env")
        os.close(fd)
        tmp_paths.append(plain)
        try:
            await run_in_threadpool(shutil.copyfile, src, plain)
        except OSError:
            log.warning("backup_env_unreadable", path=src)  # path only — never the .env bytes
            return None
        cipher = await _age_encrypt_file(plain, recipients)  # encrypt-only: <plain>.age
        tmp_paths.append(cipher)
        size, sha = await run_in_threadpool(_file_digest, cipher)  # digest the STORED ciphertext
        key = f"{_ENV_PREFIX}.env-{stamp}.age"
        await run_in_threadpool(_ensure_backup_bucket, store)
        await run_in_threadpool(_put_backup_file, store, key, cipher)
        return _ArtifactMeta(key, size, sha)
    finally:
        for p in tmp_paths:
            if os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    log.warning("backup_tempfile_unlink_failed", path=p, exc_info=True)


# ---- The jobs ------------------------------------------------------------------------------------

async def _record_backup_error(conn: AsyncConnection, kind: str, started_at: datetime, message: str,
                               label: str) -> None:
    """Best-effort error row in platform.backups (a visible alarm), committed on its own so the harness's
    rollback can't erase it. Never raises — observability must not mask the real failure being re-raised.
    `label` is the resolved destination's honest-DR label — or the caller's fallback when the failure IS
    the destination resolution (decrypt error / partial config), so the alarm still names a target."""
    try:
        await conn.rollback()
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO platform.backups (created_at, finished_at, kind, status, target, detail) "
                "VALUES (%s, now(), %s, 'error', %s, %s)",
                (started_at, kind, label, message[:1000]),
            )
        await conn.commit()
    except Exception:  # noqa: BLE001
        log.warning("backup_error_record_failed", exc_info=True)


async def run_backup(
    conn: AsyncConnection, kind: str = "scheduled", eff: EffectiveBackupSettings | None = None,
) -> MaintenanceResult:
    """Take one FULL-SYSTEM backup set (Talus 42): pg_dump of all three databases — talusgis (data + audit) +
    Keycloak (identity) + OpenFGA (authz) — then the incremental append-only media copy, then ONE
    platform.backups catalog row recording the complete set. `kind` is 'scheduled' (the registered job) or
    'manual' ("Back up now"). The job runs on the scheduler's lock-holding connection; we rollback before the
    long dumps so no snapshot is held across them (the session advisory lock survives — the T39 orphan-job
    pattern). A configured secondary DB that fails to dump raises (an incomplete set is never recorded 'ok');
    an unconfigured (empty DSN) secondary is an honest skip.

    Talus 43: encryption recipients are the EFFECTIVE list (override-or-env). The scheduler passes the `eff`
    it resolved for this tick; the manual path passes None, so we resolve it here on the same conn (manual
    "Back up now" always runs — it does NOT consult `eff.scheduled_enabled`)."""
    started_at = datetime.now(timezone.utc)
    stamp = started_at.strftime("%Y%m%d-%H%M%S")
    if eff is None:
        eff = await get_effective_backup_settings(conn)
    recipients = list(eff.age_recipients)
    encrypted = bool(recipients)
    suffix = _AGE_SUFFIX if encrypted else ""
    # The destination label for the error row when the failure IS the resolution (decrypt error /
    # partial config) — replaced by the real label the moment the destination resolves.
    label = "(destination unresolved)"
    try:
        # Resolve the destination for THIS run (with the secret — decrypt failure raises here and
        # becomes the visible error row, per the secrets.py read-path rule) and build the backend
        # once; every helper below uses it, so a mid-run settings change can't split a set.
        dest = await get_effective_backup_destination(conn, with_secret=True)
        label = dest.label
        store = _store_for(dest)

        async with conn.cursor() as cur:
            await cur.execute("SHOW server_version")
            row = await cur.fetchone()
            pg_version = row[0] if row else None
        await conn.rollback()  # don't hold a read snapshot across the dumps/uploads

        # 1) Dump all three databases. talusgis is required; keycloak/openfga skip on an empty DSN.
        db = await _dump_one(store, "talusgis", _primary_dump_dsn(), stamp, recipients)
        if db is None:  # the primary DSN is always set — defensive
            raise RuntimeError("talusgis DSN is empty — cannot back up the primary database")
        kc = await _dump_one(store, "keycloak", settings.backup_keycloak_db_url, stamp, recipients)
        of = await _dump_one(store, "openfga", settings.backup_openfga_db_url, stamp, recipients)

        # 1b) The `.env` lifeboat (Talus 45) — the fail-closed AND: included ONLY when encryption is on
        #     (`encrypted`) AND the include_env toggle is on. No recipients ⇒ `.env` is never read or written,
        #     regardless of the toggle. Encrypt-only; an unreadable file here RAISES (a loud error row), never
        #     a silent partial. The PUT-time readable check catches the common misconfig earlier.
        env = None
        if encrypted and eff.include_env:
            env = await _backup_env_file(store, recipients, stamp)
            if env is None:
                raise RuntimeError(
                    f"include_env is on but the .env file at {settings.backup_env_path} is unreadable — "
                    "mount it read-only (./.env:/secrets/.env:ro) or clear the include-env toggle"
                )

        # 2) Incremental append-only copy of the live source buckets (attachments now, raster later). Dest
        #    keys carry the same suffix, so the existing-key diff matches what we store (encrypted or not).
        existing = await run_in_threadpool(_list_backup_keys, store, _MEDIA_PREFIX)
        source_objs: dict[str, list[StoredObject]] = {}
        for bucket in settings.backup_object_buckets_list:
            source_objs[bucket] = await run_in_threadpool(_list_source_objects, bucket)
        targets = _incremental_targets(source_objs, existing, dest_suffix=suffix)
        copied = 0
        bytes_copied = 0
        for src_bucket, src_key, dest_key, sz in targets:
            try:
                await _copy_media_object(store, src_bucket, src_key, dest_key, recipients if encrypted else [])
            except MediaObjectMissing:
                # Gone since it was listed: an attachment deleted, or a field project's file replaced by a
                # newer version (R2), during this run. Not data this backup can or should hold.
                log.info("backup_media_object_gone", bucket=src_bucket, key=src_key)
                continue
            copied += 1
            bytes_copied += sz

        # 3) One catalog row for the complete set.
        enc_note = (
            f", encrypted to {len(recipients)} recipient(s)" if encrypted else ""
        )
        parts = [f"talusgis {db.size}B"]
        parts.append(f"keycloak {kc.size}B" if kc else "keycloak SKIPPED")
        parts.append(f"openfga {of.size}B" if of else "openfga SKIPPED")
        env_note = ", + .env (encrypted)" if env else ""
        detail = (
            f"backup {stamp}: {', '.join(parts)}, +{copied} new blob(s) ({bytes_copied} bytes)"
            f"{env_note}{enc_note} -> {label}"
        )
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO platform.backups "
                "(created_at, finished_at, kind, status, db_object_key, db_size_bytes, db_sha256, "
                " keycloak_object_key, keycloak_size_bytes, keycloak_sha256, "
                " openfga_object_key, openfga_size_bytes, openfga_sha256, "
                " env_object_key, env_size_bytes, env_sha256, "
                " media_objects_copied, media_bytes_copied, target, pg_version, detail) "
                "VALUES (%s, now(), %s, 'ok', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    started_at, kind,
                    db.object_key, db.size, db.sha256,
                    kc.object_key if kc else None, kc.size if kc else 0, kc.sha256 if kc else None,
                    of.object_key if of else None, of.size if of else 0, of.sha256 if of else None,
                    env.object_key if env else None, env.size if env else 0, env.sha256 if env else None,
                    copied, bytes_copied, label, pg_version, detail,
                ),
            )
        await conn.commit()
        return MaintenanceResult(items_affected=1, bytes_freed=0, detail=detail)
    except Exception as exc:  # noqa: BLE001 — record a visible alarm, then re-raise so the harness retries
        await _record_backup_error(conn, kind, started_at, str(exc), label)
        raise


async def prune_old_backups(
    conn: AsyncConnection, eff: EffectiveBackupSettings | None = None,
) -> MaintenanceResult:
    """Retention: delete DB-dump artifacts (+ their catalog rows) older than N days, but NEVER below the M
    most-recent floor (decision §4 F-retention). The append-only media archive is intentionally NOT pruned.
    HARD FENCE: only backup artifacts are removed — never live data, never the audit log, never to zero.

    Talus 43: the EFFECTIVE retention (override-or-env) — passed by the scheduler, or resolved here on the
    same conn (None) for any caller without it. The floor stays ≥1 (CHECK retention_min_keep >= 1).

    Destination (F2): retention operates on the CURRENT target only. After a destination change, a row
    whose artifacts live at the PREVIOUS destination still ages out of the catalog, but the delete against
    the current target is an idempotent no-op — the old location's bytes are never touched from here
    (recovering or clearing them is the operator's documented call, dr-restore-runbook.md)."""
    if eff is None:
        eff = await get_effective_backup_settings(conn)
    days = eff.retention_days
    min_keep = eff.retention_min_keep
    dest = await get_effective_backup_destination(conn, with_secret=True)
    store = _store_for(dest)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, created_at, db_object_key, keycloak_object_key, openfga_object_key, env_object_key "
            "FROM platform.backups WHERE status = 'ok' ORDER BY created_at DESC"
        )
        fetched = await cur.fetchall()
    await conn.rollback()  # release the snapshot before object-store deletes

    # Every artifact key per backup id. Talus 45 fixes a pre-existing orphan: prune used to remove ONLY the
    # talusgis db_object_key, leaving the V041 keycloak/openfga (and now the env) artifacts behind in the
    # target. Remove the WHOLE set — db + keycloak + openfga + env (media stays append-only, never pruned).
    keys_by_id = {r[0]: [k for k in (r[2], r[3], r[4], r[5]) if k] for r in fetched}
    rows = [(r[0], r[2], r[1]) for r in fetched]  # (id, db_object_key, created_at) for the floor/age selection
    prunable = _select_prunable(rows, datetime.now(timezone.utc), days, min_keep)
    pruned = 0
    freed = 0
    for bid, _db_key in prunable:
        for key in keys_by_id.get(bid, []):
            try:
                freed += await run_in_threadpool(_stat_backup_size, store, key)
            except Exception:  # noqa: BLE001 — a missing artifact still lets us drop the stale row
                log.warning("backup_prune_stat_failed", key=key, exc_info=True)
            try:
                await run_in_threadpool(_remove_backup_object, store, key)
            except Exception:  # noqa: BLE001
                log.warning("backup_prune_remove_failed", key=key, exc_info=True)
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM platform.backups WHERE id = %s", (bid,))
        await conn.commit()
        pruned += 1

    detail = f"pruned {pruned} backup(s) older than {days}d (kept >= {min_keep} most recent), freed {freed} bytes"
    return MaintenanceResult(items_affected=pruned, bytes_freed=freed, detail=detail)
