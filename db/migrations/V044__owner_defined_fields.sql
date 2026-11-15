-- V044 — Owner-defined layer fields (decision-owner-defined-fields.md). Tier-2, backup-gated.
--
-- Slice 1 of the data-owner-independence arc. Lets the OWNER of a `tal_` layer define their own
-- typed attribute fields from the portal — the ESRI "data owner authors their feature class" move —
-- WITHOUT any runtime DDL. The mechanism is rules-as-data:
--   * field VALUES live in a single `props jsonb` column added ONCE here (never a runtime ALTER).
--   * field DEFINITIONS live in the `platform.layer_fields` registry (pure data the owner CRUDs).
--   * enforcement is a generic BEFORE INSERT OR UPDATE trigger that validates `props` against the
--     registry — the DB-authoritative belt so QField / direct writes cannot bypass field rules.
--     The API write path validates first for friendly per-field errors (the suspenders).
--
-- Slot pool = the FROZEN V019 pool (5 `assets.tal_*`); the publisher claims slots, never issues DDL,
-- so altering the fixed set here covers every slot (mirrors V035). Built-in asset layers (signs,
-- street_lights, county_boundary) are OUT of scope — they have native typed columns + the 7 domains.
--
-- NOTE for the backend slice: slot RELEASE flips platform.layers.status to 'unclaimed' but KEEPS the
-- row (publisher.py:160), so the FK never cascades on reuse. The release transaction must therefore
-- `DELETE FROM platform.layer_fields WHERE layer_id = <slot>` so field defs don't leak to the next
-- claimant. The slot's feature rows (and their props) are already wiped by _release_slot.

-- ── Part 1 — the field-definition registry (owner-authored, pure data) ─────────────────────────────
CREATE TABLE platform.layer_fields (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    layer_id      text NOT NULL REFERENCES platform.layers(layer_id) ON DELETE CASCADE,
    field_key     text NOT NULL,                         -- the jsonb key in props (data, not SQL)
    label         text NOT NULL,                         -- display label (freely re-editable)
    data_type     text NOT NULL CHECK (data_type IN ('text','number','date','boolean')),
    required      boolean NOT NULL DEFAULT false,
    default_value jsonb,                                  -- applied by the trigger when key absent
    sort_order    int NOT NULL DEFAULT 0,
    active        boolean NOT NULL DEFAULT true,          -- soft-retire; legacy values survive
    created_at    timestamptz NOT NULL DEFAULT now(),
    created_by    uuid NOT NULL REFERENCES platform.users(id),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    updated_by    uuid NOT NULL REFERENCES platform.users(id),
    -- snake_case identifier, starts with a letter, ≤63 chars: keeps forms/exports/expression indexes sane.
    CONSTRAINT layer_fields_key_format CHECK (field_key ~ '^[a-z][a-z0-9_]{0,62}$'),
    -- never shadow a real slot column.
    CONSTRAINT layer_fields_key_reserved CHECK (field_key NOT IN (
        'id','geom','notes','props','row_version','ai_provenance',
        'created_at','created_by','updated_at','updated_by'
    )),
    CONSTRAINT layer_fields_key_unique UNIQUE (layer_id, field_key)
);

CREATE INDEX layer_fields_layer_active_idx ON platform.layer_fields (layer_id) WHERE active;

-- ── Part 2 — the value column on each frozen slot (one-time; no runtime ALTER ever again) ──────────
ALTER TABLE assets.tal_7b3aa14c ADD COLUMN props jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE assets.tal_6496fe7d ADD COLUMN props jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE assets.tal_2dbf236f ADD COLUMN props jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE assets.tal_57b25bf6 ADD COLUMN props jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE assets.tal_2c09dee7 ADD COLUMN props jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX tal_7b3aa14c_props_idx ON assets.tal_7b3aa14c USING GIN (props);
CREATE INDEX tal_6496fe7d_props_idx ON assets.tal_6496fe7d USING GIN (props);
CREATE INDEX tal_2dbf236f_props_idx ON assets.tal_2dbf236f USING GIN (props);
CREATE INDEX tal_57b25bf6_props_idx ON assets.tal_57b25bf6 USING GIN (props);
CREATE INDEX tal_2c09dee7_props_idx ON assets.tal_2c09dee7 USING GIN (props);

