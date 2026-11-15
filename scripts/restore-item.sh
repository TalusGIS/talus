#!/usr/bin/env bash
# restore-item.sh — Talus 47 (Part A): granular un-delete of ONE catalog item from a backup, WITHOUT a full
# restore that would roll the whole org back. Decision:
# docs/decision-granular-recovery.md (Part A — corrected to the verified data model).
#
# Recoverable item-level losses (the only deletable item types are `layer` (a tal_ layer) and `web_map`):
#   - a DELETED layer       — post-V061 (decision-slot-provisioning.md) delete DROPs the physical table and
#                             the registry row; recovery RE-PROVISIONS it (the V059/V061 template functions)
#                             then merges the backup's features/registry/metadata back in
#   - a RELEASED slot       — pre-V061 semantics (registry flipped to 'unclaimed'); kept for old live states
#   - a bulk FEATURE-DELETE — a still-live layer that lost rows (recover only the MISSING rows)
#   - a deleted WEB_MAP     — its platform.web_maps row + tuples + metadata gone
# There are no rasters/apps handled here.
#
# WHAT A RECOVERED LAYER GETS BACK: its rows; its field definitions (and any value list those fields use
# that no longer exists); every registry setting the backup and the live database both have (name, style,
# form configuration, display field, the editing switches, attachments on/off); its metadata record; its
# sharing.
# WHAT IT DOES NOT GET BACK (said in the output too, never silent): feature attachments (rows + files —
# a full restore includes them); relates to other tables; the layer's change history.
#
# ORDER MATTERS: the database validates every row's field values against the layer's field definitions,
# so the definitions are restored BEFORE the rows. (Until this was done, recovering any layer whose rows
# carried field values failed at the first row.)
#
# HOW IT'S SAFE:
#   - The READ side is isolated: the backup's talusgis/openfga dumps restore into THROWAWAY scratch DBs
#     (talusgis_recovery / openfga_recovery) inside the live containers — the live DBs are only READ.
#   - The WRITE side (--apply) is the one sharp edge and is heavily guarded: DRY-RUN BY DEFAULT (prints the
#     exact merge, writes nothing); --apply first takes a `pg_dump -Fc` of the live DB (it must be
#     non-zero); refuses to CLOBBER (web_map row already present, or slot already re-claimed); the DB merge is
#     ADDITIVE-ONLY (INSERT/UPDATE ... ON CONFLICT DO NOTHING) in a single transaction; tuples replay only the
#     MISSING ones; then it VERIFIES the item is back.
#   - It is a SCAFFOLD, not an in-product button (decision-dr-backup-restore.md §4 F-restore).
#
# Usage:
#   ./scripts/restore-item.sh --backup-id 41 --list                     # what the backup holds; changes nothing
#   ./scripts/restore-item.sh --backup-id 41 --item tal_2c09dee7        # dry-run (prints the plan)
#   ./scripts/restore-item.sh --backup-id 41 --item <web_map uuid>      # dry-run
#   ./scripts/restore-item.sh --backup-id 41 --item tal_2c09dee7 --apply   # actually recover (backup-gated)
#   ./scripts/restore-item.sh --backup-id 41 --item ... --age-key key.txt  # encrypted backups
#
# REQUIRES: the live stack up; KEYCLOAK_ADMIN_USER / KEYCLOAK_ADMIN_PASSWORD in ./.env (every
# install generates them — the throwaway admin below is minted from them); host `age` only for
# encrypted backups. bash 3.2-safe; jq-free (python3).
set -uo pipefail
cd "$(dirname "$0")/.."

REC_DB="talusgis_recovery"; OF_REC_DB="openfga_recovery"   # throwaway scratch DBs (in the live containers)
STAGE="platform._t47_stage"                                # transient staging tables, dropped each run

# ---- flags --------------------------------------------------------------------------------------
BACKUP_ID=""; ITEM=""; AGE_KEY=""; APPLY=0; LIST=0
while [ $# -gt 0 ]; do
  case "$1" in
    --backup-id) BACKUP_ID="${2:-}"; shift 2;;
    --item) ITEM="${2:-}"; shift 2;;
    --age-key) AGE_KEY="${2:-}"; shift 2;;
    --apply) APPLY=1; shift;;
    --list) LIST=1; shift;;
    -h|--help) sed -n '2,32p' "$0"; exit 0;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

# shellcheck disable=SC1091
. ./.env
LIVE_API="${API_URL:-http://localhost:8000}"
PGUSER="${POSTGRES_USER:-talus}"; PGDB="${POSTGRES_DB:-talusgis}"
KCUSER="${KEYCLOAK_DB_USER:-keycloak}"
OFUSER="${OPENFGA_DB_USER:-openfga}"; OFDB="${OPENFGA_DB_NAME:-openfga}"
STORE="${OPENFGA_STORE_ID:?OPENFGA_STORE_ID missing from .env}"; MODEL="${OPENFGA_MODEL_ID:?OPENFGA_MODEL_ID missing}"

