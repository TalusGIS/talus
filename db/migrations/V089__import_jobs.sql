-- V089 — bulk-import jobs (decision-9d-bulk-import.md §4). Tier-2, backup-gated.
--
-- The 9d bulk-import door runs ASYNC: a publisher streams a file (zipped SHP / GPKG / GeoJSON)
-- to a jailed per-job staging directory, a row is enqueued HERE, and a background worker
-- (api/app/import_batch_worker.py — advisory-locked, exactly-one-replica, the V081 pattern)
-- parses the staged file in streaming batches through the E4 ingest core (auto-map,
-- reproject-in, PostGIS-side geometry), inserting per batch and updating the counters. Unlike
-- geocode-batch, the STAGED FILE is the work source and it is DELETED at every terminal state
-- (completed/failed/canceled) — so a crash mid-'running' resumes by RESTARTING the parse into
-- the partial target: inserted_rows records the high-water mark and the worker skips that many
-- source rows on resume (the source order is stable: file order).
--
-- County-scale honesty: a 300k-row import cannot return 300k row verdicts. The job carries
-- COUNTS plus a BOUNDED error sample (first N row errors, JSON array, hard-capped) — the full
-- verdict contract of the synchronous E4 door does not scale and is deliberately not promised.
-- NO SECRET lives here (V081 principle); staged paths are server-derived from the job id, never
-- from a client-supplied name.

CREATE TABLE platform.import_jobs (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    status           text NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'running', 'completed', 'failed', 'canceled')),
    -- Source format (the v1 set; the endpoint validates before a row exists).
    format           text NOT NULL
                       CHECK (format IN ('shp-zip', 'gpkg', 'geojson')),
    original_name    text NOT NULL,                 -- client filename, DISPLAY ONLY (never a path)
    staged_bytes     bigint NOT NULL DEFAULT 0,     -- as received (post-cap enforcement)
    -- Target: either a NEW layer provisioned at job start, or an existing layer appended to.
    -- CASCADE mirrors V081: releasing the target layer discards its import history row.
    target_layer_id  text NOT NULL REFERENCES platform.layers(layer_id) ON DELETE CASCADE,
    created_new      boolean NOT NULL,              -- true = this job provisioned the layer
    source_srid      integer,                       -- override when the source carries no CRS (E4 rules)
    total_rows       integer,                       -- NULL until the parser has counted/estimated
    processed_rows   integer NOT NULL DEFAULT 0,    -- parsed (progress; worker-updated per batch)
    inserted_rows    integer NOT NULL DEFAULT 0,    -- committed to the target (resume high-water mark)
    failed_rows      integer NOT NULL DEFAULT 0,    -- per-row verdicts that refused
    -- Bounded sample of row-level errors (JSON array [{row, reason}, ...], first N only — the
    -- worker hard-caps entries; counts above carry the truth at scale).
    error_sample     jsonb NOT NULL DEFAULT '[]'::jsonb,
    error            text,                          -- terminal job-level error (first-line, bounded)
    created_by       uuid NOT NULL REFERENCES platform.users(id),
    created_at       timestamptz NOT NULL DEFAULT now(),
    started_at       timestamptz,
    finished_at      timestamptz,
    updated_at       timestamptz NOT NULL DEFAULT now() -- heartbeat: bumped each batch
);

-- Worker queue scan (FIFO over live jobs; terminal jobs excluded — V081 pattern).
CREATE INDEX import_jobs_active_idx ON platform.import_jobs (created_at)
    WHERE status IN ('pending', 'running');

-- Owner's job list + the authz lookup on GET/cancel (owner-scoped: cross-user reads 404).
CREATE INDEX import_jobs_owner_idx ON platform.import_jobs (created_by, created_at DESC);

COMMENT ON TABLE platform.import_jobs IS
    'Async bulk-import queue (decision-9d-bulk-import.md). Run-level state + bounded error '
    'sample only; the jailed staged file is the work source and is deleted at every terminal '
    'state. No secret stored; staged paths derive from the job id, never client input.';
