-- V070 — least-privilege runtime DB role (S-1a). decision-s1-least-privilege-db-role.md.
--
-- Closes threat T6 / makes invariant C6 true for the API's MAIN pool: the runtime role (talus_app)
-- can do normal data DML but CANNOT delete/alter audit rows, disable audit triggers, or act as
-- superuser. The rare runtime DDL (layer provisioning, view layers, geocoder index) moves to a
-- separate owner-pool connection in the API (Option C, see decision §4/§8) — so talus_app never
-- needs CREATE. pg_tileserv drops to a SELECT-only role (talus_tiles), closing the T1 residual.
--
-- Roles are created cluster-globally by db/init/00-roles.sh (fresh clusters) or a one-time manual
-- CREATE ROLE (existing clusters — decision §8.1). This migration only wires PRIVILEGES + hardens
-- the audit trigger. Idempotent (safe to re-run): GRANT/ALTER are repeatable; the guard is read-only.
--
-- NOT in S-1a (deferred to S-1b, gated on a hosted/multi-tenant offering): removing owner creds from
-- the API entirely (DDL via SECURITY DEFINER functions), per-new-layer ENABLE ALWAYS, statement_timeout.

-- 0) Guard — fail loudly if an existing cluster skipped the one-time role creation.
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'talus_app') THEN
    RAISE EXCEPTION 'V070: role "talus_app" is missing — create the roles first (decision-s1 §8.1 / db/init/00-roles.sh)';
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'talus_tiles') THEN
    RAISE EXCEPTION 'V070: role "talus_tiles" is missing — create the roles first (decision-s1 §8.1 / db/init/00-roles.sh)';
  END IF;
END $$;

-- 1) Schema usage.
GRANT USAGE ON SCHEMA assets, platform, media, audit TO talus_app;
GRANT USAGE ON SCHEMA assets TO talus_tiles;

-- 2) Data DML for the app; audit is SELECT-ONLY (writes happen only via the SECURITY DEFINER
--    trigger in §4, which runs as the owner — talus_app gets no INSERT/UPDATE/DELETE on audit).
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA assets, platform, media TO talus_app;
GRANT SELECT                        ON ALL TABLES IN SCHEMA audit                    TO talus_app;
GRANT USAGE, SELECT                 ON ALL SEQUENCES IN SCHEMA assets, platform, media TO talus_app;
GRANT EXECUTE                       ON ALL FUNCTIONS IN SCHEMA assets, platform, media TO talus_app;

-- pg_tileserv: read the feature tables + run the tile functions (assets.slot_mvt et al.). Nothing else.
GRANT SELECT  ON ALL TABLES    IN SCHEMA assets TO talus_tiles;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA assets TO talus_tiles;

-- Note: talus_app is also granted EXECUTE on platform.provision_* — harmless. Those functions run as
-- the caller (SECURITY INVOKER) and their CREATE TABLE fails for talus_app (no CREATE on any schema),
-- so a stray call is a no-op error, not an escalation. S-1b converts them to SECURITY DEFINER + tightens.

-- 3) Future owner-created objects (e.g. a provisioned layer's table) auto-grant to the roles, so
--    provisioning code needs no change and a new layer is writable/tileable the moment it exists.
ALTER DEFAULT PRIVILEGES FOR ROLE talus IN SCHEMA assets, platform, media
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO talus_app;
ALTER DEFAULT PRIVILEGES FOR ROLE talus IN SCHEMA assets, platform, media
  GRANT USAGE, SELECT ON SEQUENCES TO talus_app;
ALTER DEFAULT PRIVILEGES FOR ROLE talus IN SCHEMA assets, platform, media
  GRANT EXECUTE ON FUNCTIONS TO talus_app;
ALTER DEFAULT PRIVILEGES FOR ROLE talus IN SCHEMA audit
  GRANT SELECT ON TABLES TO talus_app;
ALTER DEFAULT PRIVILEGES FOR ROLE talus IN SCHEMA assets
  GRANT SELECT ON TABLES TO talus_tiles;
ALTER DEFAULT PRIVILEGES FOR ROLE talus IN SCHEMA assets
  GRANT EXECUTE ON FUNCTIONS TO talus_tiles;

-- 4) Make the audit write path un-tamperable by talus_app: the generic trigger function runs as its
--    OWNER (talus) instead of the caller, so it can INSERT into audit.* while talus_app holds no
--    audit DML at all. Injection-safe: the audit-table name is a trigger arg (privileged, set at
--    CREATE TRIGGER time), values are bound via USING. search_path pinned for definer hardening.
ALTER FUNCTION audit.fn_record_change() SECURITY DEFINER;
ALTER FUNCTION audit.fn_record_change() SET search_path = pg_catalog;

-- 5) ENABLE ALWAYS on every EXISTING audit trigger so it fires even under session_replication_role
--    = 'replica' (defense-in-depth against the owner path; talus_app can't set that flag anyway).
DO $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT n.nspname AS sch, c.relname AS tbl, t.tgname AS trg
    FROM pg_trigger t
    JOIN pg_class c ON c.oid = t.tgrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE NOT t.tgisinternal AND t.tgname LIKE '%audit%'
  LOOP
    EXECUTE format('ALTER TABLE %I.%I ENABLE ALWAYS TRIGGER %I', r.sch, r.tbl, r.trg);
  END LOOP;
END $$;
