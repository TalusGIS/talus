-- V111 — SQL-source connections: a connection NAME is unique per OWNER, not org-wide
-- (decision-sql-source-pull.md §10, security review 2026-10-01, secondary observation). Tier-2, backup-gated.
--
-- V110 made `name` UNIQUE across the table while connections were admin-only. F11 was amended the same day
-- to per-user, publisher-gated rows whose existence is never disclosed to another user — but an org-wide
-- UNIQUE turned a duplicate name into a 409 that told publisher B that someone already has a connection by
-- that name. Names are the owner's own vocabulary; scope the constraint to (created_by, name).
ALTER TABLE platform.sql_connections DROP CONSTRAINT sql_connections_name_key;
ALTER TABLE platform.sql_connections ADD CONSTRAINT sql_connections_owner_name_key UNIQUE (created_by, name);
