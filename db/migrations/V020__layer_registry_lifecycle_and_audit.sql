-- V020 — platform.layers claim lifecycle + trigger-grade claim audit (Layer 8, Commit 2).
--
-- Evolves platform.layers for the publisher claim flow (display_name + a status
-- lifecycle) and puts the claim event under the SAME un-bypassable audit trigger that
-- guards every asset table — without touching the sacred V003/V012 audit function.
--
-- Option 4 (generated uuid). audit.fn_record_change() identifies a row by casting its
-- PK to uuid: (row_to_json(NEW)->>id_col)::uuid. platform.layers' PK is text
-- (layer_id), which that cast rejects. So we give platform.layers a STORED generated
-- column, audit_id uuid, derived deterministically from layer_id, and feed THAT column
-- to the unmodified function. Result: trigger-grade claim audit — it catches even a
-- manual DBA UPDATE — with zero changes to the audit function and no parallel audit
-- system. The human-meaningful layer_id/status/display_name all ride in
-- before_row/after_row jsonb, so the full claim story is captured.

-- 1) Lifecycle columns. display_name is the publisher's human label, NULL until a slot
--    is claimed; status drives the claim state machine and defaults to 'unclaimed'.
ALTER TABLE platform.layers
    ADD COLUMN display_name text,
    ADD COLUMN status       text NOT NULL DEFAULT 'unclaimed'
        CHECK (status IN ('unclaimed','provisioning','ready'));

-- 2) Option 4 generated uuid. Derived deterministically from layer_id PURELY to feed
--    the audit trigger's uuid signature — the naive md5(layer_id)::uuid fails (md5
--    returns bare hex text the uuid parser rejects), so we splice md5's 32 hex chars
--    into the 8-4-4-4-12 uuid layout and cast that.
--    This determinism DEPENDS ON layer_id being immutable, which is a locked design
--    property. If layer_id ever becomes mutable/recycled, this derivation must be
--    revisited or the audit trail forks.
ALTER TABLE platform.layers
    ADD COLUMN audit_id uuid GENERATED ALWAYS AS (
        ( substr(md5(layer_id),1,8)  || '-' ||
          substr(md5(layer_id),9,4)  || '-' ||
          substr(md5(layer_id),13,4) || '-' ||
          substr(md5(layer_id),17,4) || '-' ||
          substr(md5(layer_id),21,12) )::uuid
    ) STORED;

-- 3) Shadow audit table — exact 9-column post-V012 shape (mirrors
--    audit.street_lights_audit). Its PK identity column is also named audit_id per the
--    existing shadow-table convention; that is distinct from platform.layers.audit_id.
CREATE TABLE audit.layer_registry_audit (
    audit_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    asset_id     uuid NOT NULL,
    operation    text NOT NULL CHECK (operation IN ('INSERT','UPDATE','DELETE')),
    changed_at   timestamptz NOT NULL DEFAULT now(),
    changed_by   uuid REFERENCES platform.users(id),
    actor_type   text NOT NULL DEFAULT 'human' CHECK (actor_type IN ('human','ai_inference')),
    provenance   jsonb,
    client_info  jsonb,
    before_row   jsonb,
    after_row    jsonb
);

CREATE INDEX layer_registry_audit_asset_idx   ON audit.layer_registry_audit (asset_id);
CREATE INDEX layer_registry_audit_changed_idx ON audit.layer_registry_audit (changed_at);

-- 4) Wire the trigger to the EXISTING function, passing the generated uuid column as
--    the id arg. The function reads (row_to_json(NEW)->>'audit_id')::uuid — audit_id is
--    a real uuid, so the cast succeeds. DO NOT alter audit.fn_record_change.
CREATE TRIGGER platform_layers_audit_trigger
AFTER INSERT OR UPDATE OR DELETE ON platform.layers
FOR EACH ROW EXECUTE FUNCTION audit.fn_record_change('audit.layer_registry_audit', 'audit_id');

-- 5) Backfill status so the column is total and correct. The 5 slot rows stay
--    'unclaimed' (set explicitly though it matches the default); the 3 pre-existing
--    real layers are live, claimed-equivalent, so they become 'ready'.
UPDATE platform.layers SET status = 'unclaimed' WHERE layer_id LIKE 'tal_%';
UPDATE platform.layers SET status = 'ready'
    WHERE layer_id IN ('signs', 'street_lights', 'county_boundary');
