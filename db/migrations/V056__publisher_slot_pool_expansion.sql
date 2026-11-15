-- V056 — publisher slot-pool expansion (+5 slots). Tier-2, backup-gated.
--
-- Lifts the pool ceiling from 5 (V019) to 10 by minting 5 MORE empty slots, exactly as V019 foretold
-- ("Lifting the ceiling later = adding more slot blocks in a future migration"). The line/polygon slice needs
-- headroom: 4 of the original 5 slots are already claimed (test/demo layers), leaving only 1 free — not enough
-- to author + keep several line/polygon layers for the c5 browser E2E and ongoing use.
--
-- ★ These blocks mirror the CURRENT slot shape, NOT V019's original one. Since V019 the slot tables have
-- gained: row_version (V035, optimistic concurrency) + its BEFORE UPDATE trigger; props jsonb + its GIN index
-- + the props-validate BEFORE INSERT/UPDATE trigger (V044); and the audit table's actor_type CHECK now admits
-- 'service_account' (Talus 19). New slots MUST match the live shape or feature CRUD breaks on them — the
-- post-apply verify (verify-line-polygon.sh / structural parity check) confirms each new slot is identical to
-- an existing one. The three trigger FUNCTIONS already exist (audit.fn_record_change,
-- assets.fn_validate_layer_props, assets.fn_bump_row_version) — these blocks only attach them.
--
-- Like V019, the repetition is deliberate (one explicit block per slot, copy-paste-uniform — the same style
-- V035/V044 used to touch all 5 slots) and the slots ship EMPTY at the 'unclaimed' state. geom is born
-- geometry(Point,4326); a non-point claim retypes it via the scoped ALTER (decision-line-polygon-layers.md c2).

