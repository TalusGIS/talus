-- V021 — platform.layers.display_color (Talus 10, Commit 1).
--
-- The FIRST presentation attribute on platform.layers. Every prior column has been
-- identity or lifecycle (layer_id, editable, display_name, status, audit_id) — this is
-- the first that describes how a layer is PAINTED rather than what it is or where it
-- sits in the claim flow. Per the Talus 10 decision doc, platform.layers is the agreed
-- home for publisher-chosen presentation metadata going forward (description, symbology,
-- ... ride here in later commits); display_color opens that section.
--
-- Set later by the create-layer endpoint (Layer 8) and read by GET /me/layers. NULL is a
-- first-class value meaning "no stored color — fall back to the frontend's static
-- LayerDef default". We add the column nullable with NO default and NO backfill: the 3
-- real layers (signs, street_lights, county_boundary) and the 5 tal_ slots all stay
-- NULL, and their colors keep coming from the frontend LayerDef exactly as before — this
-- migration changes no existing row's behavior.
--
-- The CHECK allows NULL OR a 7-char #RRGGBB hex string. Storing the literal CSS hex (not
-- a packed int / named enum) keeps it directly usable by the frontend and trivially
-- validated here.
--
-- No trigger/function change: display_color rides automatically in before_row/after_row
-- of the existing platform.layers audit trigger (V020) — the generic audit captures the
-- whole row, so a new column is covered with zero audit-side work.

ALTER TABLE platform.layers
    ADD COLUMN display_color text;

ALTER TABLE platform.layers
    ADD CONSTRAINT layers_display_color_hex_chk
    CHECK (display_color IS NULL OR display_color ~ '^#[0-9a-fA-F]{6}$');
