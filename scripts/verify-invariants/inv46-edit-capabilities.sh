#!/usr/bin/env bash
# Invariant (decision-layer-edit-capabilities.md §5, V109) — the four per-layer edit capabilities
# (add · attributes · geometry · delete) are enforced SERVER-SIDE at every write door, for everyone:
# a flag that is off refuses its op with 403 + the policy sentence, the other three doors still work,
# and an ADMIN is refused exactly the same way (F1: policy, not an availability switch). The clients'
# hiding is convenience; this is the decision.
# Setup: owner (publisher) creates a private point layer and a crew group holding an editor-role
#        throwaway, shares the layer to the group (shared_group grants can_edit_access — the inv41
#        pattern; a throwaway is no org member); an admin throwaway is F1.
# Legs: 0. all four on (the default) → add / attributes / geometry / delete all succeed (control)
#       1. delete OFF   → editor DELETE 403 "deleting"; add / attributes / geometry still 2xx; admin DELETE 403
#       2. geometry OFF → editor PATCH {lon,lat} 403 "moving"; PATCH {props} 200; PATCH {lon,lat,props} 403
#       3. attributes OFF → editor PATCH {props} 403 "attributes"; PATCH {lon,lat} 200; calc door 403;
#                          attachment upload 403 (F2: a photo is an attribute)
#       4. add OFF      → editor POST 403 "adding"; bulk import door 403; PATCH {props} 200; DELETE 204
#       5. the flags ride the editing door: GET echoes what PUT set; a PUT with the master switch only
#          leaves the flags untouched; a viewer cannot PUT (403)
#       6. the Esri façade (e2): a refused op is a PER-RESULT success:false code 403 with the sentence,
#          never a top-level error; the other arrays in the same request still land; `capabilities`
#          drops exactly the refused token (Update only when BOTH attributes and geometry are off) and
#          `allowGeometryUpdates` follows Move-or-reshape; all four off = "Query"
#       7. (once-over A2, decision-once-over-security-fixes.md — here because this script already holds
#          a token caller on the Esri door of a layer it can read) the Esri `where` compiler is capped
#          and linear: a `where` of 200,000 nested "(" is an HTTP 400 in under 3 seconds, and /health
#          answers straight afterwards. Controls: `1=1` and `((1=1))` are answered 200. ★ The big request
#          is sent ONLY after a 17-deep `where` has been refused (the depth cap is present): on an api
#          without the fix that one request would hold the api's only event loop for hours.
# Deny rule: a 2xx on a refused op = leaked allow = FAIL. Needs the running stack + .env. bash 3.2-safe.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv46: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
fail=0
# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh
tv_mint publisher || { echo "inv46: FAIL — no owner token"; exit 1; }
OWNER="$TV_PUBLISHER_TOKEN"
tv_mint editor || { echo "inv46: FAIL — no editor token"; exit 1; }
ED="$TV_EDITOR_TOKEN"
tv_mint admin || { echo "inv46: FAIL — no admin token"; exit 1; }
ADMIN="$TV_ADMIN_TOKEN"
tv_mint viewer || { echo "inv46: FAIL — no viewer token"; exit 1; }
VIEWER="$TV_VIEWER_TOKEN"
JSON='Content-Type: application/json'
code() { curl -sS -o /dev/null -w '%{http_code}' "$@"; }
jget() { python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }
leg() { if [ "$2" = "$3" ]; then echo "  PASS $1 ($3)"; else echo "  FAIL $1 (expected $2, got $3)"; fail=1; fi; }
# deny_leg <label> <expected-sentence-fragment> <curl args…>: 403 AND the policy sentence in the body
deny_leg() {
  _l="$1"; _frag="$2"; shift 2
  _body="$(curl -sS -w '\n%{http_code}' "$@")"
  _c="$(printf '%s' "$_body" | tail -1)"; _d="$(printf '%s' "$_body" | sed '$d')"
  case "$_c" in
    403) case "$_d" in *"$_frag"*) echo "  PASS $_l (403, sentence)";;
                        *) echo "  FAIL $_l (403 but no '$_frag' in: $_d)"; fail=1;; esac;;
    2*) echo "  FAIL $_l — LEAKED ALLOW ($_c)"; fail=1;;
    *) echo "  FAIL $_l (expected 403, got $_c)"; fail=1;;
  esac
}
LID=""; GRP=""
WFILE=""   # leg 7's oversize where, kept in a file (too long for one command-line argument on Linux)
leg_cleanup() {
  [ -n "$LID" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/layers/$LID"
  [ -n "$GRP" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/groups/$GRP"
  [ -n "$WFILE" ] && [ -f "$WFILE" ] && rm -f "$WFILE"
  return 0
}
LID="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"display_name":"inv46 edit caps","geom_type":"point","working_srid":4326,"native_storage":false}' "$API/layers" | jget 'd.get("layer_id","")')"
case "$LID" in tal_*) : ;; *) echo "inv46: FAIL — layer create ('$LID')"; exit 1 ;; esac
GRP="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"name":"inv46 crew '"$RANDOM"'"}' "$API/groups" | jget 'd.get("groupId") or d.get("group_id","")')"
[ -n "$GRP" ] || { echo "inv46: FAIL — group create"; exit 1; }
leg "editor joins the crew group" 2 "$(code -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"userIds":["'"$TV_EDITOR_ID"'"]}' "$API/groups/$GRP/members" | cut -c1)"
leg "share to the group (grants edit access)" 200 "$(code -X PUT -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"organizationOnly":false,"groups":["'"$GRP"'"],"publicAccess":false}' "$API/layers/$LID/shares")"
sleep 0.3
leg "attachments on (leg 3 needs the door)" 200 "$(code -X PUT -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"attachmentsEnabled":true}' "$API/layers/$LID/attachments-enabled")"
F="$API/layers/$LID/features"; ED_H="Authorization: Bearer $ED"
set_ops() { # <add> <attributes> <geometry> <delete>
  code -X PUT -H "Authorization: Bearer $OWNER" -H "$JSON" \
    -d "{\"editingEnabled\":true,\"editOps\":{\"add\":$1,\"attributes\":$2,\"geometry\":$3,\"delete\":$4}}" "$API/layers/$LID/editing"
}
ver() { curl -sS -H "$ED_H" "$F/$1" | jget 'd["version"]'; }
mk() { curl -sS -X POST -H "$ED_H" -H "$JSON" -d '{"lon":-82.53,"lat":27.34,"notes":"n"}' "$F" | jget 'd["id"]'; }
PNGFILE="$(mktemp -t inv46).png"; python3 -c "import base64,sys;open(sys.argv[1],'wb').write(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=='))" "$PNGFILE"