-- ============================================================================
-- Slot 6 — tal_15603025
-- ============================================================================
CREATE TABLE assets.tal_15603025 (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    geom           geometry(Point, 4326) NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    created_by     uuid NOT NULL REFERENCES platform.users(id),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    updated_by     uuid NOT NULL REFERENCES platform.users(id),
    ai_provenance  jsonb,
    notes          text,
    row_version    bigint NOT NULL DEFAULT pg_current_xact_id()::text::bigint,
    props          jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX tal_15603025_geom_idx  ON assets.tal_15603025 USING GIST (geom);
CREATE INDEX tal_15603025_props_idx ON assets.tal_15603025 USING GIN (props);
CREATE TABLE audit.tal_15603025_audit (
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
);
CREATE INDEX tal_15603025_audit_asset_idx   ON audit.tal_15603025_audit (asset_id);
CREATE INDEX tal_15603025_audit_changed_idx ON audit.tal_15603025_audit (changed_at);
CREATE TRIGGER tal_15603025_audit_trigger
    AFTER INSERT OR UPDATE OR DELETE ON assets.tal_15603025
    FOR EACH ROW EXECUTE FUNCTION audit.fn_record_change('audit.tal_15603025_audit', 'id');
CREATE TRIGGER tal_15603025_props_validate_trigger
    BEFORE INSERT OR UPDATE ON assets.tal_15603025
    FOR EACH ROW EXECUTE FUNCTION assets.fn_validate_layer_props();
CREATE TRIGGER tal_15603025_row_version_trigger
    BEFORE UPDATE ON assets.tal_15603025
    FOR EACH ROW EXECUTE FUNCTION assets.fn_bump_row_version();
CREATE OR REPLACE FUNCTION assets.tal_15603025_tile(z integer, x integer, y integer)
RETURNS bytea AS $$
  SELECT ST_AsMVT(tile, 'tal_15603025', 4096, 'geom')
  FROM (
    SELECT ST_AsMVTGeom(ST_Transform(s.geom, 3857), ST_TileEnvelope(z, x, y)) AS geom, s.id::text AS id
    FROM assets.tal_15603025 s
    WHERE s.geom && ST_Transform(ST_TileEnvelope(z, x, y), 4326)
  ) AS tile
  WHERE tile.geom IS NOT NULL;
$$ LANGUAGE sql STABLE PARALLEL SAFE;

-- ============================================================================
-- Slot 7 — tal_782e4b5c
-- ============================================================================
CREATE TABLE assets.tal_782e4b5c (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    geom           geometry(Point, 4326) NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    created_by     uuid NOT NULL REFERENCES platform.users(id),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    updated_by     uuid NOT NULL REFERENCES platform.users(id),
    ai_provenance  jsonb,
    notes          text,
    row_version    bigint NOT NULL DEFAULT pg_current_xact_id()::text::bigint,
    props          jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX tal_782e4b5c_geom_idx  ON assets.tal_782e4b5c USING GIST (geom);
CREATE INDEX tal_782e4b5c_props_idx ON assets.tal_782e4b5c USING GIN (props);
CREATE TABLE audit.tal_782e4b5c_audit (
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
);
CREATE INDEX tal_782e4b5c_audit_asset_idx   ON audit.tal_782e4b5c_audit (asset_id);
CREATE INDEX tal_782e4b5c_audit_changed_idx ON audit.tal_782e4b5c_audit (changed_at);
CREATE TRIGGER tal_782e4b5c_audit_trigger
    AFTER INSERT OR UPDATE OR DELETE ON assets.tal_782e4b5c
    FOR EACH ROW EXECUTE FUNCTION audit.fn_record_change('audit.tal_782e4b5c_audit', 'id');
CREATE TRIGGER tal_782e4b5c_props_validate_trigger
    BEFORE INSERT OR UPDATE ON assets.tal_782e4b5c
    FOR EACH ROW EXECUTE FUNCTION assets.fn_validate_layer_props();
CREATE TRIGGER tal_782e4b5c_row_version_trigger
    BEFORE UPDATE ON assets.tal_782e4b5c
    FOR EACH ROW EXECUTE FUNCTION assets.fn_bump_row_version();
CREATE OR REPLACE FUNCTION assets.tal_782e4b5c_tile(z integer, x integer, y integer)
RETURNS bytea AS $$
  SELECT ST_AsMVT(tile, 'tal_782e4b5c', 4096, 'geom')
  FROM (
    SELECT ST_AsMVTGeom(ST_Transform(s.geom, 3857), ST_TileEnvelope(z, x, y)) AS geom, s.id::text AS id
    FROM assets.tal_782e4b5c s
    WHERE s.geom && ST_Transform(ST_TileEnvelope(z, x, y), 4326)
  ) AS tile
  WHERE tile.geom IS NOT NULL;
$$ LANGUAGE sql STABLE PARALLEL SAFE;

-- ============================================================================
-- Slot 8 — tal_77175dae
-- ============================================================================
CREATE TABLE assets.tal_77175dae (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    geom           geometry(Point, 4326) NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    created_by     uuid NOT NULL REFERENCES platform.users(id),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    updated_by     uuid NOT NULL REFERENCES platform.users(id),
    ai_provenance  jsonb,
    notes          text,
    row_version    bigint NOT NULL DEFAULT pg_current_xact_id()::text::bigint,
    props          jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX tal_77175dae_geom_idx  ON assets.tal_77175dae USING GIST (geom);
CREATE INDEX tal_77175dae_props_idx ON assets.tal_77175dae USING GIN (props);
CREATE TABLE audit.tal_77175dae_audit (
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
);
CREATE INDEX tal_77175dae_audit_asset_idx   ON audit.tal_77175dae_audit (asset_id);
CREATE INDEX tal_77175dae_audit_changed_idx ON audit.tal_77175dae_audit (changed_at);
CREATE TRIGGER tal_77175dae_audit_trigger
    AFTER INSERT OR UPDATE OR DELETE ON assets.tal_77175dae
    FOR EACH ROW EXECUTE FUNCTION audit.fn_record_change('audit.tal_77175dae_audit', 'id');
CREATE TRIGGER tal_77175dae_props_validate_trigger
    BEFORE INSERT OR UPDATE ON assets.tal_77175dae
    FOR EACH ROW EXECUTE FUNCTION assets.fn_validate_layer_props();
CREATE TRIGGER tal_77175dae_row_version_trigger
    BEFORE UPDATE ON assets.tal_77175dae
    FOR EACH ROW EXECUTE FUNCTION assets.fn_bump_row_version();
CREATE OR REPLACE FUNCTION assets.tal_77175dae_tile(z integer, x integer, y integer)
RETURNS bytea AS $$
  SELECT ST_AsMVT(tile, 'tal_77175dae', 4096, 'geom')
  FROM (
    SELECT ST_AsMVTGeom(ST_Transform(s.geom, 3857), ST_TileEnvelope(z, x, y)) AS geom, s.id::text AS id
    FROM assets.tal_77175dae s
    WHERE s.geom && ST_Transform(ST_TileEnvelope(z, x, y), 4326)
  ) AS tile
  WHERE tile.geom IS NOT NULL;
$$ LANGUAGE sql STABLE PARALLEL SAFE;

-- ============================================================================
-- Slot 9 — tal_0f529c77
-- ============================================================================
CREATE TABLE assets.tal_0f529c77 (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    geom           geometry(Point, 4326) NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    created_by     uuid NOT NULL REFERENCES platform.users(id),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    updated_by     uuid NOT NULL REFERENCES platform.users(id),
    ai_provenance  jsonb,
    notes          text,
    row_version    bigint NOT NULL DEFAULT pg_current_xact_id()::text::bigint,
    props          jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX tal_0f529c77_geom_idx  ON assets.tal_0f529c77 USING GIST (geom);
CREATE INDEX tal_0f529c77_props_idx ON assets.tal_0f529c77 USING GIN (props);
CREATE TABLE audit.tal_0f529c77_audit (
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
);
CREATE INDEX tal_0f529c77_audit_asset_idx   ON audit.tal_0f529c77_audit (asset_id);
CREATE INDEX tal_0f529c77_audit_changed_idx ON audit.tal_0f529c77_audit (changed_at);
CREATE TRIGGER tal_0f529c77_audit_trigger
    AFTER INSERT OR UPDATE OR DELETE ON assets.tal_0f529c77
    FOR EACH ROW EXECUTE FUNCTION audit.fn_record_change('audit.tal_0f529c77_audit', 'id');
CREATE TRIGGER tal_0f529c77_props_validate_trigger
    BEFORE INSERT OR UPDATE ON assets.tal_0f529c77
    FOR EACH ROW EXECUTE FUNCTION assets.fn_validate_layer_props();
CREATE TRIGGER tal_0f529c77_row_version_trigger
    BEFORE UPDATE ON assets.tal_0f529c77
    FOR EACH ROW EXECUTE FUNCTION assets.fn_bump_row_version();
CREATE OR REPLACE FUNCTION assets.tal_0f529c77_tile(z integer, x integer, y integer)
RETURNS bytea AS $$
  SELECT ST_AsMVT(tile, 'tal_0f529c77', 4096, 'geom')
  FROM (
    SELECT ST_AsMVTGeom(ST_Transform(s.geom, 3857), ST_TileEnvelope(z, x, y)) AS geom, s.id::text AS id
    FROM assets.tal_0f529c77 s
    WHERE s.geom && ST_Transform(ST_TileEnvelope(z, x, y), 4326)
  ) AS tile
  WHERE tile.geom IS NOT NULL;
$$ LANGUAGE sql STABLE PARALLEL SAFE;

-- ============================================================================
-- Slot 10 — tal_e6e2964f
-- ============================================================================
CREATE TABLE assets.tal_e6e2964f (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    geom           geometry(Point, 4326) NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    created_by     uuid NOT NULL REFERENCES platform.users(id),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    updated_by     uuid NOT NULL REFERENCES platform.users(id),
    ai_provenance  jsonb,
    notes          text,
    row_version    bigint NOT NULL DEFAULT pg_current_xact_id()::text::bigint,
    props          jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX tal_e6e2964f_geom_idx  ON assets.tal_e6e2964f USING GIST (geom);
CREATE INDEX tal_e6e2964f_props_idx ON assets.tal_e6e2964f USING GIN (props);
CREATE TABLE audit.tal_e6e2964f_audit (
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
);
CREATE INDEX tal_e6e2964f_audit_asset_idx   ON audit.tal_e6e2964f_audit (asset_id);
CREATE INDEX tal_e6e2964f_audit_changed_idx ON audit.tal_e6e2964f_audit (changed_at);
CREATE TRIGGER tal_e6e2964f_audit_trigger
    AFTER INSERT OR UPDATE OR DELETE ON assets.tal_e6e2964f
    FOR EACH ROW EXECUTE FUNCTION audit.fn_record_change('audit.tal_e6e2964f_audit', 'id');
CREATE TRIGGER tal_e6e2964f_props_validate_trigger
    BEFORE INSERT OR UPDATE ON assets.tal_e6e2964f
    FOR EACH ROW EXECUTE FUNCTION assets.fn_validate_layer_props();
CREATE TRIGGER tal_e6e2964f_row_version_trigger
    BEFORE UPDATE ON assets.tal_e6e2964f
    FOR EACH ROW EXECUTE FUNCTION assets.fn_bump_row_version();
CREATE OR REPLACE FUNCTION assets.tal_e6e2964f_tile(z integer, x integer, y integer)
RETURNS bytea AS $$
  SELECT ST_AsMVT(tile, 'tal_e6e2964f', 4096, 'geom')
  FROM (
    SELECT ST_AsMVTGeom(ST_Transform(s.geom, 3857), ST_TileEnvelope(z, x, y)) AS geom, s.id::text AS id
    FROM assets.tal_e6e2964f s
    WHERE s.geom && ST_Transform(ST_TileEnvelope(z, x, y), 4326)
  ) AS tile
  WHERE tile.geom IS NOT NULL;
$$ LANGUAGE sql STABLE PARALLEL SAFE;

-- ============================================================================
-- Registry seed — one row per new slot, UNCLAIMED (status defaults to 'unclaimed';
-- working_srid 4326, native_storage false, geom_type 'point' all by column default).
-- ============================================================================
INSERT INTO platform.layers (layer_id, editable) VALUES
    ('tal_15603025', false),
    ('tal_782e4b5c', false),
    ('tal_77175dae', false),
    ('tal_0f529c77', false),
    ('tal_e6e2964f', false);
