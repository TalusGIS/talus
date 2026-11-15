-- V053 — Group metadata for the Groups management UI (decision-groups-ui.md). Tier-2, backup-gated.
--
-- The Groups UI (browse / create / membership / shared-content) needs a little more on the group than the
-- thin V026 registry carried (group_id, display_name, created_by, created_at). This adds:
--   summary      a short human description shown on the group card + detail page (AGO "Summary"). Nullable.
--   updated_at   provenance for the new PATCH /groups/{id} (display_name / summary edits).
--   updated_by   who last edited the group's metadata; ON DELETE SET NULL like created_by, so a removed
--                user never blocks a group row (the group's ACCESS lives in OpenFGA, not here).
--
-- Deliberately NOT added (decision doc §8): NO slug / group_id mutation — group_id is IMMUTABLE because
-- every group:<slug> tuple (membership + the new owner relation + every item's shared_group) references it;
-- NO visibility-tier column (deferred with featured groups); NO thumbnail. OpenFGA stays the authority on
-- membership + access (now incl. the `owner` relation added to db/openfga/model.json in this same slice);
-- this table stays thin naming/metadata with no audit shadow, consistent with V026.

ALTER TABLE platform.groups
    ADD COLUMN summary     text,
    ADD COLUMN updated_at  timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN updated_by  uuid REFERENCES platform.users(id) ON DELETE SET NULL;
