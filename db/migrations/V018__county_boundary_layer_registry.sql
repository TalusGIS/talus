-- V018 — register assets.county_boundary in platform.layers (Layer 7c, Commit D).
--
-- Data seed, not schema: the platform.layers table exists from V015, which seeded
-- signs/street_lights inline. county_boundary is the third layer and the first that
-- ships READ-ONLY — editable=false (AGO-faithful: published read-only until an owner
-- turns editing on). Per the A+ ingest dividing line, this small fixed reference row
-- is the Flyway side (exactly like V015's seed); the OpenFGA access tuples are the
-- script side (scripts/seed-county-boundary-tuples.sh).
--
-- Effect: /me/layers now considers county_boundary; users with OpenFGA can_view see it
-- with canView:true. canEdit is forced false for everyone because editable=false (the
-- editability key fails closed), so it is view-only on the portal — no county_boundary
-- write endpoint exists yet either.

INSERT INTO platform.layers (layer_id, editable) VALUES
    ('county_boundary', false);
