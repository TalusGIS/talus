-- V059 — standalone non-spatial (hosted) tables (decision-nonspatial-tables.md, part a).
--
-- A "table" is modeled as a GEOMETRY-LESS platform.layers layer (Approach A):
-- it reuses the entire feature/schema/authz/catalog stack (feature query, /columns, export, the
-- attribute grid, owner-defined fields + coded domains + constraint rules bound by layer_id, the
-- `layer` OpenFGA sharing model, metadata, favorites). The ONLY discriminator is geom_type='none'.
--
-- This migration does two things — NO existing layer/slot is touched:
--   1) Widen the geom_type CHECK to admit 'none' (aspatial).
--   2) Add platform.provision_hosted_table(text): a TEMPLATE-FIXED, scoped DDL helper the API calls
--      at create-time (on-demand creation, §4.2 — no pre-minted pool). It builds one
--      assets.<id> table with the SAME schema a slot has today MINUS geom/GiST/tile — id + edit-tracking
--      + row_version + props + notes — plus the props GIN, the feature-search GIN, the 9-column shadow
--      audit table, and the three standard triggers (audit / props-validate / row_version). The physical
--      shape is fixed (owner fields live in `props`, rules-as-data) — never arbitrary runtime columns.

-- 1) geom_type now allows 'none' (aspatial tables). ------------------------------------------------
ALTER TABLE platform.layers DROP CONSTRAINT layers_geom_type_valid;
ALTER TABLE platform.layers
  ADD CONSTRAINT layers_geom_type_valid
  CHECK (geom_type = ANY (ARRAY['point'::text, 'line'::text, 'polygon'::text, 'none'::text]));

-- 2) The scoped, template-fixed table provisioner. -------------------------------------------------
-- Called as: SELECT platform.provision_hosted_table('tal_<8hex>');  (id is app-generated + collision-checked,
-- and matches ^tal_[0-9a-f]{8}$ so it reuses the slot CRUD + slot API + isSlotId with no regex change.)
-- %I quotes every identifier; the id is the ONLY input and it is a validated token, never raw user text.
CREATE OR REPLACE FUNCTION platform.provision_hosted_table(p_id text)
RETURNS void
LANGUAGE plpgsql
AS $fn$
BEGIN
  -- The feature table: identical to a claimed slot's current schema, minus geom (and thus no GiST/tile fn).
  EXECUTE format($ddl$
    CREATE TABLE assets.%I (
      id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      created_at     timestamptz NOT NULL DEFAULT now(),
      created_by     uuid NOT NULL REFERENCES platform.users(id),
      updated_at     timestamptz NOT NULL DEFAULT now(),
      updated_by     uuid NOT NULL REFERENCES platform.users(id),
      ai_provenance  jsonb,
      notes          text,
      row_version    bigint NOT NULL DEFAULT pg_current_xact_id()::text::bigint,
      props          jsonb NOT NULL DEFAULT '{}'::jsonb
    )
  $ddl$, p_id);

  -- Owner-field props index + the field-agnostic feature-search index (V058), same as a slot.
  EXECUTE format('CREATE INDEX %I ON assets.%I USING GIN (props)', p_id || '_props_idx', p_id);
  EXECUTE format(
    'CREATE INDEX %I ON assets.%I USING GIN (assets.search_document(notes, props))',
    p_id || '_search_idx', p_id
  );

  -- The 9-column shadow audit table + its two indexes (identical to a slot's, V013/V019).
  EXECUTE format($ddl$
    CREATE TABLE audit.%I (
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
    )
  $ddl$, p_id || '_audit');
  EXECUTE format('CREATE INDEX %I ON audit.%I (asset_id)',  p_id || '_audit_asset_idx',   p_id || '_audit');
  EXECUTE format('CREATE INDEX %I ON audit.%I (changed_at)', p_id || '_audit_changed_idx', p_id || '_audit');

  -- The three standard per-feature triggers (all reference EXISTING shared functions).
  EXECUTE format(
    'CREATE TRIGGER %I AFTER INSERT OR UPDATE OR DELETE ON assets.%I '
    'FOR EACH ROW EXECUTE FUNCTION audit.fn_record_change(%L, %L)',
    p_id || '_audit_trigger', p_id, 'audit.' || p_id || '_audit', 'id'
  );
  EXECUTE format(
    'CREATE TRIGGER %I BEFORE INSERT OR UPDATE ON assets.%I '
    'FOR EACH ROW EXECUTE FUNCTION assets.fn_validate_layer_props()',
    p_id || '_props_validate_trigger', p_id
  );
  EXECUTE format(
    'CREATE TRIGGER %I BEFORE UPDATE ON assets.%I '
    'FOR EACH ROW EXECUTE FUNCTION assets.fn_bump_row_version()',
    p_id || '_row_version_trigger', p_id
  );
END;
$fn$;

COMMENT ON FUNCTION platform.provision_hosted_table(text) IS
  'decision-nonspatial-tables.md: scoped, template-fixed creation of one aspatial hosted-table asset '
  '(slot schema minus geom/GiST/tile). Called by the API create-table path; the id is app-generated + '
  'collision-checked and matches ^tal_[0-9a-f]{8}$.';
