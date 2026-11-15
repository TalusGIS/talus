-- V116 — which kinds of basemap a portal LISTS, and two more OpenStreetMap looks
-- (decision-streamed-basemap.md §16).
--
-- WHAT THIS ADDS.
--
--   1. downloaded_basemaps_listed — whether the portal lists the three DOWNLOADED looks (Light,
--      Dark, Gray), which draw from the organization's own downloaded area. With V115's
--      streamed_basemaps (which streamed publishers are offered) it gives three states:
--
--        Streamed     listed = false, at least one publisher offered   (every new install)
--        Downloaded   listed = true,  no publisher offered             (no external map requests)
--        Both         listed = true,  at least one publisher offered   (every upgraded install)
--
--      A portal must list something, so "not listed and nothing offered" is refused.
--
--   2. Two new basemap NAMES, 'osm-gray' and 'osm-dark': the standard OpenStreetMap tiles drawn
--      gray or dark by the viewer's browser. Same publisher, same tile address, no new request.
--      They are looks, not publishers: streamed_basemaps stays a subset of osm|usgstopo|imagery,
--      and an OpenStreetMap look is offered when 'osm' is.
--
--   3. "New maps open on" must be a map the portal lists. V115's rule (a streamed default must be
--      offered) is widened: a downloaded default needs the downloaded looks listed, and an
--      OpenStreetMap look needs 'osm' offered.
--
-- AN UPGRADE CHANGES NOTHING. Every existing install gets downloaded_basemaps_listed = true and
-- keeps its offered list and its default: it lists exactly what it listed before. An
-- administrator makes the choice (existing installs keep what they have).
--
-- A NEW INSTALL IS ON STREAMED. The UPDATE below runs only when platform.users is empty, the
-- same test V115 uses and for the same reason: no migration inserts a user, so "no users" is
-- true of an install nobody has ever used and of nothing else. V115 has just given such an
-- install all three publishers and a default of 'osm'; this stops listing the downloaded looks
-- (having the downloaded basemaps on by default would be confusing).
-- The extra conditions keep the UPDATE inside the constraints whatever state it finds.
--
-- Names only, as in V115: no tile address is stored or accepted anywhere.

ALTER TABLE platform.org_settings
    ADD COLUMN downloaded_basemaps_listed boolean NOT NULL DEFAULT true;

ALTER TABLE platform.org_settings
    DROP CONSTRAINT org_settings_default_basemap_check;

ALTER TABLE platform.org_settings
    ADD CONSTRAINT org_settings_default_basemap_check
        CHECK (default_basemap IN (
            'light', 'dark', 'grayscale',
            'osm', 'osm-gray', 'osm-dark', 'usgstopo', 'imagery'
        ));

ALTER TABLE platform.org_settings
    DROP CONSTRAINT org_settings_default_basemap_offered_chk;

ALTER TABLE platform.org_settings
    ADD CONSTRAINT org_settings_default_basemap_offered_chk
        CHECK (
            (default_basemap IN ('light', 'dark', 'grayscale') AND downloaded_basemaps_listed)
            OR (default_basemap IN ('osm', 'osm-gray', 'osm-dark') AND 'osm' = ANY (streamed_basemaps))
            OR (default_basemap IN ('usgstopo', 'imagery') AND default_basemap = ANY (streamed_basemaps))
        );

ALTER TABLE platform.org_settings
    ADD CONSTRAINT org_settings_basemaps_listed_chk
        CHECK (downloaded_basemaps_listed OR cardinality(streamed_basemaps) > 0);

UPDATE platform.org_settings
   SET downloaded_basemaps_listed = false
 WHERE id = true
   AND NOT EXISTS (SELECT 1 FROM platform.users)
   AND cardinality(streamed_basemaps) > 0
   AND default_basemap NOT IN ('light', 'dark', 'grayscale');

COMMENT ON COLUMN platform.org_settings.downloaded_basemaps_listed IS
  'decision-streamed-basemap.md §16: whether the portal lists the downloaded looks '
  '(light|dark|grayscale). false + a non-empty streamed_basemaps = Streamed; true + an empty one '
  '= Downloaded (no external map requests); true + non-empty = both. Upgrades start true; a new '
  'install false. Governs the switcher and what new maps open on, not what a saved map may draw.';

COMMENT ON COLUMN platform.org_settings.default_basemap IS
  'The basemap a NEW map opens on, org-wide (V093; widened by V115 and V116). One of '
  'light|dark|grayscale (the downloaded looks; then downloaded_basemaps_listed must be true) or a '
  'streamed look: osm|osm-gray|osm-dark (then osm must be in streamed_basemaps) or '
  'usgstopo|imagery (then it must be). Saved maps keep their own basemap.';
