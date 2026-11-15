#!/usr/bin/env bash
# Invariant (Talus Field f1, decision-talus-field-f1.md §6) — the field-map list door is
# entitlement-gated on the licence's MODULE LIST, authenticated-only, and reveals nothing past
# the caller's genuine view tuples: a map they cannot view never appears, a viewer's layers carry
# canEdit=false, and a map with nothing collectable is absent.
#
# Fixtures: throwaway publisher (OWNER) creates a private point layer LA and two maps —
#   M1: LA visible         (collectable)      M2: LA visible:false  (nothing collectable)
# Legs (throwaway keypair + licences written to the dev-entitlement mount, restored on exit):
#   1. licence WITH field:  anon -> 401 · forged alg:none -> 401
#   2. owner -> 200; M1 present with LA canEdit=true; M2 ABSENT
#   3. viewer (nothing shared) -> 200; M1 ABSENT; M2 ABSENT   (wrong-user: never a leak)
#   4. share M1 + LA to a crew GROUP the viewer belongs to -> viewer sees M1, LA canEdit=false
#      (view != edit); owner unchanged. (Group, not org: throwaway principals carry no
#      org:talus#member tuple — _principals.sh inserts the users row directly; real humans get
#      the tuple from User-admin. A crew group is the realistic shape anyway.)
#   5. licence WITHOUT field: owner -> 404 (indistinguishable from the feature not existing)
#   6. no licence: owner -> 404; CORE /me/web-maps still 200 (the gate never touches core)
#   7. (f2, decision-talus-field-f2.md §4.4) a collectable map WITHOUT definition.field.enabled is ABSENT;
#      flipping the switch (PATCH the definition) makes it appear — opt-in, the author decides.
#
# Deny rule: any 2xx on anon/forged, a wrong-user row, canEdit=true for the viewer, or a 200 on
# leg 5/6 = FAIL. Modes (_paid.sh, decision-paid-invariants-live.md): DEV runs every leg; LIVE — a real box
# licensed for field — runs legs 1–4 and 7 against the box's OWN licence (throwaway principals, layer, maps
# and crew group, removed on exit) and skips 5–6 by name; UNLICENSED runs leg 6's closed-door shape live.
# Not installed = SKIP. bash 3.2-safe; -sS curl; jq-free.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv38: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
ENT_DIR=".dev-entitlement"
FORGED='eyJhbGciOiJub25lIn0.eyJzdWIiOiJoYWNrZXIifQ.'
fail=0

# shellcheck disable=SC1091
. scripts/verify-invariants/_paid.sh
tv_paid_mode field inv38

# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh
tv_mint publisher || { echo "inv38: FAIL — no owner token"; exit 1; }
OWNER="$TV_PUBLISHER_TOKEN"
tv_mint viewer || { echo "inv38: FAIL — no viewer token"; exit 1; }
VIEWER="$TV_VIEWER_TOKEN"
JSON='Content-Type: application/json'

code() { curl -sS -o /dev/null -w '%{http_code}' "$@"; }
jget() { python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }
leg() { # <label> <expected> <actual>
  if [ "$2" = "$3" ]; then echo "  PASS $1 ($3)"; else echo "  FAIL $1 (expected $2, got $3)"; fail=1; fi
}
assert() { # <label> <token> <python-expr over d>
  body="$(curl -sS -H "Authorization: Bearer $2" "$API/field/maps")"
  if printf '%s' "$body" | python3 -c "import sys,json;d=json.load(sys.stdin);sys.exit(0 if ($3) else 1)" 2>/dev/null; then
    echo "  PASS $1"
  else
    echo "  FAIL $1"; echo "      body: $(printf '%s' "$body" | cut -c1-300)"; fail=1
  fi
}

# --- entitlement fixtures (the inv35 recipe) — DEV only; LIVE uses the box's own licence ---------
if [ "$TV_PAID_MODE" = dev ]; then
docker compose exec -T api python - <<'PY' > "$ENT_DIR/.inv38.json" || { echo "inv38: FAIL — licence fixtures"; exit 1; }
import json
from datetime import date, timedelta
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from app.entitlement import LicenceDocument, sign_licence
def doc(lid, mods):
    t = date.today()
    return LicenceDocument(licence_id=lid, customer="INV38 THROWAWAY", sku="talus-plus",
                           issued_at=t, term_start=t, term_end=t + timedelta(days=365), modules=mods)
