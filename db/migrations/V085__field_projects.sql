-- V085 — platform.field_projects (qgz intake & Import-URL door, decision-field-projects.md F1).
-- Tier-2, backup-gated.
--
-- A "Field project": an uploaded .qgz stored as an OPAQUE artifact in MinIO (talus-media
-- bucket, key field-projects/<id>) and served to QField's Import URL through a credentialed
-- byte door. Talus never opens or edits the content (THE FENCE) — the sniff columns below are
-- best-effort cataloging read at upload time, never a parse the artifact depends on.
--
-- Mirrors platform.rasters + T26 ownership (owned, transferable, OpenFGA-gated item):
--   * id            app-generated uuid; create is OpenFGA-FIRST (uuid -> owner tuple on
--                   field_project:<id> -> store bytes -> insert row), full rollback on failure.
--   * created_by    IMMUTABLE creator / audit anchor — never rewritten on transfer.
--   * owner_id      MUTABLE owner (T26 transfer); "Owned" list keys on it (pure DB query).
--   * sharing       NO columns — org/group/public shares are OpenFGA tuples only.
--   * no status     — unlike rasters there is no ingest lifecycle: bytes are fully validated
--                   BEFORE anything durable exists, so a row always points at a served object.
--
-- Replace-in-place (F4): one object_key per item, overwritten on replace; content_sha256 /
-- size_bytes / updated_at roll together and content_sha256 is the serving door's strong ETag
-- (Last-Modified = updated_at). No version history — Talus is distribution, not VCS.

CREATE TABLE platform.field_projects (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    title            text NOT NULL,
    description      text,

    -- MinIO location + artifact identity (the ETag pair)
    bucket           text NOT NULL,
    object_key       text NOT NULL,
    size_bytes       bigint NOT NULL,
    content_sha256   text NOT NULL,

    -- best-effort metadata sniff (F6) — cataloging, not opening; blanks on any parse failure
    qgs_title        text,
    layer_count      int,

    -- upload-lip warnings (F5): non-blocking hazards surfaced in the catalog/Manage UI,
    -- e.g. ["multiple_qgs", "os_junk", "foreign_datasources", "unreadable_metadata"]
    warnings         jsonb NOT NULL DEFAULT '[]'::jsonb,

    -- ownership / attribution (mirrors rasters/web_maps + T26)
    created_by       uuid NOT NULL REFERENCES platform.users(id),
    owner_id         uuid NOT NULL REFERENCES platform.users(id),
    updated_by       uuid NOT NULL REFERENCES platform.users(id),
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),

    -- the T24 padlock (V029/V050 parity)
    delete_protected boolean NOT NULL DEFAULT false,

    CONSTRAINT field_projects_title_not_blank_chk CHECK (length(btrim(title)) > 0),
    CONSTRAINT field_projects_title_len_chk CHECK (length(title) <= 80),
    CONSTRAINT field_projects_description_len_chk
        CHECK (description IS NULL OR length(description) <= 2000),
    CONSTRAINT field_projects_size_positive_chk CHECK (size_bytes > 0),
    CONSTRAINT field_projects_sha256_shape_chk CHECK (content_sha256 ~ '^[0-9a-f]{64}$')
);

-- "Owned" lists by owner_id; created_by mirrors the rasters/web_maps creator index.
CREATE INDEX field_projects_owner_id_idx ON platform.field_projects (owner_id);
CREATE INDEX field_projects_created_by_idx ON platform.field_projects (created_by);
