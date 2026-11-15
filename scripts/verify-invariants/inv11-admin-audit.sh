#!/usr/bin/env bash
# Invariant C11 — the admin-audit governance log (platform.admin_audit, V074) is ADMIN-READ-ONLY and its
# actor is SERVER-DERIVED (un-forgeable). decision-admin-reports-round2.md §6.
#
# The Activity report exposes who-did-what governance history — sensitive. Every read is _require_admin-
# gated: anon -> 401, a signed-in non-admin -> 403, on BOTH the JSON and the ?format=csv surface. The
# WRITE path takes the actor from the server-side session (_admin.id / user.id), never a client field, so
# a governance action can never be recorded under a spoofed actor.
#
# PASS proves: anon + non-admin denied on both formats; admin read 200; and a real governance action
# (a delete-protection toggle on a throwaway layer) lands attributed to the ACTUAL acting principal.
#
# Needs the running stack + .env (throwaways self-provisioned via _principals.sh). bash 3.2-safe; -sS curl; jq-free.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv11: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
fail=0

# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh

code() { curl -sS -o /dev/null -w '%{http_code}' "$@"; }

# [1] anon -> 401 on JSON + CSV (no anonymous surface onto the governance log)
for q in "" "?format=csv"; do
  ac=$(code "$API/admin/reports/activity$q")
  echo "  [1] anon  GET activity${q:+ (csv)} -> $ac (expect 401 — auth required, never anon)"
  case "$ac" in
    401|403) : ;;
    2*) echo "      FAIL: anonymous read LEAKED the governance log ($ac)"; fail=1 ;;
    *)  echo "      FAIL: expected 401, got $ac"; fail=1 ;;
  esac
done

# [2] signed-in NON-admin -> 403 on JSON + CSV (a publisher is not an admin)
tv_mint publisher || { echo "inv11: FAIL — no publisher token"; exit 1; }
NONADMIN="$TV_PUBLISHER_TOKEN"
for q in "" "?format=csv"; do
  nc=$(code -H "Authorization: Bearer $NONADMIN" "$API/admin/reports/activity$q")
  echo "  [2] non-admin GET activity${q:+ (csv)} -> $nc (expect 403 — admin-gated)"
  case "$nc" in
    403) : ;;
    2*) echo "      FAIL: a non-admin READ the governance log ($nc) — admin gate leaked"; fail=1 ;;
    *)  echo "      FAIL: expected 403, got $nc"; fail=1 ;;
  esac
done

# [3] positive control: admin -> 200
tv_mint admin || { echo "inv11: FAIL — no admin token"; exit 1; }
ADMIN="$TV_ADMIN_TOKEN"
oc=$(code -H "Authorization: Bearer $ADMIN" "$API/admin/reports/activity")
echo "  [3] admin GET activity -> $oc (expect 200 — control)"
[ "$oc" = "200" ] || { echo "      FAIL: admin could not read the activity report ($oc)"; fail=1; }

# [4] actor integrity: a governance action is attributed to the ACTUAL acting principal (server-derived),
#     never a client-supplied actor. The throwaway admin toggles delete-protection on a throwaway layer;
#     the new admin_audit row for that layer must carry that admin's real principal id.
AID="$TV_ADMIN_ID"
LID=$(curl -sS -X POST -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' \
  -d '{"display_name":"inv11 admin-audit test","geom_type":"point","working_srid":4326,"native_storage":false}' \
  "$API/layers" | python3 -c "import sys,json;print(json.load(sys.stdin).get('layer_id',''))" 2>/dev/null)
[ -n "$LID" ] || { echo "inv11: FAIL — layer create failed"; exit 1; }
curl -sS -o /dev/null -X PUT -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' \
  -d '{"deleteProtected":true}' "$API/layers/${LID}/delete-protection"

# read the newest activity row for THIS layer and check the recorded actor
ACTOR=$(curl -sS -H "Authorization: Bearer $ADMIN" "$API/admin/reports/activity?limit=50" | python3 -c "
import sys, json
lid = sys.argv[1]
rows = json.load(sys.stdin).get('rows', [])
hit = next((r for r in rows if r.get('action') == 'layer_delete_protection_set'
            and r.get('targetId') == lid), None)
print(hit['actorId'] if hit else '')
" "$LID" 2>/dev/null)
echo "  [4] delete-protection by $TV_ADMIN_USER -> audit actor $ACTOR (expect $AID — server-derived)"
if [ -z "$ACTOR" ]; then
  echo "      FAIL: no admin_audit row recorded for the governance action"; fail=1
elif [ "$ACTOR" != "$AID" ]; then
  echo "      FAIL: audit row attributed to $ACTOR, not the acting principal $AID"; fail=1
fi

# cleanup — protection OFF first: delete protection is ABSOLUTE, a protected layer cannot be
# deleted (the old cleanup silently left one stray protected layer per run). Verified gone.
curl -sS -o /dev/null -X PUT -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' \
  -d '{"deleteProtected":false}' "$API/layers/${LID}/delete-protection"
dc=$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE -H "Authorization: Bearer $ADMIN" "$API/layers/${LID}")
[ "$dc" = "200" ] || { echo "      FAIL: cleanup could not delete throwaway layer $LID ($dc) — residue"; fail=1; }

if [ "$fail" -eq 0 ]; then echo "inv11: PASS"; else echo "inv11: FAIL"; fi
exit "$fail"
