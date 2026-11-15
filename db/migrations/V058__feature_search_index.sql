-- V058 — Feature-search fast path (decision-feature-search.md). Tier-2, backup-gated.
--
-- The in-map "Locate" feature search (find a feature by attribute, ArcGIS-Pro-enhanced-search feel): one box,
-- type a query, it searches your layers' attributes and zooms to the matches, with ZERO per-layer config.
-- This migration is the performance substrate; the endpoint + panel land in later (migration-free) commits.
--
-- WHY AN INDEX IN v1 (reverses the decision doc's original "defer indexing" fork): a non-indexed search
-- recomputes to_tsvector on every row, so a 30k-row parcel layer costs ~1s per search and a 100k-row layer 3s+
-- — too slow for a type-and-see-results panel, and six-figure parcel/address layers are ordinary at municipal
-- scale. A functional GIN index turns each layer into a sub-millisecond index lookup instead of a full scan.
--
-- WHY FIELD-AGNOSTIC (the part the doc's original fork got wrong): a PER-FIELD index would go stale the moment
-- the owner adds or removes a field (owner-defined fields are data, not DDL — V044). Instead we index a single
-- document built from `notes` + ALL of `props` (values only, via jsonb_each_text — field NAMES are not indexed,
-- so they don't become noise tokens). This is genuinely zero-config: the owner tunes nothing, and the index
-- automatically covers any field they add later — no re-index, no per-layer setup. `assets.search_document`
-- is the ONE expression both the index and the query use; they MUST match verbatim to hit the index:
--     WHERE assets.search_document(notes, props) @@ websearch_to_tsquery('simple', :q)
--
-- WHY 'simple': the target queries are IDs / numbers / names / codes (e.g. a
-- parcel PIN text "2456897123" and an address-point long-integer 2456897123 must BOTH match "2456897123").
-- `simple` gives exact-token matching — no stemming, no stopword removal (so a ward valued "A" or a type valued
-- "OR" is not silently dropped). AND / OR / NOT / phrase all still work under `simple` (that is websearch_to_
-- tsquery operator parsing, independent of the config). The explicit 'simple' literal also keeps the two-arg
-- to_tsvector IMMUTABLE (the one-arg form is only STABLE — it reads default_text_search_config — and could not
-- back a functional index). Picking a config is a one-time commit: an `english` variant, if ever wanted for
-- prose-notes recall, would be a SECOND index, not a swap.
--
-- BOUNDARY (v1): the indexed document holds `notes` + text + number values + coded CODES (the value stored in
-- props). Coded LABELS live in platform.domain_values (a different table) and so cannot sit in a per-row
-- functional index — coded-label search + fuzzy/typo + ?/*/** wildcards + =case-sensitive are the bounded v2
-- (pg_trgm). Numeric values index as their text form (jsonb_each_text renders 2456897123 → '2456897123').
--
-- NOTE for a FUTURE slot-pool expansion (à la V056): mint this search index per new slot alongside its
-- _geom_idx / _props_idx. The `assets.search_document` function already exists after this migration; the new
-- migration only adds `CREATE INDEX tal_<new>_search_idx ... USING GIN (assets.search_document(notes, props));`.

-- ── Part 1 — the searchable-document function (one definition; used by every slot's index AND the query) ─────
-- IMMUTABLE + PARALLEL SAFE: the result depends only on the arguments (jsonb_each_text and the two-arg
-- to_tsvector with a literal config are both immutable), so it may back a functional index. string_agg order is
-- unspecified, which is irrelevant for a tsvector (a set of lexemes).
CREATE FUNCTION assets.search_document(p_notes text, p_props jsonb)
    RETURNS tsvector
    LANGUAGE sql
    IMMUTABLE
    PARALLEL SAFE
AS $$
    SELECT to_tsvector(
        'simple',
        coalesce(p_notes, '') || ' ' ||
        coalesce((SELECT string_agg(value, ' ') FROM jsonb_each_text(p_props)), '')
    );
$$;

-- ── Part 2 — the functional GIN index on every slot (copy-paste-uniform, one per slot; mirrors _props_idx) ───
CREATE INDEX tal_0f529c77_search_idx ON assets.tal_0f529c77 USING GIN (assets.search_document(notes, props));
CREATE INDEX tal_15603025_search_idx ON assets.tal_15603025 USING GIN (assets.search_document(notes, props));
CREATE INDEX tal_2c09dee7_search_idx ON assets.tal_2c09dee7 USING GIN (assets.search_document(notes, props));
CREATE INDEX tal_2dbf236f_search_idx ON assets.tal_2dbf236f USING GIN (assets.search_document(notes, props));
CREATE INDEX tal_57b25bf6_search_idx ON assets.tal_57b25bf6 USING GIN (assets.search_document(notes, props));
CREATE INDEX tal_6496fe7d_search_idx ON assets.tal_6496fe7d USING GIN (assets.search_document(notes, props));
CREATE INDEX tal_77175dae_search_idx ON assets.tal_77175dae USING GIN (assets.search_document(notes, props));
CREATE INDEX tal_782e4b5c_search_idx ON assets.tal_782e4b5c USING GIN (assets.search_document(notes, props));
CREATE INDEX tal_7b3aa14c_search_idx ON assets.tal_7b3aa14c USING GIN (assets.search_document(notes, props));
CREATE INDEX tal_e6e2964f_search_idx ON assets.tal_e6e2964f USING GIN (assets.search_document(notes, props));
