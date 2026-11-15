-- V072 — Layer display field (decision-layer-display-field.md). Tier-2, backup-gated.
--
-- The ArcGIS "Display Field": each layer's ONE attribute that names a feature, used wherever a feature
-- shows as a single line (popup title, Locate/feature-search result labels, identify, the ESRI/OGC
-- `displayField`). Set once on the layer via a star in the attribute table; travels to every map, every
-- consumer. Fixes the root cause of a ZIP code showing as the label: hosted slots hardcode their title field to `'id'`.
--
-- Storage (fork F1): a single nullable text column on platform.layers — covers slots AND built-ins
-- uniformly, and the one-field-per-layer invariant is free (it's one value). NULL means "unset" — the
-- read path derives a first-text-field fallback at read time (fork F2), so a brand-new layer reads well
-- with zero config; NULL never means `'id'`. This is a LABELING setting only (decision §0/§6 fence): it
-- does NOT drive attribute-table sort, symbology, query behavior, or the catalog item list (layer
-- display_name — a different concept).
--
-- Validation lives app-side at set time (fork F6, mirrors the geocoder address-field check): the starred
-- name must be a real field of the layer (any attribute-table column, incl. `notes`, or an owner-defined
-- layer_fields.field_key); 400 on a bad field. No CHECK here — the set of valid names is per-layer and
-- introspected, not expressible as a column constraint.
--
-- Seed (fork F5): the three built-ins get their CURRENT hardcoded layers.ts defaults, so their popups /
-- Locate labels are byte-for-byte unchanged out of the box; they stay starrable like any layer (manage
-- gate). Every other existing layer stays NULL → the read-time first-text-field fallback labels it.

ALTER TABLE platform.layers
    ADD COLUMN display_field text;

UPDATE platform.layers SET display_field = 'mutcd_code'      WHERE layer_id = 'signs';
UPDATE platform.layers SET display_field = 'light_type_code' WHERE layer_id = 'street_lights';
UPDATE platform.layers SET display_field = 'boundary_class'  WHERE layer_id = 'county_boundary';
