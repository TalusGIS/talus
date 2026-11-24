#!/usr/bin/env bash
# Invariant — a pick-list's "where used" answer never names a layer the caller cannot view.
# GET /custom-domains/{id}/usage lists the fields bound to a pick-list with each layer's id and
# title. Pick-lists are organisation-wide and readable by every signed-in account; layers are
# not. So the answer is filtered by the caller's right to VIEW each layer — the same rule the
# other where-used listings follow (omit what the caller cannot view; an admin sees all):
#
#   [1] anonymous GET usage → 401
#   [2] a viewer with NO access to the layer → 200, and the body holds neither the layer's id
#       nor its title
#   [3] the layer's owner → the binding is listed (positive control — the route is not dead)
#   [4] an admin → the binding is listed
#   [5] the layer is shared to a group the viewer belongs to → the SAME viewer now sees the
#       binding (the filter follows view access, not role or ownership)
#   [6] cleanup: group, layer and pick-list removed; the pick-list reads back 404
#
# Creates: three throwaway principals (via _principals.sh), one private layer with one
# pick-list field, one pick-list, one group. Removes all of them; says RESIDUE if it could not.
#
# PRE-CHANGE BASELINE (recorded 2026-11-22, v1.0.0 code, no filter): [2] RED — the viewer's
# answer named the private layer; every other leg green.
#
# Needs the running stack + .env (throwaways via _principals.sh). bash 3.2-safe; jq-free.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv58: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
JSON='Content-Type: application/json'
jget(){ python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }
code(){ curl -sS -o /dev/null -w '%{http_code}' "$@"; }
fail=0
NONCE="inv58-$(date +%s)-$$"
TITLE="$NONCE private layer"
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/inv58.XXXXXX")"
LA="" DOM="" GRP="" OWNER=""

# per the _principals.sh trap contract: declare leg_cleanup, never an own EXIT trap.
# Order matters: the layer goes first (its field is what binds the pick-list).
leg_cleanup(){
  rm -rf "$SCRATCH"
  [ -n "$OWNER" ] || return 0
  if [ -n "$GRP" ]; then
    rc=$(code -X DELETE -H "Authorization: Bearer $OWNER" "$API/groups/$GRP")
    case "$rc" in 2*|404) : ;; *) echo "  RESIDUE: inv58 group $GRP not removed ($rc)" ;; esac
  fi
  if [ -n "$LA" ]; then
    rc=$(code -X DELETE -H "Authorization: Bearer $OWNER" "$API/layers/$LA")
    case "$rc" in 2*|404) : ;; *) echo "  RESIDUE: inv58 layer $LA not released ($rc)" ;; esac
  fi
  if [ -n "$DOM" ]; then
    rc=$(code -X DELETE -H "Authorization: Bearer $OWNER" "$API/custom-domains/$DOM")
    case "$rc" in 2*|404) : ;; *) echo "  RESIDUE: inv58 pick-list $DOM not removed ($rc)" ;; esac
  fi
}

# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh
tv_mint publisher || { echo "inv58: FAIL — no owner token"; exit 1; }
OWNER="$TV_PUBLISHER_TOKEN"
tv_mint viewer || { echo "inv58: FAIL — no viewer token"; exit 1; }
VIEWER="$TV_VIEWER_TOKEN"
tv_mint admin || { echo "inv58: FAIL — no admin token"; exit 1; }
ADMIN="$TV_ADMIN_TOKEN"

# --- fixtures: a private layer whose one pick-list field binds a new pick-list ---------------
LA="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" \
  -d '{"display_name":"'"$TITLE"'","geom_type":"point","working_srid":4326,"native_storage":false}' \
  "$API/layers" | jget 'd.get("layer_id","")')"
case "$LA" in tal_*) : ;; *) echo "inv58: FAIL — layer create failed ('$LA')"; exit 1 ;; esac
DOM="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" \
  -d '{"name":"'"$NONCE"' condition","values":[{"code":"good","label":"Good"}]}' \
  "$API/custom-domains" | jget 'd.get("id","")')"
