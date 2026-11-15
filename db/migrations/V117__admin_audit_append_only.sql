-- V117 — the governance log is append-only for the role that serves requests.
--
-- platform.admin_audit (V074) records governance actions: sharing, role and ownership changes,
-- disabling a user, backup settings and the like. The application writes it with plain INSERTs.
-- It lives in the platform schema, where V070 grants the request role (talus_app) INSERT, UPDATE
-- and DELETE on every table, so that role could also rewrite or erase the log. Nothing in the
-- application updates or deletes a row here, and the table's own comment calls it append-only.
-- This makes that structural: talus_app keeps SELECT and INSERT and loses UPDATE, DELETE and
-- TRUNCATE.
--
-- The owner connection is unaffected (it owns the table), exactly as with the audit schema.
-- Proven by scripts/verify-invariants/inv06-audit-role.sh, which continuous integration runs on a
-- real database. Tier-2, backup-gated.

REVOKE UPDATE, DELETE, TRUNCATE ON platform.admin_audit FROM talus_app;
