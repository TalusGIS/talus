-- V068 — media.attachments: generic feature attachments (decision-attachments.md, A-1). Tier-2, backup-gated.
--
-- The must-have municipal capability (the sign photo, the hydrant inspection shot, the permit PDF) that
-- no hosted vector layer has today. ONE authority table holds the bytes once in MinIO keyed to the
-- feature; every client (web, ArcGIS Pro via GeoServices, QGIS via the write-chute) reads and writes THIS
-- table through the existing gated + audited + capped path. Attachment add/delete NEVER bumps a feature's
-- row_version (a side table — an attachment is not a feature edit; Esri-consistent, decision §2).
--
-- This migration also MIGRATES media.photos in (the sign-photos one-off from V006 becomes the generic
-- showcase — decision §4.1, fork 3): ids are preserved so signs.cover_photo_id keeps pointing at the same
-- row after the FK is re-homed, and signs is flipped attachments_enabled=true (it now has attachments).
--
-- Design choices restated where they matter to this file (full record: decision-attachments.md §4):
--   * attachments_enabled DEFAULT false — attachments are OPT-IN (matches ArcGIS's explicit per-layer
--     enable; keeps hasAttachments honest; conservative fence). The photos migration flips signs→true.
--   * feature_id has NO generic FK — the owning feature lives in a dynamic per-layer table (assets.<layer>);
--     integrity is the app delete-path's + the T39 orphan-blob job's job (decision §2.4). layer_id DOES FK
--     to platform.layers ON DELETE CASCADE (layer drop GCs the rows; T39 then GCs the orphaned blobs).
--   * The three capture columns carry media.photos forward so field EXIF/geotag survives.
--   * filename is the one real add vs the photos seed (the attachmentInfos.name / @selected_file_name).
--   * DURABLE AUDIT: media.attachments gets the same un-bypassable, GUC-driven audit trigger as feature
--     tables (audit.fn_record_change) writing a shadow audit.attachments_audit — so an attachment add OR
--     DELETE is forensically attributable (even a raw SQL write is caught), honoring §2.1. The trigger is
--     created AFTER the historical photos INSERT so this migration itself generates NO audit spam (the
--     V064 backfill discipline). The shadow CHECK includes 'service_account' from birth (the V065 lesson —
--     a PAT attachment write in A-2 is a service-account write and would 500 against a human-only CHECK).

-- 1) The per-layer opt-in toggle (V057 editing_enabled pattern; managed in Manage → Settings). -----------
ALTER TABLE platform.layers
    ADD COLUMN attachments_enabled boolean NOT NULL DEFAULT false;

-- 2) The one authority table. ---------------------------------------------------------------------------
CREATE TABLE media.attachments (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    layer_id        text NOT NULL REFERENCES platform.layers(layer_id) ON DELETE CASCADE,
    feature_id      uuid NOT NULL,               -- the feature's canonical uuid (assets.<layer_id>.id)
    object_key      text NOT NULL,               -- MinIO key in the talus-media bucket (attachments/ prefix)
    filename        text NOT NULL,               -- original name: attachmentInfos.name / @selected_file_name
    content_type    text NOT NULL,
    byte_size       bigint NOT NULL,
    created_by      uuid NOT NULL REFERENCES platform.users(id),
    created_at      timestamptz NOT NULL DEFAULT now(),
    -- Field-capture provenance (nullable) — carries media.photos forward; preserves EXIF geotag/time.
    captured_at     timestamptz,
    captured_geom   geometry(Point, 4326),
    device_metadata jsonb
);

CREATE INDEX attachments_feature_idx ON media.attachments (layer_id, feature_id);
CREATE INDEX attachments_geom_idx    ON media.attachments USING GIST (captured_geom);

-- 3) Migrate media.photos in — ids preserved so the cover-photo FK survives the re-home. Runs BEFORE the
--    audit trigger exists, so these historical rows generate no audit spam (a pure aliasing migration).
--    media.photos never stored a filename; synthesize a stable, honest default from id + a content-type
--    extension (the original name is unrecoverable — faithful best-effort, not a fabrication).
INSERT INTO media.attachments
    (id, layer_id, feature_id, object_key, filename, content_type, byte_size,
     created_by, created_at, captured_at, captured_geom, device_metadata)
SELECT
    p.id,
    'signs',
    p.sign_id,
    p.object_key,
    'photo-' || substr(p.id::text, 1, 8) || CASE
        WHEN p.content_type = 'image/jpeg' THEN '.jpg'
        WHEN p.content_type = 'image/png'  THEN '.png'
        WHEN p.content_type = 'image/gif'  THEN '.gif'
        WHEN p.content_type = 'image/webp' THEN '.webp'
        WHEN p.content_type = 'image/tiff' THEN '.tif'
        WHEN p.content_type = 'image/heic' THEN '.heic'
        WHEN p.content_type = 'image/heif' THEN '.heif'
        WHEN p.content_type = 'image/bmp'  THEN '.bmp'
        ELSE ''
    END,
    p.content_type,
    p.byte_size,
    p.captured_by,                 -- V006 media.photos: captured_by is the creator → created_by here
    p.created_at,
    p.captured_at,
    p.captured_geom,
    p.device_metadata
FROM media.photos p;

-- 4) Durable audit — shadow table + the generic un-bypassable trigger (created AFTER the migration INSERT).
--    Mirrors audit.street_lights_audit's shape; actor_type CHECK includes 'service_account' from birth.
CREATE TABLE audit.attachments_audit (
    audit_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    asset_id     uuid NOT NULL,                  -- the media.attachments.id
    operation    text NOT NULL CHECK (operation IN ('INSERT','UPDATE','DELETE')),
    changed_at   timestamptz NOT NULL DEFAULT now(),
    changed_by   uuid REFERENCES platform.users(id),
    actor_type   text NOT NULL DEFAULT 'human' CHECK (actor_type IN ('human','ai_inference','service_account')),
    provenance   jsonb,
    client_info  jsonb,
    before_row   jsonb,
    after_row    jsonb
);

CREATE INDEX attachments_audit_asset_idx   ON audit.attachments_audit (asset_id);
CREATE INDEX attachments_audit_changed_idx ON audit.attachments_audit (changed_at);

CREATE TRIGGER attachments_audit_trigger
AFTER INSERT OR UPDATE OR DELETE ON media.attachments
FOR EACH ROW EXECUTE FUNCTION audit.fn_record_change('audit.attachments_audit', 'id');

-- 5) Re-home the signs.cover_photo_id FK: media.photos(id) → media.attachments(id) (ids preserved above).
ALTER TABLE assets.signs DROP CONSTRAINT signs_cover_photo_fk;
ALTER TABLE assets.signs
    ADD CONSTRAINT signs_cover_photo_fk
    FOREIGN KEY (cover_photo_id) REFERENCES media.attachments(id);

-- 6) Signs now HAS attachments — turn the toggle on for the showcase layer. -----------------------------
UPDATE platform.layers SET attachments_enabled = true WHERE layer_id = 'signs';

-- 7) Drop the now-migrated one-off. The read routes (routers/media.py) + the T39 orphan-blob live-keys
--    query (maintenance.py) + the demo seed + restore-item.sh all move to media.attachments in the same
--    A-1 slice, so nothing references media.photos after this migration + code deploy land together.
DROP TABLE media.photos;
