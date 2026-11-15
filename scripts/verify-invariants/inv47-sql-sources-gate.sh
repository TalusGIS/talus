#!/usr/bin/env bash
# Invariant — the SQL-source pull doors (decision-sql-source-pull.md s1; F11 amended 2026-10-01) are
# PUBLISHER-gated and PER-USER, a stored password never leaves the server, and the never-list refuses
# this deployment's own services. A NEW client that holds a credential and opens a server-side
# connection to a host the customer names (the ★ A.1 question) — the gate, the ownership fence and the
# never-list ARE the control:
#
#   [1] anonymous GET /sql-connections → 401
#   [2] viewer GET → 403 · viewer POST → 403 · viewer GET /sql-pulls → 403 (publisher SPECIFICALLY)
#   [3] publisher A POST with a private literal (10.255.255.1 — allowed by class, unreachable) → 201
#       (positive control: the door is live; the response carries hasPassword:true and NEVER the password)
#   [4] publisher A GET list → the password string appears nowhere in the body
#   [5] publisher A POST with each never-listed host → 422 address_denied:
#       localhost · postgres · openfga · keycloak · api · 127.0.0.1 · 169.254.169.254 · talusgis-postgres
#  [5b] the live compose-bridge IP of keycloak-postgres / openfga-postgres → 422 address_denied (by ADDRESS)
#   [6] publisher A POST dialect=oracle → 422 dialect_unavailable (a slot refuses before any network; sqlserver live since 2026-10-01, mysql since 2026-10-02)
#   [7] publisher A /test on [3] → 200 with ok:false (unreachable host; a scrubbed reason, no password)
#   [8] ★ publisher B: GET list does NOT contain A's row; PUT / test / catalog / DELETE on A's id → 404
#       (existence not disclosed); publisher B GET /sql-connections/all → 403 (admin-only overview);
#       B creating a connection with A's exact NAME → 201 (V111: names unique per owner, never a 409 oracle)
#   [9] admin GET /sql-connections/all → contains A's row with ownerName, hasPassword:true, NO password
#  [10] the api log since [3] does not contain the password
#  [11] the row rests as Fernet ciphertext (starts with 'gAAAA'), never plaintext
#  [12] cleanup: ADMIN DELETE → 204 (oversight); a second DELETE → 404
#
# Needs the running stack + .env (throwaways via _principals.sh). bash 3.2-safe; jq-free.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv47: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
URL="$API/sql-connections"
JSON='Content-Type: application/json'
jget(){ python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }
fail=0
CID="" ADMIN="" PUBA=""
PW="inv47-Secr3t-$(date +%s)-$$"
leg_cleanup(){
  if [ -n "$CID" ]; then
    curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer ${ADMIN:-$PUBA}" "$URL/$CID" || true
  fi
}
# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh

body(){ # <name> <host> [dialect]
  printf '{"name":"%s","dialect":"%s","host":"%s","port":5432,"database":"billing","username":"talus_ro","password":"%s","tlsMode":"disable","timezone":"America/Chicago"}' \
    "$1" "${3:-postgresql}" "$2" "$PW"
}
T0=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

# [1] anonymous
rc=$(curl -sS -o /dev/null -w '%{http_code}' "$URL")
echo "  [1] anon GET -> $rc (expect 401)"; [ "$rc" = "401" ] || { echo "      FAIL"; fail=1; }

# [2] viewer: authenticated, wrong role
tv_mint viewer || { echo "inv47: FAIL — no viewer token"; exit 1; }
rc=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TV_VIEWER_TOKEN" "$URL")
echo "  [2a] viewer GET -> $rc (expect 403)"; [ "$rc" = "403" ] || { echo "      FAIL"; fail=1; }
rc=$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $TV_VIEWER_TOKEN" -H "$JSON" \
     --data "$(body inv47-viewer 10.255.255.1)" "$URL")
echo "  [2b] viewer POST -> $rc (expect 403)"
case "$rc" in 403) : ;; 2*) echo "      FAIL: a viewer can create a connection ($rc)"; fail=1 ;; *) echo "      FAIL: expected 403, got $rc"; fail=1 ;; esac
rc=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TV_VIEWER_TOKEN" "$API/sql-pulls")
echo "  [2c] viewer GET /sql-pulls -> $rc (expect 403)"; [ "$rc" = "403" ] || { echo "      FAIL"; fail=1; }

# [3] publisher A positive control
tv_mint publisher || { echo "inv47: FAIL — no publisher token"; exit 1; }
PUBA="$TV_PUBLISHER_TOKEN"
OUT=$(curl -sS -w '\n%{http_code}' -X POST -H "Authorization: Bearer $PUBA" -H "$JSON" --data "$(body "inv47-$(date +%s)" 10.255.255.1)" "$URL")
rc=$(printf '%s' "$OUT" | tail -1); BODY=$(printf '%s' "$OUT" | sed '$d')
CID=$(printf '%s' "$BODY" | jget 'd.get("id","")')
echo "  [3] publisher POST (private literal) -> $rc, id: ${CID:-none} (expect 201)"
[ "$rc" = "201" ] && [ -n "$CID" ] || { echo "      FAIL: $(printf '%.200s' "$BODY")"; fail=1; }
hp=$(printf '%s' "$BODY" | jget 'd.get("hasPassword")')
[ "$hp" = "True" ] || { echo "      FAIL: hasPassword not true ($hp)"; fail=1; }
case "$BODY" in *"$PW"*) echo "      FAIL: the password is in the create response"; fail=1 ;; *) echo "      password absent from the create response" ;; esac

