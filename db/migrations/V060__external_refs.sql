-- V060 — external catalog items (decision-external-catalog-items.md).
--
-- A referenced external service (the shipped add-from-url ExternalLayerSpec) promoted to a
-- shareable CATALOG ITEM: one row = one org-curated pointer to a remote public service. It is a
-- POINTER, not a table — no assets.<id> relation, no slot claimed, no triggers, no audit shadow
-- (the item's own lifecycle is audited via structlog like web_maps). Sharing/ownership ride the
-- existing `layer:<id>` OpenFGA object seam (fork §3.4 — ids are prefix-disjoint from layers),
-- so the share dialog, favorites (item_type 'external'), and transfer apply unchanged.
--
-- Column semantics mirror the OperationalLayer external arm (web_maps.py) — pydantic is the
-- semantic validator; these CHECKs are the belt for direct-SQL writes. url/where_clause are never
-- interpolated into HTML or SQL — they only feed the VIEWER'S browser fetch/MapLibre against the
-- REMOTE server (the no-proxy fence: our server never fetches them).

CREATE TABLE platform.external_refs (
  id            text PRIMARY KEY
                CHECK (id ~ '^xref_[0-9a-f]{8}$'),          -- app-generated, collision-checked
  owner_id      uuid NOT NULL REFERENCES platform.users(id), -- kept in step with the FGA owner
                                                             -- tuple (transfer updates both)
  title         text NOT NULL CHECK (length(title) BETWEEN 1 AND 80),
  description   text CHECK (length(description) <= 2000),
  external_kind text NOT NULL CHECK (external_kind IN
                  ('arcgis-feature','geojson','xyz','arcgis-image')),
  url           text NOT NULL CHECK (url ~ '^https?://' AND length(url) <= 2000),
  geometry_type text CHECK (geometry_type IN ('point','line','polygon')),  -- vector kinds
  color         text CHECK (color ~ '^#[0-9a-fA-F]{6}$'),                  -- vector kinds
  where_clause  text CHECK (length(where_clause) <= 2000),  -- arcgis-feature: sent as a bound
                                                             -- query param to the REMOTE server
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

-- The Owned list / owner attribution scan.
CREATE INDEX external_refs_owner_idx ON platform.external_refs (owner_id);

COMMENT ON TABLE platform.external_refs IS
  'decision-external-catalog-items.md: referenced external services as shareable catalog items. '
  'Read-only pointers (browser-direct fetch, no proxy, no slot); OpenFGA object layer:<id>; maps '
  'store the item id + a spec snapshot and heal via the snapshot if the item is deleted/unshared.';
