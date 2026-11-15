-- V017 — assets.county_boundary_tile (lean MVT function layer for pg_tileserv)
--
-- Mirror of V014 (assets.street_lights_tile) for the first POLYGON asset — the
-- geometry-generalization proof at the tile layer: same function shape, polygon
-- instead of point, no centroid, single render path. Deliberately LEAN: only `id`
-- (+ `boundary_class` for the popup title) ride in the tile. The full attribute set
-- is fetched live per click via GET /county_boundary/{id}, so tiles stay light and
-- never go stale when attributes change (no re-tiling). boundary_class is the title
-- field (always populated: MAIN/ISLD) — feature_name is empty for several features.
--
-- Projection: county_boundary.geom is stored EPSG:4326; MVT requires Web Mercator
-- (3857). The bbox filter uses the 4326 tile envelope so the GiST index
-- (county_boundary_geom_idx) does the work; only matched rows are reprojected to
-- 3857 for ST_AsMVTGeom. Polygon specifics: ST_AsMVTGeom CLIPS polygons that straddle
-- the tile edge to the tile, and returns NULL for polygons that don't intersect the
-- tile at all; the `tile.geom IS NOT NULL` guard covers BOTH cases, so the tile never
-- carries geometry-less features. No centroid — the polygon renders as fill/outline.
--
-- pg_tileserv auto-publishes this as the function layer `assets.county_boundary_tile`,
-- tiled at /assets.county_boundary_tile/{z}/{x}/{y}.pbf. The MVT internal layer name
-- is 'county_boundary' (the MapLibre `source-layer`). The api proxies it behind
-- Keycloak auth; pg_tileserv itself stays internal-only.

CREATE OR REPLACE FUNCTION assets.county_boundary_tile(z integer, x integer, y integer)
RETURNS bytea
AS $$
  SELECT ST_AsMVT(tile, 'county_boundary', 4096, 'geom')
  FROM (
    SELECT
        ST_AsMVTGeom(ST_Transform(c.geom, 3857), ST_TileEnvelope(z, x, y)) AS geom,
        c.id::text         AS id,
        c.boundary_class   AS boundary_class
    FROM assets.county_boundary c
    WHERE c.geom && ST_Transform(ST_TileEnvelope(z, x, y), 4326)
  ) AS tile
  WHERE tile.geom IS NOT NULL;
$$ LANGUAGE sql STABLE PARALLEL SAFE;
