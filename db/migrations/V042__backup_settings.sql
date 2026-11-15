-- V042 — Talus 43: editable backup settings (self-service schedule / retention / encryption).
-- docs/decision-backup-settings.md §3. Tier-2, backup-gated (HARD RULE #1).
--
-- T40/40.1/42 made the backup knobs (cadence, retention, encryption recipients, scheduled on/off) env-only
-- (BACKUP_* / MAINTENANCE_*) — tuning them meant editing `.env` and recreating the api. This slice makes the
-- editable subset admin-editable from Admin → Backups, effective with NO restart.
--
-- F-storage (decision §2): a DEDICATED SINGLETON, NOT extra platform.org_settings columns — this is
-- operational scheduler config read by the scheduler + the admin Backups endpoint only, not governance/
-- branding read by every signed-in user. Same one-row pattern as org_settings (`id boolean PK CHECK (id)`,
-- addressed as `id = true`), kept focused.
--
-- F-precedence (decision §2): NULLABLE OVERRIDES, COALESCE WITH ENV. Each editable column is nullable; the
-- effective value = COALESCE(db_override, env_default), computed in app.backup_settings.get_effective_backup_
-- settings(). So env stays the live deploy default, the UI optionally overrides a knob, and "reset to default"
-- = clear the column (falls back to env). We seed ONE all-NULL row (= "use env for everything") — no seed
-- machinery, env never goes silently inert.
--
-- HARD FENCE (decision §5): retention still prunes OLD BACKUPS ONLY, never below the floor — and the floor
-- itself can never be set to 0 from the UI either (CHECK retention_min_keep >= 1). Restore stays runbook-only.
-- Recipients are PUBLIC `age` keys; the api still cannot decrypt its own backups.

CREATE TABLE platform.backup_settings (
    id                 boolean PRIMARY KEY DEFAULT true CHECK (id),

    -- All editable knobs are NULLABLE: NULL = "use the env default" (COALESCE in the effective-settings reader).
    interval_seconds   integer CHECK (interval_seconds IS NULL OR interval_seconds > 0),       -- scheduled-backup cadence
    retention_days     integer CHECK (retention_days IS NULL OR retention_days >= 0),           -- prune dumps older than N days
    retention_min_keep integer CHECK (retention_min_keep IS NULL OR retention_min_keep >= 1),   -- the keep-at-least floor (never 0)
    age_recipients     text,                                                                    -- comma-joined `age1…` PUBLIC keys; NULL/blank = use env
    scheduled_enabled  boolean,                                                                 -- pause scheduled runs (manual "Back up now" still works)

    -- Actor attribution (AP12) — like org_settings / platform.groups. Config, not a system of record, so
    -- there is deliberately NO shadow-audit table (a structlog `backup_settings_updated` line records writes).
    updated_by         uuid REFERENCES platform.users(id),
    updated_at         timestamptz NOT NULL DEFAULT now()
);

-- Seed the single all-NULL row so the read path is never empty (PUT is an UPDATE of id = true, never an
-- INSERT). All-NULL = "use env for every knob" — env stays the live default until an admin overrides one.
INSERT INTO platform.backup_settings (id) VALUES (true);

COMMENT ON TABLE platform.backup_settings IS
    'Talus 43: SINGLETON admin-editable backup knobs (cadence / retention / age recipients / scheduled on-off). '
    'NULL column = use the BACKUP_*/MAINTENANCE_* env default (COALESCE precedence). See docs/decision-backup-settings.md.';
