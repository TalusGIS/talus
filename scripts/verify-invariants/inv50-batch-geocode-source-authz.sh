#!/usr/bin/env bash
# Invariant (decision-batch-geocoding.md, rider 2026-11-06) — batch geocoding from a table already in
# Talus reads only a table the caller can VIEW. `POST /geocode/batch/run` with `sourceLayerId` copies
# that table's rows into a new layer the caller owns and sends its address text to a locator, so a
# publisher who names a table they cannot view must be refused before a row is read.
#
# The run door checks the locator first, then the source. So the fixture needs a locator that can
# batch, or every leg would stop at the locator and prove nothing about the source. The leg makes its
# own: a Nominatim locator at an unreachable host (batch-capable by its type; never called, because
# every leg below stops before any address is geocoded). It is enabled for the seconds the leg runs
# and removed in cleanup.
#
# Legs:
#   1. a publisher WITHOUT view names the private table  -> 403, the source gate's own words
#   2. ... and nothing was created for them              -> no new layer in their catalog
#   3. the same publisher names something that is not a hosted layer -> 422
#   4. CONTROL: the owner, same locator, same table      -> passes the source gate (reaches the next
#      check, 422 "columns not found") — so leg 1's 403 is the source gate and not the fixture
# Needs a working realm (tv_mint). bash 3.2-safe.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv50: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
fail=0
# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh
tv_mint admin || { echo "inv50: FAIL — no admin principal"; exit 1; }
ADMIN="$TV_ADMIN_TOKEN"
tv_mint publisher || { echo "inv50: FAIL — no publisher principal"; exit 1; }
OTHER="$TV_PUBLISHER_TOKEN"
JSON='Content-Type: application/json'
jget() { python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }
leg() { if [ "$2" = "$3" ]; then echo "  PASS $1 ($3)"; else echo "  FAIL $1 (expected $2, got $3)"; fail=1; fi; }
# status + body in one call: prints "<status> <body>"
call() { # $1 token  $2 json body
  _t="$(mktemp)"
  curl -sS -o "$_t" -w '%{http_code}' -X POST -H "Authorization: Bearer $1" -H "$JSON" \
    -d "$2" "$API/geocode/batch/run"; printf ' '; cat "$_t" 2>/dev/null; rm -f "$_t"
}
LOC_NAME="inv50 fixture locator $$"
LA=""; GID=""
leg_cleanup() {
  [ -n "$GID" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $ADMIN" "$API/geocoders/$GID"
  [ -n "$LA" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $ADMIN" "$API/layers/$LA"
}

echo "inv50: batch geocoding reads only a table the caller can view"
LA="$(curl -sS -X POST -H "Authorization: Bearer $ADMIN" -H "$JSON" \
  -d '{"display_name":"inv50 private source","geom_type":"point","working_srid":4326,"native_storage":false}' \
  "$API/layers" | jget 'd.get("layer_id") or d.get("layerId") or d.get("id")')"
case "$LA" in tal_*) : ;; *) echo "inv50: FAIL — fixture layer create ('$LA')"; exit 1 ;; esac
GID="$(python3 -c 'import json,sys;print(json.dumps({"name":sys.argv[1],"type":"nominatim","url":"https://nominatim.invalid"}))' "$LOC_NAME" \
  | curl -sS -X POST -H "Authorization: Bearer $ADMIN" -H "$JSON" -d @- "$API/geocoders" | jget 'd["id"]')"
[ -n "$GID" ] || { echo "inv50: FAIL — fixture locator create"; exit 1; }

body() { python3 -c 'import json,sys;print(json.dumps({"locator":sys.argv[1],"columns":["inv50_no_such_column"],"sourceLayerId":sys.argv[2],"displayName":"inv50 must not exist"}))' "$LOC_NAME" "$1"; }

before="$(curl -sS -H "Authorization: Bearer $OTHER" "$API/me/layers" | jget 'len(d["layers"])')"

echo "inv50 leg 1 — a publisher without view is refused at the source"
r="$(call "$OTHER" "$(body "$LA")")"
leg "publisher without view" 403 "${r%% *}"
case "${r#* }" in
  *"not authorized to read"*) echo "  PASS the refusal is the source gate's" ;;
  *) echo "  FAIL the 403 is not the source gate's (${r#* })"; fail=1 ;;
esac

echo "inv50 leg 2 — nothing was created for them"
after="$(curl -sS -H "Authorization: Bearer $OTHER" "$API/me/layers" | jget 'len(d["layers"])')"
leg "their catalog is unchanged" "${before:-x}" "${after:-y}"

echo "inv50 leg 3 — a source that is not a hosted layer"
r="$(call "$OTHER" "$(body "platform.users")")"
leg "not a hosted layer" 422 "${r%% *}"

echo "inv50 leg 4 — control: the owner passes the source gate"
r="$(call "$ADMIN" "$(body "$LA")")"
leg "owner reaches the next check" 422 "${r%% *}"
case "${r#* }" in
  *"columns not found"*) echo "  PASS the owner's 422 is the columns check, past the source gate" ;;
  *) echo "  FAIL the owner did not reach the columns check (${r#* })"; fail=1 ;;
esac

if [ "$fail" -eq 0 ]; then echo "inv50: PASS"; else echo "inv50: FAIL"; fi
exit "$fail"
