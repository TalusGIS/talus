-- V028 — platform.org_settings (Talus 22: Org Settings & governance substrate).
--
-- The first Admin → Settings store: a SINGLETON row of org-wide policy, built deliberately once
-- because many future settings reuse it (decision-org-settings.md). One org per deployment (org is
-- an OpenFGA concept — there is no platform.orgs table), so a one-row table guarded by
-- `id boolean PRIMARY KEY CHECK (id)`: exactly one row, addressed as `id = true`.
--
-- Tenants:
--   banner_*                   LIVE first tenant — the information / maintenance banner an admin
--                              sets for an org-wide notice (maintenance window, read-only mode,
--                              classified alert). READ by every signed-in user (the shell renders
--                              it); WRITTEN by an admin. banner_level styles it (info|warning|
--                              critical). An ENABLED banner must carry a non-blank message (CHECK).
--   nonadmin_can_share_public  PRE-PLACED governance policy, INERT this slice: gate non-admins from
--                              sharing to Public Access (anonymous). Default OFF (restrictive) —
--                              making content public is the dangerous, near-irreversible action.
--                              There is no Public Access sharing tier yet (deferred anon-access
--                              slice), so this is neither exposed in the API nor enforced here; the
--                              column lives here so that slice stays migration-free and inherits the
--                              safe default. (decision-org-settings.md §3 F2/F3.)
--   updated_by / updated_at    actor attribution (AP12), like platform.groups / platform.web_maps.
--                              ATTRIBUTION-ONLY — org settings are config, not a system of record,
--                              so there is deliberately NO shadow-audit table.

CREATE TABLE platform.org_settings (
    id             boolean PRIMARY KEY DEFAULT true CHECK (id),

    banner_enabled boolean NOT NULL DEFAULT false,
    banner_message text,
    banner_level   text NOT NULL DEFAULT 'info'
                   CHECK (banner_level IN ('info', 'warning', 'critical')),

    nonadmin_can_share_public boolean NOT NULL DEFAULT false,

    updated_by     uuid REFERENCES platform.users(id),
    updated_at     timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT org_settings_banner_message_when_enabled_chk
        CHECK (NOT banner_enabled OR length(btrim(coalesce(banner_message, ''))) > 0)
);

-- Seed the single row so the read path is never empty (PUT is an UPDATE of id = true, never an
-- INSERT). All defaults: banner off, public-sharing restricted.
INSERT INTO platform.org_settings (id) VALUES (true);
