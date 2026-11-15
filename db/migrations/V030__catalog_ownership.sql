-- V030 — Talus 26: catalog ownership (decision-catalog-ownership.md). Tier-2, backup-gated.
-- (1) Mutable map owner (owner_id) so ownership can be TRANSFERRED, kept DISTINCT from the immutable
--     creator (created_by). (2) Governance toggle for owner-initiated (non-admin) transfer.

-- (1) Map ownership becomes a mutable column, distinct from created_by — which stays the IMMUTABLE
--     creator/audit anchor (web_maps has no shadow-audit table, so created_by must NOT be rewritten
--     on transfer). Backfill owner_id = created_by (creator == owner pre-transfer), then NOT NULL.
--     The "Owned" list keys on owner_id (stays a pure DB query, resilient to an OpenFGA blip).
--     Transfer updates owner_id AND moves the OpenFGA `owner` tuple on web_map:<id>; layers have no
--     DB owner column (ownership is the FGA owner tuple only). Mirrors created_by's FK (no cascade —
--     transfer is the path to reassign a leaver's content before their user row is removed).
ALTER TABLE platform.web_maps ADD COLUMN owner_id uuid REFERENCES platform.users(id);
UPDATE platform.web_maps SET owner_id = created_by;
ALTER TABLE platform.web_maps ALTER COLUMN owner_id SET NOT NULL;
CREATE INDEX web_maps_owner_id_idx ON platform.web_maps (owner_id);

-- (2) Governance: may a NON-ADMIN owner transfer their OWN items to a colleague without an admin?
--     Admin transfer is ALWAYS allowed (role-only, app-enforced) — this gates ONLY the owner-
--     initiated path. Default TRUE (self-serve, simplicity-first; an org may turn it off to
--     centralize reassignment). Read fail-safe in the app (any read error => deny the non-admin).
--     The singleton org_settings row (seeded V028) inherits this default automatically.
ALTER TABLE platform.org_settings
    ADD COLUMN nonadmin_can_transfer_ownership boolean NOT NULL DEFAULT true;
