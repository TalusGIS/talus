-- V081 — batch geocoding jobs (decision-batch-geocoding.md, F5). Tier-2, backup-gated.
--
-- Batch (bulk) geocoding runs ASYNC: every run provisions an output point layer, inserts each input
-- row up front as a null-geom `pending` feature (the assembled address in the layer's geocode_input
-- field), and enqueues a job HERE. A background worker (api/app/geocode_batch_worker.py — advisory-
-- locked, exactly-one-replica, SEPARATE from the maintenance scheduler's cruft-fence) geocodes the
-- pending features in chunks, filling geometry + the match fields, committing per chunk. The OUTPUT
-- LAYER is the work queue (pending features = remaining work), so a crash resumes with nothing to
-- replay; this table holds only run-level state: status, the chosen locator, the output layer, the
-- default threshold, progress counters, and any terminal error.
--
-- NO SECRET lives here: the worker re-resolves the locator (and decrypts its token) fresh from
-- platform.geocoders each chunk, so a token never lands in job state / a pg_dump / a status response.
-- The row is created AFTER the output layer is provisioned, so the output_layer_id FK is satisfiable;
-- ON DELETE CASCADE means releasing the output layer discards its job (a job is meaningless without it).

CREATE TABLE platform.geocode_batch_jobs (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- pending: enqueued, not yet claimed · running: a worker is geocoding its features · completed:
    -- no pending features remain · failed: a terminal error (message in `error`) · canceled: the owner
    -- stopped it (partial output layer kept, un-run rows marked canceled). The worker resumes any
    -- pending/running job with pending features, so a crash mid-'running' needs no special recovery.
    status           text NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'running', 'completed', 'failed', 'canceled')),
    -- The chosen locator NAME (resolved to a live GeocoderConfig from platform.geocoders at run time —
    -- NOT an FK, since env-union locators have no row, and a later rename shouldn't orphan history).
    locator_name     text NOT NULL,
    -- The provisioned output point layer. CASCADE: releasing it discards the job (see header).
    output_layer_id  text NOT NULL REFERENCES platform.layers(layer_id) ON DELETE CASCADE,
    -- The run's default match-score cut (F4a). Per-row raw scores live on the features, so the review
    -- UI re-buckets client-side against any threshold without re-geocoding; this is just the default.
    threshold        numeric NOT NULL DEFAULT 85,
    -- Whether this run consumes external credits (F6-C) — recorded for the audit/positioning trail.
    credit_consuming boolean NOT NULL DEFAULT false,
    total_rows       integer NOT NULL DEFAULT 0,   -- rows enqueued at run time (features created)
    processed_rows   integer NOT NULL DEFAULT 0,   -- non-pending features (progress; worker-updated)
    matched_count    integer NOT NULL DEFAULT 0,   -- features that resolved at/above the threshold
    error            text,                          -- set when status='failed' (first-line, bounded)
    created_by       uuid NOT NULL REFERENCES platform.users(id),
    created_at       timestamptz NOT NULL DEFAULT now(),
    started_at       timestamptz,                   -- first time a worker claimed it
    finished_at      timestamptz,                   -- completed / failed / canceled
    updated_at       timestamptz NOT NULL DEFAULT now()  -- heartbeat: bumped each chunk (observability)
);

-- The worker's queue scan: the oldest job with work to do (FIFO). Partial index keeps it tiny —
-- terminal jobs (the vast majority over time) are excluded.
CREATE INDEX geocode_batch_jobs_active_idx ON platform.geocode_batch_jobs (created_at)
    WHERE status IN ('pending', 'running');

-- The owner's / admin's job list + the authz lookup on GET/cancel.
CREATE INDEX geocode_batch_jobs_owner_idx ON platform.geocode_batch_jobs (created_by, created_at DESC);

COMMENT ON TABLE platform.geocode_batch_jobs IS
    'Async batch-geocoding run queue (decision-batch-geocoding.md F5). Run-level state only; the output '
    'layer''s pending features are the work queue. No secret stored — the worker resolves the locator '
    'token fresh from platform.geocoders each chunk.';
