-- V090 — admin-choosable backup destination (decision-backup-destination.md).
-- Tier-2, backup-gated (HARD RULE #1).
--
-- Talus 43 made the editable backup knobs DB overrides (V042, COALESCE-over-env); the off-host
-- S3 destination stayed env-only ("the app's first secret-at-rest is its own slice"). This is
-- that slice: the BACKUP_S3_* trio (+ bucket) becomes a nullable override GROUP on the same
-- singleton, so a non-technical admin can point backups off-host from Admin -> Backups with no
-- .env edit and no restart — the post-MinIO DR story (decision-object-store.md F1/R2).
--
-- F5 (group atomicity): endpoint / access key / secret key are all set or all NULL — the DB can
-- never hold the partial state whose env twin deliberately raises (storage.build_backup_storage).
-- Bucket is optional WITHIN the group (NULL = env backup_bucket) and NULL outside it.
--
-- F4 (fence, defense-in-depth): the endpoint must carry an explicit http:// or https:// scheme —
-- the API validates first (422); this CHECK pins it at rest so a scheme-less value can never
-- reach the client constructor as secure=False plaintext-by-accident.
--
-- Secret at rest: s3_secret_key is Fernet CIPHERTEXT (api/app/secrets.py, TALUS_ENCRYPTION_KEY —
-- the house pattern, second consumer as planned). Never plaintext in the DB, never returned by
-- the API, never logged. NULL group = "use the BACKUP_S3_* env defaults" (F-precedence).

ALTER TABLE platform.backup_settings
    ADD COLUMN s3_endpoint   text
        CHECK (s3_endpoint IS NULL OR s3_endpoint ~ '^https?://'),
    ADD COLUMN s3_bucket     text,
    ADD COLUMN s3_access_key text,
    ADD COLUMN s3_secret_key bytea;

ALTER TABLE platform.backup_settings
    ADD CONSTRAINT backup_destination_group_atomic CHECK (
        (s3_endpoint IS NULL AND s3_access_key IS NULL AND s3_secret_key IS NULL
             AND s3_bucket IS NULL)
        OR
        (s3_endpoint IS NOT NULL AND s3_access_key IS NOT NULL AND s3_secret_key IS NOT NULL)
    );

COMMENT ON COLUMN platform.backup_settings.s3_endpoint IS
    'Off-host S3 destination override (NULL = use BACKUP_S3_* env). Explicit http(s):// scheme required. '
    'All-or-nothing with s3_access_key + s3_secret_key (backup_destination_group_atomic).';
COMMENT ON COLUMN platform.backup_settings.s3_bucket IS
    'Bucket at the override destination; NULL = env BACKUP_BUCKET. Only meaningful inside the override group.';
COMMENT ON COLUMN platform.backup_settings.s3_access_key IS
    'Access key id for the override destination (an identifier, shown to admins; the SECRET is the other column).';
COMMENT ON COLUMN platform.backup_settings.s3_secret_key IS
    'Fernet CIPHERTEXT of the S3 secret key (api/app/secrets.py over TALUS_ENCRYPTION_KEY). '
    'Never plaintext, never returned by the API, never logged.';
