#!/usr/bin/env bash
# Invariant — /layers/{id}/columns is OPTIONAL-auth, and its capability flag cannot be spoofed
# (decision-columns-anon.md).
#
# The route is one of the anon read surfaces (Talus 23): a public layer's schema is readable
# without a credential, and the anon response scrubs editor identity. `c70d53a` broke that by
# reaching for `_validated_claims` (which 401s a missing bearer) to feed `edit_allowed()` a claims
# dict — turning an optional-auth route authenticated-only as a side effect of adding a UI flag.
# Anonymous public maps/embeds carrying a VIEW layer silently lost their popup attributes, and every
# PAT (HTTP Basic, not Bearer) was rejected on a documented PAT surface.
#
# Restoring anon access widens a boundary, so this leg proves the boundary by REQUEST:
#
#   [1]  anon        private layer  -> 404  (existence-hiding — not 401, not 403)
#   [2]  wrong-user  private layer  -> 403
#   [3]  forged alg:none Bearer     -> 401  (THE one: a presented credential must never downgrade
#                                            to anonymous just because it is invalid)
#   [4]  shaped-but-unminted PAT    -> 401  (same posture for the Basic path)
#   -- layer flips public --
#   [5]  anon        public layer   -> 200  (the restoration itself)
#   [6]  anon response canEdit      -> false
#   [7]  anon response omits created_by/updated_by (the scrub branch is REACHABLE, not just present)
#   [8]  anon /columns and anon /features agree on reachability, private AND public — the two share
#        _authorize_read, and this asserts they cannot drift apart
#   [9]  owner Bearer canEdit       -> true (positive control: the flag still works with claims)
#   [10] owner's READ_ONLY PAT      -> 200 but canEdit FALSE. _has_edit_role(None) returns TRUE (a
#        stub-mode convenience), and a PAT carries no Bearer — so without the `claims is not None`
#        guard a read-only credential reports "you may edit". Fail-safe direction is false.
#
# Setup mirrors inv12: owner creates a private layer, probes run, layer flips public, owner deletes.
# Needs the running stack + ./.env (throwaways self-provisioned via _principals.sh).
# bash 3.2-safe; jq-free; -sS curl.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv30: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
[ -n "${OPENFGA_STORE_ID:-}" ] || OPENFGA_STORE_ID=$(grep '^OPENFGA_STORE_ID=' .env | cut -d= -f2)
[ -n "${OPENFGA_MODEL_ID:-}" ] || OPENFGA_MODEL_ID=$(grep '^OPENFGA_MODEL_ID=' .env | cut -d= -f2)
OPENFGA="http://openfga:8080"
fga() { docker compose run --rm -T openfga-cli -c "$1" 2>/dev/null; }
FORGED='eyJhbGciOiJub25lIn0.eyJzdWIiOiJoYWNrZXIifQ.'   # alg:none, unsigned
FAKE_PAT='tpat_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'  # correctly shaped, never minted
jget(){ python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }
fail=0

# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh
tv_mint publisher || { echo "inv30: FAIL — no owner token"; exit 1; }
OWNER="$TV_PUBLISHER_TOKEN"
LID=$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H 'Content-Type: application/json' \
  -d '{"display_name":"inv30 columns anon","geom_type":"point","working_srid":4326,"native_storage":false}' \
  "$API/layers" | jget "d.get('layer_id','')")
[ -n "$LID" ] || { echo "inv30: FAIL — layer create failed"; exit 1; }
curl -sS -o /dev/null -X POST -H "Authorization: Bearer $OWNER" -H 'Content-Type: application/json' \
  -d '{"lon":-82.5,"lat":27.3,"notes":"inv30"}' "$API/layers/${LID}/features"
COLS="$API/layers/${LID}/columns"
FEAT="$API/layers/${LID}/features?limit=1"
echo "  layer $LID (private, owned by $TV_PUBLISHER_USER)"

