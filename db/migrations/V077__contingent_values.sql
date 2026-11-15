-- V077 — Contingent / cascading domain values (decision-contingent-values.md). Tier-2, backup-gated.
--
-- A coded CHILD field's picklist depends on ONE coded sibling PARENT field
-- (Category=Streets → Subcategory offers Pothole/Striping/Signage). Rules-as-data on the V044 spine,
-- ZERO runtime DDL. Domains stay flat and reusable (V046); the contingency is per-layer config on the
-- CHILD field binding (fork 1b):
--   * layer_fields.parent_field_key  — the sibling coded field this child depends on.
--   * layer_fields.contingent_values — jsonb { "<parentCode>": ["<childCode>", ...], ... }.
--
-- Semantics (forks 3/3½, decided 2026-07-13):
--   * Parent NULL/absent            → no check (full list; no parent value = nothing to violate).
--   * Parent value has NO map entry → no check (permissive; incremental mapping never breaks editing).
--   * SOFT binding (V049 precedent): INSERT always checks; UPDATE checks only when the PARENT or CHILD
--     value is actually changing — so adding a dependency never retro-rejects a legacy row on unrelated
--     edits, but switching Category out from under a Subcategory IS caught (either side binds).
--
-- Hard fence (North Star): SINGLE-PARENT only. No multi-field matrices, no "any" wildcards, no pair
-- retirement states, no expression engine, coded-parent only. If it can't be said as "this child code
-- is valid when parent = X", it does not belong here.
--
-- Fine validation (parent is coded / same layer / codes defined in the right domains / no self-ref or
-- 2-cycle) is app-enforced at author time; the DB keeps a coarse CHECK + FK, and the trigger stays
-- defensive — an incoherent mapping can only fail OPEN to permissive, never crash a feature write.

-- ── 1. the contingency binding on the child field row ──────────────────────────────────────────────
ALTER TABLE platform.layer_fields
    ADD COLUMN parent_field_key  text,
    ADD COLUMN contingent_values jsonb;

-- Both present or both absent; only on a coded field; mapping is an object; never self-referential.
ALTER TABLE platform.layer_fields ADD CONSTRAINT layer_fields_contingency_coherent
    CHECK (
        ((parent_field_key IS NULL) = (contingent_values IS NULL))
        AND (parent_field_key IS NULL OR data_type = 'coded')
        AND (contingent_values IS NULL OR jsonb_typeof(contingent_values) = 'object')
        AND (parent_field_key IS NULL OR parent_field_key <> field_key)
    );

