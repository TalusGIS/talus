-- V087 — ESRI door: repair case-clashing `objectid` / `globalid` field keys, then reserve both names.
-- (decision-esri-field-name-clash.md — APPROVED 2026-08-18; §3.2 repair, §3.3 reserved keys,
--  §3.6 Q7/Q9 the rename shape, §3.7 F1/F2 the trigger posture and the scale rules.)
--
-- Why this is a migration and not a maintenance script. `_esri_fields` seeds the FeatureServer field
-- list with hardcoded OBJECTID + GlobalID and then appends every owner-defined field, with no
-- case-folded dedupe. A layer whose source ESRI export carried an `objectid` props key is therefore
-- advertised with BOTH `OBJECTID` and `objectid`. GPKG/SQLite names are case-insensitive, so OGR
-- refuses the duplicate, `QgsOfflineEditing` aborts the feature copy — AND STILL RETURNS SUCCESS.
-- The crew gets an empty GeoPackage, the project stays pointed at the live service, and nobody
-- learns why until a field day is gone. Reproduced twice (session-summary-2026-08-15 §2).
--
-- ★ Why repair and enforce must be ONE migration: Postgres refuses to add a CHECK while violating
-- rows exist, so "constrain now, clean up later" is not on the menu. It is repair-and-enforce
-- together, or API-only enforcement indefinitely.
--
-- ★ Why it repairs WITHOUT ASKING — the auto-repair test (§3.0), all four legs:
--   1. deterministic (same input, same result)  2. value-preserving (keys move, values ride across)
--   3. announced (a RAISE NOTICE per rename — the flyway output IS the sweep record)
--   4. the defect it repairs causes SILENT data loss.
-- The standing pre-migration backup (upgrade-runbook.md step 1) is the gate; the inverse rename is
-- the undo. Nothing is dropped and no column is removed.
--
-- ★ Known interaction (§3.2): after V087, `restore-item.sh` restoring a layer from a PRE-V087 backup
-- re-inserts registry rows carrying the old key and the new CHECK REJECTS them — the restore fails
-- loudly. That is the correct failure; rename in the restored registry rows and re-run. A full-DB DR
-- restore is unaffected (it restores that era's schema, and the next `flyway migrate` re-applies this).

-- ── 1) Repair: rename the clashing keys in the registry, then in every feature row's props ─────────
DO $$
DECLARE
  r              record;
  v_key          text;
  v_new          text;
  v_n            int;
  v_old_keys     text[];
  v_new_keys     text[];
  v_tbl          regclass;
  v_kind         "char";
  v_audit_trg    text;
  v_rv_trg       text;
  v_audit_state  "char";
  v_rv_state     "char";
  v_props_state  "char";
  v_rows         bigint;
  v_layers       int := 0;
  v_renames      int := 0;
  v_features     bigint := 0;
