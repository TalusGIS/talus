-- V019 — publisher slot pool (Layer 8, Commit 1).
--
-- Pre-mints 5 identical, empty point-layer "slots" that the create-layer endpoint
-- (Layer 8) hands to a publisher at runtime. Each slot is a full, ordinary asset —
-- table + GiST index + 9-column shadow audit + trigger + lean MVT tile function —
-- built HERE in Flyway, exactly like every existing asset (V005/V013). There is NO
-- runtime DDL: claiming a slot never issues CREATE; it only flips registry state and
-- writes feature rows into the already-existing assets.<tok> table.
--
-- The 5 slot ids are FROZEN literals (tal_<8 hex>). They are deliberately identical
-- and copy-paste-uniform — the repetition below is the point, not an accident. The
-- slots ship EMPTY: this migration writes no feature/row data into assets.<tok>.
--
-- Lifting the ceiling later (6th slot, 10th slot, ...) = adding more slot blocks in a
-- future migration, the same way new assets have always been added. No schema here is
-- slot-pool-specific; each block is just another asset that happens to start unnamed.
--
-- Each slot block (repeated 5x) does:
--   1) assets.<tok>            — point asset, platform edit-tracking conventions (V005)
--   2) <tok>_geom_idx          — GiST on geom
--   3) audit.<tok>_audit       — 9-column post-V012 shadow audit + 2 indexes (V013)
--   4) <tok>_audit_trigger     — generic audit.fn_record_change(), function UNCHANGED
--   5) assets.<tok>_tile(z,x,y)— lean MVT point tile, id only (V011)
-- Then 5 registry rows are seeded into platform.layers at the UNCLAIMED state.

-- ============================================================================
-- Slot 1 — tal_7b3aa14c
-- ============================================================================

CREATE TABLE assets.tal_7b3aa14c (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    geom           geometry(Point, 4326) NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    created_by     uuid NOT NULL REFERENCES platform.users(id),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    updated_by     uuid NOT NULL REFERENCES platform.users(id),
    ai_provenance  jsonb,
    notes          text
);

CREATE INDEX tal_7b3aa14c_geom_idx ON assets.tal_7b3aa14c USING GIST (geom);

CREATE TABLE audit.tal_7b3aa14c_audit (
    audit_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    asset_id     uuid NOT NULL,
    operation    text NOT NULL CHECK (operation IN ('INSERT','UPDATE','DELETE')),
    changed_at   timestamptz NOT NULL DEFAULT now(),
    changed_by   uuid REFERENCES platform.users(id),
    actor_type   text NOT NULL DEFAULT 'human' CHECK (actor_type IN ('human','ai_inference')),
    provenance   jsonb,
    client_info  jsonb,
    before_row   jsonb,
    after_row    jsonb
);

CREATE INDEX tal_7b3aa14c_audit_asset_idx   ON audit.tal_7b3aa14c_audit (asset_id);
CREATE INDEX tal_7b3aa14c_audit_changed_idx ON audit.tal_7b3aa14c_audit (changed_at);

CREATE TRIGGER tal_7b3aa14c_audit_trigger
AFTER INSERT OR UPDATE OR DELETE ON assets.tal_7b3aa14c
FOR EACH ROW EXECUTE FUNCTION audit.fn_record_change('audit.tal_7b3aa14c_audit', 'id');

CREATE OR REPLACE FUNCTION assets.tal_7b3aa14c_tile(z integer, x integer, y integer)
RETURNS bytea
AS $$
  SELECT ST_AsMVT(tile, 'tal_7b3aa14c', 4096, 'geom')
  FROM (
    SELECT
        ST_AsMVTGeom(ST_Transform(s.geom, 3857), ST_TileEnvelope(z, x, y)) AS geom,
        s.id::text AS id
    FROM assets.tal_7b3aa14c s
    WHERE s.geom && ST_Transform(ST_TileEnvelope(z, x, y), 4326)
  ) AS tile
  WHERE tile.geom IS NOT NULL;
$$ LANGUAGE sql STABLE PARALLEL SAFE;

-- ============================================================================
-- Slot 2 — tal_6496fe7d
-- ============================================================================

CREATE TABLE assets.tal_6496fe7d (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    geom           geometry(Point, 4326) NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    created_by     uuid NOT NULL REFERENCES platform.users(id),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    updated_by     uuid NOT NULL REFERENCES platform.users(id),
    ai_provenance  jsonb,
    notes          text
);

CREATE INDEX tal_6496fe7d_geom_idx ON assets.tal_6496fe7d USING GIST (geom);

