-- V084 — basemap provisioning metadata (decision-basemap-provisioning.md §3 F5).
--
-- The portal-provisioned jurisdiction basemap: the archive itself lives in MinIO
-- (`basemaps/current.pmtiles`, F1); these columns are its LEGIBLE metadata — what the Admin →
-- Settings → Basemap section shows (bbox, build date, size, how it got here) and what the
-- System-page staleness nudge reads, with one singleton SELECT (the T43 shape). Rejected
-- alternative: sidecar JSON in MinIO (invisible to SQL, another parser).
--
-- ALL NULL = no basemap provisioned (fresh install). The columns are written together as one
-- provisioning event (fetch completes / upload promoted), never piecemeal:
--   basemap_bbox         [w, s, e, n] WGS84 — jsonb 4-array (fetch: the requested extract bbox;
--                        upload: read from the pmtiles header when present)
--   basemap_maxzoom      the extract's maxzoom (F1 default 15; CHECK mirrors the API's 1..15)
--   basemap_source_date  the Protomaps planet build date YYYYMMDD (fetch: always; upload: NULL
--                        when the archive doesn't carry it) — feeds the ~6-month staleness nudge
--   basemap_fetched_at   when THIS deployment provisioned it (fetch or upload promotion)
--   basemap_size_bytes   archive size, shown in Settings and sanity-checked against the bucket
--   basemap_origin       'fetched' (server-side go-pmtiles extract) | 'uploaded' (air-gapped walk-in)

ALTER TABLE platform.org_settings
    ADD COLUMN basemap_bbox jsonb
        CHECK (basemap_bbox IS NULL
               OR (jsonb_typeof(basemap_bbox) = 'array' AND jsonb_array_length(basemap_bbox) = 4)),
    ADD COLUMN basemap_maxzoom integer
        CHECK (basemap_maxzoom IS NULL OR basemap_maxzoom BETWEEN 1 AND 15),
    ADD COLUMN basemap_source_date text
        CHECK (basemap_source_date IS NULL OR basemap_source_date ~ '^\d{8}$'),
    ADD COLUMN basemap_fetched_at timestamptz,
    ADD COLUMN basemap_size_bytes bigint
        CHECK (basemap_size_bytes IS NULL OR basemap_size_bytes > 0),
    ADD COLUMN basemap_origin text
        CHECK (basemap_origin IN ('fetched', 'uploaded'));

COMMENT ON COLUMN platform.org_settings.basemap_fetched_at IS
  'decision-basemap-provisioning.md F5: when the current basemap archive was provisioned (fetch '
  'or upload). NULL = no basemap installed; the SPA falls back to its dev static path.';
