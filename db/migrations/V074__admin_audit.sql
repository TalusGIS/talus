-- V074 — platform.admin_audit: persisted governance-action log.
-- docs/decision-admin-reports-round2.md §4.1. Tier-2, backup-gated.
--
-- The Activity report (T29 carried-forward) needs governance actions (role change, share/unshare,
-- ownership transfer, disable/enable user, public-curate, delete-protection toggle, backup-settings
-- change) in a queryable table. These are API-level actions spanning Keycloak/OpenFGA and are INVISIBLE
-- to PG triggers, so the existing audit.* shadow-table machinery (row triggers on feature tables)
-- cannot capture them — the write path is explicit app-layer INSERTs at each handler (api/app/
-- admin_audit.py), best-effort, at the current structlog site (§F6).
--
-- Home = platform (NOT the audit schema): under S-1 (V070) the runtime role talus_app has SELECT-only
-- on schema audit but full write on platform, so an audit-schema home would re-open the write hole S-1
-- closed. Default privileges cover platform → no GRANT needed here.
--
-- GOVERNANCE-WIDE (§F1): records the action WHOEVER performs it (an owner over-sharing their own layer
-- is exactly what the report must surface), not only admin-role actors — hence actor_id is any
-- principal and there is no admin-only filter. NEVER auto-deleted (§F4, inherits the T39/T40 audit
-- fence — not wired into any housekeeping job). No FK on actor_id (§F5): an append-only log outlives
-- the principals it references; the name is resolved at read time via a platform.users join.

CREATE TABLE platform.admin_audit (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at          timestamptz NOT NULL DEFAULT now(),
    actor_id    uuid        NOT NULL,
    actor_type  text        NOT NULL DEFAULT 'human'
                  CHECK (actor_type IN ('human', 'service_account', 'ai_inference')),
    action      text        NOT NULL,
    target_type text,
    target_id   text,
    detail      jsonb       NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX admin_audit_at_idx ON platform.admin_audit (at DESC);
CREATE INDEX admin_audit_target_idx ON platform.admin_audit (target_type, target_id);

COMMENT ON TABLE platform.admin_audit IS
    'Persisted governance-action log powering the Admin -> Reports Activity report. App-layer INSERTs '
    'only (API actions span Keycloak/OpenFGA, invisible to PG triggers). Governance-WIDE: the actor is '
    'whoever performed the action, not only admins. Append-only; never auto-deleted. '
    'See docs/decision-admin-reports-round2.md.';
COMMENT ON COLUMN platform.admin_audit.actor_id IS
    'Principal that performed the action (human or service_account). No FK — an append-only audit log '
    'outlives the principals it references; the display name is resolved at read time.';
COMMENT ON COLUMN platform.admin_audit.actor_type IS
    'human | service_account | ai_inference — the vocabulary established by V012/V025/V065.';
COMMENT ON COLUMN platform.admin_audit.action IS
    'Action verb, mirroring the existing structlog event name (e.g. user_role_changed, layer_shares_set, '
    'object_owner_transferred, open_data_curated, layer_delete_protection_set, backup_settings_updated).';
COMMENT ON COLUMN platform.admin_audit.target_type IS
    'layer | web_map | raster | user | external_ref | backup_settings | ... — NULL for actions with no '
    'discrete target.';
COMMENT ON COLUMN platform.admin_audit.detail IS
    'Action-specific context (old/new role, groups, public flag, prev/new owner, protected bool, '
    'settings-change summary). Shapes/counts/flags ONLY — NEVER secret values (tokens, keys, addresses).';
