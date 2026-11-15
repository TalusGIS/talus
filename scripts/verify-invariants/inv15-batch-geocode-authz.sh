#!/usr/bin/env bash
# Invariant (batch-geocoding slice) — the async batch-geocode surface is NEVER anonymous.
#
# The threat: batch geocoding provisions layers + reads hosted tables + spends credits. Every endpoint
# must challenge an unauthenticated caller (there is no public batch path). A leaked allow here would let
# the open internet create layers, read private tables, or burn an org's credits.
#
# DENY paths asserted token-free (inv10 style — expiry-immune):
#   1. anon run / preflight / job-read / rematch  -> 401 (auth required, never anon)
#   2. forged `alg:none` bearer on run            -> 401
#
# The role/ownership negatives (non-publisher run 403, non-owner job-read 403) need live tokens and live
# in verify-batch-geocoding.sh; this invariant stays token-free so it never flakes on an expired token.
set -u

API_BASE="${API_BASE:-http://localhost:8000/api/v1}"
FORGED='eyJhbGciOiJub25lIn0.eyJzdWIiOiJoYWNrZXIifQ.'
JSON='Content-Type: application/json'
RUN_BODY='{"locator":"x","columns":["a"],"csvText":"a\n1\n"}'

fail=0
echo "inv15: batch-geocode surface = $API_BASE/geocode/batch/*"

post_anon(){ curl -sS -o /dev/null -w '%{http_code}' -X POST "$API_BASE/$1" -H "$JSON" -d "$2"; }
get_anon(){ curl -sS -o /dev/null -w '%{http_code}' "$API_BASE/$1"; }

check(){ # $1 label  $2 status
  echo "  [$1] -> $2"
  case "$2" in
    401) : ;;
    2*) echo "      FAIL: anon reached the batch surface (leaked allow)"; fail=1 ;;
    *)  echo "      FAIL: expected 401, got $2 (stack down? wrong API_BASE?)"; fail=1 ;;
  esac
}

check "anon run"       "$(post_anon geocode/batch/run "$RUN_BODY")"
check "anon preflight" "$(post_anon geocode/batch/preflight '{"locator":"x"}')"
check "anon job read"  "$(get_anon 'geocode/batch/jobs/00000000-0000-0000-0000-000000000000')"
check "anon rematch"   "$(post_anon 'geocode/batch/tal_00000000/rematch' '{}')"

c=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$API_BASE/geocode/batch/run" \
  -H "Authorization: Bearer $FORGED" -H "$JSON" -d "$RUN_BODY")
check "forged alg:none run" "$c"

if [ "$fail" -eq 0 ]; then echo "inv15: PASS"; else echo "inv15: FAIL"; fi
exit "$fail"
