-- V007 — shadow audit table for signs + trigger
--
-- Audit is on by default. To turn it off for a customer:
--   DROP TRIGGER signs_audit_trigger ON assets.signs;
-- Existing audit data is preserved.

CREATE TABLE audit.signs_audit (
    audit_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    asset_id     uuid NOT NULL,
    operation    text NOT NULL CHECK (operation IN ('INSERT','UPDATE','DELETE')),
    changed_at   timestamptz NOT NULL DEFAULT now(),
    changed_by   uuid REFERENCES platform.users(id),
    client_info  jsonb,
    before_row   jsonb,
    after_row    jsonb
);

CREATE INDEX signs_audit_asset_idx   ON audit.signs_audit (asset_id);
CREATE INDEX signs_audit_changed_idx ON audit.signs_audit (changed_at);

CREATE TRIGGER signs_audit_trigger
AFTER INSERT OR UPDATE OR DELETE ON assets.signs
FOR EACH ROW EXECUTE FUNCTION audit.fn_record_change('audit.signs_audit', 'id');
