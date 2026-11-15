-- V003 — generic audit trigger function
--
-- One function, many triggers (one per asset table). The application sets the current user
-- at transaction start via `SET LOCAL app.current_user_id = '<uuid>'`. Optional `app.current_client`
-- tags the originating client (portal-api, qfield-sync, etc.).
--
-- Trigger args: TG_ARGV[0] = fully-qualified audit table name, TG_ARGV[1] = PK column name in the source row.

CREATE OR REPLACE FUNCTION audit.fn_record_change() RETURNS trigger AS $$
DECLARE
    v_user uuid := nullif(current_setting('app.current_user_id', true), '')::uuid;
    v_client text := nullif(current_setting('app.current_client', true), '');
    v_audit_table text := TG_ARGV[0];
    v_id_col text := TG_ARGV[1];
BEGIN
    EXECUTE format(
        'INSERT INTO %s (asset_id, operation, changed_at, changed_by, client_info, before_row, after_row)
         VALUES ($1, $2, now(), $3, $4, $5, $6)',
        v_audit_table
    )
    USING
        CASE WHEN TG_OP = 'DELETE'
             THEN (row_to_json(OLD)->>v_id_col)::uuid
             ELSE (row_to_json(NEW)->>v_id_col)::uuid END,
        TG_OP,
        v_user,
        jsonb_build_object('client', v_client),
        CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE row_to_json(OLD)::jsonb END,
        CASE WHEN TG_OP = 'DELETE' THEN NULL ELSE row_to_json(NEW)::jsonb END;
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;
