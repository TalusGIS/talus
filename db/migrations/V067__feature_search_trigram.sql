-- V067 — Feature-search v2 trigram substrate (decision-feature-search-v2.md). Tier-2, backup-gated.
--
-- v1 (V058) gave the "Locate" panel token-EXACT find-by-attribute off a functional tsvector GIN
-- (`assets.search_document(notes, props) @@ websearch_to_tsquery('simple', q)`). v2 adds the operators v1
-- punted: pg_trgm FUZZY/typo tolerance and `?`/`*` WILDCARDS. Neither touches the tsvector — `%`/`similarity()`
-- and arbitrary `ILIKE '%…%'` are a DIFFERENT query path over the RAW normalized text, so they need a SECOND
-- per-slot index. This migration is that substrate; the query paths + panel badging land in later
-- (migration-free) commits. (Slot coded-LABEL search — the third v2 operator — needs NO index: it resolves
-- labels→codes at query time and matches props directly. `=case-sensitive` is a bounded v3.)
--
-- WHY NO `CREATE EXTENSION` / NO `f_unaccent` HERE: the Mode B own-data geocoder (V066) already installed
-- `pg_trgm` + `unaccent` and defined `platform.f_unaccent(text)` — the IMMUTABLE, dictionary-pinned unaccent
-- wrapper that can back an expression index (plain unaccent() is only STABLE). v2 REFERENCES that wrapper; two
-- migrations defining it would collide. So V067 is the small, extension-free one: just the trigram `search_text`
-- function + one `gin_trgm_ops` index per slot.
--
-- WHY `search_text` MIRRORS V066's `addr_document` (not V058's `search_document`): the trigram path matches raw
-- TEXT, not a tsvector. `search_text` is the un-tokenized sibling of `search_document` — the SAME `notes` + all
-- `props` values document, but returned as one `f_unaccent`-folded, whitespace-collapsed string (V066's
-- addr_document idiom verbatim). f_unaccent folding makes `%`/ILIKE both case- AND accent-insensitive, matching
-- the geocoder. IMMUTABLE + PARALLEL SAFE for V058/V066's exact reason: for text/jsonb inputs the result depends
-- only on the arguments, so it can back a functional index. `search_text` is the ONE expression both the
-- per-slot index AND every fuzzy/wildcard query use — they MUST match verbatim or the GIN is bypassed.
--
-- WHY A DO-LOOP, NOT V058's HARD-CODED PER-SLOT LIST (decision §3 F3 migration-shape): V058 hard-coded one
-- CREATE INDEX per slot for the 10 that existed in-sequence at V058 time. That is fragile now — slots are ALSO
-- runtime-minted by platform.provision_spatial_layer with NON-DETERMINISTIC hex ids (e.g. tal_83b803bf exists
-- today but was never in V058's list). A hard-coded V067 list would match only one deployment and err/miss slots
-- elsewhere. So V067 loops over the LIVE slot set — deployment-agnostic + idempotent (IF NOT EXISTS). Future
-- slots inherit the index via the provisioner (Part 3), the same way V058's search_idx propagated.
--
-- WHY plain CREATE INDEX (not CONCURRENTLY): CONCURRENTLY is illegal inside a DO block / transaction, and Flyway
-- runs migrations transactionally. This is a backup-gated maintenance op, not a live-traffic-hot moment; a
-- trigram GIN over even the 38k-row address layer builds in well under a second. Mirrors V058's blocking build.

-- ── Part 1 — the raw-text search document (one definition; every slot index AND every fuzzy/wildcard query) ───
CREATE FUNCTION assets.search_text(p_notes text, p_props jsonb)
    RETURNS text
    LANGUAGE sql
    IMMUTABLE
    PARALLEL SAFE
AS $$
    SELECT platform.f_unaccent(
        btrim(regexp_replace(
            coalesce(p_notes, '') || ' ' ||
            coalesce((SELECT string_agg(value, ' ') FROM jsonb_each_text(p_props)), ''),
            '\s+', ' ', 'g'
        ))
    )
$$;

COMMENT ON FUNCTION assets.search_text(text, jsonb) IS
    'Feature-search v2: the RAW normalized (f_unaccent-folded, whitespace-collapsed) sibling of '
    'assets.search_document — notes + all props values as one string. Backs the per-slot trigram GIN and is '
    'used verbatim by every fuzzy (%/similarity) and wildcard (ILIKE) query. See decision-feature-search-v2.md §3.';

-- ── Part 2 — one trigram GIN per EXISTING slot (loop over the live slot set; idempotent) ──────────────────────
DO $$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT tablename FROM pg_tables
        WHERE schemaname = 'assets' AND tablename ~ '^tal_[0-9a-f]{8}$'
    LOOP
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS %I ON assets.%I '
            'USING GIN (assets.search_text(notes, props) public.gin_trgm_ops)',
            r.tablename || '_search_trgm_idx', r.tablename
        );
    END LOOP;
END $$;

