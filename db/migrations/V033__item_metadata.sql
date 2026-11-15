-- V033 — Talus 31: metadata subsystem (decision-metadata.md). Tier-2, backup-gated.
-- A type-agnostic, ISO-19115-cored metadata record per catalog item (layer / web_map / future raster).
-- One shared table so rasters drop in for free and the catalog can search/sort cheaply. No OpenFGA
-- model change; no down -v.

-- doc = the ISO-19115-cored record (JSON shape per decision-metadata.md §5). title/abstract are
-- GENERATED from doc so the catalog reads them as plain columns with NO second source of truth (the
-- editor writes only doc). item_id holds a layer_id OR a web_map id::text — heterogeneous target, so
-- referential integrity to the item is app-enforced (the layer/map delete paths delete this row; an
-- orphan is harmless, read-through by (type,id)). updated_by is platform.users.id; NULL = system.
CREATE TABLE platform.item_metadata (
    item_type   text NOT NULL,                              -- 'layer' | 'web_map' | (future 'raster')
    item_id     text NOT NULL,                              -- layer_id, or web_map id::text
    doc         jsonb NOT NULL DEFAULT '{}'::jsonb,
    title       text GENERATED ALWAYS AS (doc->>'title')    STORED,
    abstract    text GENERATED ALWAYS AS (doc->>'abstract') STORED,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  uuid,
    PRIMARY KEY (item_type, item_id)
);

-- Case-insensitive title search/sort for the catalog + Open Data list.
CREATE INDEX item_metadata_title_idx ON platform.item_metadata (lower(title));
