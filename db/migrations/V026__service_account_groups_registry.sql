-- V026 — platform.groups registry (Talus 19, commit 4).
--
-- The c4 admin flow lets an admin scope a service account by GROUP MEMBERSHIP (the AGO
-- model, decision-19 Q3/Q5): the SA is added as `member` of one or more groups that are
-- `shared_group` to layers. To PICK a group, the admin UI needs to ENUMERATE the groups —
-- but OpenFGA is relationship storage, not a catalog: it cannot list all `group:` objects
-- (they exist only as seeded/created tuples, with no registry to read). decision-19 Q2(a).
--
-- So this registry is the small, durable seam that makes groups enumerable + nameable:
--   group_id      the slug == the OpenFGA object's local id (the api writes `group:<group_id>`
--                 tuples). Constrained to the same [a-z0-9_] shape publisher.py._slugify
--                 produces, so a registry row can ALWAYS name a valid OpenFGA object.
--   display_name  human label for the picker (the OpenFGA object id is not user-facing).
--   created_by    the publisher/admin who created it via POST /groups; NULL for groups that
--                 predate the registry (seeded below) or are created out-of-band (seed
--                 scripts). Nullable + ON DELETE SET NULL so a removed user never blocks a
--                 group row (the group's ACCESS lives in OpenFGA, not here).
--   created_at    provenance only.
--
-- Deliberately NOT a full audited table (no V003/V020-style shadow trigger): a group
-- registry is low-churn naming metadata, not feature data — keeping it thin per the c4
-- North-Star guard (no heavy identity console). OpenFGA remains the authority on who is in a
-- group and what a group can reach; this table only makes groups LISTABLE + LABELLED.
--
-- Reused by: the SA-admin group picker (GET /groups, c4); publisher.create_group, which now
-- self-registers each group it mints; and the future human-sharing / user-admin slices, which
-- want exactly this enumerable group list. (decision-19 retrofit note.)

CREATE TABLE platform.groups (
    group_id      text PRIMARY KEY,
    display_name  text NOT NULL,
    created_by    uuid REFERENCES platform.users(id) ON DELETE SET NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),

    -- The id must be a valid OpenFGA object slug (same shape publisher._slugify emits),
    -- because the api builds `group:<group_id>` tuples from it. Belt-and-suspenders on an
    -- id that is otherwise only ever written by slug-scrubbing code.
    CONSTRAINT groups_group_id_slug_chk CHECK (group_id ~ '^[a-z0-9_]+$'),
    CONSTRAINT groups_display_name_not_blank_chk CHECK (length(btrim(display_name)) > 0)
);

-- Backfill the groups seeded into OpenFGA before the registry existed (scripts/seed-7b-tuples.sh:
-- group:signs_team, group:street_lights_team). created_by NULL = system-seeded. This makes the
-- demo SA's grant (grant-19-demo-sa.sh adds it to street_lights_team) pickable in the new UI.
INSERT INTO platform.groups (group_id, display_name, created_by) VALUES
    ('signs_team',         'Signs Team',         NULL),
    ('street_lights_team', 'Street Lights Team', NULL)
ON CONFLICT (group_id) DO NOTHING;
