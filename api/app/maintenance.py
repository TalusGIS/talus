"""Talus 39 — self-maintenance & housekeeping scheduler (the seam).

A thin, app-internal periodic-task harness: a lifespan-managed asyncio loop (started in
api/app/main.py) that wakes every `maintenance_tick_seconds` and, for each registered job that is DUE,
runs it and records the outcome in platform.maintenance_runs. Exactly ONE api replica runs jobs at a
time — even when the api is scaled out — because each tick is guarded by a Postgres advisory lock
(`pg_try_advisory_lock`). The single Postgres is the coordination point, so the api stays stateless /
externalizable (build-plan AP13): no new singleton service, no leader election to operate.

Why this mechanism (decision-housekeeping.md §4, F-scheduler):
  - NOT pg_cron — the postgis/postgis:16-3.4 image ships none, so it would mean a custom image build +
    shared_preload_libraries (an ops surface against install simplicity), and it's SQL-only (can't do
    the object-store cleanup job).
  - NOT an external cron container — another moving piece to operate; can't share the api's Python.
  - NOT APScheduler — a heavier dep with its own job-store/persistence model that pulls toward an
    orchestration product (the drift fence). A ~hand-rolled loop is the North-Star-correct harness.

HARD FENCE (decision §2): the jobs auto-expire CRUFT ONLY — dereferenced idempotency keys, orphaned
object-store blobs. They NEVER delete authoritative layers/features and NEVER the audit log.

Fail-safe by construction: a job that throws is rolled back, recorded status='error', logged, and
retried next tick — it never crashes the loop or the api lifespan. The advisory lock is ALWAYS released
(try/finally), and if it can't be released cleanly the connection is closed rather than returned to the
pool still holding a lock — so a crashed run can never wedge the scheduler.

Slice 3 (DR) registers a scheduled-backup + retention-prune job into this same loop. Talus 43 makes the
DR jobs' cadence + on-off RUNTIME-EDITABLE: instead of baking the registry once at startup, each tick
reads the effective backup settings (on the lock-holding conn) and assembles the active job list —
housekeeping jobs keep their static env intervals; the backup jobs get the effective interval and are
included only when scheduled backups are enabled. The lock / fail-safe contract is unchanged.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import structlog
from fastapi.concurrency import run_in_threadpool
from psycopg import AsyncConnection

from .backup_settings import EffectiveBackupSettings, get_effective_backup_settings
from .config import settings
from .db import get_pool
from .attachments import OBJECT_KEY_PREFIX as ATTACHMENTS_PREFIX

# Talus+ report artifacts (V100, decision-scheduled-reports.md F6): the SECOND swept tenant. Core
# reads the core-schema registry (platform.report_artifacts.object_key) — no paid import.
REPORTS_PREFIX = "reports/"
# Talus+ field packs (V105, decision-talus-field-f3.md §6): the THIRD swept tenant. Core reads the
# core-schema registry (platform.field_packs.manifest names every file of the served build) — no
# paid import. A deleted pack (row gone, prefix delete missed) or a superseded build is collected here.
FIELD_PACKS_PREFIX = "field-packs/"
from .media import StoredObject, list_object_keys, remove_object

log = structlog.get_logger(__name__)

# A fixed, application-chosen 64-bit key for the scheduler-wide SESSION advisory lock. Any constant
# works; this one is namespaced to "Talus 39 maintenance" and documented here so nothing else reuses it.
_ADVISORY_LOCK_KEY = 3_900_000_039


@dataclass(frozen=True)
class MaintenanceResult:
    """What a job did — feeds the maintenance_runs record + the structured log line."""

    items_affected: int = 0           # rows pruned / blobs removed
    bytes_freed: int = 0              # object-store bytes reclaimed (0 for SQL-only jobs)
    detail: str | None = None         # one-line human summary


# A job is an async callable taking the lock-holding connection, returning a MaintenanceResult.
JobRun = Callable[[AsyncConnection], Awaitable[MaintenanceResult]]


@dataclass(frozen=True)
class MaintenanceJob:
    name: str                 # stable identifier, also the maintenance_runs.job_name
    interval_seconds: int     # run at most once per this window (DB-authoritative — see _is_due)
    run: JobRun


# ---- Jobs (cruft cleanup ONLY — HARD FENCE) -----------------------------------------------------

async def prune_applied_op_keys(conn: AsyncConnection) -> MaintenanceResult:
    """Job (a) — delete platform.applied_op_keys (V036) rows older than the configured retention.

    These are offline-create idempotency-replay window rows; a client replays within minutes/hours, so
    a row older than ~30 days is pure cruft (the V036 migration's own TODO). Pure SQL, the lowest-risk
    first consumer — and the proof that the seam runs. Touches NOTHING authoritative."""
    days = settings.maintenance_idempotency_key_retention_days
    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM platform.applied_op_keys "
            "WHERE created_at < now() - make_interval(days => %s)",
            (days,),
        )
        n = cur.rowcount
    await conn.commit()
    return MaintenanceResult(items_affected=n, detail=f"pruned {n} idempotency key(s) older than {days}d")


def _select_orphans(
    objects: list[StoredObject], live_keys: set[str], cutoff: datetime,
    reports_live_keys: set[str] | None = None,
    field_packs_live_keys: set[str] | None = None,
) -> list[StoredObject]:
    """The orphan SAFETY rule, split out so it is unit-testable without a live object store: a blob is
    an orphan iff it lives under a SWEPT prefix — `attachments/` (this job's original charter) or, when
    `reports_live_keys` is given, `reports/` (Talus+ report artifacts, V100) — AND is UNREFERENCED by
    THAT prefix's registry (media.attachments.object_key / platform.report_artifacts.object_key) AND
    older than the grace cutoff. Each prefix is diffed ONLY against its own registry, so an attachment
    key can never be judged by the reports table or vice versa. `reports_live_keys=None` switches the
    reports leg OFF entirely (a `reports/` key is then fenced like any other tenant).
    A blob with no last_modified is treated as too-new-to-judge (kept) — never sweep what we can't age.

    ★ The prefix fence (field-projects security review, 2026-08-16): the media bucket is no longer
    attachment-only — `basemaps/` (PR #72) and `field-projects/` (V085) objects live beside the
    attachments, tracked by registries this job does NOT read (org_settings / platform.field_projects).
    Without the prefix restriction the liveness test above classified every one of them an orphan and
    silently deleted served artifacts at grace+1 days. This job collects ATTACHMENT cruft only; any
    other tenant that ever wants sweeping brings its own registry-aware job."""
    registries: list[tuple[str, set[str]]] = [(ATTACHMENTS_PREFIX, live_keys)]
    if reports_live_keys is not None:
        registries.append((REPORTS_PREFIX, reports_live_keys))
    # The field-packs/ leg (V105): the same shape — its own registry, switched off by None.
    if field_packs_live_keys is not None:
        registries.append((FIELD_PACKS_PREFIX, field_packs_live_keys))

    def _orphan(o: StoredObject) -> bool:
        if o.last_modified is None or o.last_modified >= cutoff:
            return False
        for prefix, live in registries:
            if o.key.startswith(prefix):
                return o.key not in live
        return False  # every other tenant is fenced

    return [o for o in objects if _orphan(o)]


def _field_pack_live_keys(pack_id: str, build_no: int, manifest: dict) -> set[str]:
    """The object keys a pack's served build occupies, from its manifest (pure, unit-tested)."""
    prefix = f"{FIELD_PACKS_PREFIX}{pack_id}/build/{build_no}/"
    keys = {f"{prefix}manifest.json"}
    bm = manifest.get("basemap") or {}
    if bm.get("path"):
        keys.add(f"{prefix}{bm['path']}")
    for entry in manifest.get("layers") or []:
        if entry.get("path"):
            keys.add(f"{prefix}{entry['path']}")
    return keys


async def cleanup_orphaned_media(conn: AsyncConnection) -> MaintenanceResult:
    """Job (b) — delete ATTACHMENT blobs (keys under attachments/ — the _select_orphans prefix
    fence; basemaps/ and field-projects/ tenants are never touched) that no live media.attachments row
    references, AND (since V100) REPORT ARTIFACT blobs under reports/ that no platform.report_artifacts
    row references — each prefix against its own registry — that are older than the configured grace window (V068 generalized this from the
    sign-only media.photos to every feature attachment — the object-key set is the single authority,
    so the diff is unchanged in shape).

    An attachment blob orphans whenever its row goes away without the blob following: a layer drop
    (media.attachments.layer_id ON DELETE CASCADE), an attachment delete, or a FEATURE delete (all four
    feature-delete sites cascade as of decision-feature-delete-attachments.md) removes the ROW but the
    routers' best-effort blob delete can miss (a store hiccup, a crash between commit and remove). The grace
    window protects an in-flight upload whose DB row hasn't committed yet (a blob younger than the grace is
    never even considered). This is the job that exercises the seam's object-store capability — the reason
    we chose an app-internal task over pg_cron. Cruft only — HARD FENCE (decision §2): authoritative media
    (a live attachment row) is NEVER touched, because a referenced key is excluded by definition.

    WHAT THIS JOB CANNOT DO — and why the fix had to go on the write path. The liveness test below is
    "is this object_key still in media.attachments?", so this job can only ever collect a blob whose ROW
    IS ALREADY GONE. It is structurally incapable of noticing a LEAKED ROW: a surviving row makes its key
    look permanently live, and _select_orphans excludes it by definition. Until the cascade landed, a
    feature delete left exactly that — rows AND blobs, uncollectable forever — and this docstring claimed
    the opposite ("a feature/attachment delete removes the ROW"), which was true of attachment delete and
    false of feature delete. Orphan ROWS left from before the cascade were cleaned once by a
    development-tree script; this job then collects their blobs normally."""
    # List the bucket first, then read the live key set, so any row committed before the (cheap) DB read
    # is in live_keys. Both that ordering AND the grace window protect a concurrent upload — defence in
    # depth; the grace window is the primary guarantee (we only ever consider blobs >= grace old).
    objects = await run_in_threadpool(list_object_keys)
    async with conn.cursor() as cur:
        await cur.execute("SELECT object_key FROM media.attachments")
        live_keys = {r[0] for r in await cur.fetchall()}
        # The reports/ leg (decision-scheduled-reports.md F6/r5): artifact bytes whose row is gone —
        # a report delete (rows cascade; bytes are left for THIS sweep so a delete never races an
        # in-flight download), a retention prune whose best-effort blob delete missed, or a crash
        # between the store write and the row insert. Same grace window, same liveness rule, its
        # own registry — TWO tables: artifacts AND the reports row's template key (below). The
        # transient `_maps/<job>.png` staging keys are deliberately sweepable. An empty table (free install: the module isn't mounted) simply means every
        # aged reports/ key is an orphan — and in a free install there are none.
        await cur.execute("SELECT object_key FROM platform.report_artifacts")
        reports_live_keys = {r[0] for r in await cur.fetchall()}
        # The r8 Word template lives under the same prefix but is registered on the REPORT row, not
        # the artifacts table (security review 2026-09-05: without this union a template older than
        # the grace window was swept while its row still pointed at it — the next run failed).
        await cur.execute("SELECT template_object_key FROM platform.reports WHERE template_object_key IS NOT NULL")
        reports_live_keys |= {r[0] for r in await cur.fetchall()}
        # The field-packs/ leg (V105): every file the SERVED build's manifest names is live — the
        # manifest itself, the basemap clip, one file per layer. A pack mid-build has no manifest yet
        # and its build/<n+1>/ files are younger than the grace window, so they are never judged.
        await cur.execute("SELECT id::text, build_no, manifest FROM platform.field_packs WHERE manifest IS NOT NULL")
        field_packs_live_keys = set()
        for pid, build_no, manifest in await cur.fetchall():
            field_packs_live_keys |= _field_pack_live_keys(pid, int(build_no), manifest or {})
    # Release the read snapshot before the (potentially slow) object-store delete loop — we don't want a
    # transaction open across store I/O. The advisory lock is session-scoped, so it survives the rollback.
    await conn.rollback()
    grace_days = settings.maintenance_orphaned_media_grace_days
    cutoff = datetime.now(timezone.utc) - timedelta(days=grace_days)
    orphans = _select_orphans(objects, live_keys, cutoff, reports_live_keys=reports_live_keys,
                              field_packs_live_keys=field_packs_live_keys)
    freed = 0
    for orphan in orphans:
        await run_in_threadpool(remove_object, orphan.key)
        freed += orphan.size
    n = len(orphans)
    return MaintenanceResult(
        items_affected=n, bytes_freed=freed,
        detail=f"removed {n} orphaned blob(s) older than {grace_days}d, freed {freed} bytes",
    )


async def media_usage(conn: AsyncConnection) -> MaintenanceResult:
    """Job (c) — CACHE the media bucket's total size (decision-system-health.md F2). A bucket walk is
    O(objects) and must never run inline in a page request, so the Admin → System storage tile reads
    this job's last run instead. READ-ONLY against the bucket (the fence is untouched — the only
    write is this job's own maintenance_runs row); the cached value rides `detail` in the parseable
    'objects=N bytes=M' shape. bytes_freed stays 0 honestly — nothing is freed here."""
    objects = await run_in_threadpool(list_object_keys)
    total = sum(o.size for o in objects)
    return MaintenanceResult(
        items_affected=len(objects), bytes_freed=0,
        detail=f"objects={len(objects)} bytes={total}",
    )


def _housekeeping_jobs() -> list[MaintenanceJob]:
    """The static cruft-cleanup jobs (env-interval; NOT runtime-editable — they have no admin UI). Always
    present; the consumer-agnostic core of the harness."""
    interval = settings.maintenance_run_interval_seconds
    return [
        MaintenanceJob("prune_applied_op_keys", interval, prune_applied_op_keys),
        MaintenanceJob("cleanup_orphaned_media", interval, cleanup_orphaned_media),
        # Daily, independent of the cruft cadence: the number changes slowly and the walk is the cost.
        MaintenanceJob("media_usage", 86_400, media_usage),
    ]


def _backup_jobs(eff: EffectiveBackupSettings) -> list[MaintenanceJob]:
    """The DR jobs at the EFFECTIVE cadence (Talus 43). Included ONLY when scheduled backups are enabled —
    the runtime toggle (`scheduled_enabled`, defaulting to the BACKUP_ENABLED env master). When disabled the
    list is empty (scheduled runs pause) but manual "Back up now" still works (it bypasses this list). The
    effective recipients/retention are threaded into the jobs via `eff`, so they read the same per-tick
    snapshot the scheduler resolved on the lock-holding conn."""
    if not eff.scheduled_enabled:
        return []
    # Local import breaks the cycle: backup.py imports MaintenanceResult from this module, so it can't be
    # imported at module top here. The DR jobs run on the SAME loop + advisory lock as the cruft jobs.
    from .backup import prune_old_backups, run_backup

    return [
        MaintenanceJob("scheduled_backup", eff.interval_seconds, lambda c: run_backup(c, "scheduled", eff)),
        MaintenanceJob("prune_old_backups", eff.interval_seconds, lambda c: prune_old_backups(c, eff)),
    ]


def registered_job_names() -> list[str]:
    """The canonical job names — so the Admin → Maintenance panel can show a row for every job (including one
    that has never run yet). Housekeeping always; the backup jobs whenever the deployment does DR at all
    (BACKUP_ENABLED env master) — independent of the runtime pause toggle, so a paused job still shows its
    history."""
    names = [job.name for job in _housekeeping_jobs()]
    if settings.backup_enabled:
        names += ["scheduled_backup", "prune_old_backups"]
    # Not a scheduled job: the T44 drill script writes this row itself on a successful drill
    # (decision-system-health.md F1). Registered so the panels show "never run" honestly until
    # the first drill — visibility is the point.
    names.append("restore_drill")
    return names


# ---- Scheduler plumbing -------------------------------------------------------------------------

async def _try_advisory_lock(conn: AsyncConnection) -> bool:
    """Non-blocking attempt at the scheduler-wide session lock. True => this replica owns this tick."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT pg_try_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,))
        row = await cur.fetchone()
    return bool(row and row[0])


async def _advisory_unlock(conn: AsyncConnection) -> None:
    async with conn.cursor() as cur:
        await cur.execute("SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_KEY,))


async def _is_due(conn: AsyncConnection, job_name: str, interval_seconds: int) -> bool:
    """DB-authoritative due-check: has this job NOT SUCCEEDED within its interval? Reading
    maintenance_runs (not a per-process timer) is what stops two replicas double-running a job inside
    one interval — the per-process clock would let a replica that lost the lock retry and re-run.

    Only status='ok' runs count toward the interval: a job that threw is therefore due again on the
    NEXT tick (the decision-doc fail-safe — a transient failure must not silently delay cleanup a full
    interval), while a healthy job still runs at most once per interval. A persistently-failing job
    retries each tick, recording an 'error' row each time — a loud, visible alarm in the Admin panel."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT max(started_at) < now() - make_interval(secs => %s) "
            "FROM platform.maintenance_runs WHERE job_name = %s AND status = 'ok'",
            (interval_seconds, job_name),
        )
        row = await cur.fetchone()
    # No prior SUCCESS => max() is NULL => the predicate is NULL => row[0] is None => due.
    return row is None or row[0] is None or bool(row[0])


async def _record_run(
    conn: AsyncConnection, job_name: str, started_at: datetime, status: str,
    items_affected: int, bytes_freed: int, detail: str | None,
) -> None:
    """Insert one terminal maintenance_runs row. Best-effort: a recorder failure is logged but never
    propagates — observability must not be able to crash the scheduler."""
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO platform.maintenance_runs "
                "(job_name, started_at, status, items_affected, bytes_freed, detail) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (job_name, started_at, status, items_affected, bytes_freed, detail),
            )
        await conn.commit()
    except Exception:  # noqa: BLE001
        log.warning("maintenance_record_failed", job=job_name, exc_info=True)


async def _run_and_record(conn: AsyncConnection, job: MaintenanceJob) -> None:
    """Run one job, capturing its outcome into maintenance_runs. A throwing job is rolled back (it may
    have left an aborted transaction on this shared connection), recorded as 'error', and the loop
    moves on — fail-safe."""
    started_at = datetime.now(timezone.utc)
    status, items, freed, detail = "ok", 0, 0, None
    try:
        result = await job.run(conn)
        items, freed, detail = result.items_affected, result.bytes_freed, result.detail
    except Exception as exc:  # noqa: BLE001 — a job must never crash the loop
        await conn.rollback()
        status, detail = "error", str(exc)[:1000]
        log.warning("maintenance_job_failed", job=job.name, exc_info=True)
    await _record_run(conn, job.name, started_at, status, items, freed, detail)
    log.info("maintenance_job_ran", job=job.name, status=status, items=items, bytes_freed=freed)


async def _run_due_jobs() -> None:
    """One tick. Borrow a connection, try the advisory lock; if we own it, assemble the active job list FROM
    the lock-holding connection (housekeeping + the DR jobs at their effective cadence/enabled — Talus 43),
    run every due job on that same connection, and release. If another replica holds the lock, skip this tick.

    Building the list inside the lock means exactly the replica that runs the jobs reads the settings — one
    cheap SELECT per tick, no extra connection. If the lock can't be released cleanly, CLOSE the connection
    rather than return a lock-holding connection to the pool — a session advisory lock would otherwise
    persist on the pooled physical connection and silently wedge all future ticks that reuse it."""
    async with get_pool().connection() as conn:
        if not await _try_advisory_lock(conn):
            return  # another api replica is running maintenance this tick
        try:
            jobs = _housekeeping_jobs()
            eff = await get_effective_backup_settings(conn)
            jobs += _backup_jobs(eff)
            for job in jobs:
                if await _is_due(conn, job.name, job.interval_seconds):
                    await _run_and_record(conn, job)
        finally:
            try:
                await _advisory_unlock(conn)
            except Exception:  # noqa: BLE001
                await conn.close()
                log.warning("maintenance_unlock_failed_conn_closed", exc_info=True)


async def run_job_once_locked(job: MaintenanceJob) -> bool:
    """Run a SINGLE job immediately under the scheduler advisory lock (the path "Back up now" uses). Returns
    True if it ran, False if another replica/tick holds the lock. Mirrors _run_due_jobs' lock discipline
    (always release; close the connection if unlock fails) but runs exactly one job unconditionally — a
    manual trigger always runs, no due-check. Reuses the exact same lock key, so a manual backup can never
    overlap a scheduled tick or another replica's run."""
    async with get_pool().connection() as conn:
        if not await _try_advisory_lock(conn):
            return False
        try:
            await _run_and_record(conn, job)
        finally:
            try:
                await _advisory_unlock(conn)
            except Exception:  # noqa: BLE001
                await conn.close()
                log.warning("maintenance_unlock_failed_conn_closed", exc_info=True)
    return True


async def run_scheduler_loop() -> None:
    """The lifespan-managed loop. Sleeps a tick, runs due jobs, repeats. A failed tick is logged and
    retried next tick; the loop only ever exits on cancellation (app shutdown). The active job list is
    assembled per tick (Talus 43), so a cadence/enabled change takes effect on the next tick — no restart."""
    log.info(
        "maintenance_scheduler_started",
        jobs=registered_job_names(),
        tick_seconds=settings.maintenance_tick_seconds,
    )
    try:
        while True:
            await asyncio.sleep(settings.maintenance_tick_seconds)
            try:
                await _run_due_jobs()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — never let a bad tick kill the loop
                log.warning("maintenance_tick_failed", exc_info=True)
    except asyncio.CancelledError:
        log.info("maintenance_scheduler_stopped")
        raise
