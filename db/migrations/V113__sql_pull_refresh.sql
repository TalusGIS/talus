-- V113 — SQL-source pull s3: keyed refresh, schedule, stale posture, structural edit lock
-- (decision-sql-source-pull.md §11, RULED 2026-10-02). Tier-2, backup-gated.
--
-- S3-A  sql_pull_rows   — the diff state: one row per copied row (source key → row id + content hash). The copy's
--                         `props` stay the owner's fields only; a refresh writes only what changed, and the V003
--                         audit trail reflects real change (trap 5.1 closed as designed). Keyless sources use the
--                         content hash as the key (`<hash>#<n>` for duplicates).
-- S3-B  schedule        — manual · nightly (at schedule_hour in the connection's time zone) · interval (≥ 60 min);
--                         next_run_at is computed by the api on save and after each run; the worker enqueues due pulls.
-- S3-C  stale posture   — stale_since / last_error set by a failed run (the copy is untouched), cleared by success.
-- S3-D  the lock        — a trigger on platform.layers refuses turning any edit flag ON while a sql_pulls row targets
--                         the layer; the settings door maps the refusal to a 409. Detach deletes the pull row FIRST.
-- S3-F  run counters    — updated / deleted / unchanged beside inserted.

CREATE TABLE platform.sql_pull_rows (
    pull_id     uuid NOT NULL REFERENCES platform.sql_pulls(id) ON DELETE CASCADE,
    source_key  text NOT NULL CHECK (length(source_key) BETWEEN 1 AND 512),
    row_id      uuid NOT NULL,
    row_hash    text NOT NULL CHECK (length(row_hash) = 64),       -- sha256 hex of the canonical row
    seen_run_id uuid,
    PRIMARY KEY (pull_id, source_key)
);
CREATE INDEX sql_pull_rows_row_idx ON platform.sql_pull_rows (pull_id, row_id);
COMMENT ON TABLE platform.sql_pull_rows IS
    'SQL-source pull diff state (s3): source key → copied row id + content hash. Keyless sources key by hash.';

ALTER TABLE platform.sql_pulls
    ADD COLUMN schedule_kind    text NOT NULL DEFAULT 'manual' CHECK (schedule_kind IN ('manual', 'nightly', 'interval')),
    ADD COLUMN schedule_hour    smallint CHECK (schedule_hour IS NULL OR schedule_hour BETWEEN 0 AND 23),
    ADD COLUMN schedule_minutes integer CHECK (schedule_minutes IS NULL OR schedule_minutes >= 60),
    ADD COLUMN next_run_at      timestamptz,
    ADD COLUMN stale_since      timestamptz,
    ADD COLUMN last_success_at  timestamptz,
    ADD COLUMN last_error       text CHECK (last_error IS NULL OR length(last_error) <= 300),
    ADD CONSTRAINT sql_pulls_schedule_shape CHECK (
        (schedule_kind = 'manual'   AND schedule_hour IS NULL AND schedule_minutes IS NULL) OR
        (schedule_kind = 'nightly'  AND schedule_hour IS NOT NULL AND schedule_minutes IS NULL) OR
        (schedule_kind = 'interval' AND schedule_hour IS NULL AND schedule_minutes IS NOT NULL)
    );
CREATE INDEX sql_pulls_due_idx ON platform.sql_pulls (next_run_at) WHERE schedule_kind <> 'manual';

ALTER TABLE platform.sql_pull_runs
    ADD COLUMN updated_rows   integer NOT NULL DEFAULT 0,
    ADD COLUMN deleted_rows   integer NOT NULL DEFAULT 0,
    ADD COLUMN unchanged_rows integer NOT NULL DEFAULT 0;

-- S3-D: the structural lock. The run writes the copy directly (never through the edit doors), so the lock costs
-- the pull nothing; an owner cannot flip the flags back while the source is attached.
CREATE OR REPLACE FUNCTION platform.sql_pull_lock_guard() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    IF (NEW.editing_enabled OR NEW.edit_add OR NEW.edit_attributes OR NEW.edit_geometry OR NEW.edit_delete)
       AND EXISTS (SELECT 1 FROM platform.sql_pulls p WHERE p.target_layer_id = NEW.layer_id) THEN
        RAISE EXCEPTION 'layer % is kept up to date from a database pull; detach the pull to edit it', NEW.layer_id
            USING ERRCODE = 'check_violation', HINT = 'sql_pull_locked';
    END IF;
    RETURN NEW;
END
$fn$;
CREATE TRIGGER sql_pull_lock
    BEFORE UPDATE OF editing_enabled, edit_add, edit_attributes, edit_geometry, edit_delete ON platform.layers
    FOR EACH ROW EXECUTE FUNCTION platform.sql_pull_lock_guard();