k = Ed25519PrivateKey.generate()
pub = k.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
ALL = ["dashboards", "story_maps", "reports", "ai", "field"]; NOFIELD = ["dashboards", "story_maps", "reports", "ai"]
print(json.dumps({
    "public_pem": pub,
    "with_field":    sign_licence(doc("inv38-field", ALL), k).model_dump(mode="json"),
    "without_field": sign_licence(doc("inv38-nofield", NOFIELD), k).model_dump(mode="json"),
}))
PY
cp "$ENT_DIR/public.pem" "$ENT_DIR/.inv38.pub.bak" 2>/dev/null || true
cp "$ENT_DIR/licence.json" "$ENT_DIR/.inv38.lic.bak" 2>/dev/null || true
fi
M1=""; M2=""; M3=""; LA=""; GRP=""
leg_cleanup() { # runs from tv_on_exit before the principals are deleted
  [ -n "$GRP" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/groups/$GRP"
  [ -n "$M1" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/web-maps/$M1"
  [ -n "$M2" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/web-maps/$M2"
  [ -n "$M3" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/web-maps/$M3"
  [ -n "$LA" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/layers/$LA"
  if [ "$TV_PAID_MODE" = dev ]; then
    [ -f "$ENT_DIR/.inv38.pub.bak" ] && mv "$ENT_DIR/.inv38.pub.bak" "$ENT_DIR/public.pem"
    [ -f "$ENT_DIR/.inv38.lic.bak" ] && mv "$ENT_DIR/.inv38.lic.bak" "$ENT_DIR/licence.json"
    rm -f "$ENT_DIR/.inv38.json" "$ENT_DIR/licence-with_field.json" "$ENT_DIR/licence-without_field.json"
  fi
  return 0
}
if [ "$TV_PAID_MODE" = dev ]; then
python3 - "$ENT_DIR" <<'PY' || { echo "inv38: FAIL — fixture write"; exit 1; }
import json, sys
d = json.load(open(f"{sys.argv[1]}/.inv38.json"))
open(f"{sys.argv[1]}/public.pem", "w").write(d["public_pem"])
for n in ("with_field", "without_field"):
    json.dump(d[n], open(f"{sys.argv[1]}/licence-{n}.json", "w"))
PY
use() { cp "$ENT_DIR/licence-$1.json" "$ENT_DIR/licence.json"; sleep 0.3; }
else
  use() { :; }   # LIVE / UNLICENSED: nothing is ever written to this box's licence
fi

# --- catalog fixtures ---------------------------------------------------------------------------
LA="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" \
  -d '{"display_name":"inv38 field gate","geom_type":"point","working_srid":4326,"native_storage":false}' \
  "$API/layers" | jget 'd.get("layer_id","")')"
case "$LA" in tal_*) : ;; *) echo "inv38: FAIL — layer create failed ('$LA')"; exit 1 ;; esac
mkmap() { # <title> <visible:true|false> <field-ready:true|false> -> id
  fld=''; [ "$3" = true ] && fld=',"field":{"enabled":true}'
  curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" \
    -d '{"title":"'"$1"'","definition":{"version":1,"basemap":"light","operationalLayers":[{"layerId":"'"$LA"'","visible":'"$2"'}],"initialView":{"center":[-82.5,27.3],"zoom":15}'"$fld"'}}' \
    "$API/web-maps" | jget 'd["id"]'
}
M1="$(mkmap 'inv38 collectable' true true)"; M2="$(mkmap 'inv38 desk map' false true)"; M3="$(mkmap 'inv38 not field-ready' true false)"
[ -n "$M1" ] && [ -n "$M2" ] && [ -n "$M3" ] || { echo "inv38: FAIL — map create failed (M1='$M1' M2='$M2' M3='$M3')"; exit 1; }
echo "inv38: fixtures LA=$LA M1=$M1 (collectable) M2=$M2 (nothing collectable), owner $TV_PUBLISHER_USER"

if [ "$TV_PAID_MODE" = unlicensed ]; then
echo "inv38 — UNLICENSED: this box's own licence does not grant field, so the door must be shut"
leg "owner 404"            404 "$(code -H "Authorization: Bearer $OWNER" "$API/field/maps")"
leg "core /me/web-maps 200" 200 "$(code -H "Authorization: Bearer $OWNER" "$API/me/web-maps")"
else
echo "inv38 leg 1 — licence WITH field: unauthenticated"
use with_field
leg "anon"    401 "$(code "$API/field/maps")"
leg "forged"  401 "$(code -H "Authorization: Bearer $FORGED" "$API/field/maps")"

echo "inv38 leg 2 — owner"
leg "owner 200" 200 "$(code -H "Authorization: Bearer $OWNER" "$API/field/maps")"
assert "owner sees M1 with LA canEdit=true" "$OWNER" \
  "any(m['id']=='$M1' and [l['layerId'] for l in m['layers']]==['$LA'] and m['layers'][0]['canEdit'] is True for m in d['maps'])"
assert "owner does NOT see M2 (nothing collectable)" "$OWNER" "all(m['id']!='$M2' for m in d['maps'])"

echo "inv38 leg 3 — wrong-user (viewer, nothing shared)"
leg "viewer 200" 200 "$(code -H "Authorization: Bearer $VIEWER" "$API/field/maps")"
assert "viewer sees neither M1 nor M2" "$VIEWER" "all(m['id'] not in ('$M1','$M2') for m in d['maps'])"

echo "inv38 leg 4 — share M1 + LA to a crew group holding the viewer: viewer sees M1, canEdit=false"
GRP="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"name":"inv38 crew '"$RANDOM"'"}' "$API/groups" | jget 'd.get("groupId") or d.get("group_id","")')"
[ -n "$GRP" ] || { echo "  FAIL group create"; fail=1; }
m="$(code -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"userIds":["'"$TV_VIEWER_ID"'"]}' "$API/groups/$GRP/members")"
leg "add viewer to the crew group" 200 "$m"
s1="$(code -X PUT -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"organizationOnly":false,"groups":["'"$GRP"'"],"publicAccess":false}' "$API/web-maps/$M1/shares")"
s2="$(code -X PUT -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"organizationOnly":false,"groups":["'"$GRP"'"],"publicAccess":false}' "$API/layers/$LA/shares")"
leg "share map/layer to the group" "200200" "$s1$s2"
sleep 0.3
assert "viewer now sees M1, LA canEdit=false" "$VIEWER" \
  "any(m['id']=='$M1' and m['layers'][0]['layerId']=='$LA' and m['layers'][0]['canEdit'] is False for m in d['maps'])"
assert "owner still canEdit=true" "$OWNER" "any(m['id']=='$M1' and m['layers'][0]['canEdit'] is True for m in d['maps'])"

echo "inv38 leg 7 — Field-ready is opt-in (f2)"
assert "owner does NOT see M3 (collectable but not Field-ready)" "$OWNER" "all(m['id']!='$M3' for m in d['maps'])"
p="$(code -X PATCH -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"title":"inv38 not field-ready","definition":{"version":1,"basemap":"light","operationalLayers":[{"layerId":"'"$LA"'","visible":true}],"initialView":{"center":[-82.5,27.3],"zoom":15},"field":{"enabled":true}}}' "$API/web-maps/$M3")"
leg "flip M3 Field-ready" 200 "$p"
assert "owner now sees M3" "$OWNER" "any(m['id']=='$M3' for m in d['maps'])"

if tv_dev_leg "inv38 leg 5 (licence WITHOUT field)" "it swaps in a licence without field"; then
echo "inv38 leg 5 — licence WITHOUT field"
use without_field
leg "owner 404" 404 "$(code -H "Authorization: Bearer $OWNER" "$API/field/maps")"
fi

if tv_dev_leg "inv38 leg 6 (no licence)" "it removes the licence file"; then
echo "inv38 leg 6 — no licence"
rm -f "$ENT_DIR/licence.json"; sleep 0.3
leg "owner 404"            404 "$(code -H "Authorization: Bearer $OWNER" "$API/field/maps")"
leg "core /me/web-maps 200" 200 "$(code -H "Authorization: Bearer $OWNER" "$API/me/web-maps")"
fi
fi

tv_partial_report
[ "$fail" = 0 ] && echo "inv38: PASS" || echo "inv38: FAIL"
exit $fail