fails=0
say(){ printf '\n== %s ==\n' "$1"; }
pass(){ echo "  PASS: $1"; }
fail(){ echo "  FAIL: $1"; fails=$((fails + 1)); }
die(){ echo "FATAL: $1" >&2; exit 1; }

PG(){ docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "$PGUSER" "$@"; }                 # live postgres container
REC(){  PG -d "$REC_DB"    -tAc "$1" 2>/dev/null | tr -d '[:space:]'; }                              # scratch talusgis count/scalar
LIVE(){ PG -d "$PGDB"      -tAc "$1" 2>/dev/null | tr -d '[:space:]'; }                              # live talusgis count/scalar
OF(){   docker compose exec -T openfga-postgres psql -tAc "$1" -U "$OFUSER" -d "$2" 2>/dev/null; }   # openfga (live or scratch)
fga_write(){  # user relation object  -> replay ONE tuple via the OpenFGA Write API (the canonical path).
  # `< /dev/null` is load-bearing: this runs inside a `while read` loop, and `docker compose run` would
  # otherwise swallow the loop's stdin (the remaining tuples) — starving the loop after the first write.
  body="$(docker compose run --rm -T openfga-cli -c "curl -sS -X POST http://openfga:8080/stores/${STORE}/write -H 'content-type: application/json' -d '{\"authorization_model_id\":\"${MODEL}\",\"writes\":{\"tuple_keys\":[{\"user\":\"$1\",\"relation\":\"$2\",\"object\":\"$3\"}]}}'" < /dev/null 2>/dev/null)"
  echo "$body" | grep -q '"code"' && { echo "    tuple write FAILED ($1 $2 $3): $body"; return 1; }
  return 0
}

[ -n "$BACKUP_ID" ] || die "--backup-id <N> is required (pick a PRE-deletion backup: the ID column in Admin -> Backups & recovery)"
[ "$LIST" -eq 1 ] || [ -n "$ITEM" ] || die "--item <layer_id | web_map uuid> is required (--list shows what the backup holds)"
[ "$LIST" -eq 1 ] && [ "$APPLY" -eq 1 ] && die "--list changes nothing; it cannot be combined with --apply"

SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/talus-item.XXXXXX")"
cleanup(){
  _ci_rc=$?
  # The throwaway admin goes out FIRST and LOUDLY — a leaked ephemeral admin principal must never
  # be silent (_principals.sh trap contract). The `type` guard covers dying before the mint.
  if type tv_cleanup >/dev/null 2>&1; then
    if ! tv_cleanup; then
      echo "  RESIDUE: throwaway verification principal survived in Keycloak/platform.users — remove tverify-* manually" >&2
      _ci_residue=1
    fi
  fi
  PG -d postgres -c "DROP DATABASE IF EXISTS $REC_DB" >/dev/null 2>&1
  docker compose exec -T openfga-postgres psql -U "$OFUSER" -d postgres -c "DROP DATABASE IF EXISTS $OF_REC_DB" >/dev/null 2>&1
  PG -d "$PGDB" -c "DROP TABLE IF EXISTS $STAGE" >/dev/null 2>&1
  docker compose exec -T postgres rm -f /tmp/t47_*.csv /tmp/t47_apply.sql >/dev/null 2>&1
  rm -rf "$SCRATCH"
  # Surface residue as a failure, but NEVER mask a real one — restore-drill.sh's teardown rule.
  if [ "${_ci_residue:-0}" -ne 0 ] && [ "$_ci_rc" -eq 0 ]; then exit 1; fi
}
trap cleanup EXIT

