-- V097 — story_maps ownership column (decision-story-maps.md F4; mirrors V030's web_maps pattern).
--
-- V096 mirrored V027's original web_maps shape, which predated transferable ownership (V030 added
-- owner_id to web_maps later). The story-maps router mirrors TODAY'S web_maps queries — which key
-- the "Owned" list on owner_id (a pure DB query, resilient to an OpenFGA blip) — so the column
-- belongs here from the start. created_by stays the immutable creator; owner_id is the mutable
-- owner (creator == owner in v1; a future transfer updates owner_id AND moves the OpenFGA `owner`
-- tuple on story_map:<id>, exactly like web_maps).
--
-- The table is empty at this point (V096 landed the same session), so NOT NULL applies directly —
-- no backfill step needed, unlike V030.

ALTER TABLE platform.story_maps
    ADD COLUMN owner_id uuid NOT NULL REFERENCES platform.users(id);

-- The "Owned" list keys on owner_id (see the router's my_story_maps), so index it like created_by.
CREATE INDEX story_maps_owner_id_idx ON platform.story_maps (owner_id);
