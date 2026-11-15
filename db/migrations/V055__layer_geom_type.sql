-- V055 — Per-layer geometry type (decision-line-polygon-layers.md, c1). Tier-2, backup-gated.
--
-- Today every publisher slot is pre-minted geometry(Point,4326) (V019) and the claim path issues no DDL, so
-- users can only make POINT layers. This slice makes Line and Polygon first-class. The geometry type is
-- currently implied ONLY by the physical column (geometry_columns / ST_GeometryType) — there is no place that
-- RECORDS what kind of layer a slot was claimed as. This migration adds that authoritative record so the API,
-- catalog, renderer, and map all agree on a layer's geometry without per-request introspection (mirrors the
-- working_srid / native_storage siblings added in V051 / V052).
--
-- FRIENDLY ENUM, not the physical type. geom_type is one of 'point' | 'line' | 'polygon'. Storage for the two
-- new families is MULTI* (MultiLineString / MultiPolygon — the safe default matching Shapefile and most
-- municipal sources; single-part input is wrapped via ST_Multi). The enum hides that Multi* detail: e.g.
-- assets.county_boundary physically is MULTIPOLYGON but reports geom_type = 'polygon'. Point stays plain Point.
--
-- This migration only RECORDS the choice + backfills existing rows. The physical effect for a NON-POINT claim
-- — a bounded, one-time
--     ALTER TABLE assets.<tok> ALTER COLUMN geom TYPE geometry(MultiLineString|MultiPolygon,<srid>)
--         USING ST_Multi(ST_SetSRID(geom,<srid>))
-- on the freshly-claimed EMPTY slot + a tile-function envelope redef — is applied by the claim path in
-- publisher.py (the generalization of V052's _set_slot_storage_srid; the 3rd scoped-runtime-DDL case, fenced
-- exactly like T38 views + V052 native: single-object, type from a 3-value frozen allow-list, FK-validated
-- SRID, opt-in-by-geometry-choice, empty table). The DEFAULT (point/4326) path is byte-for-byte today's
-- architecture: pool slots are still born geometry(Point,4326), claiming a point layer issues no DDL, and the
-- 80% are completely unaffected. Release (publisher.py _release_slot) resets geom_type back to 'point' AND
-- ALTERs the geom column back to geometry(Point,4326) so a freed slot returns to the uniform pool clean.

ALTER TABLE platform.layers
    ADD COLUMN geom_type text NOT NULL DEFAULT 'point';

ALTER TABLE platform.layers
    ADD CONSTRAINT layers_geom_type_valid CHECK (geom_type IN ('point', 'line', 'polygon'));

-- Backfill: the DEFAULT sets every existing row to 'point', which is correct for the 5 slots (all
-- geometry(Point)) + signs + street_lights. The ONE existing non-point layer is county_boundary
-- (assets.county_boundary = MULTIPOLYGON), whose registry row must agree so the API reports it as a polygon.
UPDATE platform.layers SET geom_type = 'polygon' WHERE layer_id = 'county_boundary';