# ---- 0. live admin token + the backup row -------------------------------------------------------
say "0. Preflight"
# The admin identity is a THROWAWAY verification principal (tverify-admin-*), minted from .env's
# Keycloak admin credential — no formula passwords, so this runs on a clean customer box exactly
# as it does on a dev stack (Stage C finding D3; decision-clean-box-verification.md §6 — the same
# pattern restore-drill.sh already uses). It previously authenticated as admin-test/admin-test-pw,
# a user that exists ONLY in the dev realm, so this tool could never run for a customer at all.
TV_NO_TRAP=1   # this script has its own EXIT trap; cleanup() calls tv_cleanup itself
# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh
tv_mint admin || die "could not provision the throwaway verification admin (KEYCLOAK_ADMIN_* in ./.env)"
AUTH="Authorization: Bearer $TV_ADMIN_TOKEN"
ROW="$(curl -sS -H "$AUTH" "$LIVE_API/admin/backups" | ID="$BACKUP_ID" python3 -c 'import os,sys,json
b=json.load(sys.stdin)["backups"]; i=int(os.environ["ID"]); r=next((x for x in b if x["id"]==i), None)
print("%s %s %s"%(r["status"], 1 if r["encrypted"] else 0, r["openfgaSizeBytes"]) if r else "")' 2>/dev/null)"
[ -n "$ROW" ] || die "backup #$BACKUP_ID not found in the live catalog"
RST="$(echo "$ROW" | cut -d' ' -f1)"; ENC="$(echo "$ROW" | cut -d' ' -f2)"; OFSZ="$(echo "$ROW" | cut -d' ' -f3)"
[ "$RST" = ok ] || die "backup #$BACKUP_ID status=$RST (only 'ok' sets are usable)"
[ "${OFSZ:-0}" -gt 0 ] || die "backup #$BACKUP_ID has no openfga dump — cannot recover sharing tuples"
if [ "$ENC" = 1 ]; then [ -n "$AGE_KEY" ] || die "backup is encrypted — pass --age-key"; command -v age >/dev/null 2>&1 || die "'age' not on PATH"; fi
echo "  recovering item '$ITEM' from backup #$BACKUP_ID (encrypted=$ENC); mode: $([ "$APPLY" = 1 ] && echo APPLY || echo DRY-RUN)"

# ---- 1. stage + restore the two dumps into scratch DBs (live DBs only READ) ----------------------
say "1. Restore backup #$BACKUP_ID into scratch DBs (isolated; live DBs only read)"
maybe_decrypt(){ if head -c 30 "$1" | grep -q 'age-encryption.org'; then age -d -i "$AGE_KEY" -o "$1.p" "$1" || die "age decrypt failed: $1"; mv "$1.p" "$1"; fi; }
dl(){ code="$(curl -sS -H "$AUTH" -o "$2" -w '%{http_code}' "$LIVE_API/admin/backups/$BACKUP_ID/download?artifact=$1")"; [ "$code" = 200 ] || die "download $1 -> HTTP $code: $(head -c 200 "$2")"; maybe_decrypt "$2"; }
dl db "$SCRATCH/talusgis.dump"; dl openfga "$SCRATCH/openfga.dump"
PG -d postgres -c "DROP DATABASE IF EXISTS $REC_DB" >/dev/null 2>&1; PG -d postgres -c "CREATE DATABASE $REC_DB" >/dev/null 2>&1 || die "create $REC_DB failed"
docker compose cp "$SCRATCH/talusgis.dump" postgres:/tmp/t47_tg.dump >/dev/null 2>&1
PG -d "$REC_DB" -c "" >/dev/null 2>&1
docker compose exec -T postgres pg_restore --no-owner -U "$PGUSER" -d "$REC_DB" /tmp/t47_tg.dump >/dev/null 2>&1 || true
docker compose exec -T openfga-postgres psql -U "$OFUSER" -d postgres -c "DROP DATABASE IF EXISTS $OF_REC_DB" >/dev/null 2>&1
docker compose exec -T openfga-postgres psql -U "$OFUSER" -d postgres -c "CREATE DATABASE $OF_REC_DB" >/dev/null 2>&1 || die "create $OF_REC_DB failed"
docker compose cp "$SCRATCH/openfga.dump" openfga-postgres:/tmp/t47_of.dump >/dev/null 2>&1
docker compose exec -T openfga-postgres pg_restore --no-owner -U "$OFUSER" -d "$OF_REC_DB" /tmp/t47_of.dump >/dev/null 2>&1 || true
docker compose exec -T postgres rm -f /tmp/t47_tg.dump >/dev/null 2>&1; docker compose exec -T openfga-postgres rm -f /tmp/t47_of.dump >/dev/null 2>&1
[ -n "$(REC 'SELECT 1 FROM information_schema.tables WHERE table_schema=$$platform$$ AND table_name=$$layers$$')" ] || die "scratch restore failed (platform.layers absent)"
pass "scratch DBs restored ($REC_DB + $OF_REC_DB)"

