-- V046 — Reusable coded-value domains (decision-coded-domains.md). Tier-2, backup-gated.
--
-- Slice 2 of the data-owner-independence arc, slice 2a (core). Owner-authored, REUSABLE picklists
-- (the ESRI coded-value-domain model) bound to slot custom fields (V044). Chosen over inline values
-- at a scale of ~300 feature layers: define-once / edit-everywhere lowers TCO
-- and prevents drift. Still rules-as-data, ZERO runtime DDL.
--
--   * platform.domains       — a named, owner-owned, org-wide-assignable picklist.
--   * platform.domain_values — its code/label values; soft-retire (active) + sort_order.
--   * layer_fields.domain_id — a 'coded' field binds to a domain (FK; the default NO ACTION means the
--                              DB itself REFUSES deleting a domain still bound to a field — a belt under
--                              the app's friendly 409, like the V044 delete-with-data guard).
--
-- Enforcement extends the V044 belt: fn_validate_layer_props gains a 'coded' branch — the value must be
-- a code DEFINED in the bound domain. Membership is checked against ALL defined codes (active OR
-- soft-retired), NOT just active: a retired value must still pass so an UPDATE to a feature that already
-- carries it isn't rejected (retire = "hidden from the form", not "reject existing data" — mirrors the
-- V044 defined-but-retired-key rule). The edit form only OFFERS active codes.
--
-- Governance: any data owner may BIND any domain to their own field (org-wide reuse); only the domain's
-- owner or an admin edits its values (app-enforced). Built-in asset FK domains (signs/lights) untouched.

-- ── Part 1 — the reusable domain registry (owner-authored, pure data) ──────────────────────────────
CREATE TABLE platform.domains (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id    uuid NOT NULL REFERENCES platform.users(id),   -- creator/steward; only owner/admin edits
    name        text NOT NULL,
    description text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    created_by  uuid NOT NULL REFERENCES platform.users(id),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  uuid NOT NULL REFERENCES platform.users(id),
    CONSTRAINT domains_name_len    CHECK (char_length(name) BETWEEN 1 AND 120),
    CONSTRAINT domains_name_unique UNIQUE (owner_id, name)      -- unique per owner; org may have two "Condition"
);

CREATE TABLE platform.domain_values (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    domain_id   uuid NOT NULL REFERENCES platform.domains(id) ON DELETE CASCADE,
    code        text NOT NULL,                                 -- the value stored in props
    label       text NOT NULL,                                 -- what the user sees
    active      boolean NOT NULL DEFAULT true,                 -- soft-retire (legacy values survive)
    sort_order  int  NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now(),
    created_by  uuid NOT NULL REFERENCES platform.users(id),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  uuid NOT NULL REFERENCES platform.users(id),
    CONSTRAINT domain_values_code_format CHECK (char_length(code) BETWEEN 1 AND 100 AND code !~ '[[:cntrl:]]'),
    CONSTRAINT domain_values_label_len   CHECK (char_length(label) BETWEEN 1 AND 200),
    CONSTRAINT domain_values_code_unique UNIQUE (domain_id, code)   -- also the membership-lookup index
);

CREATE INDEX domain_values_domain_active_idx ON platform.domain_values (domain_id) WHERE active;

-- ── Part 2 — the 'coded' field type + the field↔domain binding ─────────────────────────────────────
ALTER TABLE platform.layer_fields DROP CONSTRAINT layer_fields_data_type_check;
ALTER TABLE platform.layer_fields ADD CONSTRAINT layer_fields_data_type_check
    CHECK (data_type IN ('text', 'number', 'date', 'boolean', 'coded'));

ALTER TABLE platform.layer_fields ADD COLUMN domain_id uuid REFERENCES platform.domains(id);
-- A coded field MUST bind a domain; a non-coded field MUST NOT — keeps the two coherent.
ALTER TABLE platform.layer_fields ADD CONSTRAINT layer_fields_coded_has_domain
    CHECK ((data_type = 'coded') = (domain_id IS NOT NULL));

-- ── Part 3 — extend the generic validation trigger with the 'coded' branch ─────────────────────────
-- Same function, attached to all 5 slots (V044) — only the body changes (adds domain_id to the loop +
-- the coded membership check). CREATE OR REPLACE leaves the triggers in place.
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
                IF jsonb_typeof(v_val) <> 'string' THEN
                    RAISE EXCEPTION 'layer_fields: field "%" must be an ISO date string', f.field_key USING ERRCODE = 'check_violation';
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
                -- membership = ANY defined code (active OR retired), so existing values survive a retire.
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
