-- V062 — personal access tokens (decision-desktop-interop-i1.md §2).
--
-- A PAT lets a desktop client (QGIS/ArcGIS Pro via HTTP Basic — first-class in both) or a script
-- authenticate as its OWNING USER on the READ surfaces without a browser/Keycloak flow. Unlike a
-- Talus 19 service account (an org-level principal whose secret lives in Keycloak), a PAT is
-- personal, minted/revoked by its owner in the portal, and stored HERE as a sha256 hash — the
-- plaintext (`tpat_<urlsafe>`) is shown exactly once at mint and never again. Resolution happens
-- in the api's optional-auth seam; every existing gate (view/edit/editing_enabled/audit actor)
-- sees the owning user unchanged, and a PAT NEVER carries the admin god-mode override.
--
-- `scope` exists now so the I-3 (FeatureServer editing) story needs no reshape: I-1 accepts PATs
-- on the read surfaces only, so read_write grants nothing extra yet.

CREATE TABLE platform.personal_access_tokens (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id     uuid NOT NULL REFERENCES platform.users(id) ON DELETE CASCADE,
    label        text NOT NULL CHECK (length(label) BETWEEN 1 AND 80),
    secret_hash  text NOT NULL UNIQUE,  -- sha256 hex of the full token; plaintext is never stored
    scope        text NOT NULL DEFAULT 'read_only' CHECK (scope IN ('read_only', 'read_write')),
    created_at   timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz,           -- NULL = no expiry (revoke-first posture)
    last_used_at timestamptz,           -- throttled best-effort stamp (the last_seen pattern)
    revoked_at   timestamptz            -- soft revoke: the row stays as an audit breadcrumb
);

-- The owner's Profile list + the per-owner mint cap scan.
CREATE INDEX personal_access_tokens_owner_idx ON platform.personal_access_tokens (owner_id);

COMMENT ON TABLE platform.personal_access_tokens IS
  'decision-desktop-interop-i1.md: per-user tokens for desktop/script access (Basic auth on the '
  'read surfaces). sha256-hashed at rest, shown once at mint, owner-revocable, never admin-scoped.';
