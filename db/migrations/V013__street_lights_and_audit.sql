-- V013 — assets.street_lights + shadow audit (Layer 7a, the second asset).
--
-- Mirrors V005 (assets.signs) and V007 (audit.signs_audit + trigger). This is the
-- SECOND consumer of the V012 generic trigger function audit.fn_record_change():
-- the new shadow table carries actor_type + provenance (the nine columns the
-- function writes), so audit comes for free with NO function change — the first
-- proof the audit architecture is asset-agnostic.
--
-- Domains: CONDITION and OWNEDBY/MAINTBY reuse the existing assets.condition_codes
-- and assets.org_codes (the source values map cleanly). LIGHTTYPE and BULBTYPE are
-- customer-derived (no national standard like MUTCD) and get their own domain
-- tables. 'unknown' is kept only where the source CSV has a literal "Unknown"
-- value (both LIGHTTYPE and BULBTYPE do): NULL = never recorded, 'unknown' = the
-- crew recorded it as unknown.

-- 1) Domain tables — same generic shape as every other picklist (V004).
CREATE TABLE assets.light_type_codes (LIKE assets.sign_categories INCLUDING ALL);
CREATE TABLE assets.bulb_type_codes  (LIKE assets.sign_categories INCLUDING ALL);

INSERT INTO assets.bulb_type_codes (code, label, sort_order) VALUES
    ('hps',     'High Pressure Sodium', 10),
    ('led',     'LED',                  20),
    ('mh',      'Metal Halide',         30),
    ('unknown', 'Unknown',              90);

INSERT INTO assets.light_type_codes (code, label, sort_order) VALUES
    ('decorative',      'Decorative',      10),
    ('decorative_dual', 'Decorative Dual', 20),
    ('cobra_head',      'Cobra Head',      30),
    ('open_bottom',     'Open Bottom',     40),
    ('acorn',           'Acorn',           50),
    ('pendant',         'Pendant',         60),
    ('full_cutoff',     'Full Cutoff',     70),
    ('semi_cutoff',     'Semi Cutoff',     80),
    ('shoe_box',        'Shoe Box',        90),
    ('teardrop',        'Tear Drop',      100),
    ('bollard',         'Bollard',        110),
    ('square_top',      'Square Top',     120),
    ('other',           'Other',          900),
    ('unknown',         'Unknown',        990);

-- 2) The asset table — platform conventions (id/geom/edit-tracking/ai_provenance)
--    + street-light-specific columns. cover_photo_id is declared without an FK for
--    now (photos are not yet wired for lights; mirrors how signs deferred its FK to
--    media.photos to V006).
CREATE TABLE assets.street_lights (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    geom                geometry(Point, 4326) NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    created_by          uuid NOT NULL REFERENCES platform.users(id),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    updated_by          uuid NOT NULL REFERENCES platform.users(id),
    ai_provenance       jsonb,

    light_type_code     text REFERENCES assets.light_type_codes(code),
    bulb_type_code      text REFERENCES assets.bulb_type_codes(code),
    condition_code      text REFERENCES assets.condition_codes(code),
    owned_by_code       text REFERENCES assets.org_codes(code),
    maintained_by_code  text REFERENCES assets.org_codes(code),
    dark_sky_compliant  boolean,
    decorative          boolean,
    install_date        date,
    notes               text,
    cover_photo_id      uuid
);

CREATE INDEX street_lights_geom_idx       ON assets.street_lights USING GIST (geom);
CREATE INDEX street_lights_updated_at_idx ON assets.street_lights (updated_at);
CREATE INDEX street_lights_light_type_idx ON assets.street_lights (light_type_code);
CREATE INDEX street_lights_condition_idx  ON assets.street_lights (condition_code);

-- 3) Shadow audit table — all nine columns audit.fn_record_change() writes,
--    including the V012 actor_type + provenance. Mirrors audit.signs_audit's
--    post-V012 shape, declared inline (this table is born after V012, so no ALTER).
--    Audit is on by default; to turn it off for a customer:
--      DROP TRIGGER street_lights_audit_trigger ON assets.street_lights;
CREATE TABLE audit.street_lights_audit (
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

CREATE INDEX street_lights_audit_asset_idx   ON audit.street_lights_audit (asset_id);
CREATE INDEX street_lights_audit_changed_idx ON audit.street_lights_audit (changed_at);

CREATE TRIGGER street_lights_audit_trigger
AFTER INSERT OR UPDATE OR DELETE ON assets.street_lights
FOR EACH ROW EXECUTE FUNCTION audit.fn_record_change('audit.street_lights_audit', 'id');
