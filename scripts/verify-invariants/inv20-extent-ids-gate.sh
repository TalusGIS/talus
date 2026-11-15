#!/usr/bin/env bash
# Invariant (zoom-to-selected slice) — the ids-scoped extent reveals nothing past the view gate.
#
# The model (decision-zoom-to-selected.md §5): GET /layers/{id}/extent?id=... rides the SAME
# optional-auth read gate as the base extent route (_authorize_read: authed no-view -> 403,
# anon on a non-public layer -> 404, fail-closed), and the ids land as bound params through the
# shared _where builder. This test keeps "same gate" a TESTED claim for the ids-scoped form —
# an ids-scoped bbox is a (coarse) geometry oracle, so it must deny exactly like /features.
#
# Setup: owner (admin-test) creates a fresh PRIVATE layer (no share). All legs run against it.
# DENY legs asserted (base form AND ids-scoped form; legs 5-6 cover the THIRD ids surface,
# /features?id= — decision-select-features.md F5 — under the same gate):
#   1. anon extent                  -> 404 (public probe; never 2xx)
#   2. anon extent?id=...           -> 404
#   3. forged `alg:none` + ids      -> 401
#   4. signed-in wrong-user + ids   -> 403 (view gate)
#   5. anon features?id=...         -> 404
#   6. wrong-user features?id=...   -> 403
# Positive control:
#   7. owner extent?id=<missing>    -> 200 with bbox null (allow path + all-ids-missing contract)
#
# Deny rule: any 2xx from anon/forged/wrong-user = leaked allow = FAIL. 401/403/404 = deny = pass.
# Needs the running stack + .env (throwaways via _principals.sh). bash 3.2-safe; -sS curl.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv20: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
FORGED='eyJhbGciOiJub25lIn0.eyJzdWIiOiJoYWNrZXIifQ.'
FID='00000000-0000-0000-0000-000000000000'   # a nonexistent id — the gate fires before it matters
fail=0

# owner token + a fresh PRIVATE layer (never shared)
# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh
tv_mint publisher || { echo "inv20: FAIL — no owner token"; exit 1; }
OWNER="$TV_PUBLISHER_TOKEN"
LID=$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H 'Content-Type: application/json' \
  -d '{"display_name":"inv20 extent ids gate test","geom_type":"point","working_srid":4326,"native_storage":false}' \
  "$API/layers" | python3 -c "import sys,json;print(json.load(sys.stdin).get('layer_id',''))" 2>/dev/null)
[ -n "$LID" ] || { echo "inv20: FAIL — layer create failed"; exit 1; }
echo "inv20: private layer $LID (owned by $TV_PUBLISHER_USER, unshared)"
E="$API/layers/${LID}/extent"

# 1) anon, base extent -> 404
c=$(curl -sS -o /dev/null -w '%{http_code}' "$E")
echo "  [1] anon extent             -> $c (expect 404)"
case "$c" in
  401|403|404) : ;;
  2*) echo "      FAIL: anon read a private layer's extent"; fail=1 ;;
  *)  echo "      FAIL: unexpected status $c (stack down? wrong API_BASE?)"; fail=1 ;;
esac

# 2) anon, ids-scoped -> 404
c=$(curl -sS -o /dev/null -w '%{http_code}' "$E?id=$FID")
echo "  [2] anon extent?id=         -> $c (expect 404)"
case "$c" in
  401|403|404) : ;;
  2*) echo "      FAIL: anon read an ids-scoped extent (geometry oracle leaked)"; fail=1 ;;
  *)  echo "      FAIL: unexpected status $c"; fail=1 ;;
esac

# 3) forged alg:none, ids-scoped -> 401
c=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $FORGED" "$E?id=$FID")
echo "  [3] forged alg:none + ids   -> $c (expect 401)"
case "$c" in
  401) : ;;
  2*) echo "      FAIL: forged token read an ids-scoped extent"; fail=1 ;;
  *)  echo "      FAIL: expected 401 for a forged token, got $c"; fail=1 ;;
esac

# 4) signed-in wrong-user (no can_view), ids-scoped -> 403
tv_mint viewer || { echo "inv20: FAIL — no wrong-user token"; exit 1; }
WRONG="$TV_VIEWER_TOKEN"
c=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $WRONG" "$E?id=$FID")
echo "  [4] wrong-user + ids        -> $c (expect 403 — no view access)"
case "$c" in
  403|404) : ;;
  2*) echo "      FAIL: a user with no view access read an ids-scoped extent"; fail=1 ;;
  *)  echo "      FAIL: expected 403, got $c"; fail=1 ;;
esac

# 5) anon, ids-scoped FEATURES page -> 404 (the F5 Show-selected surface, same gate)
F="$API/layers/${LID}/features"
c=$(curl -sS -o /dev/null -w '%{http_code}' "$F?id=$FID")
echo "  [5] anon features?id=       -> $c (expect 404)"
case "$c" in
  401|403|404) : ;;
  2*) echo "      FAIL: anon read an ids-scoped feature page"; fail=1 ;;
  *)  echo "      FAIL: unexpected status $c"; fail=1 ;;
esac

# 6) signed-in wrong-user, ids-scoped FEATURES page -> 403
c=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $WRONG" "$F?id=$FID")
echo "  [6] wrong-user features?id= -> $c (expect 403 — no view access)"
case "$c" in
  403|404) : ;;
  2*) echo "      FAIL: a user with no view access read an ids-scoped feature page"; fail=1 ;;
  *)  echo "      FAIL: expected 403, got $c"; fail=1 ;;
esac

# 7) owner positive control -> 200 with bbox null (empty layer + nonexistent id)
body=$(curl -sS -H "Authorization: Bearer $OWNER" "$E?id=$FID")
c=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $OWNER" "$E?id=$FID")
echo "  [7] owner extent?id=missing -> $c (expect 200, bbox null — allow-path control)"
if [ "$c" != "200" ]; then
  echo "      FAIL: owner could not read its own layer's ids-scoped extent ($c)"; fail=1
else
  echo "$body" | python3 -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if d.get('bbox') is None else 1)" \
    || { echo "      FAIL: expected bbox null for all-ids-missing, got: $body"; fail=1; }
fi

# cleanup
curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/layers/${LID}"

if [ "$fail" -eq 0 ]; then echo "inv20: PASS"; else echo "inv20: FAIL"; fi
exit "$fail"
