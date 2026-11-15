-- V098 — platform.dashboards (Talus+ dashboards, decision-dashboards.md F4).
--
-- Persists a dashboard: a board of widgets (charts, indicators, lists, a live map, text,
-- filter selectors) over Talus layers the viewer already has access to. Mirrors
-- platform.story_maps (V096+V097) — the ruled catalog pattern for Talus+ objects — with the
-- V097 lesson folded in: owner_id and delete_protected are here from day one.
--
--   definition (jsonb)  the versioned board document, validated by the paid module's
--                       DashboardDefinition model before it lands here. v1 shape
--                       (decision-dashboards.md §4):
--                         { "version": 1,
--                           "widgets": [ {"id": "w_<8hex>", "kind": "map|chart|indicator|list|text|filter", ...} ],
--                           "layout":  [ react-grid-layout's own serializable items ],
--                           "sharedFilters": [ {"layerId","field","op","value"} ] }
--                       Every field that reaches a query is RE-VALIDATED server-side by the
--                       gated endpoint it feeds (/features, /stats) — the definition is data,
--                       never trusted as code (§6).
--   created_by / updated_by   actor attribution (FK to platform.users), ATTRIBUTION-ONLY —
--                       no shadow-audit table, same reasoning as web_maps/story_maps:
--                       presentation config over already-audited data.
--   owner_id            the mutable owner (creator == owner in v1; a future transfer updates
--                       it AND moves the OpenFGA `owner` tuple, like web_maps). The "Owned"
--                       list keys on it — a pure DB query, resilient to an OpenFGA blip.
--   delete_protected    the catalog padlock, baked in from day one.
--
-- The table lives in the CORE schema on purpose: schema is infrastructure, not the feature.
-- Without the Talus+ module mounted there is no code path that reads or writes it — an empty
-- table in a free install is inert (open-core invariant, decision-paid-module-home-entitlement.md).
--
-- Lifecycle (paid dashboards router): create is OpenFGA-FIRST (app-gen uuid → owner tuple →
-- row) and delete is OpenFGA-FIRST too, mirroring web_maps/story_maps.

CREATE TABLE platform.dashboards (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    title             text NOT NULL,
    description       text,
    definition        jsonb NOT NULL,
    created_by        uuid NOT NULL REFERENCES platform.users(id),
    updated_by        uuid NOT NULL REFERENCES platform.users(id),
    owner_id          uuid NOT NULL REFERENCES platform.users(id),
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    delete_protected  boolean NOT NULL DEFAULT false,

    CONSTRAINT dashboards_title_not_blank_chk CHECK (length(btrim(title)) > 0)
);

-- "My dashboards" lists by creator; "Owned" keys on owner_id (story_maps parity).
CREATE INDEX dashboards_created_by_idx ON platform.dashboards (created_by);
CREATE INDEX dashboards_owner_id_idx   ON platform.dashboards (owner_id);
