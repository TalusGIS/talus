-- V086 — Generated field projects (decision-generate-field-project.md §0).
--
-- Three nullable columns on platform.field_projects; uploads keep all three NULL and are
-- untouched. A GENERATED item (the "Make field project" door) records:
--
--   * source_web_map_id — the web map it was compiled from. ON DELETE SET NULL: deleting the
--     source map leaves the item alive as a plain upload-like artifact with Regenerate
--     disabled (the cheapest honest answer, §F6). Provenance line rides this.
--   * embedded_pat_id — the dedicated per-project PAT minted at generate time and embedded in
--     the artifact's datasource URLs (never the generating user's own token, §F1). Item
--     delete auto-revokes it (routers/field_projects.py). ON DELETE SET NULL only fires on a
--     hard purge, which requires the token already dead — safe.
--   * embedded_pat_ciphertext — the minted plaintext under the house Fernet key
--     (api/app/secrets.py, V073 pattern: key in .env, ciphertext here, NEITHER ALONE IS THE
--     SECRET). Exists solely so Regenerate can re-embed the SAME token (deliberate reuse:
--     crews' imported copies keep working). The same plaintext lives in cleartext inside the
--     served artifact by design — this copy is strictly better protected than the artifact
--     it feeds. Cleared by row deletion; inert once the PAT is revoked.
--
-- Generated-ness is exactly "source_web_map_id IS NOT NULL at birth"; no status column, no
-- new item type (§F7 — same table, same doors, same sharing).

ALTER TABLE platform.field_projects
  ADD COLUMN source_web_map_id       uuid  NULL REFERENCES platform.web_maps(id) ON DELETE SET NULL,
  ADD COLUMN embedded_pat_id         uuid  NULL REFERENCES platform.personal_access_tokens(id) ON DELETE SET NULL,
  ADD COLUMN embedded_pat_ciphertext bytea NULL;

-- Regenerate + provenance lookups ("which items came from this map?").
CREATE INDEX field_projects_source_map_idx
  ON platform.field_projects (source_web_map_id)
  WHERE source_web_map_id IS NOT NULL;
