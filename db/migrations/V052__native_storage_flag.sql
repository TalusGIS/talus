-- V052 — Per-layer native-CRS storage flag (decision-crs-native-slice1.md). Tier-2, backup-gated.
--
-- Slice 1 of the (C) hybrid storage model (decision-crs-storage.md, MODEL CHOSEN): a publisher may opt a
-- new layer into NATIVE storage so its geometry physically lives in the picked projected CRS (working_srid)
-- and survives store→export BYTE-EXACT — for survey/high-accuracy users whom the canonical-4326 datum
-- round-trip (NAD83↔WGS-84 ≈ 1–2 m, grid transforms not perfectly invertible) cannot serve.
--
-- This migration only RECORDS the choice. The physical effect — a bounded, one-time
--   ALTER TABLE assets.<tok> ALTER COLUMN geom TYPE geometry(Point,<srid>)
-- on the freshly-claimed EMPTY slot + a tile-function envelope redef — is applied by the claim path in
-- publisher.py (the 2nd scoped-runtime-DDL case, fenced like T38 view layers: single-object, FK-validated
-- SRID, opt-in, empty table). The default (false) path is byte-for-byte today's architecture: pool slots are
-- still born geometry(Point,4326), claim issues no DDL, and the 80% are completely unaffected.
--
-- Release (publisher.py _release_slot) resets this flag to false AND ALTERs the geom column back to
-- geometry(Point,4326) so a freed slot returns to the uniform pool clean.

ALTER TABLE platform.layers
    ADD COLUMN native_storage boolean NOT NULL DEFAULT false;

-- Guard: native storage is only meaningful when the layer authored in a non-4326 CRS. A native layer must
-- carry a working_srid other than 4326 (storing "natively in 4326" is just the default canonical path).
ALTER TABLE platform.layers
    ADD CONSTRAINT layers_native_requires_projected
        CHECK (native_storage = false OR working_srid <> 4326);