# ---- [1][2][3][4] while PRIVATE ---------------------------------------------------------------
rc=$(curl -sS -o /dev/null -w '%{http_code}' "$COLS")
echo "  [1] anon private /columns            -> $rc (expect 404)"
case "$rc" in
  404) : ;;
  2*) echo "      FAIL: anon read a PRIVATE layer's schema (leaked allow)"; fail=1 ;;
  403) echo "      FAIL: 403 tells an anon caller the layer EXISTS — must be 404"; fail=1 ;;
  401) echo "      FAIL: 401 means the route is authenticated-only again; the anon contract"
       echo "            (shared with /features via _authorize_read) is 404 here"; fail=1 ;;
  *)  echo "      FAIL: expected 404, got $rc"; fail=1 ;;
esac

tv_mint editor || { echo "inv30: FAIL — no stranger token"; exit 1; }
rc=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TV_EDITOR_TOKEN" "$COLS")
echo "  [2] wrong-user private /columns      -> $rc (expect 403)"
case "$rc" in
  403) : ;;
  2*) echo "      FAIL: a non-shared user read a private layer's schema"; fail=1 ;;
  *)  echo "      FAIL: expected 403, got $rc"; fail=1 ;;
esac

rc=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $FORGED" "$COLS")
echo "  [3] forged alg:none Bearer           -> $rc (expect 401)"
case "$rc" in
  401) : ;;
  2*) echo "      FAIL: A FORGED TOKEN WAS TREATED AS ANONYMOUS AND SERVED ($rc)"; fail=1 ;;
  404) echo "      FAIL: forged token silently downgraded to anonymous (404 = anon path)"; fail=1 ;;
  *)  echo "      FAIL: expected 401, got $rc"; fail=1 ;;
esac

rc=$(curl -sS -o /dev/null -w '%{http_code}' -u "talus:$FAKE_PAT" "$COLS")
echo "  [4] shaped-but-unminted PAT (Basic)  -> $rc (expect 401)"
case "$rc" in
  401) : ;;
  *)  echo "      FAIL: expected 401 for an unminted PAT, got $rc"; fail=1 ;;
esac

# reachability BEFORE the flip — both routes must agree (private). BOTH fetched fresh and
# anonymously here: reusing $rc from the PAT leg above printed 401 for a call never made.
rc_c=$(curl -sS -o /dev/null -w '%{http_code}' "$COLS")
rc_f=$(curl -sS -o /dev/null -w '%{http_code}' "$FEAT")
echo "  [8a] anon private: /columns=$rc_c  /features=$rc_f (must match)"
if [ "$rc_c" != "$rc_f" ]; then
  echo "      FAIL: /columns ($rc_c) and /features ($rc_f) disagree on a private layer"; fail=1
fi

# ---- flip PUBLIC -------------------------------------------------------------------------------
# ---- anon master switch (decision-public-sharing-reachable.md) ----------------------------
# The public legs below need it ON, and a CLEAN install ships it OFF (PUBLIC_ANON_ACCESS_ENABLED=
# false, column NULL) — found 2026-09-10 when this leg first ran on a clean box and read
# the correct OFF behaviour as a FAIL. Flip the COLUMN for this leg's duration and restore it
# EXACTLY (NULL stays NULL) in leg_cleanup, the inv29 pattern; the api reads it live per request.
psql_q(){ docker compose exec -T postgres psql -U "${TV_PGUSER:-talus}" -d "${TV_PGDB:-talusgis}" -tA -c "$1" < /dev/null 2>/dev/null | tr -d '[:space:]'; }
INV30_ORIG_COL="$(psql_q "SELECT public_sharing_enabled FROM platform.org_settings WHERE id = true;")"
leg_cleanup(){
  case "$INV30_ORIG_COL" in
    t) psql_q "UPDATE platform.org_settings SET public_sharing_enabled = true WHERE id = true;" >/dev/null ;;
    f) psql_q "UPDATE platform.org_settings SET public_sharing_enabled = false WHERE id = true;" >/dev/null ;;
    *) psql_q "UPDATE platform.org_settings SET public_sharing_enabled = NULL WHERE id = true;" >/dev/null ;;
  esac
}
psql_q "UPDATE platform.org_settings SET public_sharing_enabled = true WHERE id = true;" >/dev/null
echo "  anon master switch: column was '${INV30_ORIG_COL:-NULL}' -> ON for this leg (restored by leg_cleanup)"

