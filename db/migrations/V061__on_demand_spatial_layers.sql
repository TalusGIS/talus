-- V061 — on-demand spatial layers: retire the slot pool (decision-slot-provisioning.md).
--
-- Three moves, one goal: "create a layer" must never end in "ask an admin to run a migration."
--   1) platform.provision_spatial_layer(id, geom_type, srid) — the spatial twin of V059's
--      provision_hosted_table: template-fixed DDL creating one assets.<id> feature table BORN
--      right-shaped (geometry(Point|MultiLineString|MultiPolygon, srid)) with the CURRENT slot
--      schema + indexes + audit shadow + triggers. The post-claim retype dance (_set_slot_geom)
--      and the V056-class "new slots MUST match the live shape" drift trap both disappear.
--   2) assets.slot_mvt(z, x, y, layer) — ONE generic, parameterized pg_tileserv function layer
--      serving EVERY current and future hosted spatial layer (?layer=tal_<8hex>). pg_tileserv
--      catalogs functions at startup only; this function is the last one it ever needs to learn.
--      ⇒ ONE `docker compose restart pg_tileserv` after this migration — the final one.
--   3) Pool retirement: drop the remaining UNCLAIMED slot tables/functions/registry rows, and
--      drop every per-slot tile function (the API proxy switches to slot_mvt). Already-claimed
--      slots are untouched — they are simply layers now, and DELETE drops them (V059 semantics).
--
-- Fences: the DDL stays template-fixed (a validated tal_<8hex> token is the only identifier
-- input; %I quotes everything; geom family comes from a frozen 3-value CASE; srid must exist in
-- spatial_ref_sys). slot_mvt double-validates its layer param (regex + registry) and serves an
-- empty tile otherwise — the API proxy's OpenFGA gate remains the real authz (pg_tileserv is
-- internal-network-only).

-- 1) The spatial provisioner. ------------------------------------------------------------------
CREATE OR REPLACE FUNCTION platform.provision_spatial_layer(p_id text, p_geom_type text, p_srid integer)
RETURNS void
LANGUAGE plpgsql
AS $fn$
DECLARE
  v_pg_type text;
BEGIN
  -- Second gate behind the API's pydantic Literal: the geometry family is a frozen allow-list,
  -- never raw input. line/polygon store Multi* (decision-line-polygon-layers.md).
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

  -- The feature table: the CURRENT slot shape (V019 + row_version V035 + props V044), geom born
  -- at the requested family/SRID (native storage passes working_srid; canonical passes 4326).
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
      props          jsonb NOT NULL DEFAULT '{}'::jsonb
    )
  $ddl$, p_id, v_pg_type, p_srid);

  EXECUTE format('CREATE INDEX %I ON assets.%I USING GIST (geom)', p_id || '_geom_idx', p_id);
  EXECUTE format('CREATE INDEX %I ON assets.%I USING GIN (props)', p_id || '_props_idx', p_id);
  -- The field-agnostic feature-search index (V058), same as every slot + V059 table.
  EXECUTE format(
    'CREATE INDEX %I ON assets.%I USING GIN (assets.search_document(notes, props))',
    p_id || '_search_idx', p_id
  );

  -- The 9-column shadow audit table + its two indexes (identical to a slot's).
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

  -- The three standard per-feature triggers (all reference EXISTING shared functions).
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

COMMENT ON FUNCTION platform.provision_spatial_layer(text, text, integer) IS
  'decision-slot-provisioning.md: scoped, template-fixed creation of one hosted SPATIAL layer '
  '(the current slot schema, geom born at the requested family/SRID). Called by the API create '
  'path; the id is app-generated + collision-checked and matches ^tal_[0-9a-f]{8}$.';

-- 2) The ONE generic MVT function layer. --------------------------------------------------------
-- pg_tileserv signature contract: (z int, x int, y int, <extras with defaults>) RETURNS bytea;
-- extras arrive as query params (…/assets.slot_mvt/{z}/{x}/{y}.pbf?layer=tal_xxxxxxxx). The
-- envelope SRID comes from the layer's OWN geom column (Find_SRID), so canonical-4326 and
-- native-CRS layers both tile correctly with zero per-layer state. An invalid/unknown layer
-- yields NULL ⇒ pg_tileserv 204s (an empty tile) — the API proxy already 403/404ed real callers.
CREATE OR REPLACE FUNCTION assets.slot_mvt(z integer, x integer, y integer, layer text DEFAULT '')
RETURNS bytea
LANGUAGE plpgsql STABLE PARALLEL SAFE
AS $fn$
DECLARE
  v_srid integer;
  v_result bytea;
BEGIN
  IF layer !~ '^tal_[0-9a-f]{8}$' THEN
    RETURN NULL;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM platform.layers WHERE layer_id = layer AND geom_type <> 'none') THEN
    RETURN NULL;
  END IF;
  v_srid := Find_SRID('assets', layer, 'geom');
  EXECUTE format($q$
    SELECT ST_AsMVT(tile, %L, 4096, 'geom')
    FROM (
      SELECT ST_AsMVTGeom(ST_Transform(s.geom, 3857), ST_TileEnvelope($1, $2, $3)) AS geom,
             s.id::text AS id
      FROM assets.%I s
      WHERE s.geom && ST_Transform(ST_TileEnvelope($1, $2, $3), %s)
    ) AS tile
    WHERE tile.geom IS NOT NULL
  $q$, layer, layer, v_srid)
  INTO v_result
  USING z, x, y;
  RETURN v_result;
END;
$fn$;

COMMENT ON FUNCTION assets.slot_mvt(integer, integer, integer, text) IS
  'decision-slot-provisioning.md: the one generic MVT source for every hosted spatial layer '
  '(?layer=tal_<8hex>, regex+registry validated; envelope at the geom column''s own SRID). '
  'Replaces the per-slot tal_*_tile functions; the API proxy fronts it with the OpenFGA gate.';

-- 3) Pool retirement. ---------------------------------------------------------------------------
-- 3a) Unclaimed slots: reserved empty tables with no owner, no data, no dependents — drop them
--     and delete their registry rows. (The V020 registry audit trigger records the DELETEs.)
DO $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT layer_id FROM platform.layers
    WHERE status = 'unclaimed' AND layer_id ~ '^tal_[0-9a-f]{8}$'
  LOOP
    EXECUTE format('DROP FUNCTION IF EXISTS assets.%I(integer, integer, integer)', r.layer_id || '_tile');
    EXECUTE format('DROP TABLE IF EXISTS assets.%I', r.layer_id);
    EXECUTE format('DROP TABLE IF EXISTS audit.%I', r.layer_id || '_audit');
    DELETE FROM platform.layers WHERE layer_id = r.layer_id;
  END LOOP;
END $$;

-- 3b) Claimed slots keep their tables and data untouched — but their per-slot tile functions are
--     dead code once the API proxy speaks slot_mvt, so drop them for tidiness.
DO $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT layer_id FROM platform.layers
    WHERE layer_id ~ '^tal_[0-9a-f]{8}$' AND geom_type <> 'none'
  LOOP
    EXECUTE format('DROP FUNCTION IF EXISTS assets.%I(integer, integer, integer)', r.layer_id || '_tile');
  END LOOP;
END $$;
