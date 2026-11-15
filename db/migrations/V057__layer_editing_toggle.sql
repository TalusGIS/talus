-- V057 — Per-layer "Enable editing" toggle (decision-editing-toggle.md). Tier-2, backup-gated.
--
-- The owner-controlled, SOFT read-only switch managed in Manage → Settings (sibling to DELETE PROTECTION +
-- TRANSFER OWNERSHIP). Distinct from platform.layers.editable, which is the HARD claimed-vs-unclaimed gate the
-- claim/release flow owns and which the admin override NEVER bypasses (so views / unclaimed slots stay
-- read-only for everyone, including admins). This new column is the opposite kind of gate: a soft owner switch
-- that an admin CAN override — so it needs its own column rather than repurposing `editable` (which would fight
-- the wanted admin override and risk un-gating views). Mirrors the delete_protected column add (V029).
--
-- DEFAULT true: every existing layer stays editable exactly as today — byte-for-byte current behavior, no
-- backfill needed. edit_allowed (api/app/authz.py) reads this AFTER the editable gate: when false, the genuine
-- `ok` path fails for everyone, but the talus-admin override still applies (reason `admin` → the existing
-- "editing is restricted but you have admin privileges" notice). All write paths (single create, bulk ingest,
-- attribute/geometry patch, delete) funnel through edit_allowed, so this one flag governs every write.

ALTER TABLE platform.layers
    ADD COLUMN editing_enabled boolean NOT NULL DEFAULT true;
