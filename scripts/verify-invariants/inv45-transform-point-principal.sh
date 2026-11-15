#!/usr/bin/env bash
# Invariant (Talus Field GNSS pre-bench fix)
# — GET /transform-point takes the optional-auth PRINCIPAL like every other read door: a session
# Bearer OR a personal access token sent as Basic (a desktop PAT, or a pack's device token — the only
# credential a crew phone holds once its portal session lapses; the drain's `toNative` converts a queued
# lon/lat on a native-CRS layer with it at sync). It serves two numbers and no data, so no view gate;
# anonymous is STILL refused. The pinned x/y is the database's answer for one point, so a drift in the
# transform pipeline is legible here, not on a monument. (Until the database image moved to PROJ 9 with the
# datum correction files, this pinned 484107.827, 1093132.248 for the point labelled 4326: the old image applied
# a HARN adjustment that does not belong to a NAD83(2011) system. Both labels now give the same answer.)
# Legs:
#   1. anon → 401 (the invariant that must never move)
#   2. a session Bearer → 200, and the pinned value: −82.53, 27.34 as 4326 → 484108.912, 1093132.149
#   3. a read_only PAT as Basic → 200 with the same x/y (it is a read; a desktop PAT converts too)
#   4. from_srid=6318 (NAD83(2011) geographic — what an RTK fix on a NAD83(2011) stream really is) → the same
#      484108.912, 1093132.149 (no adjustment between WGS 84 and NAD83(2011))
#   4b. a point the coordinate library refuses for the target system (lon 0, lat 0) → 422, never 500
#   5. the contract kept: an unknown srid → 422; an unknown PAT → 401 with no reason header
#   5b. /spatial-refs (r6, the Fix-datum search) takes the same principal; kind=geographic lists only GEOGCS
#   6. a pack's DEVICE token → 200 with the leg-2 x/y (and searches datums); the token revoked → 401 + X-Talus-Auth-Code:
#      token_revoked, and with `X-Talus-Client: field` NO Basic challenge (WebKit's sheet never appears).
#      Needs the dev entitlement + paid.field + V108 — those legs are reported SKIPPED elsewhere; 1–5 are core.
# inv44 is the model (the geocode doors, the same seam). Needs a working dev realm (tv_mint).
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv45: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
ENT_DIR=".dev-entitlement"
fail=0
# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh
tv_mint publisher || { echo "inv45: FAIL — no principal"; exit 1; }
OWNER="$TV_PUBLISHER_TOKEN"
JSON='Content-Type: application/json'
code() { curl -sS -o /dev/null -w '%{http_code}' "$@"; }
hdr() { curl -sS -o /dev/null -D - "$@" | tr -d '\r' | awk 'tolower($1)=="x-talus-auth-code:"{print $2; f=1} END{if(!f)print "-"}'; }
chal() { curl -sS -o /dev/null -D - "$@" | tr -d '\r' | awk 'tolower($1)=="www-authenticate:"{print $2; f=1} END{if(!f)print "-"}'; }
jget() { python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }
# "x,y" to three decimals, or "-" when the answer is not a point
xy() { curl -sS "$@" | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin); print("%.3f,%.3f" % (d["x"], d["y"]))
except Exception: print("-")'; }
leg() { if [ "$2" = "$3" ]; then echo "  PASS $1 ($3)"; else echo "  FAIL $1 (expected $2, got $3)"; fail=1; fi; }

Q="lon=-82.53&lat=27.34&srid=6443"
DOOR="$API/transform-point?$Q"
WGS="484108.912,1093132.149"   # the point labelled 4326
NAD="484108.912,1093132.149"   # the same point labelled 6318 (NAD83(2011)): no adjustment between the two

echo "inv45: /transform-point takes a session OR a PAT; anonymous stays out; the pinned numbers hold"
leg "anon → 401" 401 "$(code "$DOOR")"
leg "session Bearer → 200" 200 "$(code -H "Authorization: Bearer $OWNER" "$DOOR")"
leg "session: 4326 → 6443 is the pinned value" "$WGS" "$(xy -H "Authorization: Bearer $OWNER" "$DOOR")"

