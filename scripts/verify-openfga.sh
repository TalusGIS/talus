#!/usr/bin/env bash
# Layer 7b — OpenFGA infrastructure verification (PASS/FAIL gate).
#
# Proves the store + model are live and the model RESOLVES correctly, end to end,
# WITHOUT seeding any of the real matrix tuples (that's Step 2). It writes ONE
# temporary tuple, runs a positive and a negative check, then removes the tuple
# so the store is left exactly as the loader left it.
#
# All calls go through the on-demand `openfga-cli` helper (openfga has no host
# port). Reads OPENFGA_STORE_ID / OPENFGA_MODEL_ID from ./.env (written by
# scripts/load-openfga-model.sh). Exit 0 iff every check passes.
#
# Run:  ./scripts/verify-openfga.sh

set -uo pipefail
cd "$(dirname "$0")/.."

OPENFGA="http://openfga:8080"
STORE_ID="$(grep -E '^OPENFGA_STORE_ID=' .env 2>/dev/null | cut -d= -f2-)"
MODEL_ID="$(grep -E '^OPENFGA_MODEL_ID=' .env 2>/dev/null | cut -d= -f2-)"

if [[ -z "${STORE_ID}" || -z "${MODEL_ID}" ]]; then
  echo "FAIL: OPENFGA_STORE_ID / OPENFGA_MODEL_ID not set in .env. Run ./scripts/load-openfga-model.sh first."
  exit 1
fi
command -v jq >/dev/null || { echo "FAIL: jq not found on host"; exit 1; }

fga_curl() { docker compose run --rm -T openfga-cli -c "$1"; }

pass=0; fail=0
report() { # report <label> <expected> <actual>
  if [[ "$2" == "$3" ]]; then printf '  PASS  %-46s (%s)\n' "$1" "$3"; pass=$((pass+1));
  else printf '  FAIL  %-46s expected=%s actual=%s\n' "$1" "$2" "$3"; fail=$((fail+1)); fi
}

# A test subject + object that touch nothing the matrix will later seed.
ALICE="user:verify-alice"
BOB="user:verify-bob"
OBJ="layer:__verify__"

check() { # check <user> -> prints "true"/"false"/"error"
  local body
  body=$(fga_curl "curl -sS -X POST ${OPENFGA}/stores/${STORE_ID}/check -H 'content-type: application/json' -d '{\"authorization_model_id\":\"${MODEL_ID}\",\"tuple_key\":{\"user\":\"$1\",\"relation\":\"can_view\",\"object\":\"${OBJ}\"}}'")
  printf '%s' "$body" | jq -r 'if has("allowed") then (.allowed|tostring) else "error" end'
}

echo "Store: ${STORE_ID}"
echo "Model: ${MODEL_ID}"
echo

echo "--- baseline (no tuple): alice can_view should be denied ---"
report "can_view(alice) before write" "false" "$(check "$ALICE")"

echo "--- write ONE temporary tuple: ${OBJ}#owner@${ALICE} ---"
w=$(fga_curl "curl -sS -o /dev/null -w '%{http_code}' -X POST ${OPENFGA}/stores/${STORE_ID}/write -H 'content-type: application/json' -d '{\"authorization_model_id\":\"${MODEL_ID}\",\"writes\":{\"tuple_keys\":[{\"user\":\"${ALICE}\",\"relation\":\"owner\",\"object\":\"${OBJ}\"}]}}'")
report "write owner tuple -> HTTP 200" "200" "$w"

echo "--- positive: alice (owner) can_view -> allowed ---"
report "can_view(alice) after write" "true" "$(check "$ALICE")"

echo "--- negative: bob (unrelated) can_view -> denied ---"
report "can_view(bob)" "false" "$(check "$BOB")"

echo "--- cleanup: delete the temporary tuple ---"
d=$(fga_curl "curl -sS -o /dev/null -w '%{http_code}' -X POST ${OPENFGA}/stores/${STORE_ID}/write -H 'content-type: application/json' -d '{\"authorization_model_id\":\"${MODEL_ID}\",\"deletes\":{\"tuple_keys\":[{\"user\":\"${ALICE}\",\"relation\":\"owner\",\"object\":\"${OBJ}\"}]}}'")
report "delete owner tuple -> HTTP 200" "200" "$d"
report "can_view(alice) after delete" "false" "$(check "$ALICE")"

echo
if [[ $fail -eq 0 ]]; then
  echo "PASS — OpenFGA model loaded and resolving correctly (${pass}/${pass} checks). Store left clean."
  exit 0
else
  echo "FAIL — ${fail} check(s) failed, ${pass} passed."
  exit 1
fi
