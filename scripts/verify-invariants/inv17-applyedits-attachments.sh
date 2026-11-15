#!/usr/bin/env bash
# Invariant — the applyEdits `attachments` sub-object is exactly as gated as the dedicated
# addAttachment door (decision-geoservices-applyedits-attachments.md §5). A NEW payload shape that
# writes caller bytes to object storage must not be a second, weaker upload door:
#
#   [1] anonymous → HTTP 401 + challenge (authentication stays HTTP-level)
#   [2] authenticated NON-editor (editor-role viewer on a public layer) → per-result 403 at
#       HTTP 200, ZERO rows written (can_view never leaks into attachment writes)
#   [3] attachments_enabled=false → per-result deny, ZERO rows (the admin hard switch holds)
#   [4] a parentObjectId fished from ANOTHER layer → per-result 404, ZERO rows on either layer
#       (parent resolution is layer-scoped; no cross-layer landings)
#   [5] positive control: the owner on an enabled layer lands the add (proves [2]-[4] aren't
#       passing vacuously)
#
# PRE-FIX BASELINE (recorded 2026-07-21, per the write-the-negative-test-first discipline): against
# the pre-slice handler [2]-[5] FAIL — the server 400s "no edits supplied" instead of answering in
# the per-result contract ([1] already passed: auth precedes parsing).
#
# Needs the running stack + .env (throwaways self-provisioned via _principals.sh). bash 3.2-safe; -sS curl; jq-free.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv17: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
ESRIA="$API/esri-auth"
OPENFGA="http://openfga:8080"
[ -n "${OPENFGA_STORE_ID:-}" ] || OPENFGA_STORE_ID=$(grep '^OPENFGA_STORE_ID=' .env | cut -d= -f2)
[ -n "${OPENFGA_MODEL_ID:-}" ] || OPENFGA_MODEL_ID=$(grep '^OPENFGA_MODEL_ID=' .env | cut -d= -f2)
fga() { docker compose run --rm -T openfga-cli -c "$1" 2>/dev/null; }
jget(){ python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }
PSQL(){ docker compose exec -T postgres psql -U "${POSTGRES_USER:-talus}" -d "${POSTGRES_DB:-talusgis}" -tAc "$1" 2>/dev/null | tr -d '[:space:]'; }
fail=0

PNG_B64='iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=='
edits(){ # $1 parentObjectId → the exact GP-tool payload shape
  printf '[{"id":0,"attachments":{"adds":[{"parentObjectId":%s,"name":"inv17.png","contentType":"image/png","data":"%s"}]}}]' "$1" "$PNG_B64"
}
natt(){ PSQL "SELECT count(*) FROM media.attachments WHERE layer_id='$1'"; }

# owner token + layer A (attachments on) + one feature; layer B + one feature (the foreign parent)
# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh
tv_mint publisher || { echo "inv17: FAIL — no owner token"; exit 1; }
OWNER="$TV_PUBLISHER_TOKEN"; JSON='Content-Type: application/json'
mklayer(){ curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" \
  -d "{\"display_name\":\"$1\",\"geom_type\":\"point\"}" "$API/layers" \
  | jget "d.get('layer_id','')"; }
LA="$(mklayer 'inv17 A')"; LB="$(mklayer 'inv17 B')"
[ -n "$LA" ] && [ -n "$LB" ] || { echo "inv17: FAIL — layer create (LA='$LA' LB='$LB')"; exit 1; }
leg_cleanup(){   # runs from the helper's EXIT trap, before principal teardown
  curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/layers/$LA"
  curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/layers/$LB"
}
curl -sS -o /dev/null -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"lon":-82.5,"lat":27.3}' "$API/layers/$LA/features"
curl -sS -o /dev/null -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"lon":-82.6,"lat":27.4}' "$API/layers/$LB/features"
curl -sS -o /dev/null -X PUT -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"attachmentsEnabled":true}' "$API/layers/$LA/attachments-enabled"
AOID="$(PSQL "SELECT min(objectid) FROM assets.$LA")"
BOID="$(PSQL "SELECT max(objectid) FROM assets.$LB")"
echo "  layers: $LA (parent OID $AOID) / $LB (foreign OID $BOID)"
AE="$ESRIA/rest/services/$LA/FeatureServer/applyEdits"

# [1] anonymous → 401 + challenge
H="$(curl -sS -o /dev/null -D - -X POST "$AE" --data-urlencode 'f=json' --data-urlencode "edits=$(edits "$AOID")")"
case "$H" in
  *" 401"*) case "$H" in *"WWW-Authenticate: Basic"*|*"www-authenticate: Basic"*)
      echo "  [1] anon applyEdits-attachments -> 401 + Basic challenge (pass)";;
    *) echo "      FAIL: 401 without a Basic challenge"; fail=1;; esac;;
  *) echo "      FAIL: anon applyEdits-attachments was not 401"; fail=1;;
