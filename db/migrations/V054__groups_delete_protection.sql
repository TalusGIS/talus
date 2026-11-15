-- V054 — Group delete protection (decision-groups-ui.md follow-up). Tier-2, backup-gated.
--
-- Mirrors the layer/map/raster `delete_protected` padlock (V029/V050) on groups — BUT with an
-- intentionally STRONGER rule: protection is **absolute**. A protected group cannot
-- be deleted by ANYONE — owner OR admin — until protection is turned off (AGO-faithful; the guard is
-- against human error, including an admin's). The two-step (untick → delete) is the whole point.
--   (This is OPTION B. The existing layer/map/raster paths still let an admin override their protection;
--    bringing those in line with this absolute rule is a tracked follow-up — roadmap-backlog.)
--
-- Toggled (owner or admin) as a checkbox in the group's Edit-details form → folded into PATCH /groups/{id}
-- alongside displayName/summary (not the catalog's separate /delete-protection endpoint). System groups
-- (home/open_data) are already undeletable and uneditable, so the flag is inert for them.
--
-- Trivial additive ALTER (no OpenFGA / .env change ⇒ no api force-recreate; pg_dump + flyway migrate only).

ALTER TABLE platform.groups
    ADD COLUMN delete_protected boolean NOT NULL DEFAULT false;
