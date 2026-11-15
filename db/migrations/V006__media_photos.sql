-- V006 — media.photos + the deferred signs.cover_photo_id FK
--
-- Photos are immutable once captured. The "edit a photo" workflow is "capture a new one
-- and the old becomes history". Per-asset FK (sign_id) — generic media_objects deferred.

CREATE TABLE media.photos (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    sign_id         uuid NOT NULL REFERENCES assets.signs(id) ON DELETE CASCADE,
    object_key      text NOT NULL,
    content_type    text NOT NULL,
    byte_size       bigint NOT NULL,
    captured_at     timestamptz,
    captured_by     uuid NOT NULL REFERENCES platform.users(id),
    captured_geom   geometry(Point, 4326),
    device_metadata jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX photos_sign_id_idx ON media.photos (sign_id);
CREATE INDEX photos_geom_idx    ON media.photos USING GIST (captured_geom);

ALTER TABLE assets.signs
    ADD CONSTRAINT signs_cover_photo_fk
    FOREIGN KEY (cover_photo_id) REFERENCES media.photos(id);
