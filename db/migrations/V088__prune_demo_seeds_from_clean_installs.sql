-- V088 — prune the demo/pilot seeds from CLEAN installs (backlog 9b + 9c;
-- decision-phase1-packaging-closeout.md §3.2).
--
-- A clean customer install's catalog listed three empty, foreign layers (signs,
-- street_lights, county_boundary — registry rows seeded by V015/V018) and its sharing
-- picker listed two foreign demo teams (signs_team, street_lights_team — V026). Both are
-- B5 findings: evaluator-visible residue on day one. The demo path re-inserts all five
-- rows (scripts/seed-demo.sh), so demo/dev stacks keep them.
--
-- ★ The predicate is NEVER-USED, not currently-empty: all three asset tables AND their
-- append-only audit shadows must be empty. A used-then-emptied stack retains audit history
-- and no-ops here (sanity-check catch, 2026-08-19). Under this predicate the deletes cannot
-- reach any FK-referencing row (attachments/fields/relates require features; features leave
-- audit); if the impossible happens anyway, the layer_views RESTRICT FKs fail this migration
-- LOUDLY — the correct failure. platform.groups has zero FK references (surveyed live).
--
-- home (V032) and open_data (V034) are FUNCTIONAL system groups (system = true) and are
-- untouched — sharing into them IS the Home-featuring / Open-Data mechanism.
--
-- Existing installs: asset tables hold data -> no-op, nothing orphaned.
-- Announced per row via RAISE NOTICE (the §3.0 auto-repair discipline, applied in spirit).
--
-- Security-review note (2026-08-19, residual hardening): both deletes honor
-- delete_protected — the V054 invariant is absolute (blocks owner AND admin), and a raw
-- SQL prune must not be the one path that ignores it. Tuple-hygiene caveat, recorded
-- honestly: a Flyway migration cannot see the OpenFGA store, so this prune deletes only
-- the REGISTRY rows; under the never-used predicate no product-authored tuples can exist
-- on these objects (members/shares are demo-chain-seeded, and a repurposed group implies
-- audit history on the stack). The verified edge (pre-V088 clean install whose admin
-- repurposed a seeded group without ever touching a built-in layer) is empty for this
-- install base and cannot recur post-V088 — see decision-phase1-packaging-closeout.md §3.2.

DO $$
DECLARE
    v_used bigint;
    r record;
BEGIN
    SELECT (SELECT count(*) FROM assets.signs)
         + (SELECT count(*) FROM assets.street_lights)
         + (SELECT count(*) FROM assets.county_boundary)
         + (SELECT count(*) FROM audit.signs_audit)
         + (SELECT count(*) FROM audit.street_lights_audit)
         + (SELECT count(*) FROM audit.county_boundary_audit)
    INTO v_used;

    IF v_used > 0 THEN
        RAISE NOTICE 'V088: built-in layers carry data/history (% rows) — demo/dev stack, pruning nothing.', v_used;
        RETURN;
    END IF;

    FOR r IN
        DELETE FROM platform.layers
        WHERE layer_id IN ('signs', 'street_lights', 'county_boundary')
          AND delete_protected = false   -- V054 invariant: absolute, never bypassed by SQL
        RETURNING layer_id
    LOOP
        RAISE NOTICE 'V088: pruned clean-install registry row platform.layers.%', r.layer_id;
    END LOOP;

    FOR r IN
        DELETE FROM platform.groups
        WHERE group_id IN ('signs_team', 'street_lights_team')
          AND system = false             -- belt-and-suspenders: never a functional group
          AND delete_protected = false   -- V054 invariant: absolute, never bypassed by SQL
        RETURNING group_id
    LOOP
        RAISE NOTICE 'V088: pruned clean-install demo group platform.groups.%', r.group_id;
    END LOOP;
END $$;
