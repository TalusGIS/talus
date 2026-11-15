-- V034 — Talus 33: Open Data portal (decision-open-data.md). Tier-2, backup-gated.
-- Seed the reserved "Open Data" system group: a layer curated INTO it is a published open-data
-- dataset (the public catalog = items shared to group:open_data, filtered to what the caller may
-- see). Mirrors V032's 'home' group exactly — same mechanism, second reserved group.
--
-- `system=true` (column added in V032) is the guard that blocks delete/rename; no group-delete path
-- exists today, so "undeletable" already holds. created_by is NULL (a system group, not a user's).
-- Membership = "may curate datasets into Open Data," managed in Admin -> Users (the generic group
-- editor lists platform.groups, so 'open_data' appears automatically; admins always may curate).
-- Curation writes the existing OpenFGA shared_group tuple (layer -> group:open_data) — NO model
-- change; the group is implicit in OpenFGA, and public_shared is already modeled on layers.
INSERT INTO platform.groups (group_id, display_name, system)
    VALUES ('open_data', 'Open Data', true)
    ON CONFLICT (group_id) DO NOTHING;
