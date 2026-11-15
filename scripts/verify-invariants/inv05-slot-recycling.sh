#!/usr/bin/env bash
# Invariant C5 (threat T2) — a released/reissued layer id carries ZERO inherited permissions.
#
# Layer ids (tal_<8hex>) are on-demand random now (the slot pool was retired, V061), but an id whose
# layer was deleted CAN be regenerated later (collision is only checked against LIVE platform.layers).
# So the load-bearing property is: deleting a layer removes ALL its OpenFGA tuples — owner AND any
# share tuples (org_shared / shared_group / public_shared) — so a future reuse of that id inherits
# nothing. release_layer does an FGA-first delete of every tuple on the object; this proves it.
#
# Setup a layer with MORE than the owner tuple (add a public_shared grant), delete it, assert the
# object has zero tuples left. Assertion: delete==200 AND zero tuples after. The "before" count is
# informational — if the extra-grant write didn't take, the core (owner-tuple) invariant is still
# asserted, with a note.
#
# Needs the running stack + .env (throwaway self-provisioned via _principals.sh). bash 3.2-safe; -sS curl.
set -u

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT" || { echo "inv05: FAIL — cannot cd to repo root"; exit 2; }

API="${API_BASE:-http://localhost:8000/api/v1}"
OPENFGA="http://openfga:8080"
[ -n "${OPENFGA_STORE_ID:-}" ] || OPENFGA_STORE_ID=$(grep '^OPENFGA_STORE_ID=' .env | cut -d= -f2)
[ -n "${OPENFGA_MODEL_ID:-}" ] || OPENFGA_MODEL_ID=$(grep '^OPENFGA_MODEL_ID=' .env | cut -d= -f2)

fga() { docker compose run --rm -T openfga-cli -c "$1" 2>/dev/null; }

tuple_count() {  # echo the number of tuples on object $1 (or ERR)
  local resp
  resp=$(fga "curl -sS -X POST ${OPENFGA}/stores/${OPENFGA_STORE_ID}/read -H 'content-type: application/json' -d '{\"tuple_key\":{\"object\":\"$1\"}}'")
  printf '%s' "$resp" | python3 -c "import sys,json;print(len(json.load(sys.stdin).get('tuples',[])))" 2>/dev/null || echo "ERR"
}

# Mint an owner token (throwaway publisher via _principals.sh — self-provisioned from .env).
# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh
tv_mint publisher || { echo "inv05: FAIL — could not provision a throwaway owner (needs the running stack + Keycloak)"; exit 1; }
H="Authorization: Bearer $TV_PUBLISHER_TOKEN"
fail=0

# 1) Create a throwaway layer (owner tuple auto-written).
LID=$(curl -sS -X POST -H "$H" -H 'Content-Type: application/json' \
  -d '{"display_name":"inv05 recycle test","geom_type":"point","working_srid":4326,"native_storage":false}' \
  "$API/layers" | python3 -c "import sys,json;print(json.load(sys.stdin).get('layer_id',''))" 2>/dev/null)
[ -n "$LID" ] || { echo "inv05: FAIL — layer create failed"; exit 1; }
echo "  layer $LID created"

# 2) Add a NON-owner grant so we test the §Q4 property (delete removes share tuples, not just owner).
fga "curl -sS -X POST ${OPENFGA}/stores/${OPENFGA_STORE_ID}/write -H 'content-type: application/json' -d '{\"authorization_model_id\":\"${OPENFGA_MODEL_ID}\",\"writes\":{\"tuple_keys\":[{\"user\":\"user:*\",\"relation\":\"public_shared\",\"object\":\"layer:${LID}\"}]}}'" >/dev/null 2>&1

before=$(tuple_count "layer:${LID}")
echo "  [1] tuples before delete        -> $before"
if [ "$before" = "ERR" ] || [ "$before" = "" ]; then
  echo "      FAIL: could not read OpenFGA tuples"; fail=1
elif [ "$before" -lt 2 ] 2>/dev/null; then
  echo "      note: only $before tuple(s) present — extra-grant setup didn't apply; asserting owner-tuple cleanup only"
fi

# 3) Delete the layer via API (FGA-first: removes every tuple on the object, then DROPs the table).
dc=$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE -H "$H" "$API/layers/${LID}")
echo "  [2] delete layer                -> $dc (expect 200)"
[ "$dc" = "200" ] || { echo "      FAIL: delete returned $dc"; fail=1; }

# 4) INVARIANT: zero tuples remain — a reissued id would inherit nothing.
after=$(tuple_count "layer:${LID}")
echo "  [3] tuples after delete         -> $after (expect 0)"
if [ "$after" != "0" ]; then
  echo "      FAIL: $after tuple(s) survived delete — a reissued id would inherit them (C5 violated)"
  fail=1
  # best-effort: scrub the surviving tuples so a failed run doesn't leave a poisoned id
  fga "curl -sS -X POST ${OPENFGA}/stores/${OPENFGA_STORE_ID}/write -H 'content-type: application/json' -d '{\"authorization_model_id\":\"${OPENFGA_MODEL_ID}\",\"deletes\":{\"tuple_keys\":[{\"user\":\"user:*\",\"relation\":\"public_shared\",\"object\":\"layer:${LID}\"}]}}'" >/dev/null 2>&1
fi

if [ "$fail" -eq 0 ]; then echo "inv05: PASS"; else echo "inv05: FAIL"; fi
exit "$fail"
