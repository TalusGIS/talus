-- V096 — platform.story_maps (Talus+ story maps, decision-story-maps.md F4).
--
-- Persists a story map: a scroll-driven narrative over ONE existing web map — an ordered list
-- of steps, each an embedded viewpoint + text block + optional attachment ref + optional
-- layer-visibility list. Mirrors platform.web_maps (V027) deliberately: the story REFERENCES
-- content (a web map by id; attachments by ref) and never copies or transforms it — every byte
-- a viewer sees is still served through the existing gated layer/tile/attachment paths.
--
--   definition (jsonb)  the versioned story-map document, validated by the paid module's
--                       StoryMapDefinition model before it lands here. v1 shape:
--                         { "version": 1,
--                           "mapId": "<web_map uuid>",
--                           "steps": [ { "viewpoint": { "center": [lon, lat], "zoom": 12.0 },
--                                        "heading": "...",            -- optional
--                                        "body": "...",               -- plain text ONLY (no HTML/markdown)
--                                        "attachment": { "layerId": "...", "featureId": ..., "attachmentId": "..." },  -- optional
--                                        "visibleLayers": ["<layer_id>", ...] }, ... ] }  -- optional; order = array order
--   created_by / updated_by   actor attribution (FK to platform.users), ATTRIBUTION-ONLY —
--                       no shadow-audit table, same reasoning as web_maps (V027): presentation
--                       config over already-audited data. created_by == owner in v1; ownership
--                       AUTHZ is the OpenFGA `owner` tuple on story_map:<id>, written at save.
--   delete_protected    the catalog padlock, baked in from day one (V085 field_projects
--                       precedent) instead of a follow-up ALTER (V029 pattern).
--
-- The table lives in the CORE schema on purpose: schema is infrastructure, not the feature.
-- Without the Talus+ module mounted there is no code path that reads or writes it — an empty
-- table in a free install is inert, and nothing core depends on it (open-core invariant,
-- decision-paid-module-home-entitlement.md).
--
-- Lifecycle (paid story-maps router): create is OpenFGA-FIRST (app-gen uuid → owner tuple →
-- row) and delete is OpenFGA-FIRST too, mirroring web_maps (V027 header).

CREATE TABLE platform.story_maps (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    title             text NOT NULL,
    description       text,
    definition        jsonb NOT NULL,
    created_by        uuid NOT NULL REFERENCES platform.users(id),
    updated_by        uuid NOT NULL REFERENCES platform.users(id),
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    delete_protected  boolean NOT NULL DEFAULT false,

    CONSTRAINT story_maps_title_not_blank_chk CHECK (length(btrim(title)) > 0)
);

-- "My story maps" lists by creator (creator == owner in v1), same as web_maps.
CREATE INDEX story_maps_created_by_idx ON platform.story_maps (created_by);
