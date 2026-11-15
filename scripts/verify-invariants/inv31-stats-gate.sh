#!/usr/bin/env bash
# Invariant (dashboards F1b) — the bounded stats door reveals nothing past the view gate.
#
# The model (decision-dashboards.md §5): GET /layers/{id}/stats rides EXACTLY the /features
# optional-auth gate (_require_registered + _authorize_read, same file) and a closed grammar
# (agg frozenset, validated field/groupBy, _parse_filters). An aggregate is a (coarse) data
# oracle — count alone confirms row existence — so it must deny exactly like /features. The
# anon scrub is EXTENDED here: an anonymous caller may not aggregate or group by the
# editor-identity columns /features scrubs (created_by/updated_by/ai_provenance).
#
# Setup: throwaway publisher creates a fresh PRIVATE layer. Legs:
#   1. anon stats                    -> 404 (existence-hiding; never 2xx)
#   2. forged `alg:none` token       -> 401
#   3. signed-in wrong-user (viewer) -> 403 (view gate)
#   4. owner count                   -> 200 (positive control, value 0)
#   5. owner bad agg                 -> 422 (whitelist holds)
#   6. owner unknown field           -> 422 (column validation holds)
#   -- make the layer PUBLIC (direct public_shared tuple, inv08 pattern) --
#   7. anon count on public layer    -> 200 (the allow path works)
#   8. anon groupBy=created_by       -> 422 (scrub extension — no editor-identity aggregation)
#   9. anon agg field=created_by     -> 422 (same, aggregate side)
#
# Deny rule: any 2xx from anon-on-private/forged/wrong-user = leaked allow = FAIL.
# Needs the running stack + .env (throwaways via _principals.sh). bash 3.2-safe; -sS curl.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv31: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
FORGED='eyJhbGciOiJub25lIn0.eyJzdWIiOiJoYWNrZXIifQ.'
fail=0

# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh
tv_mint publisher || { echo "inv31: FAIL — no owner token"; exit 1; }
OWNER="$TV_PUBLISHER_TOKEN"
tv_mint viewer || { echo "inv31: FAIL — no viewer token"; exit 1; }
WRONG="$TV_VIEWER_TOKEN"

LID=$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H 'Content-Type: application/json' \
  -d '{"display_name":"inv31 stats gate test","geom_type":"point","working_srid":4326,"native_storage":false}' \
  "$API/layers" | python3 -c "import sys,json;print(json.load(sys.stdin).get('layer_id',''))" 2>/dev/null)
[ -n "$LID" ] || { echo "inv31: FAIL — layer create failed"; exit 1; }
echo "inv31: private layer $LID (owned by $TV_PUBLISHER_USER, unshared)"
S="$API/layers/${LID}/stats"

deny_leg() { # <label> <expect-note> <code>
  echo "  [$1] -> $3 ($2)"
  case "$3" in
    401|403|404) : ;;
    2*) echo "      FAIL: leaked allow"; fail=1 ;;
    *)  echo "      FAIL: unexpected status $3 (stack down? wrong API_BASE?)"; fail=1 ;;
  esac
}
want() { # <label> <expect> <code>
  echo "  [$1] -> $3 (expect $2)"
  [ "$3" = "$2" ] || { echo "      FAIL"; fail=1; }
}

deny_leg "1 anon on private          " "expect 404" \
  "$(curl -sS -o /dev/null -w '%{http_code}' "$S?agg=count")"
deny_leg "2 forged alg:none          " "expect 401" \
  "$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $FORGED" "$S?agg=count")"
deny_leg "3 wrong-user (viewer)      " "expect 403" \
  "$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $WRONG" "$S?agg=count")"
want "4 owner count (control)    " 200 \
  "$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $OWNER" "$S?agg=count")"
want "5 owner bad agg            " 422 \
  "$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $OWNER" "$S?agg=median")"
want "6 owner unknown field      " 422 \
  "$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $OWNER" "$S?agg=sum&field=no_such_col")"

# ---- anon master switch (decision-public-sharing-reachable.md) ----------------------------
# The public legs below need it ON, and a CLEAN install ships it OFF (PUBLIC_ANON_ACCESS_ENABLED=
# false, column NULL) — found 2026-09-10 when this leg first ran on a clean box and read
# the correct OFF behaviour as a FAIL. Flip the COLUMN for this leg's duration and restore it
# EXACTLY (NULL stays NULL) in leg_cleanup, the inv29 pattern; the api reads it live per request.
psql_q(){ docker compose exec -T postgres psql -U "${TV_PGUSER:-talus}" -d "${TV_PGDB:-talusgis}" -tA -c "$1" < /dev/null 2>/dev/null | tr -d '[:space:]'; }
INV31_ORIG_COL="$(psql_q "SELECT public_sharing_enabled FROM platform.org_settings WHERE id = true;")"
leg_cleanup(){
  case "$INV31_ORIG_COL" in
    t) psql_q "UPDATE platform.org_settings SET public_sharing_enabled = true WHERE id = true;" >/dev/null ;;
    f) psql_q "UPDATE platform.org_settings SET public_sharing_enabled = false WHERE id = true;" >/dev/null ;;
    *) psql_q "UPDATE platform.org_settings SET public_sharing_enabled = NULL WHERE id = true;" >/dev/null ;;
  esac
}
psql_q "UPDATE platform.org_settings SET public_sharing_enabled = true WHERE id = true;" >/dev/null
echo "  anon master switch: column was '${INV31_ORIG_COL:-NULL}' -> ON for this leg (restored by leg_cleanup)"

# make it public_shared (direct tuple, inv08 pattern) -> anon may read, but never editor identity
tv_env OPENFGA_STORE_ID >/dev/null || true
STORE=$(tv_env OPENFGA_STORE_ID); MODEL=$(tv_env OPENFGA_MODEL_ID)
docker compose run --rm -T openfga-cli -c "curl -sS -X POST http://openfga:8080/stores/${STORE}/write -H 'content-type: application/json' -d '{\"authorization_model_id\":\"${MODEL}\",\"writes\":{\"tuple_keys\":[{\"user\":\"user:*\",\"relation\":\"public_shared\",\"object\":\"layer:${LID}\"}]}}'" >/dev/null 2>&1

want "7 anon count on PUBLIC     " 200 \
  "$(curl -sS -o /dev/null -w '%{http_code}' "$S?agg=count")"
want "8 anon groupBy=created_by  " 422 \
  "$(curl -sS -o /dev/null -w '%{http_code}' "$S?agg=count&groupBy=created_by")"
want "9 anon max field=created_by" 422 \
  "$(curl -sS -o /dev/null -w '%{http_code}' "$S?agg=max&field=created_by")"

# cleanup: drop the public tuple + the layer (owner delete); principals via the _principals trap
docker compose run --rm -T openfga-cli -c "curl -sS -X POST http://openfga:8080/stores/${STORE}/write -H 'content-type: application/json' -d '{\"authorization_model_id\":\"${MODEL}\",\"deletes\":{\"tuple_keys\":[{\"user\":\"user:*\",\"relation\":\"public_shared\",\"object\":\"layer:${LID}\"}]}}'" >/dev/null 2>&1
curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/layers/$LID"

if [ "$fail" = 0 ]; then echo "inv31: PASS"; else echo "inv31: FAIL"; exit 1; fi