MINTED="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"label":"inv45 ro","scope":"read_only"}' "$API/me/tokens")"
RO="$(printf '%s' "$MINTED" | jget 'd["token"]')"
RO_ID="$(printf '%s' "$MINTED" | jget 'd["id"]')"
[ -n "$RO" ] && [ -n "$RO_ID" ] || { echo "inv45: FAIL — could not mint a PAT"; exit 1; }
leg "read_only PAT (Basic) → 200" 200 "$(code -u "talus:$RO" "$DOOR")"
leg "read_only PAT: the same x/y" "$WGS" "$(xy -u "talus:$RO" "$DOOR")"
leg "from_srid=6318 → the same value" "$NAD" "$(xy -u "talus:$RO" "$DOOR&from_srid=6318")"
# lon 0, lat 0 is a point the coordinate library REFUSES for this system ("Point outside of projection
# domain"); the door answered 500. Not every far point is refused: some still convert to a meaningless
# finite number, as they always did, and the door returns it.
leg "a point the library refuses for this system → 422, not 500" 422 "$(code -u "talus:$RO" "$API/transform-point?lon=0&lat=0&srid=6443")"
leg "unknown srid → 422" 422 "$(code -u "talus:$RO" "$API/transform-point?lon=-82.53&lat=27.34&srid=999999")"
echo "inv45 leg 5b — /spatial-refs (the Fix-datum search, r6) takes the same principal; kind=geographic is geographic only"
SR="$API/spatial-refs"
leg "anon /spatial-refs → 401" 401 "$(code "$SR?q=6318&kind=geographic")"
leg "read_only PAT: q=6318 kind=geographic → NAD83(2011)" "NAD83(2011)" "$(curl -sS -u "talus:$RO" "$SR?q=6318&kind=geographic" | jget 'd["results"][0]["name"]')"
leg "kind=geographic hides a projected system (6443)" 0 "$(curl -sS -u "talus:$RO" "$SR?q=6443&kind=geographic" | jget 'len(d["results"])')"
leg "kind=any still lists it" 1 "$(curl -sS -u "talus:$RO" "$SR?q=6443" | jget 'len(d["results"])')"
leg "empty query, kind=geographic → the curated frames, WGS 84 first" 4326 "$(curl -sS -u "talus:$RO" "$SR?kind=geographic" | jget 'd["results"][0]["srid"]')"
leg "a bad kind → 422" 422 "$(code -u "talus:$RO" "$SR?kind=sideways")"
leg "revoke the PAT" 204 "$(code -X DELETE -H "Authorization: Bearer $OWNER" "$API/me/tokens/$RO_ID")"
leg "revoked PAT → 401" 401 "$(code -u "talus:$RO" "$DOOR")"
leg "revoked PAT → token_revoked" token_revoked "$(hdr -u "talus:$RO" "$DOOR")"
leg "unknown PAT → 401" 401 "$(code -u "talus:tpat_inv45unknownunknownunknownunknown" "$DOOR")"
leg "unknown PAT → no reason header" - "$(hdr -u "talus:tpat_inv45unknownunknownunknownunknown" "$DOOR")"

# ---- leg 6: a pack's device token (the drain's credential) — the inv43 fixture recipe ------------------------
dev_skip() { echo "  SKIP leg 6 (device token) — $1"; }
if [ ! -d "$ENT_DIR" ]; then dev_skip "$ENT_DIR missing"
elif ! grep -q "paid.field" .env 2>/dev/null; then dev_skip "paid.field not in TALUS_EXTRA_MODULES"
elif ! docker compose exec -T postgres psql -U talus -d talusgis -tA < /dev/null -c "SELECT 1 FROM information_schema.columns WHERE table_schema='platform' AND table_name='personal_access_tokens' AND column_name='layer_ids'" | grep -q 1; then dev_skip "V108 not applied"
else
  docker compose exec -T api python - <<'PY' > "$ENT_DIR/.inv45.json" || { echo "inv45: FAIL — licence fixtures"; exit 1; }
