#!/usr/bin/env bash
# Invariant C7 — a viewer can never write; can_view does NOT imply can_edit.
#
# The model (db/openfga/model.fga): can_view = owner OR public_shared OR org/group share;
# can_edit_access = owner OR org/group share — deliberately NOT public_shared. So a user who can
# VIEW a public layer, even WITH the data-editor role, still cannot WRITE it. This proves the write
# gate consults can_edit_access, not can_view.
#
# Setup: a throwaway publisher (owner) creates a layer + makes it public_shared. A throwaway
# data-editor (NOT the owner) can then view it but must be denied on write.
# PASS = editor-role viewer write -> 403. Read -> 200 is a supporting check.
#
# Needs the running stack + .env (throwaways self-provisioned via _principals.sh). bash 3.2-safe; -sS curl.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv07: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
OPENFGA="http://openfga:8080"
[ -n "${OPENFGA_STORE_ID:-}" ] || OPENFGA_STORE_ID=$(grep '^OPENFGA_STORE_ID=' .env | cut -d= -f2)
[ -n "${OPENFGA_MODEL_ID:-}" ] || OPENFGA_MODEL_ID=$(grep '^OPENFGA_MODEL_ID=' .env | cut -d= -f2)
fga() { docker compose run --rm -T openfga-cli -c "$1" 2>/dev/null; }
fail=0

# owner token + a fresh layer (throwaway publisher — least shape that can create layers)
# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh
tv_mint publisher || { echo "inv07: FAIL — no owner token"; exit 1; }
OWNER="$TV_PUBLISHER_TOKEN"
LID=$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H 'Content-Type: application/json' \
  -d '{"display_name":"inv07 viewer test","geom_type":"point","working_srid":4326,"native_storage":false}' \
  "$API/layers" | python3 -c "import sys,json;print(json.load(sys.stdin).get('layer_id',''))" 2>/dev/null)
[ -n "$LID" ] || { echo "inv07: FAIL — layer create failed"; exit 1; }
echo "  layer $LID (owned by $TV_PUBLISHER_USER)"

# make it public_shared → grants can_view to everyone, NO can_edit_access
fga "curl -sS -X POST ${OPENFGA}/stores/${OPENFGA_STORE_ID}/write -H 'content-type: application/json' -d '{\"authorization_model_id\":\"${OPENFGA_MODEL_ID}\",\"writes\":{\"tuple_keys\":[{\"user\":\"user:*\",\"relation\":\"public_shared\",\"object\":\"layer:${LID}\"}]}}'" >/dev/null 2>&1

# throwaway editor: has the data-editor role, is NOT the owner, sees the layer only via public share
tv_mint editor || { echo "inv07: FAIL — no editor token"; exit 1; }
ED="$TV_EDITOR_TOKEN"

rc=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $ED" "$API/layers/${LID}/features")
echo "  [1] editor-role viewer READ  -> $rc (expect 200 — has view via public)"
[ "$rc" = "200" ] || echo "      note: read was $rc (support check, not the assertion)"

wc=$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $ED" -H 'Content-Type: application/json' \
  -d '{"lon":-82.5,"lat":27.3}' "$API/layers/${LID}/features")
echo "  [2] editor-role viewer WRITE -> $wc (expect 403 — public grants no edit-access)"
case "$wc" in
  403) : ;;
  2*) echo "      FAIL: viewer WROTE a public layer ($wc) — can_view leaked into edit"; fail=1 ;;
  *)  echo "      FAIL: expected 403, got $wc"; fail=1 ;;
esac

# positive control: the owner CAN write
ow=$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $OWNER" -H 'Content-Type: application/json' \
  -d '{"lon":-82.5,"lat":27.3}' "$API/layers/${LID}/features")
echo "  [3] owner WRITE              -> $ow (expect 201 — control)"
[ "$ow" = "201" ] || { echo "      FAIL: owner could not write its own layer ($ow)"; fail=1; }

# cleanup (owner deletes → also scrubs the public_shared tuple)
curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/layers/${LID}"

if [ "$fail" -eq 0 ]; then echo "inv07: PASS"; else echo "inv07: FAIL"; fi
exit "$fail"
