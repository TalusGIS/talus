-- V029 — Talus 24: catalog management (decision-catalog-mgmt.md). Tier-2, backup-gated.
-- (1) Generalize favorites to ANY catalog item type; (2) per-item delete protection.

-- (1) Generalize platform.user_favorites (was layer-only, V023) to any item type.
--     The LAYERS fk + ON DELETE CASCADE is dropped so favorites are polymorphic and
--     orphan-tolerant — a favorite to a deleted item is simply never matched, the same stance
--     V023 already took for "a favorite to a now-unviewable layer is never tested". The user_id
--     fk (CASCADE on user delete) is KEPT.
ALTER TABLE platform.user_favorites ADD COLUMN item_type text;
UPDATE platform.user_favorites SET item_type = 'layer';
ALTER TABLE platform.user_favorites ALTER COLUMN item_type SET NOT NULL;

ALTER TABLE platform.user_favorites DROP CONSTRAINT user_favorites_layer_id_fkey;
ALTER TABLE platform.user_favorites DROP CONSTRAINT user_favorites_pkey;
ALTER TABLE platform.user_favorites RENAME COLUMN layer_id TO item_id;
ALTER TABLE platform.user_favorites
    ADD CONSTRAINT user_favorites_pkey PRIMARY KEY (user_id, item_type, item_id);

DROP INDEX platform.user_favorites_layer_idx;
CREATE INDEX user_favorites_item_idx ON platform.user_favorites (item_type, item_id);

-- (2) Delete protection — an owner-settable flag the delete paths enforce. Admins always override
--     (enforced in the app, not here). Default false = unprotected.
ALTER TABLE platform.web_maps ADD COLUMN delete_protected boolean NOT NULL DEFAULT false;
ALTER TABLE platform.layers   ADD COLUMN delete_protected boolean NOT NULL DEFAULT false;
