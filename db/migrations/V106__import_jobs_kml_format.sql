-- V106 — admit 'kml' to the import-jobs format check.
--
-- The fifth format on the 9d door: KML and KMZ, the residue of years of Google Earth desktop use,
-- whose discontinuation (September 2026) is the migration trigger. Same rule as V099: the app's
-- format list (import_jobs_service.FORMATS) and this CHECK move together in one commit; the endpoint
-- validates against the app list first and this constraint stays the backstop.
--
-- One format id covers both spellings: a .kmz is a zip whose first .kml member is the document,
-- detected by magic bytes at sniff time, never by the upload's extension.

ALTER TABLE platform.import_jobs
    DROP CONSTRAINT import_jobs_format_check;
ALTER TABLE platform.import_jobs
    ADD CONSTRAINT import_jobs_format_check
    CHECK (format IN ('shp-zip', 'gpkg', 'geojson', 'csv', 'kml'));
