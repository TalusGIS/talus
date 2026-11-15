#!/usr/bin/env bash
# Invariant (once-over A3, decision-once-over-security-fixes.md) — a personal access token is judged
# by what its OWNER may see; it is never an administrator. The three role helpers in authz.py used
# to answer True for "no claims", meant for stub mode — and in keycloak mode "no claims" is also what
# an optional-auth door sees for a caller who came with an access token. So a viewer's READ-ONLY
# token downloaded the metadata of items the viewer's own session was refused, and a map told a
# token caller who did not own it that they could manage it.
#
# Fixtures: a throwaway publisher (OWNER) creates a PRIVATE layer and a PRIVATE web map; a throwaway
# viewer has no access to either. Each mints a read-only access token (HTTP Basic, the desktop path).
#   1. viewer, by SESSION: metadata download of the layer and of the map        -> 403 (the baseline)
#   2. viewer, by TOKEN:   the same downloads, qmd and iso19139, layer and map  -> 403, never 2xx
#      (the door answers a signed-in caller without view with 403 "not authorized to view this item";
#       a token caller is a signed-in caller — metadata.py _authorize_metadata_read)
#   3. control: the OWNER's read-only token, the same four downloads            -> 200
#      (so leg 2 is a refusal of THAT caller, not a door that refuses every token)
#   4. anonymous on the private items                                           -> 404 (existence-hiding;
#      unchanged by the fix — here so a "fix" that turned tokens into anonymous callers shows up as
#      the wrong status in leg 2)
#   5. canManage: the map is shared to a group holding the viewer; the viewer's token opens it (200)
#      and is told canManage=false; control: the owner's token is told canManage=true
#
# Not covered here: a raster (the same code branch as the web map — `_authorize_manage` treats the
# two alike — and it would need a GeoTIFF fixture; inv52 owns that).
#
# PRE-FIX BASELINE: not run by this script (written on the fix branch). The cut session reproduced
# leg 2 by hand on dev, 2026-11-14: viewer's read-only token 200, session 403.
#
# Deny rule: a 2xx in leg 2 = leaked allow = FAIL. Needs the running stack + .env. bash 3.2-safe; jq-free.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv53: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
fail=0
# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh
LID=""; MID=""; GRP=""; OPAT_ID=""; VPAT_ID=""
leg_cleanup() {   # runs from the helper's EXIT trap, before principal teardown
  [ -n "$VPAT_ID" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $VIEWER" "$API/me/tokens/$VPAT_ID"
  [ -n "$OPAT_ID" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/me/tokens/$OPAT_ID"
  [ -n "$MID" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/web-maps/$MID"
  [ -n "$GRP" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/groups/$GRP"
  [ -n "$LID" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/layers/$LID"
  return 0
}
tv_mint publisher || { echo "inv53: FAIL — no owner token"; exit 1; }
OWNER="$TV_PUBLISHER_TOKEN"
tv_mint viewer || { echo "inv53: FAIL — no viewer token"; exit 1; }
VIEWER="$TV_VIEWER_TOKEN"
JSON='Content-Type: application/json'
jget() { python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

# probe <label> <expected> <curl args…> — status must equal <expected>; the body is printed when it
# does not, and a 2xx where a refusal was expected is named for what it is.
probe() {
  _l="$1"; _want="$2"; shift 2
  _out="$(curl -sS -w '\n%{http_code}' "$@")"
  _c="$(printf '%s' "$_out" | tail -1)"; _b="$(printf '%s' "$_out" | sed '$d')"
  if [ "$_c" = "$_want" ]; then
    echo "  PASS $_l ($_c)"
  else
    case "$_want:$_c" in
      2*:*) echo "  FAIL $_l (expected $_want, got $_c)" ;;
      *:2*) echo "  FAIL $_l — LEAKED ALLOW (expected $_want, got $_c)" ;;
      *)    echo "  FAIL $_l (expected $_want, got $_c)" ;;
    esac
    echo "      body: $(printf '%s' "$_b" | cut -c1-300)"
    fail=1
  fi
}

LID="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" \
  -d '{"display_name":"inv53 private layer","geom_type":"point","working_srid":4326,"native_storage":false}' \
  "$API/layers" | jget 'd.get("layer_id","")')"
case "$LID" in tal_*) : ;; *) echo "inv53: FAIL — layer create ('$LID')"; exit 1 ;; esac
MID="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" \
  -d '{"title":"inv53 private map","definition":{"version":1,"basemap":"light","operationalLayers":[],"initialView":{"center":[-82.5,27.3],"zoom":10}}}' \
  "$API/web-maps" | jget 'd.get("id","")')"
[ -n "$MID" ] || { echo "inv53: FAIL — web map create"; exit 1; }
echo "inv53: private layer $LID and private map $MID, owned by $TV_PUBLISHER_USER; the viewer is $TV_VIEWER_USER"

mint_pat() { # <bearer> <label> -> "<token> <id>" on stdout
  curl -sS -X POST -H "Authorization: Bearer $1" -H "$JSON" \
    -d '{"label":"'"$2"'","scope":"read_only"}' "$API/me/tokens" | jget 'str(d["token"]) + " " + str(d["id"])'
}
_p="$(mint_pat "$VIEWER" 'inv53 viewer')"; VPAT="${_p%% *}"; VPAT_ID="${_p#* }"
_p="$(mint_pat "$OWNER" 'inv53 owner')";   OPAT="${_p%% *}"; OPAT_ID="${_p#* }"
case "$VPAT:$OPAT" in tpat_*:tpat_*) : ;; *) echo "inv53: FAIL — access token mint (viewer or owner)"; exit 1 ;; esac