fga "curl -sS -X POST ${OPENFGA}/stores/${OPENFGA_STORE_ID}/write -H 'content-type: application/json' -d '{\"authorization_model_id\":\"${OPENFGA_MODEL_ID}\",\"writes\":{\"tuple_keys\":[{\"user\":\"user:*\",\"relation\":\"public_shared\",\"object\":\"layer:${LID}\"}]}}'" >/dev/null 2>&1
sleep 1

# ---- [5][6][7][8b] anonymous on a PUBLIC layer -------------------------------------------------
rc_c=$(curl -sS -o /dev/null -w '%{http_code}' "$COLS")
echo "  [5] anon public /columns             -> $rc_c (expect 200 — the restoration)"
case "$rc_c" in
  200) : ;;
  401) echo "      FAIL: still authenticated-only — the regression is back"; fail=1 ;;
  *)   echo "      FAIL: expected 200, got $rc_c"; fail=1 ;;
esac

BODY=$(curl -sS "$COLS")
ce=$(printf '%s' "$BODY" | jget "d.get('canEdit')")
echo "  [6] anon canEdit                     -> ${ce:-<none>} (expect False)"
[ "$ce" = "False" ] || { echo "      FAIL: anon reported canEdit=$ce"; fail=1; }

scrub=$(printf '%s' "$BODY" | jget "('created_by' in [c['name'] for c in d['columns']]) or ('updated_by' in [c['name'] for c in d['columns']])")
echo "  [7] anon sees created_by/updated_by  -> ${scrub:-?} (expect False)"
[ "$scrub" = "False" ] || { echo "      FAIL: anon schema exposes editor identity"; fail=1; }

rc_f=$(curl -sS -o /dev/null -w '%{http_code}' "$FEAT")
echo "  [8b] anon public: /columns=$rc_c  /features=$rc_f (must match)"
[ "$rc_c" = "$rc_f" ] || { echo "      FAIL: /columns and /features disagree on a public layer"; fail=1; }

# ---- [9] positive control: claims present -> the flag still works -------------------------------
ce=$(curl -sS -H "Authorization: Bearer $OWNER" "$COLS" | jget "d.get('canEdit')")
echo "  [9] owner Bearer canEdit             -> ${ce:-<none>} (expect True)"
[ "$ce" = "True" ] || { echo "      FAIL: owner lost canEdit — the guard is too aggressive"; fail=1; }

# ---- [10] trap A: a READ_ONLY PAT must not claim edit -------------------------------------------
PAT=$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H 'Content-Type: application/json' \
  -d '{"label":"inv30 read-only","scope":"read_only"}' "$API/me/tokens" | jget "d.get('token','')")
if [ -z "$PAT" ]; then
  echo "  [10] SKIP — could not mint a PAT"
else
  rc=$(curl -sS -o /dev/null -w '%{http_code}' -u "talus:$PAT" "$COLS")
  ce=$(curl -sS -u "talus:$PAT" "$COLS" | jget "d.get('canEdit')")
  echo "  [10] read_only PAT -> $rc, canEdit=${ce:-<none>} (expect 200 / False)"
  [ "$rc" = "200" ] || { echo "      FAIL: a valid PAT was rejected on a documented PAT surface"; fail=1; }
  if [ "$ce" != "False" ]; then
    echo "      FAIL: a READ_ONLY credential reported canEdit=$ce (_has_edit_role(None)=True trap)"; fail=1
  fi
  TID=$(curl -sS -H "Authorization: Bearer $OWNER" "$API/me/tokens" \
    | jget "([t['id'] for t in (d if isinstance(d,list) else d.get('tokens',[])) if t.get('label')=='inv30 read-only'] or [''])[0]")
  [ -n "$TID" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/me/tokens/$TID"
fi

# cleanup (owner deletes -> also scrubs the public_shared tuple)
curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/layers/${LID}"

if [ "$fail" -eq 0 ]; then echo "inv30: PASS"; else echo "inv30: FAIL"; fi
exit "$fail"
