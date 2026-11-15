-- V037 — Talus 37: admin-configured org locators (geocoder management).
-- docs/decision-geocoder-management.md §3 (F1 storage, F5 seed marker). Tier-2, backup-gated.
--
-- Moves the org's geocoder list out of the GEOCODERS env var (a deploy-time .env edit + api restart)
-- into a DB table the GIS admin manages in-portal. The /geocode proxy reads its adapter list from
-- here instead of settings.geocoders; the /geocode/* API contract is unchanged. Locators stay
-- org-wide / shared-by-default (this table feeds the existing all-authenticated-users endpoints).
--
-- TOKENLESS v1 (F2): there is deliberately NO token column, so this slice introduces no
-- secret-at-rest seam. A token-bearing locator stays in .env and is still honored by the proxy via
-- an env-union (DB rows ∪ token-bearing env entries not shadowed by a DB name) until the successor
-- slice lands the encrypted write-only secret column. The startup seed backfills only TOKENLESS env
-- entries.

CREATE TABLE platform.geocoders (
    id         uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    name       text        NOT NULL UNIQUE,            -- client-facing `source` + the "Search in" picker key
    type       text        NOT NULL CHECK (type IN ('arcgis', 'nominatim')),
    url        text        NOT NULL,
    out_sr     integer     NOT NULL DEFAULT 4326,      -- SR requested from the service (v1 contract = 4326)
    -- Optional fixed search extent [xmin, ymin, xmax, ymax] in EPSG:4326 — restricts matches to (e.g.)
    -- the county boundary so a bare "100 Main" can't match worldwide. Validated app-side (4 numbers).
    extent     jsonb,
    sort_order integer     NOT NULL DEFAULT 0,         -- ascending = tried first (point locator before street)
    enabled    boolean     NOT NULL DEFAULT true,      -- disabled = excluded from the proxy walk + the picker
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by uuid        REFERENCES platform.users(id)
);

COMMENT ON TABLE platform.geocoders IS
    'Talus 37: admin-managed external locators the /geocode proxy consumes (org-wide, shared by '
    'default). Tokenless v1 — no secret column; token-bearing locators stay env-only via the proxy '
    'env-union until the successor slice. See docs/decision-geocoder-management.md.';

-- One-time env→DB backfill marker (F5). Set true after the first startup seed — EVEN IF the env was
-- empty — so the backfill is genuinely one-time: an admin who later deletes every locator gets an
-- empty list that STAYS empty (env never resurrects it on the next boot).
ALTER TABLE platform.org_settings
    ADD COLUMN geocoders_seeded boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN platform.org_settings.geocoders_seeded IS
    'Talus 37: true once the one-time env GEOCODERS → platform.geocoders backfill has run (set even '
    'when env was empty, so deleted locators are never resurrected). See decision-geocoder-management §3 F5.';