# [4] list never carries the password
LIST=$(curl -sS -H "Authorization: Bearer $PUBA" "$URL")
case "$LIST" in *"$PW"*) echo "  [4] FAIL: the password is in the list body"; fail=1 ;; *) echo "  [4] list body: password absent" ;; esac

# [5] the never-list
for h in localhost postgres openfga keycloak api 127.0.0.1 169.254.169.254 talusgis-postgres; do
  OUT=$(curl -sS -w '\n%{http_code}' -X POST -H "Authorization: Bearer $PUBA" -H "$JSON" --data "$(body "inv47-never-$h" "$h")" "$URL")
  rc=$(printf '%s' "$OUT" | tail -1); B=$(printf '%s' "$OUT" | sed '$d')
  reason=$(printf '%s' "$B" | jget 'd["detail"]["reason"]')
  if [ "$rc" = "422" ] && [ "$reason" = "address_denied" ]; then echo "  [5] $h -> 422 address_denied"
  else echo "  [5] FAIL: $h -> $rc $(printf '%.120s' "$B")"; fail=1
       nid=$(printf '%s' "$B" | jget 'd.get("id","")'); [ -n "$nid" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $PUBA" "$URL/$nid"; fi
done

# [5b] ★ the by-ADDRESS half of the never-list (security review 2026-10-01): the live compose-bridge
#      IP of a sibling database is refused like its name
for c in talusgis-keycloak-postgres talusgis-openfga-postgres; do
  ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$c" 2>/dev/null | head -1)
  if [ -z "$ip" ]; then echo "  [5b] $c: no container IP (skipped)"; continue; fi
  OUT=$(curl -sS -w '\n%{http_code}' -X POST -H "Authorization: Bearer $PUBA" -H "$JSON" --data "$(body "inv47-sib-$c" "$ip")" "$URL")
  rc=$(printf '%s' "$OUT" | tail -1); B=$(printf '%s' "$OUT" | sed '$d'); reason=$(printf '%s' "$B" | jget 'd["detail"]["reason"]')
  if [ "$rc" = "422" ] && [ "$reason" = "address_denied" ]; then echo "  [5b] $c ($ip) -> 422 address_denied"
  else echo "  [5b] FAIL: $c ($ip) -> $rc $(printf '%.120s' "$B")"; fail=1
       nid=$(printf '%s' "$B" | jget 'd.get("id","")'); [ -n "$nid" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $PUBA" "$URL/$nid"; fi
done

# [6] a slot dialect refuses before any network
OUT=$(curl -sS -w '\n%{http_code}' -X POST -H "Authorization: Bearer $PUBA" -H "$JSON" --data "$(body inv47-oracle 10.255.255.1 oracle)" "$URL")
rc=$(printf '%s' "$OUT" | tail -1); reason=$(printf '%s' "$OUT" | sed '$d' | jget 'd["detail"]["reason"]')
if [ "$rc" = "422" ] && [ "$reason" = "dialect_unavailable" ]; then echo "  [6] oracle (a slot) -> 422 dialect_unavailable"; else echo "  [6] FAIL: $rc $reason"; fail=1; fi

# [7] test door: scrubbed failure, nothing leaks
if [ -n "$CID" ]; then
  T=$(curl -sS -X POST -H "Authorization: Bearer $PUBA" "$URL/$CID/test")
  ok=$(printf '%s' "$T" | jget 'd.get("ok")'); reason=$(printf '%s' "$T" | jget 'd.get("reason")')
  echo "  [7] test -> ok=$ok reason=$reason"
  [ "$ok" = "False" ] || { echo "      FAIL: an unreachable host tested ok"; fail=1; }
  case "$T" in *"$PW"*) echo "      FAIL: password in the test result"; fail=1 ;; esac
  case "$T" in *"10.255.255.1"*) echo "      note: the host the ADMIN TYPED is named (allowed: it is their input, not a resolved detail)" ;; esac
fi

