-- V023 — platform.user_favorites (Talus 18, Commit 2).
--
-- The first PERSONAL-preference table in the schema. Every prior table is authoritative
-- GIS or platform state (assets, audit, registry, users); this is the first that records a
-- per-user *UI* choice — which layers a caller has starred in the Catalog. It is read by
-- GET /me/layers (folded in as a per-caller `favorite: bool` via LEFT JOIN) and written by
-- PUT/DELETE /me/favorites/{layer_id} (Talus 18).
--
-- Shape: one row = "this user favorited this layer". The composite PRIMARY KEY (user_id,
-- layer_id) makes a favorite idempotent (re-favoriting is a no-op / ON CONFLICT DO NOTHING)
-- and indexes the hot path — "the layers THIS user favorited" — user_id-first, which the
-- per-caller LEFT JOIN uses. created_at is informational (recency), not load-bearing.
--
-- Referential integrity (decision-18 §3 Q5 refinement): BOTH foreign keys are
-- ON DELETE CASCADE.
--   * user_id  -> platform.users(id)        : a removed user's favorites vanish with them.
--   * layer_id -> platform.layers(layer_id)  : a released/dropped slot's favorites vanish.
-- This is the row-DELETE hygiene path. The *other* orphan case — a layer merely UN-SHARED
-- from a user (an OpenFGA tuple delete, NOT a row delete) — is handled at read time, not
-- here: the favorite row survives but stops appearing because GET /me/layers only joins it
-- onto layers the caller can still VIEW. So a favorite to a now-unviewable layer is
-- harmless and needs no cleanup job (§3 Q5). The fk_layer index below backs the
-- layer-side cascade (Postgres does not auto-index a referencing column).
--
-- No audit trigger, by design: the "one un-bypassable audit" principle governs assets and
-- the layer registry, not personal UI preferences. A star is not a GIS edit; auditing it
-- would be noise. No RLS either: the table is reached ONLY through the api, which scopes
-- every read/write to the authenticated caller's user_id — the same trust boundary the
-- rest of /me/* relies on.

CREATE TABLE platform.user_favorites (
    user_id    uuid        NOT NULL REFERENCES platform.users(id)        ON DELETE CASCADE,
    layer_id   text        NOT NULL REFERENCES platform.layers(layer_id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, layer_id)
);

-- Backs the layer_id -> platform.layers ON DELETE CASCADE (and any future "who favorited
-- this layer?" lookup). The PK already covers the user_id-first read path.
CREATE INDEX user_favorites_layer_idx ON platform.user_favorites (layer_id);
