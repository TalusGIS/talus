-- V040 — Talus 40: disaster-recovery backup ARTIFACT catalog.
-- docs/decision-dr-backup-restore.md §5. Tier-2, backup-gated (HARD RULE #1).
--
-- The T39 scheduler seam (api/app/maintenance.py) gains a second consumer: a scheduled_backup job
-- (pg_dump -Fc -> object storage + an incremental append-only copy of the media bucket(s)) and a
-- prune_old_backups retention job. The scheduler's RUN history stays in platform.maintenance_runs
-- (reused — "the backup job ran at T, status ok"). THIS table is the distinct ARTIFACT catalog: one row
-- per backup *set* that actually exists, so the Admin -> Backups panel can list/download them and the
-- retention job can prune by age + a count floor.
--
-- HARD FENCE (decision §2): retention deletes OLD BACKUPS ONLY (rows here + their object-store artifacts),
-- NEVER live layers/features and NEVER the audit log, and NEVER below the M-most-recent floor (a misconfig
-- can't prune to zero). This table is itself operational, not authoritative data; it has no FK to GIS data.
--
-- A row is inserted at backup COMPLETION, so status is terminal ('ok' | 'error'). An 'error' row records a
-- failed attempt (no usable artifact) and is a visible alarm; the retention floor counts only 'ok' rows, so
-- a run of failures can never erode the supply of *recoverable* backups.

CREATE TABLE platform.backups (
    id                   bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at           timestamptz NOT NULL DEFAULT now(),   -- when the backup set was taken (the sort/retention key)
    finished_at          timestamptz NOT NULL DEFAULT now(),
    kind                 text        NOT NULL CHECK (kind IN ('scheduled', 'manual')),  -- scheduled tick vs "Back up now"
    status               text        NOT NULL CHECK (status IN ('ok', 'error')),
    db_object_key        text,                                  -- pg_dump -Fc artifact key in the backup target (NULL if the dump failed)
    db_size_bytes        bigint      NOT NULL DEFAULT 0,        -- size of the dump artifact
    db_sha256            text,                                  -- integrity digest of the dump (verify on download/restore)
    media_objects_copied bigint      NOT NULL DEFAULT 0,        -- NEW blobs appended to the media archive this run (incremental)
    media_bytes_copied   bigint      NOT NULL DEFAULT 0,
    target               text        NOT NULL,                  -- human-legible backup target, e.g. 'minio://talus-backups' (off-host endpoint when configured)
    pg_version           text,                                  -- server version captured at dump time (restore-compat legibility)
    detail               text                                   -- one-line summary, or the error message on 'error'
);

-- The Admin -> Backups list ("newest first") + the retention scan both read by recency.
CREATE INDEX backups_created_idx ON platform.backups (created_at DESC);

COMMENT ON TABLE platform.backups IS
    'Talus 40: one row per disaster-recovery backup artifact (pg_dump + incremental media archive). '
    'Distinct from platform.maintenance_runs (the scheduler run log). See docs/decision-dr-backup-restore.md.';