-- ── Part 3 — future slots inherit the trigram index via the provisioner ───────────────────────────────────────
-- Full replacement of V064's provision_spatial_layer: the ONLY change is the new trigram CREATE INDEX, right
-- after the V058 search_idx (so every runtime-minted slot is born with BOTH search indexes). Everything else is
-- V064 verbatim. (The aspatial provision_hosted_table is deliberately NOT touched — non-spatial hosted tables
-- have no geometry and are excluded from map feature search, so a trigram index there would be dead weight.)
CREATE OR REPLACE FUNCTION platform.provision_spatial_layer(p_id text, p_geom_type text, p_srid integer)
RETURNS void
LANGUAGE plpgsql
AS $fn$
DECLARE
  v_pg_type text;
BEGIN
  v_pg_type := CASE p_geom_type
                 WHEN 'point'   THEN 'Point'
                 WHEN 'line'    THEN 'MultiLineString'
                 WHEN 'polygon' THEN 'MultiPolygon'
               END;
  IF v_pg_type IS NULL THEN
    RAISE EXCEPTION 'provision_spatial_layer: invalid geom_type %', p_geom_type;
  END IF;
  IF p_id !~ '^tal_[0-9a-f]{8}$' THEN
    RAISE EXCEPTION 'provision_spatial_layer: invalid layer id %', p_id;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM public.spatial_ref_sys WHERE srid = p_srid) THEN
    RAISE EXCEPTION 'provision_spatial_layer: unknown srid %', p_srid;
  END IF;

  -- The V061 slot shape + V064 objectid (last — see header).
  EXECUTE format($ddl$
    CREATE TABLE assets.%I (
      id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      geom           geometry(%s, %s) NOT NULL,
      created_at     timestamptz NOT NULL DEFAULT now(),
      created_by     uuid NOT NULL REFERENCES platform.users(id),
      updated_at     timestamptz NOT NULL DEFAULT now(),
      updated_by     uuid NOT NULL REFERENCES platform.users(id),
      ai_provenance  jsonb,
      notes          text,
      row_version    bigint NOT NULL DEFAULT pg_current_xact_id()::text::bigint,
      props          jsonb NOT NULL DEFAULT '{}'::jsonb,
      objectid       integer NOT NULL GENERATED BY DEFAULT AS IDENTITY UNIQUE
    )
  $ddl$, p_id, v_pg_type, p_srid);

  EXECUTE format('CREATE INDEX %I ON assets.%I USING GIST (geom)', p_id || '_geom_idx', p_id);
  EXECUTE format('CREATE INDEX %I ON assets.%I USING GIN (props)', p_id || '_props_idx', p_id);
  EXECUTE format(
    'CREATE INDEX %I ON assets.%I USING GIN (assets.search_document(notes, props))',
    p_id || '_search_idx', p_id
  );
  -- v2 (V067): the trigram sibling for fuzzy/wildcard — mirrors the DO-loop above for runtime-minted slots.
  EXECUTE format(
    'CREATE INDEX %I ON assets.%I USING GIN (assets.search_text(notes, props) public.gin_trgm_ops)',
    p_id || '_search_trgm_idx', p_id
  );

  EXECUTE format($ddl$
    CREATE TABLE audit.%I (
      audit_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      asset_id     uuid NOT NULL,
      operation    text NOT NULL CHECK (operation IN ('INSERT','UPDATE','DELETE')),
      changed_at   timestamptz NOT NULL DEFAULT now(),
      changed_by   uuid REFERENCES platform.users(id),
      actor_type   text NOT NULL DEFAULT 'human' CHECK (actor_type IN ('human','ai_inference','service_account')),
      provenance   jsonb,
      client_info  jsonb,
      before_row   jsonb,
      after_row    jsonb
    )
  $ddl$, p_id || '_audit');
  EXECUTE format('CREATE INDEX %I ON audit.%I (asset_id)',   p_id || '_audit_asset_idx',   p_id || '_audit');
  EXECUTE format('CREATE INDEX %I ON audit.%I (changed_at)', p_id || '_audit_changed_idx', p_id || '_audit');

  EXECUTE format(
    'CREATE TRIGGER %I AFTER INSERT OR UPDATE OR DELETE ON assets.%I '
    'FOR EACH ROW EXECUTE FUNCTION audit.fn_record_change(%L, %L)',
    p_id || '_audit_trigger', p_id, 'audit.' || p_id || '_audit', 'id'
  );
  EXECUTE format(
    'CREATE TRIGGER %I BEFORE INSERT OR UPDATE ON assets.%I '
    'FOR EACH ROW EXECUTE FUNCTION assets.fn_validate_layer_props()',
    p_id || '_props_validate_trigger', p_id
  );
  EXECUTE format(
    'CREATE TRIGGER %I BEFORE UPDATE ON assets.%I '
    'FOR EACH ROW EXECUTE FUNCTION assets.fn_bump_row_version()',
    p_id || '_row_version_trigger', p_id
  );
END;
$fn$;