echo "inv46 leg 0 — all four on (default): every door works for the genuine editor"
F1="$(mk)"; leg "add" "ok" "$([ -n "$F1" ] && echo ok || echo empty)"
leg "attributes" 200 "$(code -X PATCH -H "$ED_H" -H "$JSON" -H "If-Match: $(ver "$F1")" -d '{"notes":"two"}' "$F/$F1")"
leg "geometry" 200 "$(code -X PATCH -H "$ED_H" -H "$JSON" -H "If-Match: $(ver "$F1")" -d '{"lon":-82.531,"lat":27.341}' "$F/$F1")"
leg "delete" 204 "$(code -X DELETE -H "$ED_H" -H "If-Match: $(ver "$F1")" "$F/$F1")"

echo "inv46 leg 1 — Delete features OFF"
leg "set flags" 200 "$(set_ops true true true false)"
F1="$(mk)"; leg "add still works" "ok" "$([ -n "$F1" ] && echo ok || echo empty)"
deny_leg "editor DELETE" "deleting" -X DELETE -H "$ED_H" -H "If-Match: $(ver "$F1")" "$F/$F1"
leg "attributes still works" 200 "$(code -X PATCH -H "$ED_H" -H "$JSON" -H "If-Match: $(ver "$F1")" -d '{"notes":"three"}' "$F/$F1")"
leg "geometry still works" 200 "$(code -X PATCH -H "$ED_H" -H "$JSON" -H "If-Match: $(ver "$F1")" -d '{"lon":-82.532,"lat":27.342}' "$F/$F1")"
deny_leg "ADMIN DELETE (F1: no override)" "deleting" -X DELETE -H "Authorization: Bearer $ADMIN" -H "If-Match: $(ver "$F1")" "$F/$F1"
leg "the row is still there" 200 "$(code -H "$ED_H" "$F/$F1")"

