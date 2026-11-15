-- V080 — join views (decision-relates-and-join-views.md PR B, Fork 5). Tier-2, backup-gated.
--
-- A JOIN VIEW is a T38 view layer whose def carries ONE extra ingredient: a LEFT JOIN LATERAL of a
-- single standalone TABLE (V059, geom_type='none') on an equality key pair, 1:first with an
-- author-picked sort (Fork 4 — the "hydrants ⋈ latest inspection" case). Fork 5 rejected a sibling
-- table: lifecycle, drop, sharing, and the dependency guard all key off platform.layer_views, so a
-- join view IS a layer_views row with two extra nullable columns. Plain views keep both NULL — the
-- CHECK makes half-configured rows unrepresentable.
--
-- The join is compiled at runtime by api/app/layer_views.py under the SAME F-DDL scoping T38
-- shipped: server-generated view name, sql.Identifier physical columns, literal-baked props
-- extractions validated against the layer_fields registry, one-transaction CREATE. Every joined
-- output column is prefixed `<alias>_<key>` (Fork 4½ — collisions impossible by construction).
-- Authoring gate is admin OR owner-of-BOTH ends (Fork 6): creation is the re-share moment for the
-- table's columns, so mere can_view on the table is NOT enough to republish it.

ALTER TABLE platform.layer_views
    -- The joined table's registry id. ON DELETE RESTRICT is the DB belt behind the friendly
    -- release guard (Fork 7): a table's registry row can't be DELETEd while a join view reads it —
    -- count_dependent_views() gives the actionable 409 in front of this constraint.
    ADD COLUMN join_table_id text NULL REFERENCES platform.layers(layer_id) ON DELETE RESTRICT,
    -- {layer_key, table_key, alias, columns[], sort_key, sort_dir} — the validated join recipe.
    -- Stored (not just baked into the PG view) for the same reason filter_def is: reproducible +
    -- auditable + editable without reverse-engineering the view definition.
    ADD COLUMN join_spec jsonb NULL,
    -- Both-or-neither: a join view has a table AND a spec; a plain view has NEITHER.
    ADD CONSTRAINT layer_views_join_pair CHECK ((join_table_id IS NULL) = (join_spec IS NULL));

-- Backs the release guard ("do any join views read this table?") + where-used lookups.
CREATE INDEX layer_views_join_table_idx ON platform.layer_views (join_table_id)
    WHERE join_table_id IS NOT NULL;
