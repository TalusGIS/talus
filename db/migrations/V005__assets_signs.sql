-- V005 — assets.signs
--
-- Platform conventions (id/geom/edit-tracking/ai_provenance) + signs-specific columns.
-- cover_photo_id is declared here but the FK to media.photos(id) is added in V006
-- to avoid the circular dependency (signs <-> photos both reference each other).

CREATE TABLE assets.signs (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    geom                geometry(Point, 4326) NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    created_by          uuid NOT NULL REFERENCES platform.users(id),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    updated_by          uuid NOT NULL REFERENCES platform.users(id),
    ai_provenance       jsonb,

    facility_id         text,
    sign_category_code  text NOT NULL REFERENCES assets.sign_categories(code),
    mutcd_code          text REFERENCES assets.mutcd_codes(code),
    sign_text           text,
    condition_code      text REFERENCES assets.condition_codes(code),
    mount_type_code     text REFERENCES assets.mount_type_codes(code),
    facing_direction    text CHECK (facing_direction IN ('N','NE','E','SE','S','SW','W','NW')),
    install_date        date,
    owned_by_code       text REFERENCES assets.org_codes(code),
    maintained_by_code  text REFERENCES assets.org_codes(code),
    notes               text,
    cover_photo_id      uuid
);

CREATE INDEX signs_geom_idx       ON assets.signs USING GIST (geom);
CREATE INDEX signs_updated_at_idx ON assets.signs (updated_at);
CREATE INDEX signs_facility_idx   ON assets.signs (facility_id);
CREATE INDEX signs_category_idx   ON assets.signs (sign_category_code);
CREATE INDEX signs_condition_idx  ON assets.signs (condition_code);