BEGIN
  FOR r IN
    SELECT layer_id, array_agg(field_key ORDER BY field_key) AS keys
    FROM platform.layer_fields
    WHERE lower(field_key) IN ('objectid', 'globalid')   -- active AND inactive: the CHECK binds every row
    GROUP BY layer_id
    ORDER BY layer_id
  LOOP
    v_layers   := v_layers + 1;
    v_old_keys := '{}';
    v_new_keys := '{}';

    -- 1a) Registry FIRST. The props-validate trigger reads platform.layer_fields for its known-key
    -- list (V044:113), so the new key must already be registered or the props UPDATE below is
    -- correctly rejected as an unknown field. Ordering is load-bearing, not stylistic.
    FOREACH v_key IN ARRAY r.keys
    LOOP
      -- §3.6 Q7: a migration has no human to ask, so a collision resolves deterministically here
      -- (`objectid_src_1`, `_2`, …) — the chute idiom from _esri_fields. The interactive import path
      -- does the opposite on purpose: it REFUSES visibly with the existing `duplicate` verdict.
      v_new := v_key || '_src';
      v_n   := 0;
      WHILE EXISTS (
        SELECT 1 FROM platform.layer_fields
        WHERE layer_id = r.layer_id AND field_key = v_new
      ) LOOP
        v_n   := v_n + 1;
        v_new := v_key || '_src_' || v_n;
      END LOOP;

      -- §3.6 Q9: APPEND to the existing label, never replace it — a label the owner customised is
      -- user data, and §3.0 leg 2 says values are preserved.
      UPDATE platform.layer_fields
         SET field_key = v_new,
             label     = label || ' (source export)',
             updated_at = now()
       WHERE layer_id = r.layer_id AND field_key = v_key;

      v_old_keys := v_old_keys || v_key;
      v_new_keys := v_new_keys || v_new;
      v_renames  := v_renames + 1;
      RAISE NOTICE 'V087: % — registry field "%" -> "%"', r.layer_id, v_key, v_new;
    END LOOP;

    -- 1b) The feature rows. Guarded by to_regclass and restricted to real TABLES (relkind 'r') so
    -- view layers (vw_*, T38) are skipped — the V064 precedent, same reason: a PG view has no props
    -- of its own and does not serve over FeatureServer in v1.
    v_tbl := to_regclass('assets.' || quote_ident(r.layer_id));
    IF v_tbl IS NULL THEN
      RAISE NOTICE 'V087: % — no feature table; registry renamed only', r.layer_id;
      CONTINUE;
    END IF;
    SELECT relkind INTO v_kind FROM pg_class WHERE oid = v_tbl;
    IF v_kind <> 'r' THEN
      RAISE NOTICE 'V087: % — relkind % (not a table); registry renamed only', r.layer_id, v_kind;
      CONTINUE;
    END IF;

    -- ★★ §3.7 F1 — trigger posture, per name, and RESTORED to what it was.
    --   audit       -> DISABLE: a system repair is not a user edit, and the audit trail's job is
    --                  attributing USER edits. This migration's NOTICE output is the record.
    --   row_version -> DISABLE: a bump would make every open editor's next save 409 (V035 optimistic
    --                  concurrency) for a change the user did not make.
    --   props_validate -> LEFT ARMED: it is the one guard that would catch a bug in the rename SQL
    --                  below. Blanket `DISABLE TRIGGER USER` (the V064 precedent) would throw it away.
    -- The restore is NOT a plain `ENABLE TRIGGER`: audit triggers are `ENABLE ALWAYS` ('A') by
    -- deliberate design (V070 S-1a step 5, re-asserted by V075) so they fire even under
    -- session_replication_role = replica. Plain ENABLE would silently return them to 'O' and make
    -- audit bypassable again — two migrations' work undone by a repair migration, with no error.
    v_audit_trg := r.layer_id || '_audit_trigger';
    v_rv_trg    := r.layer_id || '_row_version_trigger';
    SELECT tgenabled INTO v_audit_state FROM pg_trigger WHERE tgrelid = v_tbl AND tgname = v_audit_trg;
    SELECT tgenabled INTO v_rv_state    FROM pg_trigger WHERE tgrelid = v_tbl AND tgname = v_rv_trg;
    SELECT tgenabled INTO v_props_state FROM pg_trigger
      WHERE tgrelid = v_tbl AND tgname = r.layer_id || '_props_validate_trigger';

    IF v_props_state IS DISTINCT FROM 'O' THEN
      RAISE WARNING 'V087: % — props-validate trigger is % (expected O); this repair is running WITHOUT its guard',
        r.layer_id, coalesce(v_props_state::text, 'absent');
    END IF;
    IF v_audit_state IS NOT NULL THEN
      EXECUTE format('ALTER TABLE assets.%I DISABLE TRIGGER %I', r.layer_id, v_audit_trg);
    END IF;
    IF v_rv_state IS NOT NULL THEN
      EXECUTE format('ALTER TABLE assets.%I DISABLE TRIGGER %I', r.layer_id, v_rv_trg);
    END IF;

    -- ★ §3.7 F2 — ONE statement per table covering BOTH keys, never one pass per key: on this arc's
    -- own residue that is the difference between rewriting 318,761 rows once and twice, with the
    -- per-row plpgsql validator armed on every pass. Values ride across untouched (§3.0 leg 2) —
    -- only the key changes.
    EXECUTE format(
      'UPDATE assets.%I SET props = ('
      '  SELECT jsonb_object_agg(COALESCE(m.newk, e.key), e.value)'
      '  FROM jsonb_each(props) e'
      '  LEFT JOIN unnest($1::text[], $2::text[]) AS m(oldk, newk) ON m.oldk = e.key'
      ') WHERE props ?| $1::text[]',
      r.layer_id
    ) USING v_old_keys, v_new_keys;
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    v_features := v_features + v_rows;

    IF v_audit_state IS NOT NULL THEN
      EXECUTE format(
        CASE v_audit_state
          WHEN 'A' THEN 'ALTER TABLE assets.%I ENABLE ALWAYS TRIGGER %I'
          WHEN 'R' THEN 'ALTER TABLE assets.%I ENABLE REPLICA TRIGGER %I'
          WHEN 'D' THEN 'ALTER TABLE assets.%I DISABLE TRIGGER %I'   -- it arrived disabled; leave it
          ELSE          'ALTER TABLE assets.%I ENABLE TRIGGER %I'
        END, r.layer_id, v_audit_trg);
    END IF;
    IF v_rv_state IS NOT NULL THEN
      EXECUTE format(
        CASE v_rv_state
          WHEN 'A' THEN 'ALTER TABLE assets.%I ENABLE ALWAYS TRIGGER %I'
          WHEN 'R' THEN 'ALTER TABLE assets.%I ENABLE REPLICA TRIGGER %I'
          WHEN 'D' THEN 'ALTER TABLE assets.%I DISABLE TRIGGER %I'
          ELSE          'ALTER TABLE assets.%I ENABLE TRIGGER %I'
        END, r.layer_id, v_rv_trg);
    END IF;

    RAISE NOTICE 'V087: % — % feature row(s) repaired (% -> %); audit trigger restored to %',
      r.layer_id, v_rows, v_old_keys, v_new_keys, coalesce(v_audit_state::text, 'absent');
  END LOOP;

  RAISE NOTICE 'V087: repair complete — % layer(s), % field(s) renamed, % feature row(s) rewritten',
    v_layers, v_renames, v_features;
END $$;

-- ── 2) Enforce: the two names the ESRI door SYNTHESIZES join the reserved list ─────────────────────
-- Governing rule (§3.3): *reserve what the door synthesizes, nothing more.* `objectid` has been a
-- physical column on every feature table since V064 and should have been reserved then — it is the
-- only physical column this CHECK ever missed (§2.3). `globalid` has no column at all; it is the
-- door's GlobalID, backed by the uuid `id`. Deliberately NOT reserved: shape / shape_length /
-- shape_area / fid / oid — the door does not synthesize them, so they cannot clash, and real ESRI
-- exports carry them constantly. This mirrors `_RESERVED_KEYS` in api/app/routers/layer_fields.py;
-- the two move together, in the same slice, or this failure repeats.
ALTER TABLE platform.layer_fields DROP CONSTRAINT layer_fields_key_reserved;
ALTER TABLE platform.layer_fields ADD CONSTRAINT layer_fields_key_reserved CHECK (field_key NOT IN (
    'id','geom','notes','props','row_version','ai_provenance',
    'created_at','created_by','updated_at','updated_by',
    'objectid','globalid'
));
