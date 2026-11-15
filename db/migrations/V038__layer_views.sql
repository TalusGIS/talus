-- V038 — Talus 38: view layers (GIS-team-authored sanitized lenses).
-- docs/decision-view-layers.md §4. Tier-2, backup-gated.
--
-- A "view layer" is a filtered, column-limited lens over a SOURCE layer, registered as its own
-- platform.layers item with its own OpenFGA `layer` object and its own sharing. The flagship case:
-- a private internal `permits` layer + a PUBLIC view exposing only status='approved' rows and
-- public-safe columns. This is the on-thesis replacement for per-user row authz (decided OUT): the
-- SYSTEM never filters rows per logged-in user — the GIS team BAKES the filter into a view and shares
-- the view ("sanitizing is the GIS team's job").
--
-- This table stores the view DEFINITION (source + column allow-list + filter). The actual PG VIEW
-- (assets.vw_<hex>) is created at RUNTIME by api/app/layer_views.py — F-DDL, the app's FIRST DDL
-- outside Flyway, scoped to CREATE/DROP VIEW assets.vw_* with server-generated names + sql.Identifier
-- columns + a WHERE built only from the validated field:op:value grammar, all inside the registration
-- transaction. It can't be pre-minted here because source/columns/filter
-- are arbitrary. The def lives in PG (not just the app) so a view is reproducible + auditable — you
-- can't reliably reverse-engineer the column/filter intent from the PG object alone — and so its FKs
-- enforce F-lifecycle at the DB level.
--
-- The sanitization GUARANTEE is DB-enforced (F-A): the PG view only CONTAINS the allow-listed columns
-- + WHERE-passing rows, so even an app-code bug (or a `SELECT *`) can't leak what isn't in the view.
-- The generic /layers/{id}/features + /export paths introspect assets.<view_id> → only the view's
-- columns exist → query params can only NARROW. Authz gates on the VIEW's own OpenFGA object,
-- independent of the source. No OpenFGA model change (a view reuses the existing `layer` object type).

CREATE TABLE platform.layer_views (
    -- The view's own layer id (vw_<hex>) — a real platform.layers row. ON DELETE CASCADE: deleting the
    -- view's registry row (via DELETE /layer-views/{id}) auto-removes this def.
    view_layer_id   text PRIMARY KEY REFERENCES platform.layers(layer_id) ON DELETE CASCADE,
    -- The source layer this view filters. ON DELETE RESTRICT: a source's registry row can't be deleted
    -- while a view depends on it (defense-in-depth for F-lifecycle). NOTE: slot RELEASE flips a slot to
    -- 'unclaimed' rather than DELETEing the row, so this FK does NOT fire on release — release_layer
    -- carries an explicit app-level "N views depend on this" guard (decision §F-lifecycle).
    source_layer_id text NOT NULL REFERENCES platform.layers(layer_id) ON DELETE RESTRICT,
    -- The column allow-list — the only columns the view SELECTs (id + geom auto-included by the builder
    -- when present in the source: a uuid id leaks nothing and feature identity/sort need it; geom is
    -- required for map render). Everything NOT here is physically absent from the PG view.
    allowed_columns text[] NOT NULL,
    -- The baked WHERE, as the same validated [{field,op,value}] grammar the feature endpoint filters
    -- with (ops in in/eq/gte/lte/ilike). The persistent filter floor; query-param filters only narrow.
    filter_def      jsonb  NOT NULL DEFAULT '[]'::jsonb,
    created_by      uuid REFERENCES platform.users(id),
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- Backs the release_layer dependency guard ("does any view depend on this source?") + cascade lookups.
CREATE INDEX layer_views_source_idx ON platform.layer_views (source_layer_id);
