#!/usr/bin/env bash
# Invariant — the backup DESTINATION endpoints are admin-only, the endpoint fence holds live, and
# the secret key never appears in any response (decision-backup-destination.md §3; A.1-★: a client
# that calls out holding a credential, where a user chooses where it points — the gate + the fence
# + the no-read-back rule ARE the control):
#
#   [1-3] anonymous PUT / POST test / DELETE  → 401 (never reachable unauthenticated)
#   [4-6] authenticated NON-admin (viewer)    → 403 (role-gated, not just signed-in)
#   [7]   admin PUT with a SCHEME-LESS endpoint → 422 (the F4 fence, live — scheme-less used to
#         degrade to secure=False plaintext)
#   [8]   admin PUT valid values → 200; the response shows source=override + accessKey +
#         secretKeySet=true, has NO secretKey property, and the SECRET STRING ITSELF appears
#         NOWHERE in the body (the strongest scrub available)
#   [9]   admin POST test against a guaranteed-unresolvable endpoint (.invalid, RFC 2606) → 200
#         with ok=false and a terse reason that also never echoes the secret
#   [10]  admin DELETE → 200, source back to env (positive control + cleanup in one)
#
# [8] briefly sets a real override on the live singleton; [10] (run even after a failure) reverts
# it. A scheduled backup firing inside that window would record one honest error row — acceptable.
#
# PRE-MIGRATION BASELINE (recorded 2026-08-23; endpoint code live via --reload, V090 NOT yet
# applied): [1]-[7] and [9] already held — the auth deps and the pydantic fence run BEFORE any DB
# read, and the test path with an explicit secretKey touches no DB — while [8] and [10] FAILED 500
# (UndefinedColumn on the V090 columns). Recorded per the negative-test-first discipline; full
# green requires the V090 migration.
#
# Needs the running stack + .env (throwaways self-provisioned via _principals.sh). bash 3.2-safe; jq-free.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv26: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
URL="$API/admin/backups/destination"
jget(){ python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }
fail=0

VALID='{"endpoint":"https://inv26-probe.invalid","bucket":"inv26-bucket","accessKey":"inv26-ak","secretKey":"inv26-secret-value"}'
SCHEMELESS='{"endpoint":"inv26-probe.invalid","accessKey":"inv26-ak","secretKey":"inv26-secret-value"}'

# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh

# [1]-[3] anonymous
for m in PUT POST DELETE; do
  u="$URL"; [ "$m" = "POST" ] && u="$URL/test"
  body=""; [ "$m" != "DELETE" ] && body="$VALID"
  if [ -n "$body" ]; then
    rc=$(curl -sS -o /dev/null -w '%{http_code}' -X "$m" -H 'Content-Type: application/json' -d "$body" "$u")
  else
    rc=$(curl -sS -o /dev/null -w '%{http_code}' -X "$m" "$u")
  fi
  echo "  [anon $m] -> $rc (expect 401)"
  [ "$rc" = "401" ] || { echo "      FAIL: anon $m must be 401, got $rc"; fail=1; }
done

# [4]-[6] authenticated non-admin
tv_mint viewer || { echo "inv26: FAIL — no viewer token"; exit 1; }
for m in PUT POST DELETE; do
  u="$URL"; [ "$m" = "POST" ] && u="$URL/test"
  body=""; [ "$m" != "DELETE" ] && body="$VALID"
  if [ -n "$body" ]; then
    rc=$(curl -sS -o /dev/null -w '%{http_code}' -X "$m" -H "Authorization: Bearer $TV_VIEWER_TOKEN" \
         -H 'Content-Type: application/json' -d "$body" "$u")
  else
    rc=$(curl -sS -o /dev/null -w '%{http_code}' -X "$m" -H "Authorization: Bearer $TV_VIEWER_TOKEN" "$u")
  fi
  echo "  [viewer $m] -> $rc (expect 403)"
  case "$rc" in
    403) : ;;
    2*) echo "      FAIL: non-admin can steer the backup credential ($rc)"; fail=1 ;;
    *)  echo "      FAIL: expected 403, got $rc"; fail=1 ;;
  esac
done

# [7]-[10] admin
tv_mint admin || { echo "inv26: FAIL — no admin token"; exit 1; }
ADM="$TV_ADMIN_TOKEN"

# [7] the F4 fence, live
rc=$(curl -sS -o /dev/null -w '%{http_code}' -X PUT -H "Authorization: Bearer $ADM" \
     -H 'Content-Type: application/json' -d "$SCHEMELESS" "$URL")
echo "  [7] admin PUT scheme-less endpoint -> $rc (expect 422)"
[ "$rc" = "422" ] || { echo "      FAIL: scheme-less endpoint accepted ($rc)"; fail=1; }

# [8] admin PUT valid -> override visible, secret NEVER
BODY="$(curl -sS -X PUT -H "Authorization: Bearer $ADM" -H 'Content-Type: application/json' \
        -d "$VALID" "$URL")"
SRC="$(printf '%s' "$BODY" | jget 'd["config"]["destination"]["source"]')"
AK="$(printf '%s' "$BODY" | jget 'd["config"]["destination"]["accessKey"]')"
SET="$(printf '%s' "$BODY" | jget 'd["config"]["destination"]["secretKeySet"]')"
HASKEY="$(printf '%s' "$BODY" | jget '"yes" if "secretKey" in d["config"]["destination"] else "no"')"
echo "  [8] admin PUT valid -> source=${SRC:-?} accessKey=${AK:-?} secretKeySet=${SET:-?} secretKey-property=${HASKEY:-?}"
[ "$SRC" = "override" ] || { echo "      FAIL: override not effective ($(printf '%.200s' "$BODY"))"; fail=1; }
[ "$AK" = "inv26-ak" ]  || { echo "      FAIL: accessKey not echoed for the edit form"; fail=1; }
[ "$SET" = "True" ]     || { echo "      FAIL: secretKeySet should be true"; fail=1; }
[ "$HASKEY" = "no" ]    || { echo "      FAIL: a secretKey property exists in the response"; fail=1; }
case "$BODY" in
  *inv26-secret-value*) echo "      FAIL: THE SECRET ITSELF IS IN THE RESPONSE BODY"; fail=1 ;;
esac

# [9] connection test against .invalid -> 200, ok=false, secret never echoed
TBODY="$(curl -sS -X POST -H "Authorization: Bearer $ADM" -H 'Content-Type: application/json' \
         -d "$VALID" "$URL/test")"
TOK="$(printf '%s' "$TBODY" | jget 'd["ok"]')"
echo "  [9] admin POST test (.invalid endpoint) -> ok=${TOK:-?} (expect False)"
[ "$TOK" = "False" ] || { echo "      FAIL: test against an unresolvable endpoint reported ok ($(printf '%.200s' "$TBODY"))"; fail=1; }
case "$TBODY" in
  *inv26-secret-value*) echo "      FAIL: the secret leaked into the test detail"; fail=1 ;;
esac

# [10] DELETE reverts to env (positive control + cleanup — runs even if earlier legs failed)
DBODY="$(curl -sS -X DELETE -H "Authorization: Bearer $ADM" "$URL")"
DSRC="$(printf '%s' "$DBODY" | jget 'd["config"]["destination"]["source"]')"
echo "  [10] admin DELETE -> source=${DSRC:-?} (expect env)"
[ "$DSRC" = "env" ] || { echo "      FAIL: override did not clear ($(printf '%.200s' "$DBODY"))"; fail=1; }

if [ "$fail" = "0" ]; then echo "inv26: PASS"; else echo "inv26: FAIL"; exit 1; fi