esac

# [2] editor-role viewer (public share grants can_view, never edit) → per-result 403, zero rows
fga "curl -sS -X POST ${OPENFGA}/stores/${OPENFGA_STORE_ID}/write -H 'content-type: application/json' -d '{\"authorization_model_id\":\"${OPENFGA_MODEL_ID}\",\"writes\":{\"tuple_keys\":[{\"user\":\"user:*\",\"relation\":\"public_shared\",\"object\":\"layer:${LA}\"}]}}'" >/dev/null 2>&1
tv_mint editor || { echo "inv17: FAIL — no editor token"; exit 1; }
ED="$TV_EDITOR_TOKEN"
R="$(curl -sS -o - -w '|%{http_code}' -X POST "$AE" -H "Authorization: Bearer $ED" \
  --data-urlencode 'f=json' --data-urlencode "edits=$(edits "$AOID")")"
BODY="${R%|*}"; CODE="${R##*|}"
OK="$(printf '%s' "$BODY" | jget '"ok" if d[0]["attachments"]["addResults"][0]["success"]==False and d[0]["attachments"]["addResults"][0]["error"]["code"]==403 else "no"')"
N="$(natt "$LA")"
echo "  [2] editor-role viewer -> http $CODE, rows=$N (expect 200 + per-result 403 + 0 rows)"
{ [ "$CODE" = "200" ] && [ "$OK" = "ok" ] && [ "$N" = "0" ]; } || { echo "      FAIL: viewer gate ($BODY)"; fail=1; }

# [3] attachments_enabled=false (owner, so the ONLY closed gate is the hard switch) → deny, zero rows
curl -sS -o /dev/null -X PUT -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"attachmentsEnabled":false}' "$API/layers/$LA/attachments-enabled"
R="$(curl -sS -X POST "$AE" -H "Authorization: Bearer $OWNER" \
  --data-urlencode 'f=json' --data-urlencode "edits=$(edits "$AOID")")"
OK="$(printf '%s' "$R" | jget '"ok" if d[0]["attachments"]["addResults"][0]["success"]==False and "not enabled" in d[0]["attachments"]["addResults"][0]["error"]["description"].lower() else "no"')"
N="$(natt "$LA")"
echo "  [3] attachments_enabled=false -> rows=$N (expect per-result deny + 0 rows)"
{ [ "$OK" = "ok" ] && [ "$N" = "0" ]; } || { echo "      FAIL: hard-switch gate ($R)"; fail=1; }
curl -sS -o /dev/null -X PUT -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"attachmentsEnabled":true}' "$API/layers/$LA/attachments-enabled"

# [4] a parent fished from ANOTHER layer: layer B's OID does not exist in layer A → 404, zero rows anywhere
R="$(curl -sS -X POST "$AE" -H "Authorization: Bearer $OWNER" \
  --data-urlencode 'f=json' --data-urlencode "edits=$(edits 424242)")"
OK="$(printf '%s' "$R" | jget '"ok" if d[0]["attachments"]["addResults"][0]["success"]==False and d[0]["attachments"]["addResults"][0]["error"]["code"]==404 else "no"')"
NA="$(natt "$LA")"; NB="$(natt "$LB")"
echo "  [4] foreign/unknown parentObjectId -> rowsA=$NA rowsB=$NB (expect per-result 404 + 0/0)"
{ [ "$OK" = "ok" ] && [ "$NA" = "0" ] && [ "$NB" = "0" ]; } || { echo "      FAIL: parent scoping ($R)"; fail=1; }

# [5] positive control — the owner lands the add (the gates above weren't vacuous)
R="$(curl -sS -X POST "$AE" -H "Authorization: Bearer $OWNER" \
  --data-urlencode 'f=json' --data-urlencode "edits=$(edits "$AOID")")"
OK="$(printf '%s' "$R" | jget '"ok" if d[0]["attachments"]["addResults"][0]["success"] and isinstance(d[0]["attachments"]["addResults"][0]["objectId"], int) else "no"')"
N="$(natt "$LA")"
echo "  [5] owner control -> rows=$N (expect success + 1 row)"
{ [ "$OK" = "ok" ] && [ "$N" = "1" ]; } || { echo "      FAIL: positive control ($R)"; fail=1; }

if [ "$fail" = "0" ]; then echo "inv17: PASS"; else echo "inv17: FAIL"; exit 1; fi
