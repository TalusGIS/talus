-- V073 — Geocoder token locators (secrets-at-rest v1).
-- docs/decision-geocoder-token-locators.md §4. Tier-2, backup-gated.
--
-- Closes the Talus 37 §3 F2 deferral: a token-secured locator becomes admin-manageable in the UI
-- instead of env-only. The credential is stored as APP-SIDE Fernet ciphertext (api/app/secrets.py,
-- key = TALUS_ENCRYPTION_KEY in .env) — the key never enters the DB and the plaintext never transits
-- SQL, so a pg_dump/backup (and pg statement logs) never contain a usable token. NULL = no
-- credential; only type='arcgis' rows carry one (app-enforced — the token= query param is the only
-- auth mode any adapter speaks).

ALTER TABLE platform.geocoders
    ADD COLUMN token_ciphertext bytea,
    ADD COLUMN token_hint       text;

COMMENT ON COLUMN platform.geocoders.token_ciphertext IS
    'Fernet ciphertext of the locator''s static ArcGIS token (secrets-at-rest v1). Decryptable only '
    'with TALUS_ENCRYPTION_KEY from the deployment''s .env — neither the DB nor a backup alone is '
    'the secret. Write-only via the API (never echoed). See docs/decision-geocoder-token-locators.md.';

COMMENT ON COLUMN platform.geocoders.token_hint IS
    'Last 4 characters of the stored token, for admin legibility ("which token is set?"). '
    'Deliberately plaintext — 4 trailing characters are not a usable credential.';
