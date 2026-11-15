-- V104 — Talus Field f2: the layer form config + two default tokens (decision-talus-field-f2.md §4, §7).
-- Tier-2, backup-gated. Three schema bits, one migration:
--
--   1. platform.layers.form_config jsonb — ONE document per layer: groups · visibleIf · requiredIf ·
--      readOnly · templates · photo (the closed vocabulary; validated key-by-key by the form door,
--      app/form_config.py). NULL = the default form (every active field, one group, no rules). The
--      `renderer` precedent: per-layer config, manage-gated write, view-gated read.
--   2. platform.layer_fields.default_kind gains 'now' (ISO-8601 UTC timestamp, text fields) and
--      'current_user' (the acting user's display name, text fields) — the closed token set V048
--      reserved room for ("no schema change" was optimistic: the CHECKs name the set).
--   3. assets.fn_validate_layer_props: resolves the two tokens, and gains the requiredIf pass — the
--      server is authoritative for a conditional requirement exactly as it is for `required`,
--      constraints (V048) and contingency (V077): a psql or bulk write cannot bypass a rule a crew
--      cannot. Restated WHOLE (the house rule for trigger bodies).

ALTER TABLE platform.layers
    ADD COLUMN form_config jsonb;
ALTER TABLE platform.layers
    ADD CONSTRAINT layers_form_config_is_object
    CHECK (form_config IS NULL OR jsonb_typeof(form_config) = 'object');
COMMENT ON COLUMN platform.layers.form_config IS
  'decision-talus-field-f2.md: the layer''s declarative form document (groups/visibleIf/requiredIf/readOnly/templates/photo). NULL = default form.';

ALTER TABLE platform.layer_fields DROP CONSTRAINT layer_fields_default_kind_check;
ALTER TABLE platform.layer_fields
    ADD CONSTRAINT layer_fields_default_kind_check
    CHECK (default_kind IN ('constant', 'today', 'now', 'current_user'));
ALTER TABLE platform.layer_fields DROP CONSTRAINT layer_fields_today_is_date;
ALTER TABLE platform.layer_fields
    ADD CONSTRAINT layer_fields_token_type_check
    CHECK (
        default_kind = 'constant'
        OR (default_kind = 'today' AND data_type = 'date')
        OR (default_kind IN ('now', 'current_user') AND data_type = 'text')
    );

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
    v_form  jsonb;
    v_rule  jsonb;
    v_cond  jsonb;
    v_cf    text;
    v_hold  boolean;
    v_actor text;
BEGIN
    -- V104: the two new default tokens. `now` = ISO-8601 UTC timestamp (text fields); `current_user` =
    -- the acting user's display name, resolved from the audit GUC every product write already sets
    -- (app.current_user_id). A write with no actor GUC (psql, a bulk job) simply leaves the key absent —
    -- a token never invents an identity.
    v_actor := NULL;
    BEGIN
        IF current_setting('app.current_user_id', true) IS NOT NULL AND current_setting('app.current_user_id', true) <> '' THEN
            SELECT COALESCE(display_name, username, email) INTO v_actor
            FROM platform.users WHERE id = current_setting('app.current_user_id', true)::uuid;
        END IF;
    EXCEPTION WHEN others THEN
        v_actor := NULL;
    END;
    FOR f IN
        SELECT field_key, data_type, required, default_value, default_kind, domain_id, constraints, numeric_subtype
        FROM platform.layer_fields
        WHERE layer_id = v_layer AND active
    LOOP
        -- defaults (absent key only): 'today' token resolves to CURRENT_DATE; else the literal constant.
        IF NOT (v_props ? f.field_key) THEN
            IF f.default_kind = 'today' THEN
                v_props := v_props || jsonb_build_object(f.field_key, to_char(CURRENT_DATE, 'YYYY-MM-DD'));
            ELSIF f.default_kind = 'now' THEN
                v_props := v_props || jsonb_build_object(f.field_key, to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'));
            ELSIF f.default_kind = 'current_user' THEN
                IF v_actor IS NOT NULL THEN
                    v_props := v_props || jsonb_build_object(f.field_key, v_actor);
                END IF;
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

    -- requiredIf (V104, decision-talus-field-f2.md §4.3): a field the form config makes conditionally
    -- required must be present when its condition holds. Evaluated AFTER defaults + the other passes so a
    -- defaulted condition field counts. SOFT like constraints/contingency: on INSERT, or on UPDATE only when
    -- the target OR the condition field is actually changing — a legacy row never blocks a notes-only edit.
    -- The closed condition vocabulary (eq / in / isEmpty / notEmpty) is evaluated here and nowhere else in
    -- SQL; an incoherent rule fails OPEN (author-time validation is the form door's job).
    SELECT form_config INTO v_form FROM platform.layers WHERE layer_id = v_layer;
    IF v_form IS NOT NULL AND jsonb_typeof(v_form -> 'fields') = 'object' THEN
        FOR v_key, v_rule IN SELECT key, value FROM jsonb_each(v_form -> 'fields')
        LOOP
            v_cond := v_rule -> 'requiredIf';
            IF v_cond IS NULL OR jsonb_typeof(v_cond) <> 'object' THEN CONTINUE; END IF;
            v_cf := v_cond ->> 'field';
            IF v_cf IS NULL THEN CONTINUE; END IF;
            v_val := v_props -> v_key;
            v_par := v_props -> v_cf;
            v_hold := false;
            IF v_cond ? 'eq' THEN
                v_hold := v_par IS NOT NULL AND jsonb_typeof(v_par) <> 'null' AND (v_par #>> '{}') = (v_cond ->> 'eq');
            ELSIF v_cond ? 'in' AND jsonb_typeof(v_cond -> 'in') = 'array' THEN
                v_hold := v_par IS NOT NULL AND jsonb_typeof(v_par) = 'string' AND (v_cond -> 'in') ? (v_par #>> '{}');
            ELSIF v_cond ? 'isEmpty' THEN
                v_hold := v_par IS NULL OR jsonb_typeof(v_par) = 'null' OR (jsonb_typeof(v_par) = 'string' AND (v_par #>> '{}') = '');
            ELSIF v_cond ? 'notEmpty' THEN
                v_hold := NOT (v_par IS NULL OR jsonb_typeof(v_par) = 'null' OR (jsonb_typeof(v_par) = 'string' AND (v_par #>> '{}') = ''));
            END IF;
            IF v_hold
               AND (v_val IS NULL OR jsonb_typeof(v_val) = 'null' OR (jsonb_typeof(v_val) = 'string' AND (v_val #>> '{}') = ''))
               AND (TG_OP = 'INSERT'
                    OR (v_old -> v_key) IS DISTINCT FROM v_val
                    OR (v_old -> v_cf) IS DISTINCT FROM v_par) THEN
                RAISE EXCEPTION 'layer_fields: field "%" is required when "%" is "%"',
                    v_key, v_cf, COALESCE(v_par #>> '{}', '')
                    USING ERRCODE = 'check_violation';
            END IF;
        END LOOP;
    END IF;

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