import json
from datetime import date, timedelta
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from app.entitlement import LicenceDocument, sign_licence
t = date.today()
doc = LicenceDocument(licence_id="inv45-field", customer="INV45 THROWAWAY", sku="talus-plus", issued_at=t, term_start=t, term_end=t + timedelta(days=365), modules=["dashboards","story_maps","reports","ai","field"])
k = Ed25519PrivateKey.generate()
pub = k.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
print(json.dumps({"public_pem": pub, "with_field": sign_licence(doc, k).model_dump(mode="json")}))
PY
  cp "$ENT_DIR/public.pem" "$ENT_DIR/.inv45.pub.bak" 2>/dev/null || true
  cp "$ENT_DIR/licence.json" "$ENT_DIR/.inv45.lic.bak" 2>/dev/null || true
  M1=""; LA=""; P1=""
  leg_cleanup() {
    [ -n "$P1" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/field-packs/$P1"
    [ -n "$M1" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/web-maps/$M1"
    [ -n "$LA" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/layers/$LA"
    [ -f "$ENT_DIR/.inv45.pub.bak" ] && mv "$ENT_DIR/.inv45.pub.bak" "$ENT_DIR/public.pem"
    [ -f "$ENT_DIR/.inv45.lic.bak" ] && mv "$ENT_DIR/.inv45.lic.bak" "$ENT_DIR/licence.json"
    rm -f "$ENT_DIR/.inv45.json"
  }
  python3 - "$ENT_DIR" <<'PY' || { echo "inv45: FAIL — fixture write"; exit 1; }
import json, sys
d = json.load(open(f"{sys.argv[1]}/.inv45.json"))
open(f"{sys.argv[1]}/public.pem", "w").write(d["public_pem"])
json.dump(d["with_field"], open(f"{sys.argv[1]}/licence.json", "w"))
PY
  sleep 0.3
  LA="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" \
    -d '{"display_name":"inv45 pack layer","geom_type":"point","working_srid":4326,"native_storage":false,"editable":true}' \
    "$API/layers" | jget 'd.get("layer_id","")')"
  case "$LA" in tal_*) : ;; *) echo "inv45: FAIL — layer create ('$LA')"; leg_cleanup; exit 1 ;; esac
  curl -sS -o /dev/null -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"lon":-82.53,"lat":27.34}' "$API/layers/$LA/features"
  M1="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" \
    -d '{"title":"inv45 field-ready","definition":{"version":1,"basemap":"light","operationalLayers":[{"layerId":"'"$LA"'","visible":true}],"initialView":{"center":[-82.53,27.34],"zoom":15},"field":{"enabled":true}}}' \
    "$API/web-maps" | jget 'd["id"]')"
  [ -n "$M1" ] || { echo "inv45: FAIL — map create"; leg_cleanup; exit 1; }
  P1="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"title":"Ward 45","bbox":[-82.6,27.3,-82.4,27.4],"basemapMaxzoom":14,"ttlDays":7}' "$API/web-maps/$M1/field-packs" | jget 'd.get("id","")')"
  [ -n "$P1" ] || { echo "inv45: FAIL — pack create"; leg_cleanup; exit 1; }
  st=""
  for i in $(seq 1 12); do st="$(curl -sS -H "Authorization: Bearer $OWNER" "$API/web-maps/$M1/field-packs" | jget 'd["packs"][0]["status"]')"; [ "$st" = ready ] && break; [ "$st" = failed ] && break; sleep 3; done
  [ "$st" = ready ] || { echo "inv45: FAIL — pack never built ($st)"; leg_cleanup; exit 1; }
  resp="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"packId":"'"$P1"'"}' "$API/me/field-tokens")"
  DEV="$(printf '%s' "$resp" | jget 'd["token"]')"; DEVID="$(printf '%s' "$resp" | jget 'd["tokenId"]')"
  [ -n "$DEV" ] || { echo "  FAIL device token mint: $resp"; fail=1; leg_cleanup; }
  if [ -n "$DEV" ]; then
    echo "inv45 leg 6 — the pack's device token (LA=$LA P1=$P1)"
    leg "device token (Basic + X-Talus-Client) → 200" 200 "$(code -u "talus:$DEV" -H 'X-Talus-Client: field' "$DOOR")"
    leg "device token: the same x/y" "$WGS" "$(xy -u "talus:$DEV" -H 'X-Talus-Client: field' "$DOOR")"
    # The next line's LABEL is not a credential: gitleaks' generic-api-key rule reads the word before
    # the colon and the text after it as a key/value pair (red on main for days). Allowlisted per-LINE, the repo's
    # narrowest form, so a real secret anywhere else in this file still fires.
    leg "device token: from_srid=6318" "$NAD" "$(xy -u "talus:$DEV" -H 'X-Talus-Client: field' "$DOOR&from_srid=6318")"  # gitleaks:allow
    leg "device token searches datums" 200 "$(code -u "talus:$DEV" -H 'X-Talus-Client: field' "$API/spatial-refs?q=NAD83&kind=geographic")"
    leg "owner revokes the device token" 204 "$(code -X DELETE -H "Authorization: Bearer $OWNER" "$API/me/tokens/$DEVID")"
    leg "revoked device token → 401" 401 "$(code -u "talus:$DEV" -H 'X-Talus-Client: field' "$DOOR")"
    leg "revoked device token → token_revoked" token_revoked "$(hdr -u "talus:$DEV" -H 'X-Talus-Client: field' "$DOOR")"
    leg "revoked + X-Talus-Client: field → no Basic challenge" - "$(chal -u "talus:$DEV" -H 'X-Talus-Client: field' "$DOOR")"
    leg "revoked, desktop → Basic challenge kept" Basic "$(chal -u "talus:$DEV" "$DOOR")"
    leg_cleanup
  fi
fi

if [ "$fail" = 0 ]; then echo "inv45: PASS"; else echo "inv45: FAIL"; fi
exit "$fail"
