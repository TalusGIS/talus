#!/usr/bin/env bash
# Invariant — basemap provisioning gates (decision-basemap-provisioning.md §5). The archive is a
# deliberate ANON byte path (public OSM derivative; embeds need it) but every write is admin-only,
# the serving route addresses exactly ONE fixed object, and a malformed upload can never reach the
# serving path:
#
#   [1] anon POST /admin/basemap/fetch  -> 401 (never starts an extract)
#   [2] non-admin POST /admin/basemap/fetch -> 403 (role-gated, not just signed-in)
#   [3] anon POST /admin/basemap/upload -> 401
#   [4] non-admin POST /admin/basemap/upload -> 403
#   [5] archive route serves exactly one object: a suffixed/traversal path is 404, never bytes
#   [6] admin upload of NON-pmtiles bytes -> 4xx rejected in staging (magic-bytes gate), and the
#       status endpoint never reports it installed as a result
#   [7] positive control: admin GET /admin/basemap -> 200 (proves [1]-[4] aren't a dead route)
#
# PRE-SLICE BASELINE (recorded 2026-07-23): routes absent — [1]-[4] returned 404 (wrong code for
# the contract), [5]-[7] FAIL outright; recorded per the negative-test-first discipline.
#
# Needs the running stack + .env (throwaways self-provisioned via _principals.sh). bash 3.2-safe; jq-free.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv19: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
FETCH="$API/admin/basemap/fetch"
UPLOAD="$API/admin/basemap/upload"
STATUS="$API/admin/basemap"
ARCHIVE="$API/basemap/archive"
jget(){ python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }
fail=0
BBOX_BODY='{"bbox":[-82.8,27.1,-82.3,27.5],"maxzoom":12}'

# [1] anon fetch
rc=$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d "$BBOX_BODY" "$FETCH")
echo "  [1] anon fetch -> $rc (expect 401)"
[ "$rc" = "401" ] || { echo "      FAIL: anon fetch must be 401, got $rc"; fail=1; }

# [2] non-admin fetch (throwaway viewer — no admin role)
# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh
tv_mint viewer || { echo "inv19: FAIL — no viewer token"; exit 1; }
VWR="$TV_VIEWER_TOKEN"
rc=$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $VWR" \
  -H 'Content-Type: application/json' -d "$BBOX_BODY" "$FETCH")
echo "  [2] non-admin fetch -> $rc (expect 403)"
case "$rc" in
  403) : ;;
  2*) echo "      FAIL: non-admin can start an extract ($rc)"; fail=1 ;;
  *)  echo "      FAIL: expected 403, got $rc"; fail=1 ;;
esac

# [3]/[4] upload gates (tiny garbage RAW body — the endpoint is python-multipart-free, the
# rasters-ingest pattern; must die at the gate, not the validator)
GARBAGE="$(mktemp "${TMPDIR:-/tmp}/inv19-garbage.XXXXXX")"
printf 'not a pmtiles archive' > "$GARBAGE"
rc=$(curl -sS -o /dev/null -w '%{http_code}' -X POST --data-binary "@$GARBAGE" "$UPLOAD")
echo "  [3] anon upload -> $rc (expect 401)"
[ "$rc" = "401" ] || { echo "      FAIL: anon upload must be 401, got $rc"; fail=1; }
rc=$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $VWR" --data-binary "@$GARBAGE" "$UPLOAD")
echo "  [4] non-admin upload -> $rc (expect 403)"
case "$rc" in
  403) : ;;
  2*) echo "      FAIL: non-admin can replace the basemap ($rc)"; fail=1 ;;
  *)  echo "      FAIL: expected 403, got $rc"; fail=1 ;;
esac

# [5] archive addresses exactly one object — suffix/traversal probes must be 404, never 2xx
for probe in "$ARCHIVE/../../media" "$ARCHIVE/extra" "$ARCHIVE%2f..%2fsecret"; do
  rc=$(curl -sS -o /dev/null -w '%{http_code}' "$probe")
  case "$rc" in
    2*) echo "  [5] probe $probe -> $rc  FAIL: suffixed archive path returned bytes"; fail=1 ;;
    *)  echo "  [5] probe -> $rc (not 2xx — ok)" ;;
  esac
done

# [6] admin upload of non-pmtiles bytes is rejected in staging (magic-bytes gate)
tv_mint admin || { echo "inv19: FAIL — no admin token"; exit 1; }
ADM="$TV_ADMIN_TOKEN"
before="$(curl -sS "$STATUS" -H "Authorization: Bearer $ADM" | jget 'd.get("installed")')"
rc=$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $ADM" --data-binary "@$GARBAGE" "$UPLOAD")
echo "  [6] admin garbage upload -> $rc (expect 4xx reject)"
case "$rc" in
  4*) : ;;
  *)  echo "      FAIL: malformed upload not rejected (got $rc)"; fail=1 ;;
esac
after="$(curl -sS "$STATUS" -H "Authorization: Bearer $ADM" | jget 'd.get("installed")')"
[ "${before:-ERR}" = "${after:-ERR2}" ] || { echo "      FAIL: rejected upload changed installed state ($before -> $after)"; fail=1; }
rm -f "$GARBAGE"

# [7] positive control — admin status readable
rc=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $ADM" "$STATUS")
echo "  [7] admin status -> $rc (expect 200)"
[ "$rc" = "200" ] || { echo "      FAIL: admin status control ($rc)"; fail=1; }

if [ "$fail" = "0" ]; then echo "inv19: PASS"; else echo "inv19: FAIL"; exit 1; fi