# [8] ★ per-user: a SECOND publisher cannot see, use, or remove A's connection
tv_mint publisher || { echo "inv47: FAIL — no second publisher token"; exit 1; }   # a SECOND throwaway publisher (A's token is saved in PUBA)
PUBB="$TV_PUBLISHER_TOKEN"
if [ -n "$CID" ]; then
  LB=$(curl -sS -H "Authorization: Bearer $PUBB" "$URL")
  case "$LB" in *"$CID"*) echo "  [8a] FAIL: publisher B's list shows A's connection"; fail=1 ;; *) echo "  [8a] publisher B list: A's row absent" ;; esac
  for leg in "PUT $URL/$CID" "POST $URL/$CID/test" "GET $URL/$CID/catalog" "DELETE $URL/$CID"; do
    m=${leg%% *}; u=${leg#* }
    if [ "$m" = "PUT" ]; then rc=$(curl -sS -o /dev/null -w '%{http_code}' -X PUT -H "Authorization: Bearer $PUBB" -H "$JSON" --data "$(body inv47-steal 10.255.255.1)" "$u")
    else rc=$(curl -sS -o /dev/null -w '%{http_code}' -X "$m" -H "Authorization: Bearer $PUBB" "$u"); fi
    [ "$rc" = "404" ] && echo "  [8b] publisher B $m on A's id -> 404" || { echo "  [8b] FAIL: publisher B $m -> $rc (expect 404)"; fail=1; }
  done
  rc=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $PUBB" "$URL/all")
  [ "$rc" = "403" ] && echo "  [8c] publisher B GET /all -> 403" || { echo "  [8c] FAIL: /all for a publisher -> $rc"; fail=1; }
  # [8e] s3: the schedule door is owner-only too — B on a random pull id (and on nothing of A's) → 404, never 2xx
  rc=$(curl -sS -o /dev/null -w '%{http_code}' -X PUT -H "Authorization: Bearer $PUBB" -H "$JSON" --data '{"kind":"manual"}' "$API/sql-pulls/00000000-0000-0000-0000-000000000000/schedule")
  [ "$rc" = "404" ] && echo "  [8e] publisher B PUT /sql-pulls/{other}/schedule -> 404" || { echo "  [8e] FAIL: schedule door -> $rc"; fail=1; }
  # [8d] V111: a connection NAME is unique per owner — B reusing A's exact name must succeed (a 409 here would
  #      tell B that A has a connection by that name: existence disclosed by a constraint)
  ANAME=$(printf '%s' "$BODY" | jget 'd.get("name","")')
  OUT=$(curl -sS -w '\n%{http_code}' -X POST -H "Authorization: Bearer $PUBB" -H "$JSON" --data "$(body "$ANAME" 10.255.255.2)" "$URL")
  rc=$(printf '%s' "$OUT" | tail -1); BID=$(printf '%s' "$OUT" | sed '$d' | jget 'd.get("id","")')
  if [ "$rc" = "201" ] && [ -n "$BID" ]; then echo "  [8d] publisher B reuses A's name -> 201 (per-owner uniqueness)"
       curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $PUBB" "$URL/$BID"
  else echo "  [8d] FAIL: B reusing A's name -> $rc (expect 201; 409 discloses A's name)"; fail=1; fi
fi

# [9] the admin overview: the row with its owner, never the password
tv_mint admin || { echo "inv47: FAIL — no admin token"; exit 1; }
ADMIN="$TV_ADMIN_TOKEN"
ALL=$(curl -sS -H "Authorization: Bearer $ADMIN" "$URL/all")
case "$ALL" in *"$CID"*) echo "  [9] admin /all shows A's row (owner: $(printf '%s' "$ALL" | jget '[c for c in d if c["id"]=="'"$CID"'"][0]["ownerName"]'))" ;; *) echo "  [9] FAIL: admin /all lacks the row"; fail=1 ;; esac
case "$ALL" in *"$PW"*) echo "      FAIL: password in the admin overview"; fail=1 ;; esac

# [10] the api log since T0 never shows the password
LOG=$(docker compose logs --since "$T0" api 2>/dev/null || true)
case "$LOG" in *"$PW"*) echo "  [10] FAIL: the password appears in the api log"; fail=1 ;; *) echo "  [10] api log: password absent" ;; esac

# [11] at rest: Fernet ciphertext
if [ -n "$CID" ]; then
  CT=$(docker compose exec -T postgres psql -U talus -d talusgis -tAc "SELECT convert_from(password_ciphertext, 'UTF8') FROM platform.sql_connections WHERE id = '$CID'" 2>/dev/null | tr -d '[:space:]')
  case "$CT" in gAAAA*) echo "  [11] at rest: Fernet ciphertext" ;; *"$PW"*) echo "  [11] FAIL: plaintext at rest"; fail=1 ;; *) echo "  [11] FAIL: unexpected ciphertext shape: $(printf '%.12s' "$CT")"; fail=1 ;; esac
fi

# [12] cleanup (the ADMIN removes a user's connection — oversight)
if [ -n "$CID" ]; then
  rc=$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE -H "Authorization: Bearer $ADMIN" "$URL/$CID")
  rc2=$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE -H "Authorization: Bearer $ADMIN" "$URL/$CID")
  echo "  [12] admin DELETE -> $rc, again -> $rc2 (expect 204, 404)"
  [ "$rc" = "204" ] && [ "$rc2" = "404" ] || { echo "      FAIL"; fail=1; }
  [ "$rc" = "204" ] && CID=""
fi

if [ "$fail" = "0" ]; then echo "inv47: PASS"; else echo "inv47: FAIL"; fi
exit $fail
