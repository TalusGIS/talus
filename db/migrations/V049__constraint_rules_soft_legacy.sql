-- V049 — Constraint rules: SOFT legacy semantics (decision-constraint-rules.md fork 7). Tier-2, backup-gated.
--
-- V048 enforces range/length/pattern on the WHOLE row on every write. That over-reaches fork 7: a NEW or
-- tightened constraint would then retro-reject a legacy row the moment ANY field on it is edited (e.g. just
-- the notes), forcing conformance of an unrelated value. Fork 7 chose SOFT — existing rows survive; a
-- constraint binds a value only when THAT value is being set/changed.
--
-- Fix: gate ONLY the constraint block to "this key is actually changing" — enforced on INSERT (all present
-- keys), and on UPDATE only when the key's new value IS DISTINCT FROM the old one. Type / required / coded
-- membership are unchanged (they were always enforced every write; legacy data can't violate them — type is
-- invariant, coded keeps retired-but-defined). The app validator already mirrors this (on PATCH it validates
-- only the incoming keys); this aligns the DB belt with it.

CREATE OR REPLACE FUNCTION assets.fn_validate_layer_props() RETURNS trigger AS $$
DECLARE
    v_layer text  := TG_TABLE_NAME;
    v_props jsonb := COALESCE(NEW.props, '{}'::jsonb);
    v_old   jsonb := CASE WHEN TG_OP = 'UPDATE' THEN COALESCE(OLD.props, '{}'::jsonb) ELSE NULL END;
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
