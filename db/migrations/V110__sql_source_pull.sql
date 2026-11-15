-- V110 — SQL-source pull, slice s1 (decision-sql-source-pull.md): read-only copies of tables that
-- other systems own, refreshed from the source. Tier-2, backup-gated.
--
-- Three tables, all admin-only through the api (every door is _require_admin):
--   sql_connections — WHERE a pull points (host:port/database, login). The password rests as Fernet
--                     ciphertext (app/secrets.py), is write-only through the api and never logged.
--                     The connection row IS the allow-list (F11): the host is vetted by app/netguard
--                     at create, at test and at every run, with a permanent never-list of this
--                     deployment's own services.
--   sql_pulls       — ONE source table or view + an admin-chosen column list → ONE hosted non-spatial
--                     table (V059, geom_type='none'), owned by the admin who made the pull, shared with
--                     no one, editing LOCKED while attached (F3: V057 + V109 flags off). `columns` is
--                     the plan: [{name, sourceType, dataType, numericSubtype, temporal, utc, fieldKey}].
--                     `key_column` (F10) is recorded now; the keyed diff is s3 — s1 replaces whole.
--   sql_pull_runs   — every run (trigger, who, when, counts, refusals, per-column non-null stats, the
--                     failure line). s1 trigger is always 'manual'; s3 adds the schedule.
--
-- Dialect CHECK names all four of the roster (§1 dialect roster); the api refuses the ones this
-- release does not ship (422 dialect_unavailable), so the schema needs no change per dialect slice.

CREATE TABLE platform.sql_connections (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name                text NOT NULL UNIQUE CHECK (length(name) BETWEEN 1 AND 80),
    dialect             text NOT NULL CHECK (dialect IN ('postgresql', 'sqlserver', 'mysql', 'oracle')),
    host                text NOT NULL CHECK (length(host) BETWEEN 1 AND 253),
    port                integer NOT NULL CHECK (port BETWEEN 1 AND 65535),
    database            text NOT NULL CHECK (length(database) BETWEEN 1 AND 128),
    username            text NOT NULL CHECK (length(username) BETWEEN 1 AND 128),
    password_ciphertext bytea,                      -- Fernet; NULL = no password stored (write-only via the api)
    tls_mode            text NOT NULL DEFAULT 'verify-full'
                          CHECK (tls_mode IN ('verify-full', 'require', 'disable')),
    ca_pem              text CHECK (ca_pem IS NULL OR length(ca_pem) <= 65536),   -- the organization's own CA bundle (PEM)
    timezone            text NOT NULL DEFAULT 'UTC' CHECK (length(timezone) BETWEEN 1 AND 64),   -- naive timestamps (F9)
    created_by          uuid NOT NULL REFERENCES platform.users(id),
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_by          uuid REFERENCES platform.users(id),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    last_tested_at      timestamptz,
    last_test_ok        boolean,
    last_test_error     text CHECK (last_test_error IS NULL OR length(last_test_error) <= 300)
);
COMMENT ON TABLE platform.sql_connections IS
    'SQL-source pull connections (decision-sql-source-pull.md s1). Admin-only; password is Fernet ciphertext, write-only.';

CREATE TABLE platform.sql_pulls (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    connection_id    uuid NOT NULL REFERENCES platform.sql_connections(id) ON DELETE CASCADE,
    target_layer_id  text NOT NULL UNIQUE REFERENCES platform.layers(layer_id) ON DELETE CASCADE,
    source_schema    text NOT NULL CHECK (length(source_schema) BETWEEN 1 AND 128),
    source_table     text NOT NULL CHECK (length(source_table) BETWEEN 1 AND 128),
    columns          jsonb NOT NULL CHECK (jsonb_typeof(columns) = 'array'),
    key_column       text CHECK (key_column IS NULL OR length(key_column) BETWEEN 1 AND 128),
    created_by       uuid NOT NULL REFERENCES platform.users(id),
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX sql_pulls_connection_idx ON platform.sql_pulls (connection_id);
COMMENT ON TABLE platform.sql_pulls IS
    'One source table/view + column list → one hosted non-spatial table (locked while attached). s1: replace-whole runs; s3: keyed diff + schedule.';

CREATE TABLE platform.sql_pull_runs (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    pull_id         uuid NOT NULL REFERENCES platform.sql_pulls(id) ON DELETE CASCADE,
    status          text NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'running', 'completed', 'failed', 'canceled')),
    trigger         text NOT NULL DEFAULT 'manual' CHECK (trigger IN ('manual', 'schedule')),
    total_rows      integer,
    processed_rows  integer NOT NULL DEFAULT 0,
    inserted_rows   integer NOT NULL DEFAULT 0,
    failed_rows     integer NOT NULL DEFAULT 0,
    error_sample    jsonb NOT NULL DEFAULT '[]'::jsonb,
    error           text CHECK (error IS NULL OR length(error) <= 300),
    column_stats    jsonb,                           -- {column: {source: n, target: n}} — the attribute-loss guard's evidence
    created_by      uuid NOT NULL REFERENCES platform.users(id),
    created_at      timestamptz NOT NULL DEFAULT now(),
    started_at      timestamptz,
    finished_at     timestamptz,
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX sql_pull_runs_queue_idx ON platform.sql_pull_runs (status, created_at);
CREATE INDEX sql_pull_runs_pull_idx ON platform.sql_pull_runs (pull_id, created_at DESC);
COMMENT ON TABLE platform.sql_pull_runs IS
    'Every SQL-pull run: trigger, actor, counts, refusals, per-column non-null stats, failure line. One transaction per run.';