CREATE TABLE audit.tal_6496fe7d_audit (
    audit_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    asset_id     uuid NOT NULL,
    operation    text NOT NULL CHECK (operation IN ('INSERT','UPDATE','DELETE')),
    changed_at   timestamptz NOT NULL DEFAULT now(),
    changed_by   uuid REFERENCES platform.users(id),
    actor_type   text NOT NULL DEFAULT 'human' CHECK (actor_type IN ('human','ai_inference')),
    provenance   jsonb,
    client_info  jsonb,
    before_row   jsonb,
    after_row    jsonb
);

CREATE INDEX tal_6496fe7d_audit_asset_idx   ON audit.tal_6496fe7d_audit (asset_id);
CREATE INDEX tal_6496fe7d_audit_changed_idx ON audit.tal_6496fe7d_audit (changed_at);

CREATE TRIGGER tal_6496fe7d_audit_trigger
AFTER INSERT OR UPDATE OR DELETE ON assets.tal_6496fe7d
FOR EACH ROW EXECUTE FUNCTION audit.fn_record_change('audit.tal_6496fe7d_audit', 'id');

CREATE OR REPLACE FUNCTION assets.tal_6496fe7d_tile(z integer, x integer, y integer)
RETURNS bytea
AS $$
  SELECT ST_AsMVT(tile, 'tal_6496fe7d', 4096, 'geom')
  FROM (
    SELECT
        ST_AsMVTGeom(ST_Transform(s.geom, 3857), ST_TileEnvelope(z, x, y)) AS geom,
        s.id::text AS id
    FROM assets.tal_6496fe7d s
    WHERE s.geom && ST_Transform(ST_TileEnvelope(z, x, y), 4326)
  ) AS tile
  WHERE tile.geom IS NOT NULL;
$$ LANGUAGE sql STABLE PARALLEL SAFE;

-- ============================================================================
-- Slot 3 — tal_2dbf236f
-- ============================================================================

CREATE TABLE assets.tal_2dbf236f (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    geom           geometry(Point, 4326) NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    created_by     uuid NOT NULL REFERENCES platform.users(id),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    updated_by     uuid NOT NULL REFERENCES platform.users(id),
    ai_provenance  jsonb,
    notes          text
);

CREATE INDEX tal_2dbf236f_geom_idx ON assets.tal_2dbf236f USING GIST (geom);

CREATE TABLE audit.tal_2dbf236f_audit (
    audit_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    asset_id     uuid NOT NULL,
    operation    text NOT NULL CHECK (operation IN ('INSERT','UPDATE','DELETE')),
    changed_at   timestamptz NOT NULL DEFAULT now(),
    changed_by   uuid REFERENCES platform.users(id),
    actor_type   text NOT NULL DEFAULT 'human' CHECK (actor_type IN ('human','ai_inference')),
    provenance   jsonb,
    client_info  jsonb,
    before_row   jsonb,
    after_row    jsonb
);

CREATE INDEX tal_2dbf236f_audit_asset_idx   ON audit.tal_2dbf236f_audit (asset_id);
CREATE INDEX tal_2dbf236f_audit_changed_idx ON audit.tal_2dbf236f_audit (changed_at);

CREATE TRIGGER tal_2dbf236f_audit_trigger
AFTER INSERT OR UPDATE OR DELETE ON assets.tal_2dbf236f
FOR EACH ROW EXECUTE FUNCTION audit.fn_record_change('audit.tal_2dbf236f_audit', 'id');

CREATE OR REPLACE FUNCTION assets.tal_2dbf236f_tile(z integer, x integer, y integer)
RETURNS bytea
AS $$
  SELECT ST_AsMVT(tile, 'tal_2dbf236f', 4096, 'geom')
  FROM (
    SELECT
        ST_AsMVTGeom(ST_Transform(s.geom, 3857), ST_TileEnvelope(z, x, y)) AS geom,
        s.id::text AS id
    FROM assets.tal_2dbf236f s
    WHERE s.geom && ST_Transform(ST_TileEnvelope(z, x, y), 4326)
  ) AS tile
  WHERE tile.geom IS NOT NULL;
$$ LANGUAGE sql STABLE PARALLEL SAFE;

-- ============================================================================
-- Slot 4 — tal_2c09dee7
-- ============================================================================

CREATE TABLE assets.tal_2c09dee7 (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    geom           geometry(Point, 4326) NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    created_by     uuid NOT NULL REFERENCES platform.users(id),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    updated_by     uuid NOT NULL REFERENCES platform.users(id),
    ai_provenance  jsonb,
    notes          text
);

CREATE INDEX tal_2c09dee7_geom_idx ON assets.tal_2c09dee7 USING GIST (geom);