# ---- --list: what the backup holds, and what of it is gone from the live portal (read-only) --------
# A deleted layer is gone from the catalog, so its id (tal_…) is not findable in the portal any more;
# this is where an administrator finds it (once-over 2026-11-15 R3). Reads the scratch DB and the live
# one; writes nothing; the scratch DBs go with the EXIT trap as on every run.
if [ "$LIST" -eq 1 ]; then
  say "Backup #$BACKUP_ID holds"
  LIVE_LAYERS=",$(LIVE "SELECT string_agg(layer_id, ',') FROM platform.layers"),"
  LIVE_MAPS=",$(LIVE "SELECT string_agg(id::text, ',') FROM platform.web_maps"),"
  echo "  Layers (an id marked NOT LIVE is gone from the portal; give it to --item):"
  PG -d "$REC_DB" -tA -F '|' -c "SELECT layer_id, coalesce(display_name, ''), status FROM platform.layers ORDER BY display_name" 2>/dev/null \
    | while IFS='|' read -r lid lname lstatus; do
        [ -n "$lid" ] || continue
        case "$LIVE_LAYERS" in *",$lid,"*) mark="live    " ;; *) mark="NOT LIVE" ;; esac
        printf '    %s  %-14s  %s  (%s)\n' "$mark" "$lid" "$lname" "$lstatus"
      done
  echo "  Web maps:"
  PG -d "$REC_DB" -tA -F '|' -c "SELECT id::text, coalesce(title, '') FROM platform.web_maps ORDER BY title" 2>/dev/null \
    | while IFS='|' read -r mid mtitle; do
        [ -n "$mid" ] || continue
        case "$LIVE_MAPS" in *",$mid,"*) mark="live    " ;; *) mark="NOT LIVE" ;; esac
        printf '    %s  %s  %s\n' "$mark" "$mid" "$mtitle"
      done
  echo
  echo "Nothing was changed. To recover one: --backup-id $BACKUP_ID --item <its id> (a dry run), then add --apply."
  exit 0
fi

# ---- 2. locate + classify the item in scratch ---------------------------------------------------
say "2. Locate '$ITEM' in the backup"
KIND=""
if [ -n "$(REC "SELECT 1 FROM platform.layers WHERE layer_id='$ITEM'")" ]; then KIND=layer
elif [ -n "$(REC "SELECT 1 FROM platform.web_maps WHERE id::text='$ITEM'")" ]; then KIND=web_map
else die "item '$ITEM' is not in backup #$BACKUP_ID (not a layer_id or web_map id there) — pick another backup"; fi
OBJ="$KIND:$ITEM"
echo "  classified as: $KIND ($OBJ)"

# extract the item's tuples present in the backup vs live (replay only the MISSING ones)
SCR_TUPLES="$SCRATCH/scr_tuples.txt"; LIVE_TUPLES="$SCRATCH/live_tuples.txt"
OF "SELECT relation||'|'||_user FROM tuple WHERE object_type='$KIND' AND object_id='$ITEM'" "$OF_REC_DB" | sed '/^$/d' | LC_ALL=C sort > "$SCR_TUPLES"
OF "SELECT relation||'|'||_user FROM tuple WHERE object_type='$KIND' AND object_id='$ITEM'" "$OFDB"       | sed '/^$/d' | LC_ALL=C sort > "$LIVE_TUPLES"
MISSING_TUPLES="$(comm -23 "$SCR_TUPLES" "$LIVE_TUPLES")"
N_SCR_T="$(grep -c . "$SCR_TUPLES" 2>/dev/null)"; [ -n "$N_SCR_T" ] || N_SCR_T=0
N_MISS_T="$(printf '%s' "$MISSING_TUPLES" | grep -c . 2>/dev/null)"; [ -n "$N_MISS_T" ] || N_MISS_T=0

# ---- 3. determine the recovery mode + the no-clobber check ---------------------------------------
say "3. Plan (no-clobber check)"
FEAT_TABLE=""; N_FEAT=0; MODE=""
if [ "$KIND" = layer ]; then
  FEAT_TABLE="assets.$ITEM"
  LIVE_STATUS="$(LIVE "SELECT status FROM platform.layers WHERE layer_id='$ITEM'")"
  N_FEAT="$(REC "SELECT count(*) FROM $FEAT_TABLE")"
  if [ -z "$LIVE_STATUS" ]; then
    # Post-V061 delete DROPped the table + registry row — recovery re-provisions then merges.
    MODE="dropped-layer"; N_LIVE_FEAT=0
  elif [ "$LIVE_STATUS" = unclaimed ]; then MODE="released-slot"; N_LIVE_FEAT="$(LIVE "SELECT count(*) FROM $FEAT_TABLE")"
  elif [ "$LIVE_STATUS" = ready ]; then MODE="feature-restore"; N_LIVE_FEAT="$(LIVE "SELECT count(*) FROM $FEAT_TABLE")"
  else die "live layer '$ITEM' is '$LIVE_STATUS' (provisioning?) — refuse to touch a layer mid-claim"; fi
  HAS_LF="$(REC "SELECT to_regclass('platform.layer_fields') IS NOT NULL")"
  HAS_DOM="$(REC "SELECT to_regclass('platform.domains') IS NOT NULL AND to_regclass('platform.domain_values') IS NOT NULL")"
  N_LF=0; N_LIVE_LF=0
  if [ "$HAS_LF" = t ]; then
    N_LF="$(REC "SELECT count(*) FROM platform.layer_fields WHERE layer_id='$ITEM'")"
    N_LIVE_LF="$(LIVE "SELECT count(*) FROM platform.layer_fields WHERE layer_id='$ITEM'")"
  fi
  echo "  mode: $MODE | backup features=$N_FEAT live features=$N_LIVE_FEAT | field definitions backup=$N_LF live=$N_LIVE_LF | missing tuples=$N_MISS_T/$N_SCR_T"
