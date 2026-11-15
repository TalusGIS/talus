-- V048 — Constraint / default rules (data-owner independence, slice 3). Tier-2, backup-gated.
--
-- Adds DECLARATIVE per-field validation rules beyond type + domain, plus one dynamic default — all on the
-- existing rules-as-data trigger spine, ZERO runtime DDL, NO expression language (decision-constraint-rules.md):
--   * platform.layer_fields.constraints jsonb — range (min/max on number+date), length (minLength/maxLength
--     on text), pattern (regex on text). NULL = no extra rules. Coarse CHECK keeps the key set honest for
--     non-UI writers (QField/direct API); fine per-type validation lives in the trigger + app validator.
--   * platform.layer_fields.default_kind — closed enum 'constant'|'today'. 'today' (date fields only) resolves
--     to CURRENT_DATE at write time. NOT a formula language: a fixed token, no args/operators/composition.
--     The discriminator leaves room for a fenced 'current_user' later with no schema change (deferred).
--
-- Drift fence (paramount): declarative parameterised constraints only — NO Arcade clone, NO expression/formula
-- engine, NO cross-field predicate builder, NO batch-validation engine. A format regex is on-thesis: it can
-- only accept/reject one string, never derive a value (match/reject ONLY). ReDoS is GUARDED at author time
-- (anchored + length-capped + tested) with the session statement_timeout as the DB backstop.

-- ── 1. additive columns on the field registry ─────────────────────────────────────────────────────
ALTER TABLE platform.layer_fields
    ADD COLUMN constraints  jsonb,
    ADD COLUMN default_kind text NOT NULL DEFAULT 'constant';

-- closed enum; 'today' is date-only (pairing enforced below).
ALTER TABLE platform.layer_fields
    ADD CONSTRAINT layer_fields_default_kind_check CHECK (default_kind IN ('constant', 'today'));

-- 'today' default only valid on date fields. 'constant' keeps the existing default_value path untouched.
ALTER TABLE platform.layer_fields
    ADD CONSTRAINT layer_fields_today_is_date CHECK (default_kind = 'constant' OR data_type = 'date');

-- coarse shape: an object whose keys are a subset of the known constraint keys. (`jsonb - text[]` deletes the
-- allowed keys; an empty result means every key was allowed.) Per-type validity (min/max only for number/date,
-- length/pattern only for text) is enforced by the app validator at author time + the trigger at write time.
ALTER TABLE platform.layer_fields
    ADD CONSTRAINT layer_fields_constraints_shape CHECK (
        constraints IS NULL OR (
            jsonb_typeof(constraints) = 'object'
            AND (constraints - ARRAY['min', 'max', 'minLength', 'maxLength', 'pattern']) = '{}'::jsonb
        )
    );

-- ── 2. extend the generic validation trigger ──────────────────────────────────────────────────────
-- Re-state the V047 function (date branch unchanged) with: (a) 'today' default resolution, and (b) a
-- per-field constraint block (range/length/pattern) after the type check. Triggers stay attached
-- (CREATE OR REPLACE). Belt (this trigger) is DB-authoritative; the app validator (layer_props.py) mirrors
-- it for friendly 422s.
CREATE OR REPLACE FUNCTION assets.fn_validate_layer_props() RETURNS trigger AS $$
DECLARE
    v_layer text  := TG_TABLE_NAME;
    v_props jsonb := COALESCE(NEW.props, '{}'::jsonb);
    f       record;
    v_val   jsonb;
    v_txt   text;
    v_key   text;
    v_known text[];
