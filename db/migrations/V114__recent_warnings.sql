-- V114 — the recent-warnings ring survives a restart.
-- Tier-2, backup-gated.
--
-- The api keeps its last ≤ 200 WARNING+ structlog records in process memory (app/logging_config.py), and a
-- restart — the moment a box is most likely to be asked "what went wrong" — empties it. A lifespan flusher
-- (app/recent_warnings_store.py) copies new ring records here every few seconds, tagged with the process's
-- boot id, and the snapshot's `recent_errors` reads memory first, then this table for what memory lost.
--
-- Bounded on both axes like the ring: ≤ 500 rows (the flusher prunes the oldest after every write) and
-- ≤ 500 chars per text field — the SAME already-scrubbed dicts the ring holds (query-secret scrub applied
-- before capture; the snapshot redacts again on read). Never a traceback, never a connection string.
-- request_id is the contextvar the RequestIDMiddleware binds (item 4 reads by it); background workers
-- have none, so it is nullable.

CREATE TABLE platform.recent_warnings (
    id          bigserial PRIMARY KEY,
    boot_id     text        NOT NULL CHECK (length(boot_id) = 32),       -- uuid4 hex of the api process
    seq         bigint      NOT NULL,                                      -- the ring's monotonic counter
    at          timestamptz,                                               -- the record's own timestamp
    level       text        NOT NULL CHECK (level IN ('warning', 'error', 'critical')),
    logger      text        CHECK (logger IS NULL OR length(logger) <= 200),
    event       text        NOT NULL CHECK (length(event) <= 500),
    fields      jsonb       NOT NULL DEFAULT '{}'::jsonb CHECK (length(fields::text) <= 20000),
    request_id  text        CHECK (request_id IS NULL OR length(request_id) <= 128),
    UNIQUE (boot_id, seq)
);
CREATE INDEX recent_warnings_request_idx ON platform.recent_warnings (request_id) WHERE request_id IS NOT NULL;
COMMENT ON TABLE platform.recent_warnings IS
    'The api''s WARNING+ ring, persisted (V114): ≤ 500 rows, pruned by the flusher; scrubbed before capture. Read by the diagnostic snapshot after a restart.';

-- Default privileges (V070) cover platform tables AND sequences for talus_app → no GRANT needed here.