echo "inv46 leg 2 — Move or reshape OFF"
leg "set flags" 200 "$(set_ops true true false true)"
deny_leg "editor PATCH {lon,lat}" "moving" -X PATCH -H "$ED_H" -H "$JSON" -H "If-Match: $(ver "$F1")" -d '{"lon":-82.54,"lat":27.35}' "$F/$F1"
leg "PATCH {props} still works" 200 "$(code -X PATCH -H "$ED_H" -H "$JSON" -H "If-Match: $(ver "$F1")" -d '{"notes":"four"}' "$F/$F1")"
deny_leg "PATCH {lon,lat,notes} (both ops)" "moving" -X PATCH -H "$ED_H" -H "$JSON" -H "If-Match: $(ver "$F1")" -d '{"lon":-82.54,"lat":27.35,"notes":"five"}' "$F/$F1"
deny_leg "ADMIN PATCH {lon,lat}" "moving" -X PATCH -H "Authorization: Bearer $ADMIN" -H "$JSON" -H "If-Match: $(ver "$F1")" -d '{"lon":-82.54,"lat":27.35}' "$F/$F1"
leg "add still carries geometry (F5)" "ok" "$([ -n "$(mk)" ] && echo ok || echo empty)"

echo "inv46 leg 3 — Edit attributes OFF"
leg "set flags" 200 "$(set_ops true false true true)"
deny_leg "editor PATCH {notes}" "attributes" -X PATCH -H "$ED_H" -H "$JSON" -H "If-Match: $(ver "$F1")" -d '{"notes":"six"}' "$F/$F1"
leg "PATCH {lon,lat} still works" 200 "$(code -X PATCH -H "$ED_H" -H "$JSON" -H "If-Match: $(ver "$F1")" -d '{"lon":-82.55,"lat":27.36}' "$F/$F1")"
deny_leg "calc door" "attributes" -X POST -H "$ED_H" -H "$JSON" -d '{"op":"set","target_field":"notes","value":"x"}' "$F/calc"
deny_leg "attachment upload (F2)" "attributes" -X POST -H "$ED_H" -F "file=@$PNGFILE;type=image/png" "$API/layers/$LID/features/$F1/attachments"
deny_leg "ADMIN PATCH {notes}" "attributes" -X PATCH -H "Authorization: Bearer $ADMIN" -H "$JSON" -H "If-Match: $(ver "$F1")" -d '{"notes":"seven"}' "$F/$F1"

echo "inv46 leg 4 — Add features OFF"
leg "set flags" 200 "$(set_ops false true true true)"
deny_leg "editor POST" "adding" -X POST -H "$ED_H" -H "$JSON" -d '{"lon":-82.53,"lat":27.34}' "$F"
deny_leg "bulk import door" "adding" -X POST -H "$ED_H" -H "$JSON" -d '{"format":"geojson","geojson_text":"{\"type\":\"FeatureCollection\",\"features\":[]}"}' "$F/import"
deny_leg "ADMIN POST" "adding" -X POST -H "Authorization: Bearer $ADMIN" -H "$JSON" -d '{"lon":-82.53,"lat":27.34}' "$F"
leg "PATCH {props} still works" 200 "$(code -X PATCH -H "$ED_H" -H "$JSON" -H "If-Match: $(ver "$F1")" -d '{"notes":"eight"}' "$F/$F1")"
leg "DELETE still works" 204 "$(code -X DELETE -H "$ED_H" -H "If-Match: $(ver "$F1")" "$F/$F1")"

