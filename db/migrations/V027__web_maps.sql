-- V027 — platform.web_maps (Talus 20: save & own web maps).
--
-- Persists the map a publisher composes in New Map (Slice A was front-end only) as an owned,
-- re-openable item: a saved VIEW over existing layers. The map REFERENCES layers (by layer_id)
-- in its `definition` JSON; it never copies or transforms their data — the layers + tiles +
-- renderers stay the single source of truth. (decision-webmap-save.md F1.)
--
--   definition (jsonb)  the versioned web-map document, validated by the API's WebMapDefinition
--                       model before it lands here. v1 shape:
--                         { "version": 1,
--                           "basemap": "light"|"dark"|"grayscale"|"imagery",
--                           "operationalLayers": [ {"layerId": "...", "visible": true}, ... ],  -- order = array order
--                           "initialView": { "center": [lon, lat], "zoom": 12.0 } }
--   created_by / updated_by   actor attribution (FK to platform.users). ATTRIBUTION-ONLY — there
--                       is deliberately NO shadow-audit table (F1b): a web map is presentation
--                       config over already-audited data, owner-edited, not a system of record.
--                       (cf. platform.groups V026.) created_by == owner in v1 (no transfer), so
--                       "my maps" lists cheaply by created_by; ownership AUTHZ is the OpenFGA
--                       `owner` tuple on web_map:<id> (model: Talus 20 c1), written at save.
--
-- Lifecycle (routers/web_maps.py): create is OpenFGA-FIRST (app-gen uuid → owner tuple → row),
-- so a mid-failure leaves a harmless orphan tuple, not an unopenable orphan row; delete is
-- OpenFGA-FIRST too (read all web_map:<id> tuples → delete → delete row), mirroring release_layer.

CREATE TABLE platform.web_maps (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    title        text NOT NULL,
    description  text,
    definition   jsonb NOT NULL,
    created_by   uuid NOT NULL REFERENCES platform.users(id),
    updated_by   uuid NOT NULL REFERENCES platform.users(id),
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT web_maps_title_not_blank_chk CHECK (length(btrim(title)) > 0)
);

-- "My maps" lists by creator (creator == owner in v1).
CREATE INDEX web_maps_created_by_idx ON platform.web_maps (created_by);
