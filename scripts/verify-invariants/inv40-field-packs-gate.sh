#!/usr/bin/env bash
# Invariant (Talus Field f3, decision-talus-field-f3.md §4.3 / §6) — field-pack AUTHORING doors.
# The pack row is the only way to reach pack bytes, so who may create/list/rebuild/delete a pack is
# the first half of the pack's security story (the file doors' per-layer gates are f3d's legs).
#
# Fixtures: throwaway publisher (OWNER) creates a private point layer LA + two maps —
#   M1: LA visible, Field-ready        M2: LA visible, NOT Field-ready
# Legs (licence WITH field, restored on exit):
#   1. anon POST/GET/DELETE -> 401 · forged alg:none -> 401
#   2. viewer (no view) POST on M1 -> 403 (the manage gate: the map exists, the caller is not its owner)
#   3. owner POST on M2 (not Field-ready) -> 409 · owner POST on M1 -> 201 with an estimate > 0
#   3b. (f3g) POST …/field-packs/estimate: anon 401 · viewer 403 · owner on M2 409 · owner on M1 200
#       with totalBytes > 0 — and the list still holds exactly one pack (an estimate writes nothing)
#   4. viewer GET /web-maps/M1/field-packs -> 404 (no view) · owner -> 200, one pack, status pending
#   5. viewer rebuild/delete -> 404 · owner rebuild while pending -> 409 (queue guard)
#   6. share M1 to a crew GROUP holding the viewer (throwaway principals carry no org membership —
#      an organizationOnly share never reaches them): viewer GET list -> 200 but DELETE -> 403
#   6b. (f3d) the pack builds live (the worker's next tick); manifest: owner 200 with the layer;
#       viewer (map shared, LAYER not shared) 200 with ZERO layers; files/layers/LA.geojson -> 403 for
#       the viewer, 200 for the owner; files/basemap.pmtiles Range -> 206 with Content-Range; a path
#       the manifest does not name (../, manifest.json, a foreign layer) -> 404; after sharing LA to
#       the group the viewer's manifest carries it and the file is 200 (sha256 matches the manifest)
#   7. owner DELETE -> 204; owner GET list -> 0 packs; the manifest -> 404 (the row is the only way in)
#   8. licence WITHOUT field -> owner POST -> 404
# Requires the dev entitlement mount + paid.field in TALUS_EXTRA_MODULES + V105 applied (SKIP otherwise).
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv40: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
ENT_DIR=".dev-entitlement"
FORGED='eyJhbGciOiJub25lIn0.eyJzdWIiOiJoYWNrZXIifQ.'
fail=0
# shellcheck disable=SC1091
. scripts/verify-invariants/_paid.sh
tv_paid_mode field inv40
docker compose exec -T postgres psql -U talus -d talusgis -tA < /dev/null -c "SELECT 1 FROM information_schema.tables WHERE table_schema='platform' AND table_name='field_packs'" | grep -q 1 || { echo "inv40: SKIP — V105 not applied"; exit 3; }

# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh
tv_mint publisher || { echo "inv40: FAIL — no owner token"; exit 1; }
OWNER="$TV_PUBLISHER_TOKEN"
tv_mint viewer || { echo "inv40: FAIL — no viewer token"; exit 1; }
VIEWER="$TV_VIEWER_TOKEN"
JSON='Content-Type: application/json'
code() { curl -sS -o /dev/null -w '%{http_code}' "$@"; }
jget() { python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }
leg() { if [ "$2" = "$3" ]; then echo "  PASS $1 ($3)"; else echo "  FAIL $1 (expected $2, got $3)"; fail=1; fi; }

if [ "$TV_PAID_MODE" = dev ]; then
docker compose exec -T api python - <<'PY' > "$ENT_DIR/.inv40.json" || { echo "inv40: FAIL — licence fixtures"; exit 1; }
import json
from datetime import date, timedelta
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from app.entitlement import LicenceDocument, sign_licence
def doc(lid, mods):
    t = date.today()
    return LicenceDocument(licence_id=lid, customer="INV40 THROWAWAY", sku="talus-plus", issued_at=t, term_start=t, term_end=t + timedelta(days=365), modules=mods)
