-- V105 — Talus Field packs (decision-talus-field-f3.md §4.1 / §7). Tier-2, backup-gated.
--
-- A pack is a portal-authored, build-once / download-many offline bundle for ONE web map: an
-- extent, a basemap zoom ceiling, a TTL. The row is the authority; the bytes live under the media
-- bucket at field-packs/<pack_id>/build/<n>/... (the storage seam), written by the advisory-locked
-- pack worker (paid — instance five of the reports/import/geocode pattern) and served file-by-file
-- through gated doors that re-check the DOWNLOADER's view rights per layer file. The manifest
-- (jsonb) freezes the per-layer schema at build time so a form change reaches a phone with the
-- next build, never mid-day.
--
-- Tables live in the CORE schema on purpose (V100's reasoning): schema is infrastructure, not a
-- paid feature; only the code that gives these rows meaning is paid. Deleting the web map deletes
-- its packs (CASCADE) — the worker's orphan sweep then collects the bytes.

CREATE TABLE platform.field_packs (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    web_map_id       uuid NOT NULL REFERENCES platform.web_maps(id) ON DELETE CASCADE,
    title            text NOT NULL CHECK (length(btrim(title)) BETWEEN 1 AND 120),
    created_by       uuid NOT NULL REFERENCES platform.users(id),

    -- The area + detail the crew lead asked for. bbox is WGS-84 [w, s, e, n], validated like the
    -- basemap fetch; basemap_maxzoom is the clip's zoom ceiling (14 streets · 15 lots · 16 detail).
    bbox             double precision[] NOT NULL CHECK (array_length(bbox, 1) = 4),
    basemap_maxzoom  integer NOT NULL CHECK (basemap_maxzoom BETWEEN 12 AND 16),
    ttl_days         integer NOT NULL DEFAULT 7 CHECK (ttl_days BETWEEN 1 AND 30),

    status           text NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'building', 'ready', 'failed', 'expired')),
    -- Which build/<n>/ prefix the manifest points at; bumps per rebuild so a half-written build
    -- can never replace a served one (the previous prefix is deleted AFTER the new manifest lands).
    build_no         integer NOT NULL DEFAULT 0 CHECK (build_no >= 0),
    manifest         jsonb,                 -- §4.2; NULL until the first successful build
    estimate_bytes   bigint CHECK (estimate_bytes IS NULL OR estimate_bytes >= 0),
    size_bytes       bigint CHECK (size_bytes IS NULL OR size_bytes >= 0),
    built_at         timestamptz,
    expires_at       timestamptz,           -- built_at + ttl_days; the 410 line and the phone's banner
    error            text,                  -- terminal build error, bounded by the worker

    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT field_packs_map_title_uniq UNIQUE (web_map_id, title)
);

CREATE INDEX field_packs_map_idx ON platform.field_packs (web_map_id, created_at DESC);
-- The expiry job's scan: ready packs past their date.
CREATE INDEX field_packs_expiry_idx ON platform.field_packs (expires_at)
    WHERE status = 'ready';

COMMENT ON TABLE platform.field_packs IS
    'Talus Field offline packs (decision-talus-field-f3.md). One per (web map, title); bytes at '
    'field-packs/<id>/build/<build_no>/ on the media bucket; the manifest freezes the layer schema '
    'at build time. Served file-by-file behind per-layer view checks; 410 after expires_at.';

-- One row per build request (create or rebuild) — the V100 report_jobs shape: FIFO over live
-- jobs, claimed one-at-a-time by the pack worker, run as the pack's AUTHOR (no admin override).
CREATE TABLE platform.field_pack_jobs (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    pack_id       uuid NOT NULL REFERENCES platform.field_packs(id) ON DELETE CASCADE,
    status        text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'running', 'completed', 'failed', 'canceled')),
    requested_by  uuid REFERENCES platform.users(id),
    error         text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    started_at    timestamptz,
    finished_at   timestamptz,
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX field_pack_jobs_active_idx ON platform.field_pack_jobs (created_at)
    WHERE status IN ('pending', 'running');
CREATE INDEX field_pack_jobs_pack_idx ON platform.field_pack_jobs (pack_id, created_at DESC);

COMMENT ON TABLE platform.field_pack_jobs IS
    'Build queue for Talus Field packs. Claimed one-at-a-time by the advisory-locked pack worker; '
    'builds as the pack author regardless of requested_by (attribution, never authorization).';
