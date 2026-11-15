-- V107 — internal locator: locality fields (decision-batch-geocoding-county-scale.md F3 / F3a).
--
-- An internal locator's `address_fields` is the STREET document: it is what the per-locator trigram
-- GIN indexes, what the prefilters run against, and what `word_similarity` scores. Putting the city
-- and ZIP in it flipped the F1a assumption ("the document sits inside the query"): a county locator
-- mapped [address, postalcomm, zip] scored every correct hit 65–84 against a threshold of 85 (7 of 197
-- matched on one test file), and made the `%` prefilter nearly unselective.
--
-- `locality_fields` is the second, ordered list — city, ZIP, community: fields that BREAK TIES between
-- equal street matches (the same street number and name in two towns) and appear in the result label,
-- but never lower a score and are never indexed. Empty by default, so every existing locator behaves
-- exactly as before; a county-scale admin moves city/ZIP across in the admin form, which rebuilds the
-- index on the narrowed street document (the PUT path already drops + recreates it on a mapping change).
-- Disjointness from `address_fields` and membership in the layer's field catalog are app-validated
-- (geocoders.py + geocode_internal.validate_mapping); the CHECKs below are the shape backstop.

ALTER TABLE platform.geocoders
    ADD COLUMN locality_fields jsonb NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE platform.geocoders
    ADD CONSTRAINT geocoders_locality_fields_array
    CHECK (jsonb_typeof(locality_fields) = 'array');

-- Locality fields only mean something on an internal locator; external types keep the empty default.
ALTER TABLE platform.geocoders
    ADD CONSTRAINT geocoders_locality_internal_only
    CHECK (type = 'internal' OR locality_fields = '[]'::jsonb);