[ -n "$DOM" ] || { echo "inv58: FAIL — pick-list create failed"; exit 1; }
rc=$(code -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" \
  -d '{"fieldKey":"condition","label":"Condition","dataType":"coded","domainId":"'"$DOM"'","sortOrder":10}' \
  "$API/layers/$LA/custom-fields")
[ "$rc" = "201" ] || { echo "inv58: FAIL — field fixture ($rc)"; exit 1; }
echo "  fixtures: layer $LA, pick-list $DOM, owner $TV_PUBLISHER_USER"
URL="$API/custom-domains/$DOM/usage"

names_layer(){ # <body file> — 0 if the body names the layer (by id or by title)
  grep -q "$LA" "$1" 2>/dev/null && return 0
  grep -q "$TITLE" "$1" 2>/dev/null && return 0
  return 1
}
listed(){ # <token> — prints "<http> <yes|no>"
  rc=$(curl -sS -o "$SCRATCH/body" -w '%{http_code}' -H "Authorization: Bearer $1" "$URL")
  if names_layer "$SCRATCH/body"; then echo "$rc yes"; else echo "$rc no"; fi
}

# [1] anonymous
rc=$(code "$URL")
echo "  [1] anon GET usage -> $rc (expect 401)"
[ "$rc" = "401" ] || { echo "      FAIL: anon must be 401, got $rc"; fail=1; }

# [2] a viewer who cannot view the layer
r="$(listed "$VIEWER")"
echo "  [2] viewer without access -> ${r% *}, layer named: ${r#* } (expect 200, no)"
case "$r" in
  "200 no") : ;;
  "200 yes") echo "      FAIL: the answer names a layer this viewer cannot view"; fail=1 ;;
  *) echo "      FAIL: expected 200 with the layer omitted, got '$r'"; fail=1 ;;
esac
# and the viewer really cannot view it — otherwise [2] proves nothing
rc=$(code -H "Authorization: Bearer $VIEWER" "$API/layers/$LA/usage")
echo "      the viewer's own read of that layer -> $rc (expect 403)"
[ "$rc" = "403" ] || { echo "      FAIL: fixture is not private to the viewer ($rc)"; fail=1; }

# [3] the owner · [4] an admin
r="$(listed "$OWNER")"
echo "  [3] layer owner -> ${r% *}, layer named: ${r#* } (expect 200, yes)"
[ "$r" = "200 yes" ] || { echo "      FAIL: the owner must see the binding"; fail=1; }
r="$(listed "$ADMIN")"
echo "  [4] admin -> ${r% *}, layer named: ${r#* } (expect 200, yes)"
[ "$r" = "200 yes" ] || { echo "      FAIL: an admin must see every binding"; fail=1; }

# [5] share the layer to a group the viewer is in — the same viewer now sees it
GRP="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"name":"'"$NONCE"' crew"}' \
  "$API/groups" | jget 'd.get("groupId") or d.get("group_id","")')"
m=$(code -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"userIds":["'"$TV_VIEWER_ID"'"]}' "$API/groups/$GRP/members")
s=$(code -X PUT -H "Authorization: Bearer $OWNER" -H "$JSON" \
  -d '{"organizationOnly":false,"groups":["'"$GRP"'"],"publicAccess":false}' "$API/layers/$LA/shares")
case "$m$s" in 2??2??) : ;; *) echo "      FAIL: share fixture (member $m, share $s)"; fail=1 ;; esac
sleep 1
r="$(listed "$VIEWER")"
echo "  [5] same viewer, layer now shared to their group -> ${r% *}, layer named: ${r#* } (expect 200, yes)"
[ "$r" = "200 yes" ] || { echo "      FAIL: a viewer who CAN view the layer must see the binding"; fail=1; }

# [6] cleanup, verified
g=$(code -X DELETE -H "Authorization: Bearer $OWNER" "$API/groups/$GRP")
l=$(code -X DELETE -H "Authorization: Bearer $OWNER" "$API/layers/$LA")
d=$(code -X DELETE -H "Authorization: Bearer $OWNER" "$API/custom-domains/$DOM")
d2=$(code -H "Authorization: Bearer $OWNER" "$API/custom-domains/$DOM")
echo "  [6] cleanup: group $g, layer $l, pick-list $d then GET $d2 (expect 2xx, 2xx, 204, 404)"
case "$g" in 2*) GRP="" ;; *) echo "      FAIL: group not removed"; fail=1 ;; esac
case "$l" in 2*) LA="" ;; *) echo "      FAIL: layer not released"; fail=1 ;; esac
if [ "$d" = "204" ] && [ "$d2" = "404" ]; then DOM=""; else echo "      FAIL: pick-list not removed"; fail=1; fi

if [ "$fail" = "0" ]; then echo "inv58: PASS"; else echo "inv58: FAIL"; exit 1; fi
