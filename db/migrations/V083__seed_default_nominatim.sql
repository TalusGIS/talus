-- V083 — seed the public OpenStreetMap Nominatim geocoder as a shipped default (all tiers).
-- Out of the box, a fresh Talus install has a working address search with zero configuration —
-- a built-in default locator. Tier-2, backup-gated.
--
-- Compliance posture (OSMF Nominatim usage policy):
--   * batch geocoding against this host is already hard-blocked (geocode.py F6a, PR #54);
--   * autocomplete (search-as-you-type) is suppressed for public-OSM hosts in the /geocode/suggest
--     walk (geocode.py, this slice) — press-Enter search and click-driven reverse lookups remain;
--   * every request carries the descriptive Talus User-Agent (geocode.py _NOMINATIM_UA).
-- Air-gapped deployments simply can't reach it: search fails soft (no results), and the admin can
-- disable or delete the row in Admin → Geocoders — deletion is permanent (no re-seed: this
-- migration runs once ever, mirroring the V037 geocoders_seeded one-time principle).
--
-- Idempotence: skipped when the org already has ANY locator pointed at a public OSM host (however
-- they named it), and ON CONFLICT-guarded against an unrelated locator that happens to hold the
-- 'OpenStreetMap' name. Sorted LAST so an org's own locators (point layers, internal) stay first
-- in the proxy walk — OSM is the fallback, not the override.

INSERT INTO platform.geocoders (name, type, url, sort_order, enabled)
SELECT
    'OpenStreetMap',
    'nominatim',
    'https://nominatim.openstreetmap.org',
    COALESCE((SELECT MAX(sort_order) + 1 FROM platform.geocoders), 0),
    true
WHERE NOT EXISTS (
    SELECT 1 FROM platform.geocoders
    WHERE url ILIKE '%nominatim.openstreetmap.org%'
       OR url ILIKE '%nominatim.osm.org%'
)
ON CONFLICT (name) DO NOTHING;