k = Ed25519PrivateKey.generate()
pub = k.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
print(json.dumps({"public_pem": pub,
  "with_field": sign_licence(doc("inv40-field", ["dashboards","story_maps","reports","ai","field"]), k).model_dump(mode="json"),
  "without_field": sign_licence(doc("inv40-nofield", ["dashboards","story_maps","reports","ai"]), k).model_dump(mode="json")}))
PY
cp "$ENT_DIR/public.pem" "$ENT_DIR/.inv40.pub.bak" 2>/dev/null || true
cp "$ENT_DIR/licence.json" "$ENT_DIR/.inv40.lic.bak" 2>/dev/null || true
fi
M1=""; M2=""; LA=""; P1=""; GRP=""
leg_cleanup() {
  [ -n "$GRP" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/groups/$GRP"
  [ -n "$P1" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/field-packs/$P1"
  [ -n "$M1" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/web-maps/$M1"
  [ -n "$M2" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/web-maps/$M2"
  [ -n "$LA" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/layers/$LA"
  if [ "$TV_PAID_MODE" = dev ]; then
    [ -f "$ENT_DIR/.inv40.pub.bak" ] && mv "$ENT_DIR/.inv40.pub.bak" "$ENT_DIR/public.pem"
    [ -f "$ENT_DIR/.inv40.lic.bak" ] && mv "$ENT_DIR/.inv40.lic.bak" "$ENT_DIR/licence.json"
    rm -f "$ENT_DIR/.inv40.json" "$ENT_DIR/licence-with_field.json" "$ENT_DIR/licence-without_field.json"
  fi
}
if [ "$TV_PAID_MODE" = dev ]; then
python3 - "$ENT_DIR" <<'PY' || { echo "inv40: FAIL — fixture write"; exit 1; }
import json, sys
d = json.load(open(f"{sys.argv[1]}/.inv40.json"))
open(f"{sys.argv[1]}/public.pem", "w").write(d["public_pem"])
for n in ("with_field", "without_field"):
    json.dump(d[n], open(f"{sys.argv[1]}/licence-{n}.json", "w"))
PY
use() { cp "$ENT_DIR/licence-$1.json" "$ENT_DIR/licence.json"; sleep 0.3; }
else
  use() { :; }   # LIVE: nothing is ever written to this box's licence
fi

LA="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" \
  -d '{"display_name":"inv40 pack gate","geom_type":"point","working_srid":4326,"native_storage":false}' \
  "$API/layers" | jget 'd.get("layer_id","")')"
case "$LA" in tal_*) : ;; *) echo "inv40: FAIL — layer create ('$LA')"; exit 1 ;; esac
curl -sS -o /dev/null -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"lon":-82.53,"lat":27.34}' "$API/layers/$LA/features"
mkmap() { # <title> <field-ready true|false>
  fld=''; [ "$2" = true ] && fld=',"field":{"enabled":true}'
  curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" \
    -d '{"title":"'"$1"'","definition":{"version":1,"basemap":"light","operationalLayers":[{"layerId":"'"$LA"'","visible":true}],"initialView":{"center":[-82.53,27.34],"zoom":15}'"$fld"'}}' \
    "$API/web-maps" | jget 'd["id"]'
}
M1="$(mkmap 'inv40 field-ready' true)"; M2="$(mkmap 'inv40 desk' false)"
[ -n "$M1" ] && [ -n "$M2" ] || { echo "inv40: FAIL — map create"; exit 1; }
BODY='{"title":"Ward 3","bbox":[-82.6,27.3,-82.4,27.4],"basemapMaxzoom":14,"ttlDays":7}'
echo "inv40: fixtures LA=$LA M1=$M1 (Field-ready) M2=$M2"

if [ "$TV_PAID_MODE" = unlicensed ]; then
echo "inv40 — UNLICENSED: this box's own licence does not grant field, so pack authoring must be shut"
leg "owner POST" 404 "$(code -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d "$BODY" "$API/web-maps/$M1/field-packs")"
leg "owner estimate" 404 "$(code -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d "$BODY" "$API/web-maps/$M1/field-packs/estimate")"
else
echo "inv40 leg 1 — unauthenticated"
use with_field
leg "anon POST"    401 "$(code -X POST -H "$JSON" -d "$BODY" "$API/web-maps/$M1/field-packs")"
leg "anon GET"     401 "$(code "$API/web-maps/$M1/field-packs")"
leg "anon crew"    401 "$(code "$API/field/packs")"
leg "forged POST"  401 "$(code -X POST -H "Authorization: Bearer $FORGED" -H "$JSON" -d "$BODY" "$API/web-maps/$M1/field-packs")"

echo "inv40 leg 2 — viewer without view"
leg "viewer POST M1" 403 "$(code -X POST -H "Authorization: Bearer $VIEWER" -H "$JSON" -d "$BODY" "$API/web-maps/$M1/field-packs")"

if tv_field_no_basemap "$OWNER" "$M1"; then
  tv_env_leg "inv40 legs 3–7 (create, estimate, list, rebuild/delete, crew share, the pack's file doors, delete)" \
    "no basemap archive is installed (Admin ▸ Basemap) — the product refuses to build packs, so there is nothing to gate"
else
# Hold the pack worker's advisory lock (packs_worker.py `_ADVISORY_LOCK_KEY` 9300000105) so the pack the owner
# creates STAYS pending through legs 3–5. The worker ticks every 5 s and a one-feature pack builds in well
# under a second, so without this the two "pending" legs race the worker: on a release rehearsal
# the list read `ready` and rebuild-while-pending answered 200 — a fixture race read as a
# RELEASE BLOCKER (feedback: a fixture that can fail on timing proves nothing). The lock is the worker's own
# try-lock: while a session holds it the tick skips; released below, the build proceeds (leg 7 waits for ready).
_pg(){ docker compose exec -T postgres psql -U "${TV_PGUSER:-talus}" -d "${TV_PGDB:-talusgis}" -tAq -c "$1" < /dev/null 2>/dev/null; }
_pg "SELECT pg_advisory_lock(9300000105); SELECT pg_sleep(180);" >/dev/null &
WORKER_HOLD=$!
sleep 1
held="$(_pg "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND classid=2 AND objid=710065513;" | tr -d '[:space:]')"
[ "${held:-0}" -ge 1 ] && echo "  (worker lock held for legs 3–5)" || echo "  NOTE: could not hold the pack worker's lock — the pending legs may race the worker"
release_worker_hold(){
  _pg "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE query LIKE '%pg_sleep(180)%' AND pid <> pg_backend_pid();" >/dev/null
  kill "$WORKER_HOLD" 2>/dev/null; wait "$WORKER_HOLD" 2>/dev/null
}
echo "inv40 leg 3 — owner creates"
leg "owner POST M2 (not Field-ready)" 409 "$(code -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d "$BODY" "$API/web-maps/$M2/field-packs")"
resp="$(curl -sS -w '\n%{http_code}' -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d "$BODY" "$API/web-maps/$M1/field-packs")"
c="$(printf '%s' "$resp" | tail -1)"; b="$(printf '%s' "$resp" | sed '$d')"
leg "owner POST M1" 201 "$c"
P1="$(printf '%s' "$b" | jget 'd.get("id","")')"
est="$(printf '%s' "$b" | jget 'd["estimate"]["totalBytes"]')"
[ -n "$P1" ] && [ "${est:-0}" -gt 0 ] && echo "  PASS estimate > 0 ($est bytes; $(printf '%s' "$b" | jget 'd["estimate"]["layers"][0]["featureCount"]') feature)" || { echo "  FAIL estimate"; fail=1; }
leg "duplicate title" 409 "$(code -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d "$BODY" "$API/web-maps/$M1/field-packs")"

echo "inv40 leg 3b — the estimate door (f3g) carries create's gates and writes nothing"
EST='{"bbox":[-82.6,27.3,-82.4,27.4],"basemapMaxzoom":14}'
leg "anon estimate"   401 "$(code -X POST -H "$JSON" -d "$EST" "$API/web-maps/$M1/field-packs/estimate")"
leg "viewer estimate" 403 "$(code -X POST -H "Authorization: Bearer $VIEWER" -H "$JSON" -d "$EST" "$API/web-maps/$M1/field-packs/estimate")"
leg "owner estimate M2 (not Field-ready)" 409 "$(code -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d "$EST" "$API/web-maps/$M2/field-packs/estimate")"
e2="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d "$EST" "$API/web-maps/$M1/field-packs/estimate" | jget 'd["totalBytes"]')"
[ "${e2:-0}" -gt 0 ] && echo "  PASS owner estimate M1 ($e2 bytes)" || { echo "  FAIL owner estimate M1"; fail=1; }
n="$(curl -sS -H "Authorization: Bearer $OWNER" "$API/web-maps/$M1/field-packs" | jget 'len(d["packs"])')"
leg "estimate created nothing (still one pack)" 1 "$n"

echo "inv40 leg 4 — list"
leg "viewer GET list (no view)" 404 "$(code -H "Authorization: Bearer $VIEWER" "$API/web-maps/$M1/field-packs")"
leg "owner GET list" 200 "$(code -H "Authorization: Bearer $OWNER" "$API/web-maps/$M1/field-packs")"
st="$(curl -sS -H "Authorization: Bearer $OWNER" "$API/web-maps/$M1/field-packs" | jget 'd["packs"][0]["status"]')"
leg "pack status pending" pending "$st"

echo "inv40 leg 5 — rebuild / delete gates"
leg "viewer rebuild" 403 "$(code -X POST -H "Authorization: Bearer $VIEWER" "$API/field-packs/$P1/rebuild")"
leg "viewer delete"  403 "$(code -X DELETE -H "Authorization: Bearer $VIEWER" "$API/field-packs/$P1")"
leg "owner rebuild while pending" 409 "$(code -X POST -H "Authorization: Bearer $OWNER" "$API/field-packs/$P1/rebuild")"
release_worker_hold

echo "inv40 leg 6 — view is not manage (share to a crew group holding the viewer)"
GRP="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"name":"inv40 crew '"$RANDOM"'"}' "$API/groups" | jget 'd.get("groupId") or d.get("group_id","")')"
[ -n "$GRP" ] || { echo "  FAIL group create"; fail=1; }
leg "add viewer to the crew group" 200 "$(code -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"userIds":["'"$TV_VIEWER_ID"'"]}' "$API/groups/$GRP/members")"
s1="$(code -X PUT -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"organizationOnly":false,"groups":["'"$GRP"'"],"publicAccess":false}' "$API/web-maps/$M1/shares")"
leg "share M1 to the group" 200 "$s1"; sleep 0.3
leg "viewer GET list (can view)" 200 "$(code -H "Authorization: Bearer $VIEWER" "$API/web-maps/$M1/field-packs")"
leg "viewer DELETE (view != manage)" 403 "$(code -X DELETE -H "Authorization: Bearer $VIEWER" "$API/field-packs/$P1")"
leg "viewer crew list (pack not ready → absent)" 200 "$(code -H "Authorization: Bearer $VIEWER" "$API/field/packs")"
# The rule is "a pack is in the crew list exactly when it is ready" — not "it is still pending here": on a
# fast production box the worker had already built it by now, and the old leg went
# red on correct behaviour. Read the status on both sides of the list read; a status that moved in between
# is re-read, never guessed.
pst() { curl -sS -H "Authorization: Bearer $OWNER" "$API/web-maps/$M1/field-packs" | jget 'd["packs"][0]["status"]'; }
for i in 1 2 3; do
  s0="$(pst)"
  n="$(curl -sS -H "Authorization: Bearer $VIEWER" "$API/field/packs" | jget "sum(1 for p in d['packs'] if p['id']=='$P1')")"
  s1="$(pst)"
  [ -n "$s0" ] && [ "$s0" = "$s1" ] && break
done
if [ -z "$s0" ] || [ "$s0" != "$s1" ]; then echo "  FAIL crew list vs status: the status would not hold still ($s0 → $s1)"; fail=1
elif [ "$s0" = ready ]; then leg "ready pack IS in the crew list (it built before this read)" 1 "$n"
else leg "$s0 pack NOT in the crew list" 0 "$n"; fi

echo "inv40 leg 6b — the download doors (the pack builds on the worker's next tick)"
for i in $(seq 1 12); do st="$(curl -sS -H "Authorization: Bearer $OWNER" "$API/web-maps/$M1/field-packs" | jget 'd["packs"][0]["status"]')"; [ "$st" = ready ] && break; [ "$st" = failed ] && break; sleep 3; done
leg "pack built (status)" ready "$st"
MAN="$API/field/packs/$P1/manifest"; FILES="$API/field/packs/$P1/files"
leg "anon manifest" 401 "$(code "$MAN")"
leg "owner manifest" 200 "$(code -H "Authorization: Bearer $OWNER" "$MAN")"
n="$(curl -sS -H "Authorization: Bearer $OWNER" "$MAN" | jget 'len(d["layers"])')"; leg "owner manifest lists the layer" 1 "$n"
n="$(curl -sS -H "Authorization: Bearer $VIEWER" "$MAN" | jget 'len(d["layers"])')"; leg "viewer manifest (map shared, layer not) lists ZERO layers" 0 "$n"
leg "viewer layer file (no layer view)" 403 "$(code -H "Authorization: Bearer $VIEWER" "$FILES/layers/$LA.geojson")"
leg "owner layer file" 200 "$(code -H "Authorization: Bearer $OWNER" "$FILES/layers/$LA.geojson")"
leg "viewer basemap (map view is enough)" 200 "$(code -H "Authorization: Bearer $VIEWER" "$FILES/basemap.pmtiles")"
rc="$(curl -sS -o /dev/null -w '%{http_code} %header{content-range}' -H "Authorization: Bearer $VIEWER" -H 'Range: bytes=0-126' "$FILES/basemap.pmtiles")"
leg "basemap Range 0-126" "206 bytes 0-126/$(curl -sS -H "Authorization: Bearer $OWNER" "$MAN" | jget 'd["basemap"]["bytes"]')" "$rc"
leg "path not in manifest: manifest.json" 404 "$(code -H "Authorization: Bearer $OWNER" "$FILES/manifest.json")"
leg "path not in manifest: traversal" 404 "$(code -H "Authorization: Bearer $OWNER" "$FILES/..%2Fbuild%2F1%2Fbasemap.pmtiles")"
leg "path not in manifest: foreign layer" 404 "$(code -H "Authorization: Bearer $OWNER" "$FILES/layers/tal_00000000.geojson")"
s2="$(code -X PUT -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"organizationOnly":false,"groups":["'"$GRP"'"],"publicAccess":false}' "$API/layers/$LA/shares")"
leg "share LA to the group" 200 "$s2"; sleep 0.3
n="$(curl -sS -H "Authorization: Bearer $VIEWER" "$MAN" | jget 'len(d["layers"])')"; leg "viewer manifest now lists the layer" 1 "$n"
leg "viewer layer file now" 200 "$(code -H "Authorization: Bearer $VIEWER" "$FILES/layers/$LA.geojson")"
want="$(curl -sS -H "Authorization: Bearer $VIEWER" "$MAN" | jget 'd["layers"][0]["sha256"]')"
got="$(curl -sS -H "Authorization: Bearer $VIEWER" "$FILES/layers/$LA.geojson" | shasum -a 256 | cut -d' ' -f1)"
leg "layer file sha256 matches the manifest" "$want" "$got"
n="$(curl -sS -H "Authorization: Bearer $VIEWER" "$API/field/packs" | jget "sum(1 for p in d['packs'] if p['id']=='$P1')")"
leg "ready pack now in the viewer's crew list" 1 "$n"

echo "inv40 leg 7 — owner deletes"
leg "owner DELETE" 204 "$(code -X DELETE -H "Authorization: Bearer $OWNER" "$API/field-packs/$P1")"
n="$(curl -sS -H "Authorization: Bearer $OWNER" "$API/web-maps/$M1/field-packs" | jget 'len(d["packs"])')"
leg "list empty" 0 "$n"
leg "manifest after delete" 404 "$(code -H "Authorization: Bearer $OWNER" "$MAN")"; P1=""

fi   # legs 3–7 (a basemap archive is installed)

if tv_dev_leg "inv40 leg 8 (licence WITHOUT field)" "it swaps in a licence without field"; then
echo "inv40 leg 8 — licence WITHOUT field"
use without_field
leg "owner POST" 404 "$(code -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d "$BODY" "$API/web-maps/$M1/field-packs")"
fi
fi   # LIVE/DEV legs 1–8

tv_partial_report
[ "$fail" = 0 ] && echo "inv40: PASS" || echo "inv40: FAIL"
exit $fail