else
  if [ -n "$(LIVE "SELECT 1 FROM platform.web_maps WHERE id::text='$ITEM'")" ]; then
    die "REFUSE TO CLOBBER: web_map '$ITEM' still EXISTS in live — it wasn't deleted (or was re-created). Aborting."
  fi
  MODE="web_map"
  echo "  mode: $MODE | live row absent (good) | missing tuples=$N_MISS_T/$N_SCR_T"
fi

# ---- helpers for the additive SQL ---------------------------------------------------------------
coldefs(){  # <schema.table in the scratch DB> -> "col type, col type, …" at the BACKUP's own shape.
  # Direct PG call, not REC(): REC strips ALL whitespace and would mangle the list.
  PG -d "$REC_DB" -tAc "SELECT string_agg(quote_ident(attname)||' '||format_type(atttypid,atttypmod), ', ' ORDER BY attnum) FROM pg_attribute WHERE attrelid='$1'::regclass AND attnum>0 AND NOT attisdropped" | tr -d '\r' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'
}
merge_sql(){  # <staged temp table> <live table> -> SQL inserting the columns BOTH have, never overwriting.
  cat <<SQL
DO \$\$
DECLARE v_cols text;
BEGIN
  SELECT string_agg(quote_ident(s.attname), ',' ORDER BY s.attnum) INTO v_cols
  FROM pg_attribute s
  JOIN pg_attribute l ON l.attname = s.attname
                     AND l.attrelid = '$2'::regclass
                     AND l.attnum > 0 AND NOT l.attisdropped AND l.attgenerated = ''
  WHERE s.attrelid = '$1'::regclass AND s.attnum > 0 AND NOT s.attisdropped;
  IF v_cols IS NULL THEN
    RAISE EXCEPTION 'restore-item: no shared columns between the backup and live $2';
  END IF;
  EXECUTE format('INSERT INTO $2 (%s) SELECT %s FROM $1 ON CONFLICT DO NOTHING', v_cols, v_cols);
END \$\$;
SQL
}

