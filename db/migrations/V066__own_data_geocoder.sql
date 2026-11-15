-- V066 — Mode B: own-data geocoder substrate (decision-own-data-geocoder.md). Tier-2, backup-gated.
--
-- An `internal` locator geocodes against a hosted address-point layer IN PostGIS — no external
-- service in the loop (the first locator type that works in an air-gapped deployment). This migration is
-- the substrate only: the matching extensions, the index-safe unaccent wrapper, and the
-- platform.geocoders shape for the new type. The adapter + admin UI land in later (migration-free)
-- commits; the per-locator trigram index is RUNTIME DDL owned by locator create/delete (decision §3
-- F5, the T38-views precedent) — deliberately NOT minted here.

-- ── Part 1 — matching extensions (first CREATE EXTENSION since V001's postgis + pgcrypto) ───────────
-- pg_trgm: trigram similarity (typo-tolerant, case-folded) + the GIN opclass that serves both the
-- `%` similarity operator and the ILIKE prefix path. unaccent: accent/diacritic folding ("Muñoz" ~
-- "Munoz"). Both ship in the image (postgis default); SCHEMA public pinned so the qualified
-- references below never depend on search_path. fuzzystrmatch/tiger stay uninstalled — not the
-- vehicle (decision §1 fences).
CREATE EXTENSION IF NOT EXISTS pg_trgm  WITH SCHEMA public;
CREATE EXTENSION IF NOT EXISTS unaccent WITH SCHEMA public;

-- ── Part 2 — the index-safe unaccent wrapper ─────────────────────────────────────────────────────────
-- unaccent(text) is only STABLE (it reads the session's dictionary setting), so it cannot back an
-- expression index. Pinning the dictionary makes it genuinely immutable. This is the ONE expression
-- seam both the per-locator index and every query use — they MUST match verbatim to hit the index
-- (the assets.search_document discipline from V058). STRICT: NULL in → NULL out.
CREATE FUNCTION platform.f_unaccent(p_text text)
    RETURNS text
    LANGUAGE sql
    IMMUTABLE
    PARALLEL SAFE
    STRICT
AS $$ SELECT public.unaccent('public.unaccent'::regdictionary, p_text) $$;

COMMENT ON FUNCTION platform.f_unaccent(text) IS
    'Mode B geocoder: IMMUTABLE unaccent (dictionary pinned) so it can back the per-locator trigram '
    'index; used verbatim on both the index expression and the query side. '
    'See decision-own-data-geocoder.md §3 F1/F5.';

-- The one match-document expression (the assets.search_document discipline from V058): the mapped
-- field values → one normalized string — unaccented, whitespace collapsed + trimmed (an empty house-
-- number field must not leave a leading space that breaks the ILIKE prefix path). concat_ws/
-- array_to_string are only STABLE in the catalog, which is why this wrapper exists and is DECLARED
-- immutable: for text inputs the result depends only on the arguments (V058's exact reasoning).
-- Both the per-locator GIN index and every suggest/search query call THIS function with the SAME
-- literal field expressions — they must match verbatim to hit the index.
CREATE FUNCTION platform.addr_document(p_parts text[])
    RETURNS text
    LANGUAGE sql
    IMMUTABLE
    PARALLEL SAFE
AS $$
    SELECT platform.f_unaccent(
        btrim(regexp_replace(array_to_string(p_parts, ' '), '\s+', ' ', 'g'))
    )
$$;

COMMENT ON FUNCTION platform.addr_document(text[]) IS
    'Mode B geocoder: the ONE normalized match-document expression (unaccent + whitespace collapse) '
    'used verbatim by both the per-locator trigram index and the suggest/search queries. '
    'See decision-own-data-geocoder.md §3 F1/F5.';

-- ── Part 3 — platform.geocoders grows the internal type (decision §3 F2) ─────────────────────────────
ALTER TABLE platform.geocoders DROP CONSTRAINT geocoders_type_check;
ALTER TABLE platform.geocoders
    ADD CONSTRAINT geocoders_type_check CHECK (type IN ('arcgis', 'nominatim', 'internal'));

ALTER TABLE platform.geocoders
    -- An internal locator has no service URL — NULL, not a sentinel (the per-type CHECK below keeps
    -- url mandatory for the external types, so nothing existing loosens).
    ALTER COLUMN url DROP NOT NULL,
    -- The source layer. Deliberately NO ON DELETE action: RESTRICT semantics are the F6 belt — a
    -- layer a locator references cannot be deleted out from under it (release_layer adds the
    -- friendly guard in front, mirroring the platform.layer_views view-dependency precedent).
    ADD COLUMN layer_id text REFERENCES platform.layers(layer_id),
    -- Ordered array (1..N) of the layer's address fields, concatenated (concat_ws ' ') for BOTH
    -- matching and the display label. On a hosted layer each entry is a platform.layer_fields
    -- field_key (value lives in props) or the literal 'notes'; on a built-in point layer, a real
    -- column. Validated app-side at create/edit/Test time against the registry / live schema.
    ADD COLUMN address_fields jsonb;

-- Per-type shape: internal ⇒ layer_id + address_fields, no url; external ⇒ url, no internal config.
ALTER TABLE platform.geocoders ADD CONSTRAINT geocoders_shape_by_type CHECK (
    (type =  'internal' AND url IS NULL     AND layer_id IS NOT NULL AND address_fields IS NOT NULL)
 OR (type <> 'internal' AND url IS NOT NULL AND layer_id IS NULL     AND address_fields IS NULL)
);

COMMENT ON COLUMN platform.geocoders.layer_id IS
    'Mode B (type=internal): the hosted address-point layer this locator searches. FK with no '
    'cascade — the F6 delete-block belt. NULL for external types.';
COMMENT ON COLUMN platform.geocoders.address_fields IS
    'Mode B (type=internal): ordered jsonb array of the layer''s address fields (layer_fields keys / '
    '''notes'' / built-in columns), concat_ws-joined for matching AND display. NULL for external types.';