-- The parent must be a real field on the SAME layer (rides V044's UNIQUE (layer_id, field_key)).
-- Default NO ACTION = the DB itself refuses hard-deleting a parent field that still has dependents —
-- the belt under the app's friendly 409 (same pattern as the V046 domain-delete guard). Layer deletion
-- cascades both rows in one statement, which NO ACTION (checked at statement end) permits.
ALTER TABLE platform.layer_fields ADD CONSTRAINT layer_fields_parent_fk
    FOREIGN KEY (layer_id, parent_field_key)
    REFERENCES platform.layer_fields (layer_id, field_key);

CREATE INDEX layer_fields_parent_idx ON platform.layer_fields (layer_id, parent_field_key)
    WHERE parent_field_key IS NOT NULL;

-- ── 2. extend the validation trigger (V051 body + a SECOND pass for pair checks) ───────────────────
-- The main loop applies defaults per-field IN LOOP ORDER, so a child processed before its parent would
-- not yet see the parent's defaulted value. The pair check therefore runs as a second pass AFTER all
-- defaults/types/domains/constraints resolve — a defaulted parent contingently constrains too.
CREATE OR REPLACE FUNCTION assets.fn_validate_layer_props() RETURNS trigger AS $$
DECLARE
    v_layer text  := TG_TABLE_NAME;
    v_props jsonb := COALESCE(NEW.props, '{}'::jsonb);
    v_old   jsonb := CASE WHEN TG_OP = 'UPDATE' THEN COALESCE(OLD.props, '{}'::jsonb) ELSE NULL END;
    f       record;
    v_val   jsonb;
    v_txt   text;
    v_num   numeric;
    v_key   text;
    v_known text[];
    v_par   jsonb;
    v_allowed jsonb;
BEGIN
    FOR f IN
        SELECT field_key, data_type, required, default_value, default_kind, domain_id, constraints, numeric_subtype
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

            -- numeric subtype (Short/Long integer): whole-number + bit-range. SOFT (like constraints): on
            -- INSERT, or on UPDATE only when this key's value is actually CHANGING — a subtype added later
            -- never retro-rejects a legacy row whose unrelated fields are edited. Float/Double = no check.
            IF f.data_type = 'number' AND f.numeric_subtype IN ('short_integer', 'long_integer')
               AND jsonb_typeof(v_val) = 'number'
               AND (TG_OP = 'INSERT' OR (v_old -> f.field_key) IS DISTINCT FROM v_val) THEN
                v_num := (v_val #>> '{}')::numeric;
                IF v_num <> trunc(v_num) THEN
                    RAISE EXCEPTION 'layer_fields: field "%" must be a whole number', f.field_key USING ERRCODE = 'check_violation';
                END IF;
                IF f.numeric_subtype = 'short_integer' AND (v_num < -32768 OR v_num > 32767) THEN
                    RAISE EXCEPTION 'layer_fields: field "%" is out of range for a short integer (-32768..32767)', f.field_key USING ERRCODE = 'check_violation';
                END IF;
                IF f.numeric_subtype = 'long_integer' AND (v_num < -2147483648 OR v_num > 2147483647) THEN
                    RAISE EXCEPTION 'layer_fields: field "%" is out of range for a long integer (-2147483648..2147483647)', f.field_key USING ERRCODE = 'check_violation';
                END IF;
            END IF;

            -- declarative constraints (range/length/pattern). SOFT (fork 7): enforced on INSERT, and on
            -- UPDATE only when this key's value is actually CHANGING — so a new/tightened constraint never
            -- retro-rejects a legacy row whose unrelated fields are edited.
            IF f.constraints IS NOT NULL
               AND (TG_OP = 'INSERT' OR (v_old -> f.field_key) IS DISTINCT FROM v_val) THEN
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

    -- contingent values (V077): SECOND pass, after every field's default has resolved, so a defaulted
    -- parent constrains too. The check binds only when BOTH sides are set, the mapping HAS an entry for
    -- the parent's value (fork 3: absent parent / unmapped parent value = permissive), and — SOFT, fork
    -- 3½ — the row is an INSERT or EITHER side of the pair is actually changing. Non-string values and a
    -- non-array mapping entry fail OPEN here (permissive): shape errors are the earlier branches' /
    -- author-time validation's job; a feature write must never crash on incoherent config.
    FOR f IN
        SELECT field_key, parent_field_key, contingent_values
        FROM platform.layer_fields
        WHERE layer_id = v_layer AND active AND parent_field_key IS NOT NULL AND contingent_values IS NOT NULL
    LOOP
        v_val := v_props -> f.field_key;
        v_par := v_props -> f.parent_field_key;
        IF v_val IS NOT NULL AND jsonb_typeof(v_val) = 'string'
           AND v_par IS NOT NULL AND jsonb_typeof(v_par) = 'string'
           AND (TG_OP = 'INSERT'
                OR (v_old -> f.field_key) IS DISTINCT FROM v_val
                OR (v_old -> f.parent_field_key) IS DISTINCT FROM v_par) THEN
            v_allowed := f.contingent_values -> (v_par #>> '{}');
            IF v_allowed IS NOT NULL AND jsonb_typeof(v_allowed) = 'array'
               AND NOT (v_allowed ? (v_val #>> '{}')) THEN
                RAISE EXCEPTION 'layer_fields: field "%" value "%" is not allowed when "%" is "%"',
                    f.field_key, (v_val #>> '{}'), f.parent_field_key, (v_par #>> '{}')
                    USING ERRCODE = 'check_violation';
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
