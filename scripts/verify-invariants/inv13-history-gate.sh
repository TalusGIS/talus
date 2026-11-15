#!/usr/bin/env bash
# Invariant (edit-history-ui slice) — a feature's edit history never leaks past the view gate,
# and its editor identity is NEVER anon-reachable.
#
# The model (decision-edit-history-ui.md): GET /layers/{id}/features/{fid}/history requires
# authentication AT THE DOOR (anon denied — history is identity-bearing, stricter than the T23
# scrub), then view_allowed (fork B: ANY can_view principal may see it; a signed-in user WITHOUT
# view is denied). The endpoint is never mounted on a public router, so there is no anon branch to
# leak through even for a public layer.
#
# Setup: owner (throwaway publisher) creates a fresh PRIVATE layer (no share). All legs run against it.
# DENY legs asserted:
#   1. anon history                         -> 401 (challenged at the door; NOT 2xx)
#   2. forged `alg:none` bearer             -> 401
#   3. signed-in wrong-user (no can_view)   -> 403 (view gate)
# Positive control:
#   4. owner history                        -> 200 (the allow path works, not a blanket deny)
#
# Deny rule: any 2xx on the private layer's history from anon/forged/wrong-user = leaked allow =
# FAIL. 401/403/404 = deny = pass. (OpenFGA-down -> 503 fail-closed is inherited from view_allowed,
# the same code path inv01/inv07 exercise; not re-stopped here.)
#
# The positive fork-B leg (a genuine can_view VIEWER gets 200 + history) + the diff/actor/paging
# assertions live in verify-edit-history.sh, which needs a shared layer; this invariant stays
# deny-focused. Needs the running stack + .env (throwaways via _principals.sh). bash 3.2-safe; -sS curl.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv13: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
FORGED='eyJhbGciOiJub25lIn0.eyJzdWIiOiJoYWNrZXIifQ.'
FID='00000000-0000-0000-0000-000000000000'   # a nonexistent feature — every deny fires before it matters
fail=0

# owner token + a fresh PRIVATE layer (never shared)
# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh
tv_mint publisher || { echo "inv13: FAIL — no owner token"; exit 1; }
OWNER="$TV_PUBLISHER_TOKEN"
LID=$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H 'Content-Type: application/json' \
  -d '{"display_name":"inv13 history gate test","geom_type":"point","working_srid":4326,"native_storage":false}' \
  "$API/layers" | python3 -c "import sys,json;print(json.load(sys.stdin).get('layer_id',''))" 2>/dev/null)
[ -n "$LID" ] || { echo "inv13: FAIL — layer create failed"; exit 1; }
echo "inv13: private layer $LID (owned by $TV_PUBLISHER_USER, unshared)"
H="$API/layers/${LID}/features/${FID}/history"

# 1) anon -> 401
c=$(curl -sS -o /dev/null -w '%{http_code}' "$H")
echo "  [1] anon history            -> $c (expect 401)"
case "$c" in
  401|403|404) : ;;
  2*) echo "      FAIL: anon retrieved history (leaked identity-bearing data)"; fail=1 ;;
  *)  echo "      FAIL: unexpected status $c (stack down? wrong API_BASE?)"; fail=1 ;;
esac

# 2) forged alg:none -> 401
c=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $FORGED" "$H")
echo "  [2] forged alg:none history -> $c (expect 401)"
case "$c" in
  401) : ;;
  2*) echo "      FAIL: forged token retrieved history"; fail=1 ;;
  *)  echo "      FAIL: expected 401 for a forged token, got $c"; fail=1 ;;
esac

# 3) signed-in wrong-user (no can_view on this private layer) -> 403
tv_mint viewer || { echo "inv13: FAIL — no wrong-user token"; exit 1; }
WRONG="$TV_VIEWER_TOKEN"
c=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $WRONG" "$H")
echo "  [3] wrong-user history      -> $c (expect 403 — no view access)"
case "$c" in
  403|404) : ;;
  2*) echo "      FAIL: a user with no view access read the history (view gate bypassed)"; fail=1 ;;
  *)  echo "      FAIL: expected 403, got $c"; fail=1 ;;
esac

# 4) owner positive control -> 200 (nonexistent fid => empty timeline, but the ALLOW path works)
c=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $OWNER" "$H")
echo "  [4] owner history           -> $c (expect 200 — allow-path control)"
[ "$c" = "200" ] || { echo "      FAIL: owner could not read its own layer's history ($c)"; fail=1; }

# cleanup
curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/layers/${LID}"

if [ "$fail" -eq 0 ]; then echo "inv13: PASS"; else echo "inv13: FAIL"; fi
exit "$fail"