L_MD="$API/items/layer/$LID/metadata/download"
M_MD="$API/items/web_map/$MID/metadata/download"

echo "inv53 leg 1 — the viewer's SESSION is refused (the baseline the token must match)"
probe "viewer session, layer metadata (qmd)" 403 -H "Authorization: Bearer $VIEWER" "$L_MD?format=qmd"
probe "viewer session, map metadata (qmd)"   403 -H "Authorization: Bearer $VIEWER" "$M_MD?format=qmd"

echo "inv53 leg 2 — the viewer's read-only TOKEN is refused the same way"
probe "viewer token, layer metadata (qmd)"      403 -u "talus:$VPAT" "$L_MD?format=qmd"
probe "viewer token, layer metadata (iso19139)" 403 -u "talus:$VPAT" "$L_MD?format=iso19139"
probe "viewer token, map metadata (qmd)"        403 -u "talus:$VPAT" "$M_MD?format=qmd"
probe "viewer token, map metadata (iso19139)"   403 -u "talus:$VPAT" "$M_MD?format=iso19139"

echo "inv53 leg 3 — control: the OWNER's read-only token downloads all four"
probe "owner token, layer metadata (qmd)"      200 -u "talus:$OPAT" "$L_MD?format=qmd"
probe "owner token, layer metadata (iso19139)" 200 -u "talus:$OPAT" "$L_MD?format=iso19139"
probe "owner token, map metadata (qmd)"        200 -u "talus:$OPAT" "$M_MD?format=qmd"
probe "owner token, map metadata (iso19139)"   200 -u "talus:$OPAT" "$M_MD?format=iso19139"

echo "inv53 leg 4 — anonymous on the private items stays hidden"
probe "anon, layer metadata (qmd)" 404 "$L_MD?format=qmd"
probe "anon, map metadata (qmd)"   404 "$M_MD?format=qmd"

echo "inv53 leg 5 — a token caller who does not own the map is not told it can manage it"
# The viewer must be able to OPEN the map for the flag to be read at all: share it to a group that
# holds the viewer (the inv41 pattern — a throwaway is no organization member).
GRP="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" \
  -d '{"name":"inv53 readers '"$(openssl rand -hex 4)"'"}' "$API/groups" | jget 'd.get("groupId") or d.get("group_id","")')"
if [ -z "$GRP" ]; then
  echo "  FAIL group create — leg 5 cannot run"; fail=1
else
  probe "viewer joins the group" 200 -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" \
    -d '{"userIds":["'"$TV_VIEWER_ID"'"]}' "$API/groups/$GRP/members"
  probe "map shared to the group" 200 -X PUT -H "Authorization: Bearer $OWNER" -H "$JSON" \
    -d '{"organizationOnly":false,"groups":["'"$GRP"'"],"publicAccess":false}' "$API/web-maps/$MID/shares"
  sleep 0.3
  vm="$(curl -sS -w '\n%{http_code}' -u "talus:$VPAT" "$API/web-maps/$MID")"
  vc="$(printf '%s' "$vm" | tail -1)"; vflag="$(printf '%s' "$vm" | sed '$d' | jget 'd["canManage"]')"
  if [ "$vc" = "200" ] && [ "$vflag" = "False" ]; then
    echo "  PASS viewer token opens the shared map and canManage is false"
  else
    echo "  FAIL viewer token on the shared map: http $vc, canManage=${vflag:-unreadable} (expected 200, False)"
    echo "      body: $(printf '%s' "$vm" | sed '$d' | cut -c1-300)"; fail=1
  fi
  om="$(curl -sS -w '\n%{http_code}' -u "talus:$OPAT" "$API/web-maps/$MID")"
  oc="$(printf '%s' "$om" | tail -1)"; oflag="$(printf '%s' "$om" | sed '$d' | jget 'd["canManage"]')"
  if [ "$oc" = "200" ] && [ "$oflag" = "True" ]; then
    echo "  PASS control: the owner's token is told canManage is true"
  else
    echo "  FAIL control, owner token on the map: http $oc, canManage=${oflag:-unreadable} (expected 200, True)"
    echo "      body: $(printf '%s' "$om" | sed '$d' | cut -c1-300)"; fail=1
  fi
fi

# cleanup: tokens revoked, fixtures out (leg_cleanup is re-run harmlessly by the EXIT trap)
probe "[cleanup] revoke the viewer's token" 204 -X DELETE -H "Authorization: Bearer $VIEWER" "$API/me/tokens/$VPAT_ID"
probe "[cleanup] revoke the owner's token"  204 -X DELETE -H "Authorization: Bearer $OWNER" "$API/me/tokens/$OPAT_ID"
VPAT_ID=""; OPAT_ID=""
leg_cleanup; MID=""; GRP=""; LID=""

if [ "$fail" = "0" ]; then echo "inv53: PASS"; else echo "inv53: FAIL"; exit 1; fi