# ---- build the additive SQL (CSV-stage from scratch -> live, ON CONFLICT DO NOTHING) -------------
# Everything below writes ONLY via INSERT/UPDATE ... ON CONFLICT, in one transaction, from staged scratch rows.
build_sql(){
  # export the rows we need from scratch to CSVs in the container
  if [ "$KIND" = layer ]; then
    PG -d "$REC_DB" -c "\copy (SELECT * FROM $FEAT_TABLE) TO '/tmp/t47_feat.csv' CSV" >/dev/null
    # The registry row at the BACKUP's own shape (every column it had), so a setting added by a later
    # release is carried when both sides have it and ignored when only one does.
    PG -d "$REC_DB" -c "\copy (SELECT * FROM platform.layers WHERE layer_id='$ITEM') TO '/tmp/t47_reg.csv' CSV" >/dev/null
    REG_COLDEFS="$(coldefs platform.layers)"
    [ -n "$REG_COLDEFS" ] || die "could not introspect platform.layers columns in the scratch DB"
    # Field definitions, and the value lists they point at. Absent from a backup older than the fields
    # feature: then there is nothing to stage and nothing to validate against.
    LF_SQL=""
    if [ "$HAS_LF" = t ]; then
      PG -d "$REC_DB" -c "\copy (SELECT * FROM platform.layer_fields WHERE layer_id='$ITEM') TO '/tmp/t47_lf.csv' CSV" >/dev/null
      LF_COLDEFS="$(coldefs platform.layer_fields)"
      [ -n "$LF_COLDEFS" ] || die "could not introspect platform.layer_fields columns in the scratch DB"
      DOM_SQL=""
      if [ "$HAS_DOM" = t ]; then
        PG -d "$REC_DB" -c "\copy (SELECT * FROM platform.domains WHERE id IN (SELECT domain_id FROM platform.layer_fields WHERE layer_id='$ITEM' AND domain_id IS NOT NULL)) TO '/tmp/t47_dom.csv' CSV" >/dev/null
        PG -d "$REC_DB" -c "\copy (SELECT * FROM platform.domain_values WHERE domain_id IN (SELECT domain_id FROM platform.layer_fields WHERE layer_id='$ITEM' AND domain_id IS NOT NULL)) TO '/tmp/t47_domv.csv' CSV" >/dev/null
        DOM_COLDEFS="$(coldefs platform.domains)"; DOMV_COLDEFS="$(coldefs platform.domain_values)"
        [ -n "$DOM_COLDEFS" ] && [ -n "$DOMV_COLDEFS" ] || die "could not introspect the value-list tables in the scratch DB"
        DOM_SQL="-- value lists the layer's fields use: ONLY those that no longer exist in live are re-created,
-- with their values. A list that still exists is left exactly as it is now.
CREATE TEMP TABLE _dom ($DOM_COLDEFS) ON COMMIT DROP;
\\copy _dom FROM '/tmp/t47_dom.csv' CSV
CREATE TEMP TABLE _domv ($DOMV_COLDEFS) ON COMMIT DROP;
\\copy _domv FROM '/tmp/t47_domv.csv' CSV
DELETE FROM _domv v WHERE EXISTS (SELECT 1 FROM platform.domains x WHERE x.id = v.domain_id);
DELETE FROM _dom d WHERE EXISTS (SELECT 1 FROM platform.domains x WHERE x.id = d.id);
$(merge_sql _dom platform.domains)
$(merge_sql _domv platform.domain_values)"
      fi
      LF_SQL="$DOM_SQL
-- field definitions BEFORE the rows: the rows' values are validated against them. Additive: a
-- definition live already has (same layer + key) is left as it is.
CREATE TEMP TABLE _lf ($LF_COLDEFS) ON COMMIT DROP;
\\copy _lf FROM '/tmp/t47_lf.csv' CSV
$(merge_sql _lf platform.layer_fields)"
    fi
    PG -d "$REC_DB" -c "\copy (SELECT item_type,item_id,doc,updated_at,updated_by FROM platform.item_metadata WHERE item_type='layer' AND item_id='$ITEM') TO '/tmp/t47_meta.csv' CSV" >/dev/null
    # dropped-layer (post-V061): the physical table + registry row are GONE — recovery re-provisions
    # via the template functions then merges. The feature merge is SCHEMA-DRIFT TOLERANT (V064): the
    # backup rows stage at the BACKUP's own shape, and only the columns live also has are inserted —
    # so a pre-V064 backup (no objectid) restores into a post-V064 live (identity assigns fresh
    # objectids via the column default), and a post-V064 backup preserves its objectids verbatim.
    if [ "$MODE" = "dropped-layer" ]; then
      REG_MERGE="SELECT CASE WHEN r.geom_type = 'none'
            THEN platform.provision_hosted_table(r.layer_id)
            ELSE platform.provision_spatial_layer(r.layer_id, r.geom_type,
                   CASE WHEN r.native_storage THEN r.working_srid ELSE 4326 END)
       END
FROM _reg r;
UPDATE _reg SET status = 'ready';
-- every registry column the backup and live share (generated columns excluded)
$(merge_sql _reg platform.layers)"
    else
      REG_MERGE="UPDATE platform.layers l SET status=r.status, display_name=r.display_name, editable=r.editable, delete_protected=r.delete_protected, display_color=r.display_color, renderer=r.renderer FROM _reg r WHERE l.layer_id=r.layer_id;"
    fi
    # The backup table's OWN column definitions (name + type, backup order) — _feat is built at the
    # BACKUP's shape so the CSV always loads, whatever era the dump is from. (Direct PG call, not
    # REC(): REC strips ALL whitespace and would mangle the defs list.)
    FEAT_COLDEFS="$(PG -d "$REC_DB" -tAc "SELECT string_agg(quote_ident(attname)||' '||format_type(atttypid,atttypmod), ', ' ORDER BY attnum) FROM pg_attribute WHERE attrelid='$FEAT_TABLE'::regclass AND attnum>0 AND NOT attisdropped" | tr -d '\r' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    [ -n "$FEAT_COLDEFS" ] || die "could not introspect $FEAT_TABLE columns in the scratch DB"
    cat > "$SCRATCH/apply.sql" <<SQL
BEGIN;
-- registry first: dropped-layer re-provisions the physical table here, so the feature staging
-- below has a live table to LIKE/INSERT into (all inside this one transaction).
CREATE TEMP TABLE _reg ($REG_COLDEFS) ON COMMIT DROP;
\copy _reg FROM '/tmp/t47_reg.csv' CSV
$REG_MERGE
$LF_SQL
-- features: stage all backup rows AT THE BACKUP'S OWN SHAPE, then insert only the columns live
-- ALSO has (schema-drift tolerant, V064): columns live gained since the backup (objectid) fall to
-- their defaults — the identity assigns fresh objectids for pre-V064 dumps; post-V064 dumps carry
-- theirs through verbatim.
CREATE TEMP TABLE _feat ($FEAT_COLDEFS) ON COMMIT DROP;
\copy _feat FROM '/tmp/t47_feat.csv' CSV
DO \$\$
DECLARE
  v_cols text;
  v_seq  text;
  v_last bigint;
BEGIN
  SELECT string_agg(quote_ident(s.attname), ',' ORDER BY s.attnum) INTO v_cols
  FROM pg_attribute s
  JOIN pg_attribute l ON l.attname = s.attname
                     AND l.attrelid = '$FEAT_TABLE'::regclass
                     AND l.attnum > 0 AND NOT l.attisdropped
  WHERE s.attrelid = '_feat'::regclass AND s.attnum > 0 AND NOT s.attisdropped;
  IF v_cols IS NULL THEN
    RAISE EXCEPTION 't47: no shared columns between the backup and live $FEAT_TABLE';
  END IF;
  EXECUTE format('INSERT INTO $FEAT_TABLE (%s) SELECT %s FROM _feat ON CONFLICT (id) DO NOTHING',
                 v_cols, v_cols);
  -- V064: restored rows may carry explicit objectids — resync the identity sequence so future
  -- inserts can't collide. GREATEST so a feature-restore into a table whose sequence is already
  -- ahead never REWINDS it.
  v_seq := pg_get_serial_sequence('$FEAT_TABLE', 'objectid');
  IF v_seq IS NOT NULL THEN
    EXECUTE format('SELECT last_value FROM %s', v_seq) INTO v_last;
    PERFORM setval(v_seq, GREATEST((SELECT coalesce(max(objectid), 1) FROM $FEAT_TABLE), v_last));
  END IF;
END \$\$;
-- metadata: re-insert if absent
CREATE TEMP TABLE _meta (item_type text, item_id text, doc jsonb, updated_at timestamptz, updated_by uuid) ON COMMIT DROP;
\copy _meta FROM '/tmp/t47_meta.csv' CSV
INSERT INTO platform.item_metadata (item_type,item_id,doc,updated_at,updated_by) SELECT item_type,item_id,doc,updated_at,updated_by FROM _meta ON CONFLICT (item_type,item_id) DO NOTHING;
COMMIT;
SQL
  else
    # The web map row at the BACKUP's own shape, merged on the columns live also has — the same
    # schema-drift tolerance as a layer. (A fixed column list here broke the day the table gained a
    # column: the staged row no longer matched the table it was copied into.)
    PG -d "$REC_DB" -c "\copy (SELECT * FROM platform.web_maps WHERE id::text='$ITEM') TO '/tmp/t47_wm.csv' CSV" >/dev/null
    WM_COLDEFS="$(coldefs platform.web_maps)"
    [ -n "$WM_COLDEFS" ] || die "could not introspect platform.web_maps columns in the scratch DB"
    PG -d "$REC_DB" -c "\copy (SELECT item_type,item_id,doc,updated_at,updated_by FROM platform.item_metadata WHERE item_type='web_map' AND item_id='$ITEM') TO '/tmp/t47_meta.csv' CSV" >/dev/null
    cat > "$SCRATCH/apply.sql" <<SQL
BEGIN;
CREATE TEMP TABLE _wm ($WM_COLDEFS) ON COMMIT DROP;
\copy _wm FROM '/tmp/t47_wm.csv' CSV
$(merge_sql _wm platform.web_maps)
CREATE TEMP TABLE _meta (item_type text, item_id text, doc jsonb, updated_at timestamptz, updated_by uuid) ON COMMIT DROP;
\copy _meta FROM '/tmp/t47_meta.csv' CSV
INSERT INTO platform.item_metadata (item_type,item_id,doc,updated_at,updated_by) SELECT item_type,item_id,doc,updated_at,updated_by FROM _meta ON CONFLICT (item_type,item_id) DO NOTHING;
COMMIT;
SQL
  fi
}
build_sql