BEGIN
    FOR f IN
        SELECT field_key, data_type, required, default_value, default_kind, domain_id, constraints
        FROM platform.layer_fields
        WHERE layer_id = v_layer AND active
    LOOP
        -- defaults (absent key only): 'today' token resolves to CURRENT_DATE; else the literal constant.
        IF NOT (v_props ? f.field_key) THEN
            IF f.default_kind = 'today' THEN
                v_props := v_props || jsonb_build_object(f.field_key, to_char(CURRENT_DATE, 'YYYY-MM-DD'));
            ELSIF f.default_value IS NOT NULL THEN
                v_props := v_props || jsonb_build_object(f.field_key, f.default_value);
            END IF;
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
                -- Strict YYYY-MM-DD first (rejects 'today'/'epoch'/'01/02/2026'); the ::date cast then
                -- still rejects impossible calendar dates like 2026-02-30.
                IF jsonb_typeof(v_val) <> 'string' OR (v_val #>> '{}') !~ '^\d{4}-\d{2}-\d{2}$' THEN
                    RAISE EXCEPTION 'layer_fields: field "%" must be a date (YYYY-MM-DD)', f.field_key USING ERRCODE = 'check_violation';
                END IF;
                BEGIN
                    PERFORM (v_val #>> '{}')::date;
                EXCEPTION WHEN others THEN
                    RAISE EXCEPTION 'layer_fields: field "%" must be a valid date (YYYY-MM-DD)', f.field_key
                        USING ERRCODE = 'check_violation';
                END;
            ELSIF f.data_type = 'coded' THEN
                IF jsonb_typeof(v_val) <> 'string' THEN
                    RAISE EXCEPTION 'layer_fields: field "%" must be a code (text)', f.field_key USING ERRCODE = 'check_violation';
                ELSIF NOT EXISTS (
                    SELECT 1 FROM platform.domain_values dv
                    WHERE dv.domain_id = f.domain_id AND dv.code = (v_val #>> '{}')
                ) THEN
                    RAISE EXCEPTION 'layer_fields: field "%" has no value "%" in its domain', f.field_key, (v_val #>> '{}')
                        USING ERRCODE = 'check_violation';
                END IF;
            END IF;

            -- declarative constraints (range/length/pattern). The type checks above guarantee the cast/length
            -- operations below are safe. Only the keys relevant to the field's type are read.
            IF f.constraints IS NOT NULL THEN
                IF f.data_type = 'number' THEN
                    IF f.constraints ? 'min' AND (v_val #>> '{}')::numeric < (f.constraints ->> 'min')::numeric THEN
                        RAISE EXCEPTION 'layer_fields: field "%" must be at least %', f.field_key, (f.constraints ->> 'min') USING ERRCODE = 'check_violation';
                    END IF;
                    IF f.constraints ? 'max' AND (v_val #>> '{}')::numeric > (f.constraints ->> 'max')::numeric THEN
                        RAISE EXCEPTION 'layer_fields: field "%" must be at most %', f.field_key, (f.constraints ->> 'max') USING ERRCODE = 'check_violation';
                    END IF;
                ELSIF f.data_type = 'date' THEN
                    IF f.constraints ? 'min' AND (v_val #>> '{}')::date < (f.constraints ->> 'min')::date THEN
                        RAISE EXCEPTION 'layer_fields: field "%" must be on or after %', f.field_key, (f.constraints ->> 'min') USING ERRCODE = 'check_violation';
                    END IF;
                    IF f.constraints ? 'max' AND (v_val #>> '{}')::date > (f.constraints ->> 'max')::date THEN
                        RAISE EXCEPTION 'layer_fields: field "%" must be on or before %', f.field_key, (f.constraints ->> 'max') USING ERRCODE = 'check_violation';
                    END IF;
                ELSIF f.data_type = 'text' THEN
                    v_txt := v_val #>> '{}';
                    IF f.constraints ? 'minLength' AND char_length(v_txt) < (f.constraints ->> 'minLength')::int THEN
                        RAISE EXCEPTION 'layer_fields: field "%" must be at least % characters', f.field_key, (f.constraints ->> 'minLength') USING ERRCODE = 'check_violation';
                    END IF;
                    IF f.constraints ? 'maxLength' AND char_length(v_txt) > (f.constraints ->> 'maxLength')::int THEN
                        RAISE EXCEPTION 'layer_fields: field "%" must be at most % characters', f.field_key, (f.constraints ->> 'maxLength') USING ERRCODE = 'check_violation';
                    END IF;
                    IF f.constraints ? 'pattern' AND v_txt !~ (f.constraints ->> 'pattern') THEN
                        RAISE EXCEPTION 'layer_fields: field "%" does not match the required format', f.field_key USING ERRCODE = 'check_violation';
                    END IF;
                END IF;
            END IF;
        END IF;
    END LOOP;

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