echo "inv46 leg 5 — the flags ride the editing door"
g="$(curl -sS -H "Authorization: Bearer $OWNER" "$API/layers/$LID/editing")"
leg "GET echoes add=false" False "$(printf '%s' "$g" | jget 'd["editOps"]["add"]')"
leg "GET echoes delete=true" True "$(printf '%s' "$g" | jget 'd["editOps"]["delete"]')"
leg "PUT master switch only" 200 "$(code -X PUT -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"editingEnabled":true}' "$API/layers/$LID/editing")"
leg "flags untouched by a master-only PUT" False "$(curl -sS -H "Authorization: Bearer $OWNER" "$API/layers/$LID/editing" | jget 'd["editOps"]["add"]')"
leg "viewer cannot PUT" 403 "$(code -X PUT -H "Authorization: Bearer $VIEWER" -H "$JSON" -d '{"editingEnabled":true,"editOps":{"add":true,"attributes":true,"geometry":true,"delete":true}}' "$API/layers/$LID/editing")"
leg "editor (not owner) cannot PUT" 403 "$(code -X PUT -H "$ED_H" -H "$JSON" -d '{"editingEnabled":true,"editOps":{"add":true,"attributes":true,"geometry":true,"delete":true}}' "$API/layers/$LID/editing")"
echo "inv46 leg 6 — the Esri façade: per-result refusals + honest capabilities"
RW="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"label":"inv46 rw","scope":"read_write"}' "$API/me/tokens" | jget 'd["token"]')"
case "$RW" in tpat_*) : ;; *) echo "  FAIL read_write PAT mint"; fail=1 ;; esac
FS="$API/esri-auth/rest/services/$LID/FeatureServer"
caps() { curl -sS -u "talus:$RW" "$FS/0?f=json" | jget 'd["capabilities"]'; }
agu() { curl -sS -u "talus:$RW" "$FS/0?f=json" | jget 'd["allowGeometryUpdates"]'; }
ae() { curl -sS -u "talus:$RW" -X POST "$FS/0/applyEdits" --data-urlencode 'f=json' "$@"; }
# a per-result refusal: success false, code 403, the sentence — never an HTTP error
res_denied() { # <label> <array> <fragment> <response>
  printf '%s' "$4" | jget "'ok' if (not d['$2'][0]['success']) and d['$2'][0]['error']['code']==403 and '$3' in d['$2'][0]['error']['description'] else 'no: '+str(d)" | grep -q '^ok$' \
    && echo "  PASS $1 (per-result 403, sentence)" || { echo "  FAIL $1: $(printf '%s' "$4" | cut -c1-200)"; fail=1; }
}
res_ok() { printf '%s' "$3" | jget "'ok' if d['$2'][0]['success'] else 'no'" | grep -q '^ok$' && echo "  PASS $1" || { echo "  FAIL $1: $(printf '%s' "$3" | cut -c1-200)"; fail=1; }; }
leg "all four on" 200 "$(set_ops true true true true)"
leg "capabilities = today's string" "Query,Create,Update,Delete,Editing" "$(caps)"
leg "allowGeometryUpdates true" True "$(agu)"
r="$(ae --data-urlencode 'adds=[{"geometry":{"x":-82.53,"y":27.34},"attributes":{"notes":"fs"}}]')"
res_ok "façade add (control)" addResults "$r"
OID="$(printf '%s' "$r" | jget 'd["addResults"][0]["objectId"]')"
leg "set delete OFF" 200 "$(set_ops true true true false)"
leg "capabilities drop Delete only" "Query,Create,Update,Editing" "$(caps)"
r="$(ae --data-urlencode "deletes=$OID" --data-urlencode 'adds=[{"geometry":{"x":-82.53,"y":27.34},"attributes":{"notes":"fs2"}}]' --data-urlencode 'rollbackOnFailure=false')"
res_denied "façade delete" deleteResults "deleting" "$r"
res_ok "the add in the same request still lands" addResults "$r"
OID2="$(printf '%s' "$r" | jget 'd["addResults"][0]["objectId"]')"
leg "set geometry OFF" 200 "$(set_ops true true false true)"
leg "capabilities keep Update (attributes on)" "Query,Create,Update,Delete,Editing" "$(caps)"
leg "allowGeometryUpdates false" False "$(agu)"
# ★ bash 3.2: `\"` inside $(…) in ARGUMENT position loses its quoting and the braces brace-expand
# (three words, "unparseable updates JSON"); an assignment keeps it — so each payload is built first.
U="updates=[{\"geometry\":{\"x\":-82.54,\"y\":27.35},\"attributes\":{\"OBJECTID\":$OID}}]"
res_denied "façade update carrying geometry" updateResults "moving" "$(ae --data-urlencode "$U")"
U="updates=[{\"attributes\":{\"OBJECTID\":$OID,\"notes\":\"fs3\"}}]"
res_ok "façade attribute-only update still lands" updateResults "$(ae --data-urlencode "$U")"
leg "set attributes OFF" 200 "$(set_ops true false true true)"
leg "capabilities keep Update (geometry on)" "Query,Create,Update,Delete,Editing" "$(caps)"
U="updates=[{\"attributes\":{\"OBJECTID\":$OID,\"notes\":\"fs4\"}}]"
res_denied "façade attribute update" updateResults "attributes" "$(ae --data-urlencode "$U")"
U="updates=[{\"geometry\":{\"x\":-82.55,\"y\":27.36},\"attributes\":{\"OBJECTID\":$OID}}]"
res_ok "façade geometry-only update still lands" updateResults "$(ae --data-urlencode "$U")"
leg "set attributes + geometry OFF" 200 "$(set_ops true false false true)"
leg "capabilities drop Update" "Query,Create,Delete,Editing" "$(caps)"
leg "set add OFF" 200 "$(set_ops false true true true)"
leg "capabilities drop Create" "Query,Update,Delete,Editing" "$(caps)"
res_denied "façade add" addResults "adding" "$(ae --data-urlencode 'adds=[{"geometry":{"x":-82.53,"y":27.34},"attributes":{}}]')"
leg "set all four OFF (master on)" 200 "$(set_ops false false false false)"
leg "capabilities = Query" "Query" "$(caps)"
leg "restore all four" 200 "$(set_ops true true true true)"
res_ok "façade delete works again (control)" deleteResults "$(ae --data-urlencode "deletes=$OID,$OID2")"

