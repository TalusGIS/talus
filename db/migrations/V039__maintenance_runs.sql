-- V039 — Talus 39: maintenance-run history (the housekeeping scheduler's observability).
-- docs/decision-housekeeping.md §5 (F-observability). Tier-2, backup-gated.
--
-- Talus has zero scheduled-job infra today. This slice stands up an app-internal periodic-task
-- scheduler (api/app/maintenance.py — a lifespan-managed asyncio loop guarded by a Postgres advisory
-- lock so exactly ONE api replica runs a job even when scaled out; see decision §4 F-scheduler) whose
-- first consumers are safe, N-day-configurable cruft-cleanup jobs. This table is the seam's
-- observability: one row per job run, so an admin can see "cleanup last ran X, freed N" (the
-- self-tidying story), and so slice 3 (DR) can reuse it for backup-run history.
--
-- One row is inserted at job COMPLETION (not at start) — status is therefore terminal ('ok' | 'error'),
-- there is no transient 'running' row to leave stale if a replica crashes. A failed job still records a
-- row (status='error', detail=the message) and is retried on the next tick — the scheduler fails safe.
--
-- HARD FENCE (decision §2): the jobs that write here only ever expire CRUFT (dereferenced idempotency
-- keys, orphaned object-store blobs). They NEVER delete authoritative layers/features and NEVER the
-- audit log. This table is itself not authoritative data — it's an operational log; pruning IT is fine.

CREATE TABLE platform.maintenance_runs (
    id             bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_name       text        NOT NULL,        -- e.g. 'prune_applied_op_keys', 'cleanup_orphaned_media'
    started_at     timestamptz NOT NULL,        -- captured when the job began (so duration is finished-started)
    finished_at    timestamptz NOT NULL DEFAULT now(),
    status         text        NOT NULL CHECK (status IN ('ok', 'error')),
    items_affected bigint      NOT NULL DEFAULT 0,   -- rows pruned / blobs removed
    bytes_freed    bigint      NOT NULL DEFAULT 0,   -- object-store bytes reclaimed (0 for SQL-only jobs)
    detail         text                             -- one-line summary, or the error message on 'error'
);

-- "Most recent run(s) for a job" — the Admin panel's read pattern + the scheduler's due-check
-- (has this job run within its interval?).
CREATE INDEX maintenance_runs_job_started_idx
    ON platform.maintenance_runs (job_name, started_at DESC);

COMMENT ON TABLE platform.maintenance_runs IS
    'Talus 39: one row per housekeeping scheduler job run (last-run + outcome). '
    'See docs/decision-housekeeping.md §5.';
