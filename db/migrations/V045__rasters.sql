-- V045 — platform.rasters (raster store & serve, decision-raster-store-serve.md). Tier-2, backup-gated.
--
-- A user-produced raster (analysis output OR ortho/IR basemap) uploaded as a Cloud-Optimized
-- GeoTIFF (COG), stored OUT-DB in MinIO, served dynamically by rio-tiler embedded in the api.
-- This table is the catalog item + the pointer to the COG + its render config. The pixels live
-- in MinIO (bucket/object_key); Postgres stays lean (NO postgis_raster — extent/bands are read
-- from the COG via GDAL at ingest and recorded here as plain columns).
--
-- Mirrors platform.web_maps + T26 ownership (it is an owned, transferable, OpenFGA-gated item):
--   * id            app-generated uuid; create is OpenFGA-FIRST (uuid -> owner tuple on
--                   raster:<id> -> insert row), so a mid-failure leaves a harmless orphan tuple,
--                   not an unopenable orphan row (cf. routers/web_maps.py).
--   * created_by    IMMUTABLE creator / audit anchor — never rewritten on transfer.
--   * owner_id      MUTABLE owner (T26 transfer): updated AND the OpenFGA `owner` tuple moved
--                   together. The "Owned" catalog list keys on owner_id (pure DB query, resilient
--                   to an OpenFGA blip). Backfill-equivalent at create: owner_id = created_by.
--   * sharing       NO columns here — org/group/public shares are OpenFGA tuples only (as for
--                   layer / web_map). Attribution-only; no shadow-audit table (a raster is
--                   owner-edited content; edit = re-upload/delete, not row-level editing).
--
-- Ingest lifecycle (routers/rasters.py, c5): POST /rasters inserts status='pending' and returns a
-- presigned MinIO multipart upload target (bucket/object_key are known at create). The browser
-- uploads the COG direct to MinIO. POST /rasters/{id}/finalize validates it is a real COG, extracts
-- srid/extent/bands/dtype/dims/size, generates a thumbnail, sets default_render, and flips
-- status='ready'. Bad/non-COG/oversize input -> status='failed' (fail-closed), object purgeable.
-- The status_metadata_chk below ENFORCES that a 'ready' raster has its core metadata populated.

CREATE TABLE platform.rasters (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    title          text NOT NULL,
    description    text,

    -- ingest lifecycle
    status         text NOT NULL DEFAULT 'pending',          -- pending | ready | failed

    -- MinIO location of the COG (known at create; bucket is the dedicated rasters bucket)
    bucket         text NOT NULL,
    object_key     text NOT NULL,
    thumb_key      text,                                     -- set at finalize

    -- COG metadata, extracted via GDAL at finalize (NULL until 'ready')
    srid           int,
    extent         geometry(Polygon, 4326),                  -- plain PostGIS (postgis already enabled for vectors)
    band_count     int,
    dtype          text,
    width          int,
    height         int,
    pixel_size     double precision,                         -- native resolution, map units
    size_bytes     bigint,

    -- render config consumed by the serve path + web-map operational layer:
    --   { "bidx": [..], "rescale": [[min,max], ..], "colormap": "<name>", "opacity": 1.0 }
    default_render jsonb NOT NULL DEFAULT '{}'::jsonb,

    -- ownership / attribution (mirrors web_maps + T26)
    created_by     uuid NOT NULL REFERENCES platform.users(id),
    owner_id       uuid NOT NULL REFERENCES platform.users(id),
    updated_by     uuid NOT NULL REFERENCES platform.users(id),
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT rasters_title_not_blank_chk CHECK (length(btrim(title)) > 0),
    CONSTRAINT rasters_status_chk CHECK (status IN ('pending', 'ready', 'failed')),
    -- a 'ready' raster MUST carry its core metadata (fail-closed integrity)
    CONSTRAINT rasters_ready_metadata_chk CHECK (
        status <> 'ready'
        OR (srid IS NOT NULL AND extent IS NOT NULL AND band_count IS NOT NULL)
    )
);

-- "Owned" lists by owner_id; created_by index mirrors web_maps' creator index.
CREATE INDEX rasters_owner_id_idx ON platform.rasters (owner_id);
CREATE INDEX rasters_created_by_idx ON platform.rasters (created_by);
