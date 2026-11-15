#!/usr/bin/env bash
# Invariant (Talus Field f2, decision-talus-field-f2.md §6) — the layer form document is manage-gated
# to write, view-gated to read, validated key-by-key against the layer's OWN fields, and its
# `requiredIf` rule is enforced by the SERVER on every write door — not just drawn by the client.
#
# Fixtures: throwaway publisher (OWNER) creates a private point layer with a coded field
# `condition` (domain good/poor), a text `damage_desc`, a number `height_ft` (max 30), a boolean `lit`.
# Legs:
#   1. anon PUT /form                         -> 401 · forged alg:none -> 401
#   2. viewer PUT /form                       -> 403 (manage-gated) · viewer GET on the PRIVATE layer -> 403
#   3. owner PUT with a foreign field key     -> 400 · with an unknown rule key ("calculate") -> 400
#      · with a template default height_ft=99 -> 400 (constraint) · with `in` value not in domain -> 400
#   4. owner PUT the good document            -> 200; GET echoes it; /me/layers carries formConfig
#   5. requiredIf on the NATIVE write door: POST {condition:"poor"} without damage_desc -> 422;
#      POST {condition:"good"} without it -> 201; PATCH the good one to condition=poor -> 422;
#      PATCH with condition=poor AND damage_desc -> 200
#   6. owner DELETE /form -> 204; the same POST {condition:"poor"} now -> 201 (rule gone)
#
# Deny rule: any 2xx on leg 1/2/3, or a 2xx on the "-> 422" writes of leg 5 = FAIL (the client is not
# the gate). Requires V104 applied (form_config column + trigger pass). bash 3.2-safe; -sS curl; jq-free.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv39: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
FORGED='eyJhbGciOiJub25lIn0.eyJzdWIiOiJoYWNrZXIifQ.'
fail=0

# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh
tv_mint publisher || { echo "inv39: FAIL — no owner token"; exit 1; }
OWNER="$TV_PUBLISHER_TOKEN"
tv_mint viewer || { echo "inv39: FAIL — no viewer token"; exit 1; }
VIEWER="$TV_VIEWER_TOKEN"
JSON='Content-Type: application/json'

code() { curl -sS -o /dev/null -w '%{http_code}' "$@"; }
jget() { python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }
leg() { if [ "$2" = "$3" ]; then echo "  PASS $1 ($3)"; else echo "  FAIL $1 (expected $2, got $3)"; fail=1; fi; }

# --- fixtures --------------------------------------------------------------------------------------
LA="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" \
  -d '{"display_name":"inv39 form gate","geom_type":"point","working_srid":4326,"native_storage":false}' \
  "$API/layers" | jget 'd.get("layer_id","")')"
case "$LA" in tal_*) : ;; *) echo "inv39: FAIL — layer create failed ('$LA')"; exit 1 ;; esac
DOM="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" \
  -d '{"name":"inv39 condition '"$RANDOM"'","values":[{"code":"good","label":"Good"},{"code":"poor","label":"Poor"}]}' \
  "$API/custom-domains" | jget 'd.get("id","")')"
