-- V082 — batch-geocoding job options (decision-batch-geocoding.md, country-narrow). Tier-2, backup-gated.
--
-- Async batch-geocode runs gained an optional per-run OPTIONS bag: an extensible jsonb the worker reads
-- each chunk. First key: {"country": ["ke","tz", ...]} — ISO alpha-2 codes narrowing the EXTERNAL
-- locators (ArcGIS sourceCountry / Nominatim countrycodes) to reduce false matches; NULL/absent = global
-- (the default — geocoding is worldwide out of the box, so this is an accuracy refinement, not a gate).
-- The internal own-data locator ignores it. Reserved for later external-only refinements (category,
-- preferred location type) so they don't each need their own migration.
--
-- Additive + nullable: every existing job reads as NULL (global), so no backfill.

ALTER TABLE platform.geocode_batch_jobs
    ADD COLUMN options jsonb;

COMMENT ON COLUMN platform.geocode_batch_jobs.options IS
    'Optional per-run bag the worker reads each chunk (decision-batch-geocoding.md). '
    'e.g. {"country": ["ke"]} narrows external locators; NULL = global. Internal locator ignores it.';
