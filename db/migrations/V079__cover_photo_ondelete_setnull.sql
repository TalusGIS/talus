-- V079 — signs_cover_photo_fk: ON DELETE SET NULL (attachments residue, item (a)). Tier-2, backup-gated.
--
-- THE cover-photo-delete FK 500 (pre-existing since the legacy media.photos era, A-1 notes). Deleting an
-- attachment that a sign points at as its cover_photo 500s: signs_cover_photo_fk (assets.signs.cover_photo_id
-- → media.attachments(id)) was re-homed onto the generic authority in V068 (step 5) but inherited the
-- DEFAULT ON DELETE action (NO ACTION), so the referenced attachment row can never be deleted while a sign
-- still points at it. Both delete doors trip it: routers/attachments.py delete_attachment (web/core + QGIS
-- chute, via att.delete_row) and the GeoServices deleteAttachments loop (att.delete_by_esri_id).
--
-- The settled model wants the FK KEPT (decision-attachments.md §4.1: "re-homing to media.attachments(id)",
-- which V068 did) — the ONLY defect is the missing delete action. So the minimal correct fix is the right
-- ON DELETE action, DB-level, applied once at the authority: SET NULL. Deleting a cover photo clears the
-- sign's cover (feature survives, cover_photo_id → NULL — the media.py read routes already treat NULL cover
-- as "no cover set", a 404 on the cover endpoint, never an error). SET NULL over CASCADE deliberately: an
-- attachment delete must NOT delete the owning sign feature — an attachment is not the feature (side-table
-- model, decision §2). Being a DB constraint, this covers EVERY door uniformly and can never drift, and
-- keeps the generic media.attachments authority free of any signs-specific special-case.
--
-- Scope: signs ONLY — the one layer with this FK and the only attachments-enabled layer. assets.street_lights
-- carries a cover_photo_id column but was deliberately declared WITHOUT an FK (V013) and has attachments
-- disabled + zero covers set; if street-light attachments are ever enabled, the same SET NULL FK is the
-- follow-up (latent gap, recorded — not a bug today).

ALTER TABLE assets.signs DROP CONSTRAINT signs_cover_photo_fk;
ALTER TABLE assets.signs
    ADD CONSTRAINT signs_cover_photo_fk
    FOREIGN KEY (cover_photo_id) REFERENCES media.attachments(id) ON DELETE SET NULL;
