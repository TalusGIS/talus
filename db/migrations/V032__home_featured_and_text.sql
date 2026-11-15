-- V032 — Talus 30: public home page substrate (decision-home-public.md). Tier-2, backup-gated.
-- (1) A reserved, undeletable "Home (Featured)" group: maps/layers shared to it are featured on the
--     home page. (2) Admin-configurable home text on the org_settings singleton.

-- (1) Mark protected/system groups, then seed the Home group. `system=true` is the guard that blocks
--     delete/rename (no group-delete path exists today, so "undeletable" already holds; this keeps it
--     true if a delete path is ever added). The group_id 'home' is a valid slug ([a-z0-9_]+).
--     created_by is NULL (a system group, not a user's). Featuring writes the existing OpenFGA
--     shared_group tuple (item -> group:home) — no model change; the group is implicit in OpenFGA.
ALTER TABLE platform.groups ADD COLUMN system boolean NOT NULL DEFAULT false;
INSERT INTO platform.groups (group_id, display_name, system)
    VALUES ('home', 'Home (Featured)', true)
    ON CONFLICT (group_id) DO NOTHING;

-- (2) Admin-configurable home text (the singleton org_settings row, seeded V028). All NULL by default;
--     the app renders sensible built-in defaults when unset. Mirrors the banner/governance columns.
ALTER TABLE platform.org_settings
    ADD COLUMN home_heading text,
    ADD COLUMN home_intro text,
    ADD COLUMN home_hero_image_url text;
