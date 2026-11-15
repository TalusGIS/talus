-- V069 — media.attachments.esri_id: the stable integer attachment id the GeoServices attachments
-- protocol addresses (decision-attachments.md §11.1, A-2). Tier-2, backup-gated.
--
-- Esri `attachmentInfos[].id` is an INT, and updateAttachment/deleteAttachments key on it
-- (`attachmentIds=1,2,3`) — but media.attachments has only the uuid PK. The uuid stays the
-- INTERNAL key (and the signs.cover_photo_id FK target); esri_id is the EXTERNAL Esri handle.
-- A GLOBAL identity is the smallest correct shape: Esri only requires uniqueness within a
-- feature, which global uniqueness trivially satisfies, with one monotonic column and no
-- per-feature sequence machinery. NEVER derived from creation order — Esri ids must survive
-- deletes unchanged (§11.1 records the rejected alternates).
--
-- ADD COLUMN … IDENTITY back-fills every existing row (the V068-migrated signs photos) during
-- the ALTER's rewrite. Back-fill order is physical, not created_at — acceptable; Esri imposes
-- no ordering on attachment ids (§11.1).

ALTER TABLE media.attachments
    ADD COLUMN esri_id bigint GENERATED ALWAYS AS IDENTITY;

-- Structural stability for the external handle (also the fast lookup path). Endpoint queries
-- stay (layer_id, feature_id, esri_id)-scoped ON TOP of this — an id fished from feature A
-- resolves to nothing under feature B (cross-talk hygiene, sweep-asserted).
ALTER TABLE media.attachments
    ADD CONSTRAINT attachments_esri_id_key UNIQUE (esri_id);
