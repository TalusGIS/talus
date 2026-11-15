-- V047 — Coded-domains hardening (post-review). Tier-2, backup-gated.
--
-- Three defensive tightenings from the self-review of slices 2/2a (no behaviour change for the UI path,
-- which already sends well-formed data — these close gaps for QField / direct-API / future writers):
--   1. Index the FK referencing column `layer_fields.domain_id` so deleting a domain doesn't seq-scan
--      layer_fields (the FK existence check). Partial (coded fields only) since it's NULL otherwise.
--   2. Forbid whitespace-padded domain codes (`code = btrim(code)`), so a stored props value can never
--      become un-matchable against its domain via a trailing/leading space.
--   3. Tighten the `date` branch of fn_validate_layer_props: require strict `YYYY-MM-DD` BEFORE the
--      ::date cast, so the DB belt matches the app validator + the error message (the bare ::date cast
--      accepts 'today'/'epoch'/ambiguous locale forms — rejected now).

-- ── 1. index the domain_id FK column ───────────────────────────────────────────────────────────────
CREATE INDEX layer_fields_domain_idx ON platform.layer_fields (domain_id) WHERE domain_id IS NOT NULL;

-- ── 2. forbid whitespace-padded codes ──────────────────────────────────────────────────────────────
ALTER TABLE platform.domain_values
    ADD CONSTRAINT domain_values_code_trimmed CHECK (code = btrim(code));

-- ── 3. strict ISO date in the validation trigger ───────────────────────────────────────────────────
-- Re-state the V046 function with ONLY the date branch tightened (membership branch unchanged: coded
-- still accepts any DEFINED code, active OR retired). Triggers stay attached (CREATE OR REPLACE).
CREATE OR REPLACE FUNCTION assets.fn_validate_layer_props() RETURNS trigger AS $$
DECLARE
    v_layer text  := TG_TABLE_NAME;
    v_props jsonb := COALESCE(NEW.props, '{}'::jsonb);
    f       record;
    v_val   jsonb;
    v_key   text;
    v_known text[];
BEGIN
    FOR f IN
        SELECT field_key, data_type, required, default_value, domain_id
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
