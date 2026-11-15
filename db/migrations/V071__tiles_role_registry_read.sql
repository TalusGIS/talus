-- V071 — S-1a fix: pg_tileserv's SELECT-only role (talus_tiles) must READ the layer registry.
--
-- assets.slot_mvt (the one generic hosted-layer tile function, V061) validates the requested layer
-- against platform.layers before tiling. Under S-1a's talus_tiles role (USAGE on assets only) that
-- read fails "permission denied for schema platform", so every AUTHORIZED hosted-layer tile would
-- break. Caught in S-1a cutover verification. Minimal fix: USAGE on platform + SELECT on the one
-- table slot_mvt reads. (The API proxy's OpenFGA gate is still the real authz; this is just the
-- function's internal existence check.)
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'talus_tiles') THEN
    RAISE EXCEPTION 'V071: role "talus_tiles" is missing — see decision-s1 §8.1 / db/init/00-roles.sh';
  END IF;
END $$;

GRANT USAGE  ON SCHEMA   platform         TO talus_tiles;
GRANT SELECT ON TABLE    platform.layers  TO talus_tiles;