[ -n "$DOM" ] || { echo "inv39: FAIL — domain create failed"; exit 1; }
leg_cleanup() {
  [ -n "$LA" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/layers/$LA"
  [ -n "$DOM" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/custom-domains/$DOM"
}
mkfield() { curl -sS -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d "$1" "$API/layers/$LA/custom-fields"; }
f1="$(mkfield '{"fieldKey":"condition","label":"Condition","dataType":"coded","domainId":"'"$DOM"'","sortOrder":10}')"
f2="$(mkfield '{"fieldKey":"damage_desc","label":"Damage","dataType":"text","sortOrder":20}')"
f3="$(mkfield '{"fieldKey":"height_ft","label":"Height","dataType":"number","numericSubtype":"double","constraints":{"max":30},"sortOrder":30}')"
f4="$(mkfield '{"fieldKey":"lit","label":"Lit","dataType":"boolean","sortOrder":40}')"
[ "$f1$f2$f3$f4" = "201201201201" ] || { echo "inv39: FAIL — field fixtures ($f1 $f2 $f3 $f4)"; exit 1; }
echo "inv39: fixtures LA=$LA domain=$DOM owner $TV_PUBLISHER_USER"
F="$API/layers/$LA/form"
GOOD='{"version":1,"groups":[{"title":"Condition","fields":["condition","damage_desc"]}],"fields":{"damage_desc":{"visibleIf":{"field":"condition","eq":"poor"},"requiredIf":{"field":"condition","eq":"poor"}},"height_ft":{"readOnly":true}},"templates":[{"id":"ok","name":"Good one","defaults":{"condition":"good","lit":false}}],"photo":{"prompt":true}}'

echo "inv39 leg 1 — unauthenticated"
leg "anon PUT"   401 "$(code -X PUT -H "$JSON" -d "$GOOD" "$F")"
leg "forged PUT" 401 "$(code -X PUT -H "Authorization: Bearer $FORGED" -H "$JSON" -d "$GOOD" "$F")"

echo "inv39 leg 2 — viewer (no manage, no view on the private layer)"
leg "viewer PUT" 403 "$(code -X PUT -H "Authorization: Bearer $VIEWER" -H "$JSON" -d "$GOOD" "$F")"
leg "viewer GET" 403 "$(code -H "Authorization: Bearer $VIEWER" "$F")"

echo "inv39 leg 3 — the document is validated against THIS layer's fields"
leg "foreign field key"  400 "$(code -X PUT -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"groups":[{"title":"x","fields":["someone_elses_field"]}]}' "$F")"
leg "unknown rule key"   400 "$(code -X PUT -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"fields":{"damage_desc":{"calculate":"now()"}}}' "$F")"
leg "template violates constraint" 400 "$(code -X PUT -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"templates":[{"id":"t","name":"T","defaults":{"height_ft":99}}]}' "$F")"
leg "in-value not in domain" 400 "$(code -X PUT -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"fields":{"damage_desc":{"visibleIf":{"field":"condition","in":["destroyed"]}}}}' "$F")"

echo "inv39 leg 4 — owner sets the good document"
leg "owner PUT" 200 "$(code -X PUT -H "Authorization: Bearer $OWNER" -H "$JSON" -d "$GOOD" "$F")"
leg "GET echoes requiredIf" "poor" "$(curl -sS -H "Authorization: Bearer $OWNER" "$F" | jget 'd["formConfig"]["fields"]["damage_desc"]["requiredIf"]["eq"]')"
leg "/me/layers carries formConfig" "1" "$(curl -sS -H "Authorization: Bearer $OWNER" "$API/me/layers" | jget 'len([l for l in d["layers"] if l["id"]=="'"$LA"'" and (l.get("formConfig") or {}).get("templates")])')"

echo "inv39 leg 5 — requiredIf enforced on the native write door (the trigger)"
leg "POST poor w/o damage_desc" 422 "$(code -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"lon":-82.5,"lat":27.3,"notes":null,"props":{"condition":"poor"}}' "$API/layers/$LA/features")"
GID="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"lon":-82.5,"lat":27.3,"notes":null,"props":{"condition":"good"}}' "$API/layers/$LA/features" | jget 'd.get("id","")')"
leg "POST good (no rule fires)" "ok" "$([ -n "$GID" ] && echo ok || echo none)"
V="$(curl -sS -H "Authorization: Bearer $OWNER" "$API/layers/$LA/features/$GID" | jget 'd["version"]')"
leg "PATCH to poor w/o damage_desc" 422 "$(code -X PATCH -H "Authorization: Bearer $OWNER" -H "$JSON" -H "If-Match: $V" -d '{"props":{"condition":"poor"}}' "$API/layers/$LA/features/$GID")"
leg "PATCH to poor WITH damage_desc" 200 "$(code -X PATCH -H "Authorization: Bearer $OWNER" -H "$JSON" -H "If-Match: $V" -d '{"props":{"condition":"poor","damage_desc":"cracked"}}' "$API/layers/$LA/features/$GID")"

echo "inv39 leg 6 — clearing the document removes the rule"
leg "owner DELETE" 204 "$(code -X DELETE -H "Authorization: Bearer $OWNER" "$F")"
leg "POST poor now allowed" 201 "$(code -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"lon":-82.5,"lat":27.3,"notes":null,"props":{"condition":"poor"}}' "$API/layers/$LA/features")"

[ "$fail" = 0 ] && echo "inv39: PASS" || echo "inv39: FAIL"
exit $fail