# ---- 4. DRY-RUN: show the plan; or APPLY (backup-gated) ------------------------------------------
if [ "$APPLY" -ne 1 ]; then
  say "4. DRY-RUN — the additive merge that --apply would run (nothing written)"
  echo "  DB transaction (CSV-staged from the backup, ON CONFLICT DO NOTHING):"
  sed 's/^/    /' "$SCRATCH/apply.sql"
  [ "$KIND" = layer ] && echo "  Field definitions in the backup: $N_LF (live has $N_LIVE_LF; missing ones are restored before the rows)"
  echo "  OpenFGA tuples to replay (missing in live): $N_MISS_T"
  printf '%s\n' "$MISSING_TUPLES" | sed '/^$/d' | while IFS='|' read -r rel usr; do echo "    write  $usr  $rel  $OBJ"; done
  echo
  echo "Re-run with --apply to recover (it will pg_dump the live DB first)."
  exit 0
fi

say "4. APPLY — backup-gate, then the additive merge"
mkdir -p "$HOME/talusgis-backups"
STAMP="$(date '+%Y%m%d-%H%M%S')"; GATE="$HOME/talusgis-backups/pre-restore-item-$STAMP.dump"
docker compose exec -T postgres pg_dump -Fc -U "$PGUSER" -d "$PGDB" > "$GATE" 2>/dev/null
GSZ="$(wc -c < "$GATE" | tr -d '[:space:]')"; [ "${GSZ:-0}" -gt 0 ] || die "backup-gate dump is empty — refusing to proceed"
pass "backup-gate: live pg_dump captured ($GSZ bytes) -> $GATE"
docker compose cp "$SCRATCH/apply.sql" postgres:/tmp/t47_apply.sql >/dev/null 2>&1
MERGE_OK=1
if PG -d "$PGDB" -f /tmp/t47_apply.sql > "$SCRATCH/apply.out" 2>&1; then
  pass "DB merge committed (additive, one transaction)"
