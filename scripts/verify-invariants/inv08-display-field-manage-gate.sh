#!/usr/bin/env bash
# Invariant C8 — the layer display field is MANAGE-gated; a viewer/editor can never move it, anon never touches
# it, and only a REAL field of the layer can be set (decision-layer-display-field.md, fork F4/F6).
#
# The display field names a feature everywhere it shows as one line (popup title, Locate labels, ESRI
# displayField). Changing it alters the layer for everyone in every map — a config act, so PUT
# /layers/{id}/display-field consults manage_allowed (owner OR admin), NOT can_view or can_edit. This proves a
# public-shared layer's viewer (even with the data-editor role) is denied the write, anon is denied, and a
# bogus field name is a 400 (not a silent no-op that would let a typo blank the label).
#
# Setup: a throwaway publisher (owner) creates a slot + one feature (provisions the backing table so `notes`
# is a real column), then makes it public_shared → a throwaway data-editor gets can_view but NO manage. PASS =
# viewer PUT -> 403, anon PUT -> 401. Owner PUT (valid) -> 200 and owner PUT (bogus) -> 400 are the controls.
#
# Needs the running stack + .env (throwaways self-provisioned via _principals.sh). bash 3.2-safe; -sS curl.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv08: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
OPENFGA="http://openfga:8080"
[ -n "${OPENFGA_STORE_ID:-}" ] || OPENFGA_STORE_ID=$(grep '^OPENFGA_STORE_ID=' .env | cut -d= -f2)
[ -n "${OPENFGA_MODEL_ID:-}" ] || OPENFGA_MODEL_ID=$(grep '^OPENFGA_MODEL_ID=' .env | cut -d= -f2)
fga() { docker compose run --rm -T openfga-cli -c "$1" 2>/dev/null; }
fail=0

# owner token + a fresh layer + one feature (so the assets table — and its `notes` column — exists)
# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh
tv_mint publisher || { echo "inv08: FAIL — no owner token"; exit 1; }
OWNER="$TV_PUBLISHER_TOKEN"
LID=$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H 'Content-Type: application/json' \
  -d '{"display_name":"inv08 display-field test","geom_type":"point","working_srid":4326,"native_storage":false}' \
  "$API/layers" | python3 -c "import sys,json;print(json.load(sys.stdin).get('layer_id',''))" 2>/dev/null)
[ -n "$LID" ] || { echo "inv08: FAIL — layer create failed"; exit 1; }
curl -sS -o /dev/null -X POST -H "Authorization: Bearer $OWNER" -H 'Content-Type: application/json' \
  -d '{"lon":-82.5,"lat":27.3}' "$API/layers/${LID}/features"
echo "  layer $LID (owned by $TV_PUBLISHER_USER)"

# make it public_shared → grants can_view to everyone, NO manage
fga "curl -sS -X POST ${OPENFGA}/stores/${OPENFGA_STORE_ID}/write -H 'content-type: application/json' -d '{\"authorization_model_id\":\"${OPENFGA_MODEL_ID}\",\"writes\":{\"tuple_keys\":[{\"user\":\"user:*\",\"relation\":\"public_shared\",\"object\":\"layer:${LID}\"}]}}'" >/dev/null 2>&1

# throwaway editor: has the data-editor role, is NOT the owner, sees the layer only via public share
tv_mint editor || { echo "inv08: FAIL — no editor token"; exit 1; }
ED="$TV_EDITOR_TOKEN"

# [1] the ASSERTION: an editor-role viewer cannot MOVE the display field
wc=$(curl -sS -o /dev/null -w '%{http_code}' -X PUT -H "Authorization: Bearer $ED" -H 'Content-Type: application/json' \
  -d '{"displayField":"notes"}' "$API/layers/${LID}/display-field")
echo "  [1] editor-role viewer PUT display-field -> $wc (expect 403 — manage-gated, view/edit don't imply it)"
case "$wc" in
  403) : ;;
  2*) echo "      FAIL: viewer MOVED a public layer's display field ($wc) — manage gate leaked"; fail=1 ;;
  *)  echo "      FAIL: expected 403, got $wc"; fail=1 ;;
esac

# [2] the viewer STILL SEES the field + that they can't move it (GET is open; canManage:false)
cm=$(curl -sS -H "Authorization: Bearer $ED" "$API/layers/${LID}/display-field" \
  | python3 -c "import sys,json;print(json.load(sys.stdin).get('canManage'))" 2>/dev/null)
echo "  [2] editor-role viewer GET canManage -> $cm (expect False — sees the star, can't move it)"
[ "$cm" = "False" ] || { echo "      FAIL: viewer GET canManage was $cm (expected False)"; fail=1; }

# [3] anon is denied the write entirely (no new anon surface)
ac=$(curl -sS -o /dev/null -w '%{http_code}' -X PUT -H 'Content-Type: application/json' \
  -d '{"displayField":"notes"}' "$API/layers/${LID}/display-field")
echo "  [3] anon PUT display-field -> $ac (expect 401 — authentication required)"
case "$ac" in
  401|403) : ;;
  2*) echo "      FAIL: anon set the display field ($ac)"; fail=1 ;;
  *)  echo "      FAIL: expected 401/403, got $ac"; fail=1 ;;
esac

# [4] positive control: the owner CAN set a real field
oc=$(curl -sS -o /dev/null -w '%{http_code}' -X PUT -H "Authorization: Bearer $OWNER" -H 'Content-Type: application/json' \
  -d '{"displayField":"notes"}' "$API/layers/${LID}/display-field")
echo "  [4] owner PUT display-field=notes -> $oc (expect 200 — control)"
[ "$oc" = "200" ] || { echo "      FAIL: owner could not set its own layer's display field ($oc)"; fail=1; }

# [5] validation control: a non-existent field is a 400, not a silent no-op
bc=$(curl -sS -o /dev/null -w '%{http_code}' -X PUT -H "Authorization: Bearer $OWNER" -H 'Content-Type: application/json' \
  -d '{"displayField":"no_such_field"}' "$API/layers/${LID}/display-field")
echo "  [5] owner PUT display-field=<bogus> -> $bc (expect 400 — field validated)"
[ "$bc" = "400" ] || { echo "      FAIL: bogus field was not rejected ($bc)"; fail=1; }

# cleanup (owner deletes → also scrubs the public_shared tuple)
curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/layers/${LID}"

if [ "$fail" -eq 0 ]; then echo "inv08: PASS"; else echo "inv08: FAIL"; fi
exit "$fail"
