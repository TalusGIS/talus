-- V024 — platform.users: principal_type + kc_client_id (Talus 19, commit 2).
--
-- Admits NON-HUMAN principals (service accounts) as first-class identities. Until now
-- every platform.users row was a person (V002: "populated lazily on first login from
-- Keycloak sub"). A service account (Keycloak client-credentials grant) needs a row too
-- — not by choice but by FORCE: every asset/slot table's created_by/updated_by is
-- `uuid NOT NULL REFERENCES platform.users(id)` (V005/V013/V019/V020), so a principal
-- that writes MUST resolve to a users row or the INSERT fails. (decision-19 §2.5, Q2.)
--
-- So the row is mandatory; this migration only MARKS it non-human, keeping platform.users
-- the SINGLE FK target for every actor column (no parallel principals table — that would
-- force a polymorphic FK; decision-19 Q2 rejected it).
--
--   principal_type  'human' (default) | 'service'. NOT NULL DEFAULT 'human' backfills
--                   every existing row to 'human' in place — no separate UPDATE. Mirrors
--                   V012's audit actor_type precedent.
--   kc_client_id    the Keycloak clientId for a service principal (e.g. 'talus-sa-demo'),
--                   so a row maps back to its client for revoke/rotate/admin views. NULL
--                   for humans.
--
-- api/app/auth.py is UNCHANGED in its admit LOGIC by this migration: get_current_user still
-- resolves sub -> users row; the row now simply EXISTS for a provisioned service account
-- (inserted by scripts/bootstrap-19-service-accounts.sh). An UN-provisioned sub is still
-- rejected (no_platform_user_row) — fail-closed posture preserved. principal_type is for
-- audit attribution (commit 3: actor_type='service_account') and admin filtering, NOT the
-- auth gate.

ALTER TABLE platform.users
    ADD COLUMN principal_type text NOT NULL DEFAULT 'human';

ALTER TABLE platform.users
    ADD CONSTRAINT users_principal_type_chk
    CHECK (principal_type IN ('human', 'service'));

ALTER TABLE platform.users
    ADD COLUMN kc_client_id text;

-- Invariant (enforced in DDL, not by convention): a service principal NAMES its Keycloak
-- client; a human never carries one. Existing rows (principal_type defaulted to 'human',
-- kc_client_id NULL) satisfy this, so no backfill is needed.
ALTER TABLE platform.users
    ADD CONSTRAINT users_kc_client_id_service_only
    CHECK (
        (principal_type = 'service' AND kc_client_id IS NOT NULL)
        OR (principal_type = 'human' AND kc_client_id IS NULL)
    );
