-- V078 — Table↔layer relates (decision-relates-and-join-views.md, PR A). Tier-2, backup-gated.
--
-- The nonspatial-tables arc's deferred half: a RELATE is the AGO/Pro "relationship" gesture as pure
-- NAVIGATION metadata — click a sign, see its inspections. One registry row per relate: an origin
-- (spatial) layer + a field key on it, a related (geometry-less V059) table + a field key on it, and
-- display metadata. Related rows are read through the table's EXISTING gated features endpoint
-- filtered on the key — this migration adds NO data path, NO trigger, NO runtime DDL.
--
-- Hard fences (North Star, carried from the nonspatial arc):
--   * Documented-FK ONLY: no PG FK between feature tables, no cascade, no integrity enforcement —
--     orphan table rows are legal and simply don't surface. A relate documents, it never constrains.
--   * No relationship-class semantics: no compound relates, no editing through a relate.
--   * Cardinality is a display LABEL (what the drawer says), never enforced.
--
-- Authz shape (fork 3, THE crux): this table stores no permissions. The API lists a relate to a
-- caller ONLY when they can_view the related table (fail-closed omission — no count, no name), and
-- row reads ride the table's own OpenFGA gate. "Origin is spatial, related end is a geom_type='none'
-- table" is API-enforced at author time (a CHECK can't cleanly reach layers.geom_type; the app
-- validates both ends before INSERT).
--
-- Lifecycle: deleting EITHER end cascades the relate row (a relate is cheap navigation, not
-- data — fork 7); the Where-used panel lists relates on both ends so deletion is informed. The
-- slot pool is retired (decision-slot-provisioning.md) — every hosted delete removes its
-- platform.layers row via _drop_hosted_layer, so the two FKs' ON DELETE CASCADE fully cover
-- cleanup; no release-path code change needed.

CREATE TABLE platform.layer_relates (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    origin_layer_id    text NOT NULL REFERENCES platform.layers(layer_id) ON DELETE CASCADE,
    origin_field_key   text NOT NULL,
    related_table_id   text NOT NULL REFERENCES platform.layers(layer_id) ON DELETE CASCADE,
    related_field_key  text NOT NULL,
    display_name       text NOT NULL,
    cardinality        text NOT NULL DEFAULT '1:M' CHECK (cardinality IN ('1:1', '1:M')),
    sort_order         int  NOT NULL DEFAULT 0,
    created_at         timestamptz NOT NULL DEFAULT now(),
    created_by         uuid NOT NULL REFERENCES platform.users(id),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    updated_by         uuid NOT NULL REFERENCES platform.users(id),

    CONSTRAINT layer_relates_no_self CHECK (origin_layer_id <> related_table_id),
    -- Same grammar the V044 layer_fields CHECK enforces; also admits native columns (id, notes).
    CONSTRAINT layer_relates_key_format CHECK (
        origin_field_key  ~ '^[a-z][a-z0-9_]{0,62}$'
        AND related_field_key ~ '^[a-z][a-z0-9_]{0,62}$'
    ),
    CONSTRAINT layer_relates_display_name_len CHECK (
        display_name <> '' AND char_length(display_name) <= 200
    ),
    -- One relate per (origin, table, key-pair); the same pair under a different name is a duplicate.
    CONSTRAINT layer_relates_unique UNIQUE
        (origin_layer_id, related_table_id, origin_field_key, related_field_key)
);

-- The drawer's per-layer lookup and the where-used reverse lookup.
CREATE INDEX layer_relates_origin_idx  ON platform.layer_relates (origin_layer_id, sort_order);
CREATE INDEX layer_relates_related_idx ON platform.layer_relates (related_table_id);
