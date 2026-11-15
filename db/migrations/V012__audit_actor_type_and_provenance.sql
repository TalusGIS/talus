-- V012 — audit: actor_type + provenance (Architecture Principle 9)
--
-- Extends the existing SINGLE audit system rather than adding a parallel event
-- table: the V003 generic trigger function + the V007 signs shadow table. A DB
-- trigger is un-bypassable; an app-layer insert is not — so provenance rides the
-- trigger, keeping the audit forensically authoritative.
--
-- audit.signs_audit is the only table audit.fn_record_change() writes to today, so
-- it is the only one that needs the new columns. Future shadow tables created with
-- this generic function must include actor_type + provenance.
--
-- The API sets these per mutation via SET LOCAL inside the same transaction:
--   app.current_user_id     -> changed_by  (existing)
--   app.current_actor_type  -> actor_type  ('human' | 'ai_inference'; default 'human')
--   app.current_provenance  -> provenance  (jsonb; null for human edits)

-- 1) Extend the shadow table. NOT NULL DEFAULT 'human' backfills existing rows
--    to 'human' in place — no separate UPDATE needed.
ALTER TABLE audit.signs_audit ADD COLUMN actor_type text NOT NULL DEFAULT 'human';
ALTER TABLE audit.signs_audit ADD CONSTRAINT signs_audit_actor_type_chk
    CHECK (actor_type IN ('human', 'ai_inference'));
ALTER TABLE audit.signs_audit ADD COLUMN provenance jsonb;

-- 2) Teach the generic function to read the two new GUCs and write the two new
--    columns. Generic-safe: signs_audit (its only consumer) gains the columns above
--    in this same migration.
CREATE OR REPLACE FUNCTION audit.fn_record_change() RETURNS trigger AS $$
DECLARE
    v_user uuid := nullif(current_setting('app.current_user_id', true), '')::uuid;
    v_client text := nullif(current_setting('app.current_client', true), '');
    v_actor_type text := coalesce(nullif(current_setting('app.current_actor_type', true), ''), 'human');
    v_provenance jsonb := nullif(current_setting('app.current_provenance', true), '')::jsonb;
    v_audit_table text := TG_ARGV[0];
    v_id_col text := TG_ARGV[1];
BEGIN
    EXECUTE format(
        'INSERT INTO %s (asset_id, operation, changed_at, changed_by, actor_type, provenance, client_info, before_row, after_row)
         VALUES ($1, $2, now(), $3, $4, $5, $6, $7, $8)',
        v_audit_table
    )
    USING
        CASE WHEN TG_OP = 'DELETE'
             THEN (row_to_json(OLD)->>v_id_col)::uuid
             ELSE (row_to_json(NEW)->>v_id_col)::uuid END,
        TG_OP,
        v_user,
        v_actor_type,
        v_provenance,
        jsonb_build_object('client', v_client),
        CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE row_to_json(OLD)::jsonb END,
        CASE WHEN TG_OP = 'DELETE' THEN NULL ELSE row_to_json(NEW)::jsonb END;
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;
