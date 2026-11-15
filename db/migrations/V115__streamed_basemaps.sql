-- V115 — streamed basemaps (decision-streamed-basemap.md §14, §15:
-- OpenStreetMap, streamed by default; downloaded for offline use).
--
-- WHAT THIS ADDS. A streamed basemap is a public map each viewer's browser fetches straight from
-- its publisher: OpenStreetMap's standard tiles ('osm'), USGS Topo ('usgstopo') and the USGS
-- imagery the product has offered since Talus 18d ('imagery'). Talus hosts and relays nothing.
--
--   1. streamed_basemaps — WHICH streamed maps this portal offers. A set of NAMES from a closed
--      list; the tile addresses are constants in the web client and are never stored or accepted.
--      An empty set is the setting for "no map background is fetched from the internet".
--   2. default_basemap (V093) may now name 'osm' or 'usgstopo' as well as the original four.
--   3. A default that names a streamed map must be one the portal offers — the backstop for the
--      api's own check, so the switcher is never told to open on a map it will not show.
--
-- AN UPGRADE CHANGES NOTHING. Every existing install gets streamed_basemaps = {imagery} — exactly
-- what it offers today — and keeps its default_basemap. No existing install starts contacting a
-- new host because it upgraded; an administrator has to tick a box.
--
-- A NEW INSTALL OPENS ON OPENSTREETMAP. The UPDATE below runs only when platform.users is empty.
-- No migration inserts a user (the first one is created by the guided setup, after migrations),
-- so "no users" is true of an install nobody has ever used and of nothing else. The org_settings
-- row itself exists since V028.
--
-- Saved maps are untouched: a web map restores the basemap it was saved with, and one that names a
-- streamed map the portal has since stopped offering falls back to Light in the client.

ALTER TABLE platform.org_settings
    ADD COLUMN streamed_basemaps text[] NOT NULL DEFAULT ARRAY['imagery']::text[]
        CONSTRAINT org_settings_streamed_basemaps_chk
        CHECK (
            streamed_basemaps <@ ARRAY['osm', 'usgstopo', 'imagery']::text[]
            AND cardinality(streamed_basemaps) <= 3
        );

ALTER TABLE platform.org_settings
    DROP CONSTRAINT org_settings_default_basemap_check;

ALTER TABLE platform.org_settings
    ADD CONSTRAINT org_settings_default_basemap_check
        CHECK (default_basemap IN ('light', 'dark', 'grayscale', 'imagery', 'osm', 'usgstopo'));

ALTER TABLE platform.org_settings
    ADD CONSTRAINT org_settings_default_basemap_offered_chk
        CHECK (
            default_basemap IN ('light', 'dark', 'grayscale')
            OR default_basemap = ANY (streamed_basemaps)
        );

UPDATE platform.org_settings
   SET streamed_basemaps = ARRAY['osm', 'usgstopo', 'imagery']::text[],
       default_basemap   = 'osm'
 WHERE id = true
   AND NOT EXISTS (SELECT 1 FROM platform.users);

COMMENT ON COLUMN platform.org_settings.streamed_basemaps IS
  'decision-streamed-basemap.md §14: the streamed (browser-fetched, publisher-hosted) basemaps '
  'this portal offers — any subset of osm|usgstopo|imagery; empty = none. Names only; the tile '
  'addresses are client constants. Upgrades start at {imagery}; a new install at all three.';

COMMENT ON COLUMN platform.org_settings.default_basemap IS
  'The basemap a NEW map opens on, org-wide (V093; widened by V115). One of '
  'light|dark|grayscale (drawn from the downloaded area) or osm|usgstopo|imagery (streamed, and '
  'then it must be in streamed_basemaps). Saved maps keep their own basemap.';
