-- V094 — org default map extent + units (decision-org-default-extent.md).
--
-- WHAT THIS ADDS. The extent ladder's deliberately-reserved rung (AssetMap.tsx: "a future
-- org-set default extent will win"): an admin-owned answer to "where does this deployment's
-- map open." Precedence, settled in fork H:
--   saved map initialView  >  ORG DEFAULT EXTENT (new)  >  basemap coverage fit  >  world frame
-- The setting seeds NEW maps only, and /map is the ONLY route that can consume it — every saved
-- map owns its view (initialView is REQUIRED on WebMapDefinition, no back-compat default), and
-- both anonymous routes open saved maps (decision doc §2.3). An anonymous viewer never sees it.
--
-- default_extent — WGS84 [w, s, e, n] jsonb 4-array, the same shape and CHECK idiom as V084's
-- basemap_bbox. NULLABLE ON PURPOSE: NULL is a meaningful state (unset -> today's behaviour,
-- coverage fit then world frame), not a missing value. The CHECK is shape-only; numeric range
-- and ordering (-180 <= w < e <= 180, -85.06 <= s < n <= 85.06) are the API validator's job,
-- shared with the basemap fetch's — the V084 division of labour.
-- Seeded at basemap provision time (write_metadata) ONLY when NULL (fork F): a routine
-- re-fetch must not silently move everyone's opening view. An admin-cleared extent therefore
-- re-seeds on the next provision — accepted, the re-seeded value is the helpful answer.
--
-- default_units — the units for the EMBED scale bar, this value's only consumer today (the main
-- map shows a unit-agnostic 1:N ratio; there is no measurement tool — decision doc §2.1).
-- 'metric' factory default (fork C): the previous hardcoded 'imperial' was a US-centric default
-- for a product that is not US-only — the DEFAULT_CENTER-off-CONUS reasoning. Deliberately NOT
-- seeded from the browser locale: that is the Talus 41 theme-seed shape V093 just retired
-- (implicit, invisible, per-viewer). An org that wants imperial picks it. Two values only —
-- MapLibre's 'nautical' is fenced (§7).

ALTER TABLE platform.org_settings
    ADD COLUMN default_extent jsonb
        CHECK (default_extent IS NULL
               OR (jsonb_typeof(default_extent) = 'array'
                   AND jsonb_array_length(default_extent) = 4)),
    ADD COLUMN default_units text NOT NULL DEFAULT 'metric'
        CHECK (default_units IN ('metric', 'imperial'));

COMMENT ON COLUMN platform.org_settings.default_extent IS
  'decision-org-default-extent.md: WGS84 [w,s,e,n] a NEW map (/map only) opens on; beats the '
  'coverage fit, loses to a saved map''s initialView. NULL = unset (coverage fit stands). '
  'Auto-seeded from the basemap bbox at provision time only when NULL (fork F). Shape-checked '
  'here; range/ordering validated by the API.';

COMMENT ON COLUMN platform.org_settings.default_units IS
  'decision-org-default-extent.md fork C: metric|imperial for the embed scale bar — its only '
  'consumer (the main map shows a unitless ratio scale). Factory ''metric''; deliberately never '
  'derived from the browser locale.';
