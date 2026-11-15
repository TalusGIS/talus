-- V022 — layer symbology: platform.layers.renderer + attribute-bearing tile functions
-- (Talus 13).
--
-- Adds AGOL-style layer-default symbology. Three cohesive parts, all in service of one
-- slice (a layer carries a persisted renderer that paints for every viewer):
--
--   A. platform.layers.renderer jsonb — the portable, self-contained renderer spec
--      (type=simple|uniqueValue, field, stops[value,color,label], default, minZoom).
--      Sits alongside display_color, which V021 opened as the presentation section.
--      Resolution at render time: renderer ?? single-symbol(display_color) ?? LayerDef
--      constant. The future per-user saved-map override stores the SAME shape, resolved
--      ahead of this (map override -> layer default).
--
--   B/C. Regenerate ALL tile functions to carry the user-facing ATTRIBUTE SET, not just
--      the lean id + title field. This is a DELIBERATE AMENDMENT to the lean-tile
--      invariant in V011/V014/V017 ("only id + title ride; attributes never go stale in
--      the tile"): client-side MapLibre `match` coloring REQUIRES the categorizing
--      attribute to be physically in the tile, so unique-value symbology on any field is
--      impossible without it. We trade the "never stale" half of the lean promise for the
--      baked attributes; in exchange any field is symbolizable with no structural
--      re-tiling on a symbology change — only a tile-cache bump when a feature's value is
--      edited. Cost is bounded: MVT dictionary-encodes property values per tile, so
--      low-cardinality fields (the must-have condition_code = 4 values) cost ~O(distinct
--      values) regardless of feature count; only high-cardinality free-text scales
--      linearly (rare, and soft-warned in the UI). Popups still fetch full attributes
--      live per click (always fresh) — the tile attributes exist ONLY to drive paint.
--
--      Baked set = every user-facing column EXCEPT geometry, audit/system (created_*,
--      updated_*), provenance (ai_provenance), photo refs (cover_photo_id), and — for
--      county_boundary — pure import lineage (source_*, shape_*). id is kept.
--
--   D. Seed signs.renderer with a Good->Replace condition_code ramp, so the headline
--      portal must-have ("signs colored Good/Fair/Poor/Replace at a glance") renders out
--      of the box with no admin action. Other layers stay NULL (single-symbol fallback).
--
-- pivot note: dropping the long free-text columns (sign_text, notes, facility_id) from
-- the SELECTs later is a one-line-per-function CREATE OR REPLACE — non-destructive, no
-- data migration, and the renderer spec is field-name-agnostic so it is untouched.

-- ============================================================================
-- A. renderer column
-- ============================================================================

ALTER TABLE platform.layers
    ADD COLUMN renderer jsonb;

-- Gross-shape guard only (NULL or a known renderer type). Full stop/field validation is
-- the API's job; this just stops a malformed type from being persisted.
ALTER TABLE platform.layers
    ADD CONSTRAINT layers_renderer_type_chk
    CHECK (
        renderer IS NULL
        OR (renderer->>'type') IN ('simple', 'uniqueValue')
    );

-- ============================================================================
-- B. Bespoke tile functions — regenerated with the user-facing attribute set.
--    (Geometry/projection logic unchanged from V011/V014/V017; only the SELECT
--    list grows. The MVT internal layer name stays the MapLibre source-layer.)
-- ============================================================================

CREATE OR REPLACE FUNCTION assets.signs_tile(z integer, x integer, y integer)
RETURNS bytea
AS $$
  SELECT ST_AsMVT(tile, 'signs', 4096, 'geom')
  FROM (
    SELECT
        ST_AsMVTGeom(ST_Transform(s.geom, 3857), ST_TileEnvelope(z, x, y)) AS geom,
        s.id::text                   AS id,
        s.facility_id                AS facility_id,
        s.sign_category_code         AS sign_category_code,
        s.mutcd_code                 AS mutcd_code,
        s.sign_text                  AS sign_text,
        s.condition_code             AS condition_code,
        s.mount_type_code            AS mount_type_code,
        s.facing_direction           AS facing_direction,
        s.facing_direction_secondary AS facing_direction_secondary,
        s.install_date::text         AS install_date,
        s.owned_by_code              AS owned_by_code,
        s.maintained_by_code         AS maintained_by_code,
        s.notes                      AS notes
    FROM assets.signs s
    WHERE s.geom && ST_Transform(ST_TileEnvelope(z, x, y), 4326)
  ) AS tile
  WHERE tile.geom IS NOT NULL;
$$ LANGUAGE sql STABLE PARALLEL SAFE;

CREATE OR REPLACE FUNCTION assets.street_lights_tile(z integer, x integer, y integer)
RETURNS bytea
AS $$
  SELECT ST_AsMVT(tile, 'street_lights', 4096, 'geom')
  FROM (
    SELECT
        ST_AsMVTGeom(ST_Transform(s.geom, 3857), ST_TileEnvelope(z, x, y)) AS geom,
        s.id::text             AS id,
        s.light_type_code      AS light_type_code,
        s.bulb_type_code       AS bulb_type_code,
        s.condition_code       AS condition_code,
        s.owned_by_code        AS owned_by_code,
        s.maintained_by_code   AS maintained_by_code,
        s.dark_sky_compliant   AS dark_sky_compliant,
        s.decorative           AS decorative,
        s.install_date::text   AS install_date,
        s.notes                AS notes
    FROM assets.street_lights s
    WHERE s.geom && ST_Transform(ST_TileEnvelope(z, x, y), 4326)
  ) AS tile
  WHERE tile.geom IS NOT NULL;
$$ LANGUAGE sql STABLE PARALLEL SAFE;

-- county_boundary: descriptive attributes only. source_* / shape_* are import lineage,
-- not symbology candidates — excluded to keep the polygon tiles relevant (and lean).
CREATE OR REPLACE FUNCTION assets.county_boundary_tile(z integer, x integer, y integer)
RETURNS bytea
AS $$
  SELECT ST_AsMVT(tile, 'county_boundary', 4096, 'geom')
  FROM (
    SELECT
        ST_AsMVTGeom(ST_Transform(c.geom, 3857), ST_TileEnvelope(z, x, y)) AS geom,
        c.id::text         AS id,
        c.boundary_class   AS boundary_class,
        c.feature_name     AS feature_name,
        c.acreage          AS acreage,
        c.municipality     AS municipality
    FROM assets.county_boundary c
    WHERE c.geom && ST_Transform(ST_TileEnvelope(z, x, y), 4326)
  ) AS tile
  WHERE tile.geom IS NOT NULL;
$$ LANGUAGE sql STABLE PARALLEL SAFE;

-- ============================================================================
-- C. Slot tile functions — the static V019 pool of 5. Each slot's only user
--    attribute is `notes`; add it so a claimed slot can do single-symbol AND
--    unique-value-on-notes symbology like any other layer. (Claiming a slot does
--    no DDL — it assigns a pre-built pool slot — so these 5 are the whole set.)
-- ============================================================================

CREATE OR REPLACE FUNCTION assets.tal_7b3aa14c_tile(z integer, x integer, y integer)
RETURNS bytea
AS $$
  SELECT ST_AsMVT(tile, 'tal_7b3aa14c', 4096, 'geom')
  FROM (
    SELECT
        ST_AsMVTGeom(ST_Transform(s.geom, 3857), ST_TileEnvelope(z, x, y)) AS geom,
        s.id::text AS id,
        s.notes    AS notes
    FROM assets.tal_7b3aa14c s
    WHERE s.geom && ST_Transform(ST_TileEnvelope(z, x, y), 4326)
  ) AS tile
  WHERE tile.geom IS NOT NULL;
$$ LANGUAGE sql STABLE PARALLEL SAFE;

CREATE OR REPLACE FUNCTION assets.tal_6496fe7d_tile(z integer, x integer, y integer)
RETURNS bytea
AS $$
  SELECT ST_AsMVT(tile, 'tal_6496fe7d', 4096, 'geom')
  FROM (
    SELECT
        ST_AsMVTGeom(ST_Transform(s.geom, 3857), ST_TileEnvelope(z, x, y)) AS geom,
        s.id::text AS id,
        s.notes    AS notes
    FROM assets.tal_6496fe7d s
    WHERE s.geom && ST_Transform(ST_TileEnvelope(z, x, y), 4326)
  ) AS tile
  WHERE tile.geom IS NOT NULL;
$$ LANGUAGE sql STABLE PARALLEL SAFE;

CREATE OR REPLACE FUNCTION assets.tal_2dbf236f_tile(z integer, x integer, y integer)
RETURNS bytea
AS $$
  SELECT ST_AsMVT(tile, 'tal_2dbf236f', 4096, 'geom')
  FROM (
    SELECT
        ST_AsMVTGeom(ST_Transform(s.geom, 3857), ST_TileEnvelope(z, x, y)) AS geom,
        s.id::text AS id,
        s.notes    AS notes
    FROM assets.tal_2dbf236f s
    WHERE s.geom && ST_Transform(ST_TileEnvelope(z, x, y), 4326)
  ) AS tile
  WHERE tile.geom IS NOT NULL;
$$ LANGUAGE sql STABLE PARALLEL SAFE;

CREATE OR REPLACE FUNCTION assets.tal_2c09dee7_tile(z integer, x integer, y integer)
RETURNS bytea
AS $$
  SELECT ST_AsMVT(tile, 'tal_2c09dee7', 4096, 'geom')
  FROM (
    SELECT
        ST_AsMVTGeom(ST_Transform(s.geom, 3857), ST_TileEnvelope(z, x, y)) AS geom,
        s.id::text AS id,
        s.notes    AS notes
    FROM assets.tal_2c09dee7 s
    WHERE s.geom && ST_Transform(ST_TileEnvelope(z, x, y), 4326)
  ) AS tile
  WHERE tile.geom IS NOT NULL;
$$ LANGUAGE sql STABLE PARALLEL SAFE;

CREATE OR REPLACE FUNCTION assets.tal_57b25bf6_tile(z integer, x integer, y integer)
RETURNS bytea
AS $$
  SELECT ST_AsMVT(tile, 'tal_57b25bf6', 4096, 'geom')
  FROM (
    SELECT
        ST_AsMVTGeom(ST_Transform(s.geom, 3857), ST_TileEnvelope(z, x, y)) AS geom,
        s.id::text AS id,
        s.notes    AS notes
    FROM assets.tal_57b25bf6 s
    WHERE s.geom && ST_Transform(ST_TileEnvelope(z, x, y), 4326)
  ) AS tile
  WHERE tile.geom IS NOT NULL;
$$ LANGUAGE sql STABLE PARALLEL SAFE;

-- ============================================================================
-- D. Seed the signs default renderer (Good -> Replace condition ramp).
--    UPDATE fires the V020 platform.layers audit trigger — appropriate: a seeded
--    config change is audited like any other. Other layers stay NULL.
-- ============================================================================

UPDATE platform.layers
SET renderer = jsonb_build_object(
    'type',    'uniqueValue',
    'field',   'condition_code',
    'minZoom', NULL,
    'default', jsonb_build_object('color', '#cccccc', 'label', '«No value»'),
    'stops',   jsonb_build_array(
        jsonb_build_object('value', 'good',    'color', '#1a9850', 'label', 'Good'),
        jsonb_build_object('value', 'fair',    'color', '#fee08b', 'label', 'Fair'),
        jsonb_build_object('value', 'poor',    'color', '#fc8d59', 'label', 'Poor'),
        jsonb_build_object('value', 'replace', 'color', '#d73027', 'label', 'Replace')
    )
)
WHERE layer_id = 'signs';
