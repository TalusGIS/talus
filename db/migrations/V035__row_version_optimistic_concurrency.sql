-- V035 — Talus 34: optimistic-concurrency token `row_version` (decision-optimistic-concurrency.md).
-- Tier-2, backup-gated.
--
-- Same-feature multi-editor safety. Every feature row gets a durable `row_version` (bigint)
-- stamped from `pg_current_xact_id()` — the 64-bit, monotonic, non-wrapping transaction clock.
-- The API guards writes on it (`... AND row_version = :expected`); a stale write hits 0 rows -> 409.
--
-- WHY a durable column and not `xmin`: `xmin` is 32-bit, wraps, and VACUUM FREEZE rewrites it, so
-- it is unusable as a token a field device holds OFFLINE for days. `row_version` is durable and
-- DOUBLES AS THE FUTURE DISCONNECTED-EDITING SYNC WATERMARK (`WHERE row_version > :watermark`) —
-- one token, online guard now + delta-sync later (decision §3a). pg_current_xact_id() is globally
-- ordered across all feature tables for free and groups a transaction's edits under one version.
--
-- Stamping is two-part and un-bypassable, like the V003 audit guarantee:
--   * column DEFAULT  -> stamps INSERTs AND backfills existing rows during ADD COLUMN
--                        (a volatile default forces a rewrite per row; NO audit-polluting UPDATE).
--   * BEFORE UPDATE trigger -> bumps row_version on every update (fires before the AFTER audit
--                        trigger, so the audit after_row records the new version too).
--
-- Slot tables are the FROZEN V019 pool (5 `assets.tal_*`); the publisher claims, never issues DDL,
-- so altering the fixed set here covers every slot. Signs + street_lights are the bespoke layers.

-- One function, many triggers (mirrors V003's audit pattern).
CREATE OR REPLACE FUNCTION assets.fn_bump_row_version() RETURNS trigger AS $$
BEGIN
    NEW.row_version := pg_current_xact_id()::text::bigint;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- --- signs ---------------------------------------------------------------------
ALTER TABLE assets.signs
    ADD COLUMN row_version bigint NOT NULL DEFAULT (pg_current_xact_id()::text::bigint);
CREATE TRIGGER signs_row_version_trigger
    BEFORE UPDATE ON assets.signs
    FOR EACH ROW EXECUTE FUNCTION assets.fn_bump_row_version();

-- --- street_lights -------------------------------------------------------------
ALTER TABLE assets.street_lights
    ADD COLUMN row_version bigint NOT NULL DEFAULT (pg_current_xact_id()::text::bigint);
CREATE TRIGGER street_lights_row_version_trigger
    BEFORE UPDATE ON assets.street_lights
    FOR EACH ROW EXECUTE FUNCTION assets.fn_bump_row_version();

-- --- slot pool (the 5 frozen V019 slots) ---------------------------------------
ALTER TABLE assets.tal_7b3aa14c
    ADD COLUMN row_version bigint NOT NULL DEFAULT (pg_current_xact_id()::text::bigint);
CREATE TRIGGER tal_7b3aa14c_row_version_trigger
    BEFORE UPDATE ON assets.tal_7b3aa14c
    FOR EACH ROW EXECUTE FUNCTION assets.fn_bump_row_version();

ALTER TABLE assets.tal_6496fe7d
    ADD COLUMN row_version bigint NOT NULL DEFAULT (pg_current_xact_id()::text::bigint);
CREATE TRIGGER tal_6496fe7d_row_version_trigger
    BEFORE UPDATE ON assets.tal_6496fe7d
    FOR EACH ROW EXECUTE FUNCTION assets.fn_bump_row_version();

ALTER TABLE assets.tal_2dbf236f
    ADD COLUMN row_version bigint NOT NULL DEFAULT (pg_current_xact_id()::text::bigint);
CREATE TRIGGER tal_2dbf236f_row_version_trigger
    BEFORE UPDATE ON assets.tal_2dbf236f
    FOR EACH ROW EXECUTE FUNCTION assets.fn_bump_row_version();

ALTER TABLE assets.tal_57b25bf6
    ADD COLUMN row_version bigint NOT NULL DEFAULT (pg_current_xact_id()::text::bigint);
CREATE TRIGGER tal_57b25bf6_row_version_trigger
    BEFORE UPDATE ON assets.tal_57b25bf6
    FOR EACH ROW EXECUTE FUNCTION assets.fn_bump_row_version();

ALTER TABLE assets.tal_2c09dee7
    ADD COLUMN row_version bigint NOT NULL DEFAULT (pg_current_xact_id()::text::bigint);
CREATE TRIGGER tal_2c09dee7_row_version_trigger
    BEFORE UPDATE ON assets.tal_2c09dee7
    FOR EACH ROW EXECUTE FUNCTION assets.fn_bump_row_version();