else
  MERGE_OK=0
  fail "DB merge FAILED — live unchanged (transaction rolled back). The database said:"
  grep -E 'ERROR|DETAIL|CONTEXT|HINT' "$SCRATCH/apply.out" | head -8 | sed 's/^/      /'
fi
# tuples: replay only the missing ones — and ONLY after a committed merge. Sharing for an item that
# was not recovered would grant access to nothing today and to whatever takes that id tomorrow.
if [ "$MERGE_OK" -ne 1 ]; then
  echo "  sharing NOT replayed (the merge failed); nothing was written to the authorization store"
  echo
  echo "T47 ITEM RECOVERY: FAILED — '$ITEM' was not recovered and live is unchanged. Undo dump (not needed): $GATE"
  exit 1
fi
if [ "$N_MISS_T" -gt 0 ]; then
  printf '%s\n' "$MISSING_TUPLES" | sed '/^$/d' | while IFS='|' read -r rel usr; do fga_write "$usr" "$rel" "$OBJ" && echo "  replayed: $usr $rel $OBJ"; done
fi

# ---- 5. verify the item is back -----------------------------------------------------------------
say "5. Verify recovery"
if [ "$KIND" = layer ]; then
  LF="$(LIVE "SELECT count(*) FROM $FEAT_TABLE")"; LS="$(LIVE "SELECT status FROM platform.layers WHERE layer_id='$ITEM'")"
  { [ "$LF" -ge "$N_FEAT" ] 2>/dev/null; } && pass "features present: $LF (>= backup's $N_FEAT)" || fail "features short: live=$LF backup=$N_FEAT"
  [ "$LS" = ready ] && pass "registry restored (status=ready)" || fail "registry status=$LS (want ready)"
  LLF="$(LIVE "SELECT count(*) FROM platform.layer_fields WHERE layer_id='$ITEM'")"
  { [ "${LLF:-0}" -ge "${N_LF:-0}" ] 2>/dev/null; } && pass "field definitions present: $LLF (>= backup's $N_LF)" || fail "field definitions short: live=$LLF backup=$N_LF"
  echo "  NOT restored by this tool: feature attachments, relates to other tables, the layer's change history"
else
  [ -n "$(LIVE "SELECT 1 FROM platform.web_maps WHERE id::text='$ITEM'")" ] && pass "web_map row restored" || fail "web_map row still absent"
fi
LT="$(OF "SELECT count(*) FROM tuple WHERE object_type='$KIND' AND object_id='$ITEM'" "$OFDB" | tr -d '[:space:]')"
{ [ "$LT" -ge "$N_SCR_T" ] 2>/dev/null; } && pass "sharing tuples present: $LT (>= backup's $N_SCR_T)" || fail "tuples short: live=$LT backup=$N_SCR_T"

echo
if [ "$fails" -eq 0 ]; then echo "T47 ITEM RECOVERY: ALL PASS — '$ITEM' ($KIND) recovered additively. Undo dump: $GATE"; else echo "T47 ITEM RECOVERY: $fails FAILURE(S) — see above. Undo dump: $GATE"; exit 1; fi
