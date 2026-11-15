-- V041 — Talus 42: full-system DR completeness — track the identity + authz dump artifacts.
-- docs/decision-dr-completeness.md §4. Tier-2, backup-gated (HARD RULE #1).
--
-- T40 backed up only the `talusgis` database (+ media). But Talus is a THREE-key system — Postgres (data)
-- + Keycloak (identity, its own DB) + OpenFGA (authorization, its own DB) — so a talusgis-only restore loses
-- every login and the entire sharing graph. The backup job now dumps all three databases; this migration
-- extends the per-backup catalog so one row records the COMPLETE set (the keycloak + openfga dump artifacts
-- alongside the existing db_* talusgis columns).
--
-- Nullable + no backfill: pre-T42 rows simply have NULL keycloak_/openfga_ keys (= "DB-only era" backups).
-- An empty secondary DSN (a deployment running identity/authz externally) also leaves these NULL — an
-- honest "talusgis + media only" backup, not a silent partial.

ALTER TABLE platform.backups
    ADD COLUMN keycloak_object_key  text,                       -- pg_dump artifact key for the Keycloak DB (NULL = not captured)
    ADD COLUMN keycloak_size_bytes  bigint NOT NULL DEFAULT 0,
    ADD COLUMN keycloak_sha256      text,
    ADD COLUMN openfga_object_key   text,                       -- pg_dump artifact key for the OpenFGA DB (NULL = not captured)
    ADD COLUMN openfga_size_bytes   bigint NOT NULL DEFAULT 0,
    ADD COLUMN openfga_sha256       text;

COMMENT ON COLUMN platform.backups.keycloak_object_key IS
    'Talus 42: Keycloak (identity) pg_dump artifact key in the backup target; NULL if not captured.';
COMMENT ON COLUMN platform.backups.openfga_object_key IS
    'Talus 42: OpenFGA (authorization) pg_dump artifact key in the backup target; NULL if not captured.';