CREATE TABLE audit.tal_2c09dee7_audit (
    audit_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    asset_id     uuid NOT NULL,
    operation    text NOT NULL CHECK (operation IN ('INSERT','UPDATE','DELETE')),
    changed_at   timestamptz NOT NULL DEFAULT now(),
    changed_by   uuid REFERENCES platform.users(id),
    actor_type   text NOT NULL DEFAULT 'human' CHECK (actor_type IN ('human','ai_inference')),
    provenance   jsonb,
    client_info  jsonb,
    before_row   jsonb,
    after_row    jsonb
);

CREATE INDEX tal_2c09dee7_audit_asset_idx   ON audit.tal_2c09dee7_audit (asset_id);
CREATE INDEX tal_2c09dee7_audit_changed_idx ON audit.tal_2c09dee7_audit (changed_at);

CREATE TRIGGER tal_2c09dee7_audit_trigger
AFTER INSERT OR UPDATE OR DELETE ON assets.tal_2c09dee7
FOR EACH ROW EXECUTE FUNCTION audit.fn_record_change('audit.tal_2c09dee7_audit', 'id');

CREATE OR REPLACE FUNCTION assets.tal_2c09dee7_tile(z integer, x integer, y integer)
RETURNS bytea
AS $$
  SELECT ST_AsMVT(tile, 'tal_2c09dee7', 4096, 'geom')
  FROM (
    SELECT
        ST_AsMVTGeom(ST_Transform(s.geom, 3857), ST_TileEnvelope(z, x, y)) AS geom,
        s.id::text AS id
    FROM assets.tal_2c09dee7 s
    WHERE s.geom && ST_Transform(ST_TileEnvelope(z, x, y), 4326)
  ) AS tile
  WHERE tile.geom IS NOT NULL;
$$ LANGUAGE sql STABLE PARALLEL SAFE;

-- ============================================================================
-- Slot 5 — tal_57b25bf6
-- ============================================================================

CREATE TABLE assets.tal_57b25bf6 (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    geom           geometry(Point, 4326) NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    created_by     uuid NOT NULL REFERENCES platform.users(id),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    updated_by     uuid NOT NULL REFERENCES platform.users(id),
    ai_provenance  jsonb,
    notes          text
);

CREATE INDEX tal_57b25bf6_geom_idx ON assets.tal_57b25bf6 USING GIST (geom);

CREATE TABLE audit.tal_57b25bf6_audit (
    audit_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    asset_id     uuid NOT NULL,
    operation    text NOT NULL CHECK (operation IN ('INSERT','UPDATE','DELETE')),
    changed_at   timestamptz NOT NULL DEFAULT now(),
    changed_by   uuid REFERENCES platform.users(id),
    actor_type   text NOT NULL DEFAULT 'human' CHECK (actor_type IN ('human','ai_inference')),
    provenance   jsonb,
    client_info  jsonb,
    before_row   jsonb,
    after_row    jsonb
);

CREATE INDEX tal_57b25bf6_audit_asset_idx   ON audit.tal_57b25bf6_audit (asset_id);
CREATE INDEX tal_57b25bf6_audit_changed_idx ON audit.tal_57b25bf6_audit (changed_at);

CREATE TRIGGER tal_57b25bf6_audit_trigger
AFTER INSERT OR UPDATE OR DELETE ON assets.tal_57b25bf6
FOR EACH ROW EXECUTE FUNCTION audit.fn_record_change('audit.tal_57b25bf6_audit', 'id');

CREATE OR REPLACE FUNCTION assets.tal_57b25bf6_tile(z integer, x integer, y integer)
RETURNS bytea
AS $$
  SELECT ST_AsMVT(tile, 'tal_57b25bf6', 4096, 'geom')
  FROM (
    SELECT
        ST_AsMVTGeom(ST_Transform(s.geom, 3857), ST_TileEnvelope(z, x, y)) AS geom,
        s.id::text AS id
    FROM assets.tal_57b25bf6 s
    WHERE s.geom && ST_Transform(ST_TileEnvelope(z, x, y), 4326)
  ) AS tile
  WHERE tile.geom IS NOT NULL;
$$ LANGUAGE sql STABLE PARALLEL SAFE;

-- ============================================================================
-- Registry seed — one row per slot, UNCLAIMED.
-- ============================================================================
--
-- editable=false: an unclaimed slot must never accept edits. The claim flow flips
-- it to true. display_name and status columns don't exist yet — they're added in
-- V020 (Commit 2), which backfills these 5 rows to status='unclaimed'. So this seed
-- is intentionally just layer_id + editable for now.
INSERT INTO platform.layers (layer_id, editable) VALUES
    ('tal_7b3aa14c', false),
    ('tal_6496fe7d', false),
    ('tal_2dbf236f', false),
    ('tal_2c09dee7', false),
    ('tal_57b25bf6', false);