-- ── Part 3 — generic enforcement (one function, many triggers; mirrors V003/V035) ──────────────────
-- Reads the registry for THIS slot (layer_id == TG_TABLE_NAME), applies defaults, enforces type +
-- required for active fields, and rejects unknown keys. Defined fields (active OR soft-retired) are
-- allowed through so legacy values survive a retire; only never-defined keys are rejected.
CREATE OR REPLACE FUNCTION assets.fn_validate_layer_props() RETURNS trigger AS $$
DECLARE
    v_layer text  := TG_TABLE_NAME;                  -- slot table name IS the layer_id
    v_props jsonb := COALESCE(NEW.props, '{}'::jsonb);
    f       record;
    v_val   jsonb;
    v_key   text;
    v_known text[];
BEGIN
    -- defaults + per-field type/required validation (active fields only)
    FOR f IN
        SELECT field_key, data_type, required, default_value
        FROM platform.layer_fields
        WHERE layer_id = v_layer AND active
    LOOP
        IF NOT (v_props ? f.field_key) AND f.default_value IS NOT NULL THEN
            v_props := v_props || jsonb_build_object(f.field_key, f.default_value);
        END IF;

        v_val := v_props -> f.field_key;

        IF f.required AND (v_val IS NULL OR jsonb_typeof(v_val) = 'null') THEN
            RAISE EXCEPTION 'layer_fields: field "%" is required', f.field_key
                USING ERRCODE = 'check_violation';
        END IF;

        IF v_val IS NOT NULL AND jsonb_typeof(v_val) <> 'null' THEN
            IF f.data_type = 'text' AND jsonb_typeof(v_val) <> 'string' THEN
                RAISE EXCEPTION 'layer_fields: field "%" must be text', f.field_key USING ERRCODE = 'check_violation';
            ELSIF f.data_type = 'number' AND jsonb_typeof(v_val) <> 'number' THEN
                RAISE EXCEPTION 'layer_fields: field "%" must be a number', f.field_key USING ERRCODE = 'check_violation';
            ELSIF f.data_type = 'boolean' AND jsonb_typeof(v_val) <> 'boolean' THEN
                RAISE EXCEPTION 'layer_fields: field "%" must be true or false', f.field_key USING ERRCODE = 'check_violation';
            ELSIF f.data_type = 'date' THEN
                IF jsonb_typeof(v_val) <> 'string' THEN
                    RAISE EXCEPTION 'layer_fields: field "%" must be an ISO date string', f.field_key USING ERRCODE = 'check_violation';
                END IF;
                BEGIN
                    PERFORM (v_val #>> '{}')::date;          -- must parse as a real date
                EXCEPTION WHEN others THEN
                    RAISE EXCEPTION 'layer_fields: field "%" must be a valid date (YYYY-MM-DD)', f.field_key
                        USING ERRCODE = 'check_violation';
                END;
            END IF;
        END IF;
    END LOOP;

    -- reject keys that were never defined for this layer (typos / arbitrary injection)
    SELECT array_agg(field_key) INTO v_known
    FROM platform.layer_fields WHERE layer_id = v_layer;

    FOR v_key IN SELECT jsonb_object_keys(v_props)
    LOOP
        IF v_known IS NULL OR NOT (v_key = ANY (v_known)) THEN
            RAISE EXCEPTION 'layer_fields: unknown field "%" for layer %', v_key, v_layer
                USING ERRCODE = 'check_violation';
        END IF;
    END LOOP;

    NEW.props := v_props;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER tal_7b3aa14c_props_validate_trigger
    BEFORE INSERT OR UPDATE ON assets.tal_7b3aa14c
    FOR EACH ROW EXECUTE FUNCTION assets.fn_validate_layer_props();
CREATE TRIGGER tal_6496fe7d_props_validate_trigger
    BEFORE INSERT OR UPDATE ON assets.tal_6496fe7d
    FOR EACH ROW EXECUTE FUNCTION assets.fn_validate_layer_props();
CREATE TRIGGER tal_2dbf236f_props_validate_trigger
    BEFORE INSERT OR UPDATE ON assets.tal_2dbf236f
    FOR EACH ROW EXECUTE FUNCTION assets.fn_validate_layer_props();
CREATE TRIGGER tal_57b25bf6_props_validate_trigger
    BEFORE INSERT OR UPDATE ON assets.tal_57b25bf6
    FOR EACH ROW EXECUTE FUNCTION assets.fn_validate_layer_props();
CREATE TRIGGER tal_2c09dee7_props_validate_trigger
    BEFORE INSERT OR UPDATE ON assets.tal_2c09dee7
    FOR EACH ROW EXECUTE FUNCTION assets.fn_validate_layer_props();
