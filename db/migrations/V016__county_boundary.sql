-- V016 — assets.county_boundary (Layer 7c, the first polygon asset) + shadow audit.
--
-- Schema only — NO row data. Rows are loaded separately by the verified GDAL loader
-- scripts/load-county-boundary.sh (the "A+" ingest pattern: schema in Flyway,
-- row data through a checked loader, never inline INSERTs). The reference load used a
-- county boundary shapefile (a projected CRS in US survey feet, POLYGON, single-part features),
-- reprojected to the 4326 storage SRID and promoted to MULTIPOLYGON on load.
--
-- This is a REFERENCE layer, so it deliberately omits the created_by/updated_by/
-- ai_provenance edit-tracking columns that assets.signs (V005) and
-- assets.street_lights (V013) carry: those are NOT NULL FKs to platform.users and a
-- shapefile bulk-load has no authoring user. The Talus PK is a server-minted uuid
-- `id` (the point-table convention) — NOT the ESRI objectid, which is preserved as a
-- reference-only column. The ArcGIS GlobalID is preserved verbatim in source_globalid.
--
-- Audit is wired EXACTLY as V013 did for street_lights: a 9-column shadow table and
-- the asset-agnostic audit.fn_record_change() trigger (no function change). To turn
-- audit off for a customer: DROP TRIGGER county_boundary_audit_trigger ON
-- assets.county_boundary;

-- 1) The asset table — uuid id + 4326 geometry + cleaned source columns.
--    Clean names <- truncated DBF source (see the loader's field rename). ESRI-derived
--    columns (source_objectid, shape_leng, shape_area) are kept as non-editable
--    reference. source_globalid is the ArcGIS GlobalID, preserved verbatim on load.
CREATE TABLE assets.county_boundary (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    geom                geometry(MultiPolygon, 4326) NOT NULL,

    source_objectid     integer,   -- ESRI OID (DBF objectid), reference only
    boundary_class      text,      -- DBF boundarycl (MAIN/ISLD)
    feature_name        text,      -- DBF featurenam (may be empty)
    acreage             numeric,   -- DBF acreage
    municipality        text,      -- DBF municipali
    source_last_update  date,      -- DBF lastupdate
    source_creator      text,      -- DBF creator
    source_create_date  date,      -- DBF createdate
    source_globalid     text,      -- DBF globalid — ArcGIS GlobalID, verbatim
    source_last_editor  text,      -- DBF lasteditor
    shape_leng          numeric,   -- DBF shape_Leng — ESRI-derived, reference only
    shape_area          numeric    -- DBF shape_Area — ESRI-derived, reference only
);

CREATE INDEX county_boundary_geom_idx ON assets.county_boundary USING GIST (geom);

-- 2) Shadow audit table — the same nine columns audit.fn_record_change() writes
--    (post-V012 shape, declared inline). Mirrors audit.street_lights_audit.
CREATE TABLE audit.county_boundary_audit (
    audit_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    asset_id     uuid NOT NULL,
    operation    text NOT NULL CHECK (operation IN ('INSERT','UPDATE','DELETE')),
    changed_at   timestamptz NOT NULL DEFAULT now(),
    changed_by   uuid REFERENCES platform.users(id),
    actor_type   text NOT NULL DEFAULT 'human' CHECK (actor_type IN ('human','ai_inference')),
    provenance   jsonb,
    client_info  jsonb,
    before_row   jsonb,
    after_row    jsonb
);

CREATE INDEX county_boundary_audit_asset_idx   ON audit.county_boundary_audit (asset_id);
CREATE INDEX county_boundary_audit_changed_idx ON audit.county_boundary_audit (changed_at);

CREATE TRIGGER county_boundary_audit_trigger
AFTER INSERT OR UPDATE OR DELETE ON assets.county_boundary
FOR EACH ROW EXECUTE FUNCTION audit.fn_record_change('audit.county_boundary_audit', 'id');
