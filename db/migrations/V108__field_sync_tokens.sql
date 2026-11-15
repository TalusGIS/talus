-- V108 — Talus Field f4 (sync): the per-pack DEVICE TOKEN's scope columns.
-- decision-talus-field-f4.md §4.1 / §4.2 / §7. Tier-2, backup-gated.
--
-- A personal access token has never carried a per-layer scope (umbrella §15.2 #2): `scope` is
-- read_only | read_write over the owner's whole rights. f4 opens the NATIVE feature write doors to a
-- read_write PAT for the first time — and only to one that names the layers it may write.
-- `layer_ids` is that allow-list, frozen at mint time to the pack's layers as the OWNER could view
-- them; NULL means "not a field-sync token" and the native doors refuse it (a general-purpose PAT
-- keeps riding the GeoServices façade, exactly as before this migration).
-- `pack_id` ties the token's life to its pack (fork 2: one token per pack, revoked with it); a
-- deleted pack SETS NULL so a stale token is visible in Profile → Access tokens rather than silently
-- orphaned, and the f5 wipe path can find it.
--
-- Safe to apply before or after the code: the auth path reads `layer_ids` behind a guard and treats
-- an absent column as NULL (refuse), so nothing 500s in either order.

ALTER TABLE platform.personal_access_tokens
    ADD COLUMN layer_ids text[] NULL,
    ADD COLUMN pack_id   uuid   NULL REFERENCES platform.field_packs(id) ON DELETE SET NULL;

CREATE INDEX personal_access_tokens_pack_idx
    ON platform.personal_access_tokens (pack_id)
    WHERE pack_id IS NOT NULL AND revoked_at IS NULL;

COMMENT ON COLUMN platform.personal_access_tokens.layer_ids IS
    'Talus Field f4: the hosted-layer ids this token may WRITE through the native feature doors '
    '(frozen at mint to the pack''s layers as the owner could view them). NULL = not a field-sync '
    'token; the native write doors refuse it. See docs/decision-talus-field-f4.md §4.2.';
COMMENT ON COLUMN platform.personal_access_tokens.pack_id IS
    'Talus Field f4: the field pack this device token was minted for (one token per pack). '
    'SET NULL when the pack is deleted so the token stays visible for revocation.';
