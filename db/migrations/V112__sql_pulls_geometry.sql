-- V112 — SQL-source pull s2: a pull may build POINT geometry from two coordinate columns
-- (decision-sql-source-pull.md s2; kickoff §0 "getting geometry from non-spatial rows"). Tier-2, backup-gated.
--
-- `geometry` is the plan for the target layer's shape, or NULL for a non-spatial table (s1):
--   {"kind": "xy", "x": "<source column>", "y": "<source column>", "srid": <EPSG>}
-- The x/y columns are also ordinary pulled columns (they stay as attributes); the SRID follows the
-- bulk import's E4 rule — detected 4326 for a lon/lat-named pair, otherwise declared by the admin and
-- validated against spatial_ref_sys at pull creation. The target is an ordinary point layer
-- (provision_spatial_layer, canonical 4326 storage, reprojected in), locked while attached (F3).
-- SDE/PostGIS shape columns are s4; addresses go through the existing batch geocoder on the pulled table.
ALTER TABLE platform.sql_pulls
    ADD COLUMN geometry jsonb CHECK (geometry IS NULL OR jsonb_typeof(geometry) = 'object');
COMMENT ON COLUMN platform.sql_pulls.geometry IS
    'NULL = non-spatial table; {"kind":"xy","x":..,"y":..,"srid":..} = point layer from two coordinate columns (s2).';
