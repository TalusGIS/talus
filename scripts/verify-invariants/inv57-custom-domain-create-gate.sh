#!/usr/bin/env bash
# Invariant — creating a pick-list (a custom domain) is PUBLISHER-gated, like every other
# "create a catalog thing" door. A pick-list is organisation-wide: it appears in every
# publisher's picker and holds a unique name, so who may add one is a role question, not
# "anyone signed in":
#
#   [1] anonymous POST /custom-domains → 401
#   [2] viewer POST → 403, and no pick-list of that name exists afterwards
#   [3] data editor POST → 403, and no pick-list of that name exists afterwards
#       (an editor may change features; that is not the right to add to the shared registry)
#   [4] publisher POST → 201 + an id (positive control — [1]–[3] are not a dead route)
#   [5] viewer GET /custom-domains → 200 and lists [4] (reads stay open to every signed-in
#       account: the edit form and the picker need them — the gate must not close them)
#   [6] data editor POST a value onto the publisher's list → 403 (the edit gate — the list's
#       owner or an admin — is untouched by this change)
#   [7] cleanup: publisher DELETE → 204; GET → 404
#
# Creates: three throwaway principals (via _principals.sh) and one pick-list. Removes all of
# them. If a refused leg wrongly SUCCEEDS, the pick-list it made is deleted with the same
# token (its owner) — or reported as RESIDUE.
#
# PRE-CHANGE BASELINE (recorded 2026-11-22, v1.0.0 code, no gate): [2] and [3] RED — the
# viewer and the data editor each got 201; every other leg green.
#
# Needs the running stack + .env (throwaways via _principals.sh). bash 3.2-safe; jq-free.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv57: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
URL="$API/custom-domains"
JSON='Content-Type: application/json'
jget(){ python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }
fail=0
NONCE="inv57-$(date +%s)-$$"
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/inv57.XXXXXX")"
DID="" PUB=""
LEAKED=""   # "<id>:<token>" pairs for pick-lists a refused leg wrongly created

release(){ # <id> <token> <what>
  rc=$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE -H "Authorization: Bearer $2" "$URL/$1")
  case "$rc" in 2*|404) : ;; *) echo "  RESIDUE: inv57 $3 pick-list $1 not released ($rc)" ;; esac
}
# per the _principals.sh trap contract: declare leg_cleanup, never an own EXIT trap.
leg_cleanup(){
  rm -rf "$SCRATCH"
  for pair in $LEAKED; do release "${pair%%:*}" "${pair#*:}" "leaked"; done
  if [ -n "$DID" ] && [ -n "$PUB" ]; then release "$DID" "$PUB" "fixture"; fi
}

# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh

payload(){ printf '{"name":"%s","values":[{"code":"a","label":"A"}]}' "$1"; }
names(){ # <token> — every pick-list name, one per line
  curl -sS -H "Authorization: Bearer $1" "$URL" \
    | python3 -c "import sys,json;[print(d['name']) for d in json.load(sys.stdin).get('domains',[])]" 2>/dev/null
}
refused(){ # <leg> <who> <token> — POST must be 403; a 2xx is recorded for cleanup
  rc=$(curl -sS -o "$SCRATCH/body" -w '%{http_code}' -X POST -H "Authorization: Bearer $3" -H "$JSON" \
       --data "$(payload "$NONCE-$2")" "$URL")
  echo "  [$1] $2 POST -> $rc (expect 403)"
  case "$rc" in
    403) : ;;
    2*) echo "      FAIL: the $2 can create an organisation-wide pick-list ($rc)"; fail=1
        lid=$(jget 'd.get("id","")' < "$SCRATCH/body")
        [ -n "$lid" ] && LEAKED="$LEAKED $lid:$3" ;;
    *)  echo "      FAIL: expected 403, got $rc"; fail=1 ;;
  esac
}

# [1] anonymous
rc=$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H "$JSON" --data "$(payload "$NONCE-anon")" "$URL")
echo "  [1] anon POST -> $rc (expect 401)"
[ "$rc" = "401" ] || { echo "      FAIL: anon must be 401, got $rc"; fail=1; }

# [2] viewer · [3] data editor — signed in, wrong role
tv_mint viewer || { echo "inv57: FAIL — no viewer token"; exit 1; }
refused 2 viewer "$TV_VIEWER_TOKEN"
tv_mint editor || { echo "inv57: FAIL — no editor token"; exit 1; }
refused 3 editor "$TV_EDITOR_TOKEN"

# [4] publisher positive control
tv_mint publisher || { echo "inv57: FAIL — no publisher token"; exit 1; }
PUB="$TV_PUBLISHER_TOKEN"
rc=$(curl -sS -o "$SCRATCH/body" -w '%{http_code}' -X POST -H "Authorization: Bearer $PUB" -H "$JSON" \
     --data "$(payload "$NONCE-publisher")" "$URL")
DID=$(jget 'd.get("id","")' < "$SCRATCH/body")
echo "  [4] publisher POST -> $rc, id: ${DID:-none} (expect 201 + id)"
if [ "$rc" != "201" ] || [ -z "$DID" ]; then
  echo "      FAIL: the publisher could not create ($(head -c 200 "$SCRATCH/body"))"; fail=1
fi

# [2]/[3] second half + [5]: what the registry holds now, read as the VIEWER (reads stay open)
rc=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TV_VIEWER_TOKEN" "$URL")
LIST="$(names "$TV_VIEWER_TOKEN")"
for who in viewer editor; do
  if printf '%s\n' "$LIST" | grep -qx "$NONCE-$who"; then
    echo "      FAIL: a pick-list named $NONCE-$who exists — the refusal left a row"; fail=1
  else
    echo "      no pick-list left behind for the $who"
  fi
done
echo "  [5] viewer GET list -> $rc (expect 200, listing the publisher's)"
[ "$rc" = "200" ] || { echo "      FAIL: reads must stay open to a signed-in viewer, got $rc"; fail=1; }
if [ -n "$DID" ]; then
  printf '%s\n' "$LIST" | grep -qx "$NONCE-publisher" \
    || { echo "      FAIL: the viewer's list does not show the publisher's pick-list"; fail=1; }
fi

# [6] the edit gate is untouched: a non-owner cannot add a value
if [ -n "$DID" ]; then
  rc=$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $TV_EDITOR_TOKEN" -H "$JSON" \
       --data '{"code":"b","label":"B"}' "$URL/$DID/values")
  echo "  [6] editor POST value onto the publisher's list -> $rc (expect 403)"
  [ "$rc" = "403" ] || { echo "      FAIL: a non-owner must not edit a pick-list, got $rc"; fail=1; }
fi

# [7] cleanup, verified
if [ -n "$DID" ]; then
  rc=$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE -H "Authorization: Bearer $PUB" "$URL/$DID")
  rc2=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $PUB" "$URL/$DID")
  echo "  [7] publisher DELETE -> $rc, then GET -> $rc2 (expect 204, 404)"
  if [ "$rc" = "204" ] && [ "$rc2" = "404" ]; then DID=""; else echo "      FAIL: fixture not removed"; fail=1; fi
fi

if [ "$fail" = "0" ]; then echo "inv57: PASS"; else echo "inv57: FAIL"; exit 1; fi
