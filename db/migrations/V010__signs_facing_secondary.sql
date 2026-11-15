-- V010 — secondary facing direction
--
-- Roughly 8% of a typical sign inventory records compound facings (e.g., "East/West") for signs
-- visible from both directions on a corridor. The platform represents these as two
-- columns rather than a single multi-value string. Queries for "all east-facing signs"
-- use:
--
--   WHERE 'E' IN (facing_direction, facing_direction_secondary)
--
-- The secondary column may be NULL when a sign faces a single direction. Same 8-point
-- CHECK as the primary.

ALTER TABLE assets.signs
    ADD COLUMN facing_direction_secondary text
        CHECK (facing_direction_secondary IN ('N','NE','E','SE','S','SW','W','NW'));
