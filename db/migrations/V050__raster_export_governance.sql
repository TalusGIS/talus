-- V050 — raster export governance (decision-raster-export.md). Tier-2, backup-gated.
--
-- Two per-raster governance flags batched into one migration (base slice deferred both —
-- decision-raster-store-serve.md §10 "batch with downloadable below"):
--
--   * downloadable     — does the clip/whole-raster /export path serve this raster's pixels?
--                        Default TRUE (most user-produced rasters are freely downloadable). Set
--                        FALSE for LICENSED imagery that may be VIEWED on the map (tiles still
--                        served) but NOT downloaded — the view-but-not-download governance knob,
--                        the raster analog of an Open-Data dataset's export. /export -> 403 when off.
--
--   * delete_protected — the T24 padlock parity deferred from the base slice's c7. When TRUE,
--                        DELETE /rasters/{id} is refused (the owner must clear the flag first), so a
--                        load-bearing basemap can't be removed by a stray click. Default FALSE.
--
-- Both are plain owner/admin-toggled booleans; neither is an OpenFGA relation (download is can_view
-- + this flag; protection is an app-layer guard). No backfill needed — the NOT NULL defaults apply
-- to every existing row.

ALTER TABLE platform.rasters
    ADD COLUMN downloadable     boolean NOT NULL DEFAULT true,
    ADD COLUMN delete_protected boolean NOT NULL DEFAULT false;
