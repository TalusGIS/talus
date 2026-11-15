-- V099 — admit 'csv' to the import-jobs format check.
--
-- V089 froze the v1 format set into a CHECK; the CSV slice adds the fourth format. Found the
-- honest way: staging a CSV job 500'd on this constraint AFTER layer provisioning had already
-- committed — the DB fence did its job, the app's format list and the DB's must move together.
-- (The endpoint still validates against svc.FORMATS first; this constraint stays the backstop.)

ALTER TABLE platform.import_jobs
    DROP CONSTRAINT import_jobs_format_check;
ALTER TABLE platform.import_jobs
    ADD CONSTRAINT import_jobs_format_check
    CHECK (format IN ('shp-zip', 'gpkg', 'geojson', 'csv'));