echo "inv46 leg 7 — the Esri where compiler: capped, and fast to refuse (once-over A2)"
# The compiler's wrapper-stripping was quadratic in nesting depth: 4,000 nested parentheses (7.8 KB)
# cost a second, and it runs on the api's event loop — one request froze every other caller. It is
# now one pass, behind three caps (64,000 characters, 16 deep, 64 consecutive whitespace characters).
Q="$FS/0/query"
HEALTH="${API%/api/v1}/health"     # /health is unversioned (main.py), and the edge routes it too
nest() { # <depth> <inner> -> inner wrapped in that many balanced parentheses (bash 3.2-safe)
  printf '%s%s%s' "$(printf '%*s' "$1" '' | tr ' ' '(')" "$2" "$(printf '%*s' "$1" '' | tr ' ' ')')"
}
leg "control: where=1=1 by token" 200 "$(code -u "talus:$RW" -X POST "$Q" --data-urlencode 'f=json' --data-urlencode 'where=1=1')"
W2="where=$(nest 2 '1=1')"
leg "control: where=((1=1)) — parentheses themselves are fine" 200 "$(code -u "talus:$RW" -X POST "$Q" --data-urlencode 'f=json' --data-urlencode "$W2")"
W17="where=$(nest 17 '1=1')"
g="$(code -u "talus:$RW" -X POST "$Q" --data-urlencode 'f=json' --data-urlencode "$W17")"
leg "where nested 17 deep (the cap is 16)" 400 "$g"
if [ "$g" != "400" ]; then
  echo "  FAIL the depth cap is absent — NOT sending the 200,000-parenthesis where: without the fix it would freeze this api"; fail=1
else
  WFILE="$(mktemp "${TMPDIR:-/tmp}/inv46-where.XXXXXX")"
  printf '%*s' 200000 '' | tr ' ' '(' > "$WFILE"
  # name@file: curl url-encodes the file's content as the form field `where`
  r="$(curl -sS --max-time 30 -o /dev/null -w '%{http_code} %{time_total}' -u "talus:$RW" -X POST "$Q" \
        --data-urlencode 'f=json' --data-urlencode "where@$WFILE")"
  wc="${r%% *}"; wt="${r#* }"
  leg "where of 200,000 nested parentheses (POST form)" 400 "$wc"
  if awk -v t="$wt" 'BEGIN { exit !(t + 0 < 3) }'; then
    echo "  PASS …refused in ${wt}s (under 3 s)"
  else
    echo "  FAIL …took ${wt}s to answer (expected under 3 s) — the compiler is not linear, or the cap is not first"; fail=1
  fi
  h="$(curl -sS --max-time 5 -o /dev/null -w '%{http_code} %{time_total}' "$HEALTH")"
  leg "/health straight afterwards (${h#* }s)" 200 "${h%% *}"
  [ -f "$WFILE" ] && rm -f "$WFILE"
  WFILE=""
fi
rm -f "$PNGFILE"
if [ "$fail" -eq 0 ]; then echo "inv46: PASS"; else echo "inv46: FAIL"; fi
exit "$fail"
