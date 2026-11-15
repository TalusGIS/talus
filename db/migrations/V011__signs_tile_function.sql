-- V011 — assets.signs_tile (lean MVT function layer for pg_tileserv)
--
-- Serves the signs layer as Mapbox Vector Tiles for the map's vector source.
-- Deliberately LEAN: only `id` (+ `mutcd_code` for the popup title) ride in the
-- tile. The full attribute set is fetched live per click via GET /signs/{id}, so
-- tiles stay light and never go stale when attributes change (no re-tiling).
--
-- Projection: signs.geom is stored EPSG:4326; MVT requires Web Mercator (3857).
-- The bbox filter uses the 4326 tile envelope so the GiST index (signs_geom_idx)
-- does the work; only the matched rows are reprojected to 3857 for ST_AsMVTGeom.
-- ST_AsMVTGeom returns NULL for points clipped outside the tile — filtered out so
-- the tile carries no geometry-less features.
--
-- pg_tileserv auto-publishes this as the function layer `assets.signs_tile`,
-- tiled at /assets.signs_tile/{z}/{x}/{y}.pbf. The MVT internal layer name is
-- 'signs' (the MapLibre `source-layer`). The api proxies it behind Keycloak auth;
-- pg_tileserv itself stays internal-only.

CREATE OR REPLACE FUNCTION assets.signs_tile(z integer, x integer, y integer)
RETURNS bytea
AS $$
  SELECT ST_AsMVT(tile, 'signs', 4096, 'geom')
  FROM (
    SELECT
        ST_AsMVTGeom(ST_Transform(s.geom, 3857), ST_TileEnvelope(z, x, y)) AS geom,
        s.id::text   AS id,
        s.mutcd_code AS mutcd_code
    FROM assets.signs s
    WHERE s.geom && ST_Transform(ST_TileEnvelope(z, x, y), 4326)
  ) AS tile
  WHERE tile.geom IS NOT NULL;
$$ LANGUAGE sql STABLE PARALLEL SAFE;
