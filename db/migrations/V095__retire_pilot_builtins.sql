-- V095 — retire the pilot built-ins (decision-retire-builtins.md phase 2, fork B).
--
-- Drops the pilot scaffolding the generic substrate replaced: the three bespoke asset
-- tables (V004/V013/V015-18 era), their seven FK-domain tables (sign_categories + the six
-- *_codes clones), their three append-only audit shadows, and their three pg_tileserv
-- functions — plus every platform row that still names them (registry rows, the two demo
-- groups, pilot-era import-review log lines, and any locator / metadata / favorite that
-- points at them). This COMPLETES V088's prune unconditionally: V088 removed registry
-- rows + demo groups only under its never-used predicate, an applied migration is
-- checksum-locked, and the used-then-emptied case therefore lands here.
--
-- ★ GUARD (fork B, fail-closed): refuses while any of the three asset tables still holds
-- features. Auto-migration is REJECTED by ruling — the operator exports via the OGC/export
-- doors, re-imports through the 9d bulk-import wizard into hosted layers, TRUNCATEs the
-- three tables, and re-runs the upgrade. install.sh pre-flights this same predicate BEFORE
-- Flyway so a data-ful box refuses cleanly instead of dying mid-migration; this RAISE is
-- the fence that cannot be bypassed. Also refuses if any doomed row is delete_protected:
-- the V054 invariant is absolute (raw SQL must never be the one path that ignores it), so
-- protection must be consciously cleared through the product before the retirement runs.
--
-- Audit-shadow rows deliberately do NOT gate (unlike V088's never-used predicate): a
-- used-then-emptied box keeps edit history in its shadows, and that history retires with
-- them — the export path preserves the DATA, which is what the guard protects.
--
-- Tuple hygiene (same honest caveat as V088): a migration cannot see OpenFGA, so tuples on
-- layer:signs / layer:street_lights / layer:county_boundary and the two demo groups become
-- dead references on nonexistent objects — harmless, deny-by-default.
--
-- FK posture: the ten FKs into platform.layers are RESTRICT/NO ACTION. Rows that could
-- reference the built-ins are deleted below BEFORE the registry rows; anything unforeseen
-- fails this migration LOUDLY mid-transaction (transactional DDL: all-or-nothing), which
-- is the correct failure (V088's own posture).

-- ── The fence ────────────────────────────────────────────────────────────────────────
DO $$
DECLARE
    n_rows bigint;
    n_protected bigint;
BEGIN
    SELECT (SELECT count(*) FROM assets.signs)
         + (SELECT count(*) FROM assets.street_lights)
         + (SELECT count(*) FROM assets.county_boundary)
    INTO n_rows;
    IF n_rows > 0 THEN
        RAISE EXCEPTION USING
            message = format('V095: the pilot built-ins still hold %s feature row(s); refusing to drop them.', n_rows),
            hint    = 'Export each built-in via the OGC/export doors, re-import through the bulk-import wizard '
                      'into a hosted layer, TRUNCATE assets.signs / assets.street_lights / assets.county_boundary, '
                      'then re-run the upgrade. See docs/decision-retire-builtins.md (fork B).';
    END IF;

    SELECT (SELECT count(*) FROM platform.layers
             WHERE layer_id IN ('signs','street_lights','county_boundary') AND delete_protected)
         + (SELECT count(*) FROM platform.groups
             WHERE group_id IN ('signs_team','street_lights_team') AND delete_protected)
    INTO n_protected;
    IF n_protected > 0 THEN
        RAISE EXCEPTION USING
            message = format('V095: %s pilot built-in registry row(s)/group(s) are delete-protected; refusing.', n_protected),
            hint    = 'The V054 protection invariant is absolute — clear it through the product (Manage -> '
                      'delete protection) before re-running the upgrade.';
    END IF;
END $$;

-- ── Platform rows that name the built-ins (announced per row, V088 style; every delete
--    is a no-op on a clean box — production has been clean since V088) ────────────────
DO $$
DECLARE r record;
BEGIN
    -- Children of platform.layers first (FKs are RESTRICT). All zero on every known box;
    -- deleted defensively so a dev-only binding can never strand the registry delete.
    FOR r IN DELETE FROM platform.geocoders
             WHERE layer_id IN ('signs','street_lights','county_boundary')
             RETURNING name LOOP
        RAISE NOTICE 'V095: removed built-in-bound locator platform.geocoders.%', r.name;
    END LOOP;

    -- Free-text references (no FK): catalog metadata, favorites, pilot-era import-review
    -- log lines about relations this migration drops.
    FOR r IN DELETE FROM platform.item_metadata
             WHERE item_type = 'layer' AND item_id IN ('signs','street_lights','county_boundary')
             RETURNING item_id LOOP
        RAISE NOTICE 'V095: removed catalog metadata for %', r.item_id;
    END LOOP;
    FOR r IN DELETE FROM platform.user_favorites
             WHERE item_type = 'layer' AND item_id IN ('signs','street_lights','county_boundary')
             RETURNING item_id LOOP
        RAISE NOTICE 'V095: removed favorite for %', r.item_id;
    END LOOP;
    DELETE FROM platform.data_quality_review
     WHERE source_table IN ('assets.signs','assets.street_lights','assets.county_boundary',
                            'assets.sign_categories','assets.condition_codes','assets.mount_type_codes',
                            'assets.mutcd_codes','assets.org_codes','assets.light_type_codes',
                            'assets.bulb_type_codes');
    IF FOUND THEN
        RAISE NOTICE 'V095: removed pilot-era import-review log lines';
    END IF;

    -- The registry rows + demo groups (V088's job, finished unconditionally — the guard
    -- above already proved none are delete-protected; home/open_data are system groups
    -- and are untouched).
    FOR r IN DELETE FROM platform.groups
             WHERE group_id IN ('signs_team','street_lights_team') AND system = false
             RETURNING group_id LOOP
        RAISE NOTICE 'V095: removed demo group platform.groups.%', r.group_id;
    END LOOP;
    FOR r IN DELETE FROM platform.layers
             WHERE layer_id IN ('signs','street_lights','county_boundary')
             RETURNING layer_id LOOP
        RAISE NOTICE 'V095: removed registry row platform.layers.%', r.layer_id;
    END LOOP;
END $$;

-- ── The bespoke tile functions (V011/V014/V017 — pg_tileserv re-catalogs on restart) ──
DROP FUNCTION assets.signs_tile(z integer, x integer, y integer);
DROP FUNCTION assets.street_lights_tile(z integer, x integer, y integer);
DROP FUNCTION assets.county_boundary_tile(z integer, x integer, y integer);

-- ── The audit shadows (append-only history of a retired demo surface) ────────────────
DROP TABLE audit.signs_audit;
DROP TABLE audit.street_lights_audit;
DROP TABLE audit.county_boundary_audit;

-- ── The asset tables (their audit triggers and owned sequences drop with them; the
--    shared V003/V012 audit trigger FUNCTION serves the slots and stays) ──────────────
DROP TABLE assets.signs;
DROP TABLE assets.street_lights;
DROP TABLE assets.county_boundary;

-- ── The seven FK-domain tables (clones first; sign_categories last — mutcd_codes
--    carries an FK to it) ─────────────────────────────────────────────────────────────
DROP TABLE assets.condition_codes;
DROP TABLE assets.mount_type_codes;
DROP TABLE assets.mutcd_codes;
DROP TABLE assets.org_codes;
DROP TABLE assets.light_type_codes;
DROP TABLE assets.bulb_type_codes;
DROP TABLE assets.sign_categories;
