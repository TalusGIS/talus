-- V109 — Per-layer edit capabilities: add · attributes · geometry · delete
-- (decision-layer-edit-capabilities.md §3.1). Tier-2, backup-gated.
--
-- Four owner-set flags BESIDE the master switch (V057 editing_enabled): what anyone with edit access may do
-- on this layer. Esri exposes the same granularity (Create/Update/Delete capabilities + allowGeometryUpdates)
-- and municipal crews are handed layers under exactly this policy — "update the sign's condition, never move
-- a surveyed asset, never delete". Enforced in authz.edit_allowed(op) AFTER the master switch and BEFORE the
-- role/access checks; NOT admin-overridable (F1: policy, like `editable` — the admin changes the flag on the
-- settings screen if they mean it). Every write door names its op; the Esri façade names one per edit array.
--
-- DEFAULT true on all four: every existing layer behaves byte-for-byte as today, no backfill. Mirrors V057.
--   edit_add        — create a feature; bulk-import into the layer (a new feature always carries its
--                     geometry — F5: edit_geometry governs MOVING existing features only)
--   edit_attributes — change props / notes; the calc door; attachments (a photo is an attribute — F2)
--   edit_geometry   — move a point / reshape a line or polygon; batch-geocode rematch
--   edit_delete     — delete a feature (delete_protected, V029, protects the LAYER, not its features)

ALTER TABLE platform.layers
    ADD COLUMN edit_add        boolean NOT NULL DEFAULT true,
    ADD COLUMN edit_attributes boolean NOT NULL DEFAULT true,
    ADD COLUMN edit_geometry   boolean NOT NULL DEFAULT true,
    ADD COLUMN edit_delete     boolean NOT NULL DEFAULT true;
