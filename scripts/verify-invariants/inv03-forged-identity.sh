#!/usr/bin/env bash
# Invariant C3 — identity comes from the validated token, NEVER from the request payload.
#
# POST a feature with a forged created_by/updated_by/user_id in the body and assert the stored
# created_by is the TOKEN's identity, not the forged value. The write path sets created_by/updated_by
# server-side from the principal (layer_features.py), so a hostile client cannot forge authorship.
#
# Needs the running stack + .env (throwaway self-provisioned via _principals.sh). bash 3.2-safe; -sS curl.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv03: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
FORGED="00000000-0000-0000-0000-000000000000"
fail=0

# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh
tv_mint publisher || { echo "inv03: FAIL — no token"; exit 1; }
USER="$TV_PUBLISHER_USER"
H="Authorization: Bearer $TV_PUBLISHER_TOKEN"

# the token's real platform.users id (what created_by MUST end up being) — by username, no /me endpoint
me_id=$(docker compose exec -T postgres psql -U talus -d talusgis -tA < /dev/null \
  -c "SELECT id FROM platform.users WHERE username='${USER}';" | tr -d '[:space:]')
echo "  token identity ($USER): ${me_id:-<unknown>}"

LID=$(curl -sS -X POST -H "$H" -H 'Content-Type: application/json' \
  -d '{"display_name":"inv03 forged-id test","geom_type":"point","working_srid":4326,"native_storage":false}' \
  "$API/layers" | python3 -c "import sys,json;print(json.load(sys.stdin).get('layer_id',''))" 2>/dev/null)
[ -n "$LID" ] || { echo "inv03: FAIL — layer create failed"; exit 1; }
echo "  layer $LID"

# POST a feature with a FORGED created_by/updated_by/user_id in the body
FID=$(curl -sS -X POST -H "$H" -H 'Content-Type: application/json' \
  -d "{\"lon\":-82.5,\"lat\":27.3,\"created_by\":\"$FORGED\",\"updated_by\":\"$FORGED\",\"user_id\":\"$FORGED\"}" \
  "$API/layers/${LID}/features" | python3 -c "import sys,json;print(json.load(sys.stdin).get('id',''))" 2>/dev/null)
echo "  [1] feature created -> ${FID:-<none>}"
if [ -z "$FID" ]; then echo "      FAIL: create returned no id"; fail=1; fi

if [ -n "$FID" ]; then
  stored=$(docker compose exec -T postgres psql -U talus -d talusgis -tA < /dev/null \
    -c "SELECT created_by::text FROM assets.${LID} WHERE id='${FID}';" | tr -d '[:space:]')
  echo "  [2] stored created_by       -> $stored"
  echo "      forged (must NOT match)  -> $FORGED"
  if [ "$stored" = "$FORGED" ]; then
    echo "      FAIL: payload created_by was trusted — C3 violated"; fail=1
  elif [ -n "$me_id" ] && [ "$stored" != "$me_id" ]; then
    echo "      FAIL: created_by ($stored) is neither the forged value nor the token id ($me_id)"; fail=1
  else
    echo "      OK: created_by is the token identity, not the payload"
  fi
fi

curl -sS -o /dev/null -X DELETE -H "$H" "$API/layers/${LID}"
if [ "$fail" -eq 0 ]; then echo "inv03: PASS"; else echo "inv03: FAIL"; fi
exit "$fail"
