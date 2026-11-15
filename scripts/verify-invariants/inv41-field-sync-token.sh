#!/usr/bin/env bash
# Invariant (Talus Field f4, decision-talus-field-f4.md §4.2 / §6) — the DEVICE TOKEN and the
# read_write PAT seam on the native write doors. Until f4 every native feature write was Bearer-only;
# now a PAT that is read_write AND a field-sync token (V108 layer_ids) AND minted for THIS layer may
# create / update features and upload attachments. Everything else must still be refused, and the
# token must never widen what its owner could do.
#
# Fixtures (throwaway publisher = OWNER, viewer): layers LA (in the pack) and LB (NOT in the pack), a
# Field-ready map M1 over LA, a pack P1 built by the worker (inv40's recipe).
# Legs (licence WITH field):
#   1. mint door: anon 401 · a Basic (PAT) credential 401 (Bearer only) · owner on an unbuilt pack 409
#      (waits for the build) · owner 201 with layerIds == [LA] · viewer without view on M1 → 404
#   2. general-purpose PATs on the native doors: read_only → 403 (read-only sentence) · read_write
#      without layer_ids → 403 (device-token sentence); both still READ fine
#   3. the device token: POST LA → 201 · replay with the same Idempotency-Key → the SAME id · PATCH with
#      If-Match → 200 · PATCH with a stale If-Match → 409 · POST LB → 403 ("another pack") · DELETE a
#      feature → 401 (delete stays Bearer-only) · /me/tokens → 401
#   4. attachments: upload with Idempotency-Key → 201; the same key again → 200 with the SAME id; the
#      feature holds exactly one attachment
#   5. the owner's rights bound the token: unshare nothing, but a viewer's device token (map + LA shared
#      to the crew group, no edit tuple) → mint 201, POST LA → 403 (not an editor)
#   5b. the Esri write doors refuse a device token ENTIRELY:
#      applyEdits on LA (in the pack) and on LB (not in it), an
#      update naming an OBJECTID that does not exist (nothing is written either way) → HTTP 200 with a
#      per-edit refusal, code 403, whose sentence begins "This is a Talus Field device credential.";
#      the same for deleteFeatures (the door the devices page's "can never delete" is about) and, as a
#      plain 403, the QGIS attachment chute. Control: the owner's ordinary read_write token, the same
#      applyEdits on LA → past the gate (a per-edit "not found", never that sentence)
#   6. tokens are per DEVICE (f5): a second mint leaves the first live; a mint with `replaces` retires
#      only that token; DELETE /me/field-tokens/{P1} revokes every device's;  the token is never admin (an admin's device token on a layer
#      they do not genuinely edit → 403) — SKIPPED when no admin principal can be minted
#   7. licence WITHOUT field → mint 404
# Requires the dev entitlement mount + paid.field + V108 (SKIP otherwise).
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv41: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
ENT_DIR=".dev-entitlement"
fail=0
# shellcheck disable=SC1091
. scripts/verify-invariants/_paid.sh
tv_paid_mode field inv41
[ "$TV_PAID_MODE" = unlicensed ] && { echo "inv41: SKIP — field is not licensed on this box: a device token needs a built pack (inv38 and inv40 check the closed doors)"; exit 3; }
docker compose exec -T postgres psql -U talus -d talusgis -tA < /dev/null -c "SELECT 1 FROM information_schema.columns WHERE table_schema='platform' AND table_name='personal_access_tokens' AND column_name='layer_ids'" | grep -q 1 || { echo "inv41: SKIP — V108 not applied"; exit 3; }

# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh
tv_mint publisher || { echo "inv41: FAIL — no owner token"; exit 1; }
OWNER="$TV_PUBLISHER_TOKEN"
tv_mint viewer || { echo "inv41: FAIL — no viewer token"; exit 1; }
VIEWER="$TV_VIEWER_TOKEN"
JSON='Content-Type: application/json'
code() { curl -sS -o /dev/null -w '%{http_code}' "$@"; }
# the X-Talus-Auth-Code a 401 carries (inv43's helper), and a write to the throwaway's own rows
hdr() { curl -sS -o /dev/null -D - "$@" | tr -d '\r' | awk 'tolower($1)=="x-talus-auth-code:"{print $2; f=1} END{if(!f)print "-"}'; }
psqlw() { docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "$TV_PGUSER" -d "$TV_PGDB" -tA -c "$1" < /dev/null; }
jget() { python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }
leg() { if [ "$2" = "$3" ]; then echo "  PASS $1 ($3)"; else echo "  FAIL $1 (expected $2, got $3)"; fail=1; fi; }
uuid() { python3 -c "import uuid;print(uuid.uuid4())"; }

if [ "$TV_PAID_MODE" = dev ]; then
docker compose exec -T api python - <<'PY' > "$ENT_DIR/.inv41.json" || { echo "inv41: FAIL — licence fixtures"; exit 1; }
import json
from datetime import date, timedelta
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from app.entitlement import LicenceDocument, sign_licence
def doc(lid, mods):
    t = date.today()
    return LicenceDocument(licence_id=lid, customer="INV41 THROWAWAY", sku="talus-plus", issued_at=t, term_start=t, term_end=t + timedelta(days=365), modules=mods)
k = Ed25519PrivateKey.generate()
pub = k.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
print(json.dumps({"public_pem": pub,
  "with_field": sign_licence(doc("inv41-field", ["dashboards","story_maps","reports","ai","field"]), k).model_dump(mode="json"),
  "without_field": sign_licence(doc("inv41-nofield", ["dashboards","story_maps","reports","ai"]), k).model_dump(mode="json")}))
PY
cp "$ENT_DIR/public.pem" "$ENT_DIR/.inv41.pub.bak" 2>/dev/null || true
cp "$ENT_DIR/licence.json" "$ENT_DIR/.inv41.lic.bak" 2>/dev/null || true
fi
M1=""; LA=""; LB=""; P1=""; GRP=""
leg_cleanup() {
  [ -n "$P1" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/field-packs/$P1"
  [ -n "$GRP" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/groups/$GRP"
  [ -n "$M1" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/web-maps/$M1"
  [ -n "$LA" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/layers/$LA"
  [ -n "$LB" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/layers/$LB"
  if [ "$TV_PAID_MODE" = dev ]; then
    [ -f "$ENT_DIR/.inv41.pub.bak" ] && mv "$ENT_DIR/.inv41.pub.bak" "$ENT_DIR/public.pem"
    [ -f "$ENT_DIR/.inv41.lic.bak" ] && mv "$ENT_DIR/.inv41.lic.bak" "$ENT_DIR/licence.json"
    rm -f "$ENT_DIR/.inv41.json" "$ENT_DIR/licence-with_field.json" "$ENT_DIR/licence-without_field.json"
  fi
  rm -f /tmp/inv41-photo.png
}
if [ "$TV_PAID_MODE" = dev ]; then
python3 - "$ENT_DIR" <<'PY' || { echo "inv41: FAIL — fixture write"; exit 1; }
import json, sys
d = json.load(open(f"{sys.argv[1]}/.inv41.json"))
open(f"{sys.argv[1]}/public.pem", "w").write(d["public_pem"])
for n in ("with_field", "without_field"):
    json.dump(d[n], open(f"{sys.argv[1]}/licence-{n}.json", "w"))
PY
use() { cp "$ENT_DIR/licence-$1.json" "$ENT_DIR/licence.json"; sleep 0.3; }
else
  use() { :; }   # LIVE: nothing is ever written to this box's licence
fi
mklayer() { curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" \
  -d '{"display_name":"'"$1"'","geom_type":"point","working_srid":4326,"native_storage":false,"editable":true,"attachments_enabled":true}' \
  "$API/layers" | jget 'd.get("layer_id","")'; }
use with_field
LA="$(mklayer 'inv41 in pack')"; LB="$(mklayer 'inv41 not in pack')"
case "$LA$LB" in tal_*tal_*) : ;; *) echo "inv41: FAIL — layer create ('$LA' '$LB')"; leg_cleanup; exit 1 ;; esac
curl -sS -o /dev/null -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"lon":-82.53,"lat":27.34}' "$API/layers/$LA/features"
M1="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" \
  -d '{"title":"inv41 field-ready","definition":{"version":1,"basemap":"light","operationalLayers":[{"layerId":"'"$LA"'","visible":true}],"initialView":{"center":[-82.53,27.34],"zoom":15},"field":{"enabled":true}}}' \
  "$API/web-maps" | jget 'd["id"]')"
[ -n "$M1" ] || { echo "inv41: FAIL — map create"; leg_cleanup; exit 1; }
if tv_field_no_basemap "$OWNER" "$M1"; then
  echo "inv41: SKIP — no basemap archive is installed on this box (Admin ▸ Basemap): the product refuses to build packs, and a device token needs one"
  leg_cleanup; exit 3
fi
P1="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"title":"Ward 41","bbox":[-82.6,27.3,-82.4,27.4],"basemapMaxzoom":14,"ttlDays":7}' "$API/web-maps/$M1/field-packs" | jget 'd.get("id","")')"
[ -n "$P1" ] || { echo "inv41: FAIL — pack create"; leg_cleanup; exit 1; }
echo "inv41: fixtures LA=$LA LB=$LB M1=$M1 P1=$P1"
MINT="$API/me/field-tokens"; MB='{"packId":"'"$P1"'"}'

echo "inv41 leg 1 — the mint door"
leg "anon mint" 401 "$(code -X POST -H "$JSON" -d "$MB" "$MINT")"
leg "owner mint on an unbuilt pack" 409 "$(code -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d "$MB" "$MINT")"
for i in $(seq 1 12); do st="$(curl -sS -H "Authorization: Bearer $OWNER" "$API/web-maps/$M1/field-packs" | jget 'd["packs"][0]["status"]')"; [ "$st" = ready ] && break; [ "$st" = failed ] && break; sleep 3; done
leg "pack built" ready "$st"
leg "viewer mint (no view on M1)" 404 "$(code -X POST -H "Authorization: Bearer $VIEWER" -H "$JSON" -d "$MB" "$MINT")"
resp="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d "$MB" "$MINT")"
DEV="$(printf '%s' "$resp" | jget 'd["token"]')"; DEVID="$(printf '%s' "$resp" | jget 'd["tokenId"]')"
[ -n "$DEV" ] && echo "  PASS owner mint (201; layerIds=$(printf '%s' "$resp" | jget 'd["layerIds"]'))" || { echo "  FAIL owner mint: $resp"; fail=1; }
leg "device token layerIds == [LA]" "['$LA']" "$(printf '%s' "$resp" | jget 'd["layerIds"]')"
leg "a PAT cannot mint (Bearer only)" 401 "$(code -X POST -u "talus:$DEV" -H "$JSON" -d "$MB" "$MINT")"

echo "inv41 leg 2 — general-purpose PATs stay off the native doors"
RO="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"label":"inv41 ro","scope":"read_only"}' "$API/me/tokens" | jget 'd["token"]')"
RW="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"label":"inv41 rw","scope":"read_write"}' "$API/me/tokens" | jget 'd["token"]')"
NEW='{"lon":-82.531,"lat":27.341}'
leg "read_only PAT POST" 403 "$(code -X POST -u "talus:$RO" -H "$JSON" -d "$NEW" "$API/layers/$LA/features")"
leg "read_write PAT (no layer_ids) POST" 403 "$(code -X POST -u "talus:$RW" -H "$JSON" -d "$NEW" "$API/layers/$LA/features")"
leg "read_write PAT still reads" 200 "$(code -u "talus:$RW" "$API/layers/$LA/features?limit=1")"
leg "anon POST" 401 "$(code -X POST -H "$JSON" -d "$NEW" "$API/layers/$LA/features")"

echo "inv41 leg 3 — the device token on the native doors"
K="$(uuid)"
r1="$(curl -sS -w '\n%{http_code}' -X POST -u "talus:$DEV" -H "$JSON" -H "Idempotency-Key: $K" -d "$NEW" "$API/layers/$LA/features")"
leg "device token POST LA" 201 "$(printf '%s' "$r1" | tail -1)"
F1="$(printf '%s' "$r1" | sed '$d' | jget 'd["id"]')"
F2="$(curl -sS -X POST -u "talus:$DEV" -H "$JSON" -H "Idempotency-Key: $K" -d "$NEW" "$API/layers/$LA/features" | jget 'd["id"]')"
leg "replayed create returns the SAME id" "$F1" "$F2"
# the single-feature READ door is Bearer-only (the drain never reads it: versions ride the pack + the 409 body)
V="$(curl -sS -H "Authorization: Bearer $OWNER" "$API/layers/$LA/features/$F1" | jget 'd["version"]')"
leg "PATCH with If-Match" 200 "$(code -X PATCH -u "talus:$DEV" -H "$JSON" -H "If-Match: $V" -d '{"notes":"synced"}' "$API/layers/$LA/features/$F1")"
leg "PATCH with a stale If-Match" 409 "$(code -X PATCH -u "talus:$DEV" -H "$JSON" -H "If-Match: $V" -d '{"notes":"stale"}' "$API/layers/$LA/features/$F1")"
leg "device token POST LB (another pack)" 403 "$(code -X POST -u "talus:$DEV" -H "$JSON" -d "$NEW" "$API/layers/$LB/features")"
V2="$(curl -sS -H "Authorization: Bearer $OWNER" "$API/layers/$LA/features/$F1" | jget 'd["version"]')"
leg "DELETE stays Bearer-only" 401 "$(code -X DELETE -u "talus:$DEV" -H "If-Match: $V2" "$API/layers/$LA/features/$F1")"
leg "/me/tokens stays Bearer-only" 401 "$(code -u "talus:$DEV" "$API/me/tokens")"

echo "inv41 leg 4 — attachments: idempotent upload"
# attachments are switched on through the Manage door, not the create body
leg "enable attachments on LA" 200 "$(code -X PUT -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"attachmentsEnabled":true}' "$API/layers/$LA/attachments-enabled")"
python3 -c "import zlib,struct;sig=b'\x89PNG\r\n\x1a\n';
def ch(t,d): return struct.pack('>I',len(d))+t+d+struct.pack('>I',zlib.crc32(t+d)&0xffffffff)
open('/tmp/inv41-photo.png','wb').write(sig+ch(b'IHDR',struct.pack('>IIBBBBB',1,1,8,2,0,0,0))+ch(b'IDAT',zlib.compress(b'\x00\x00\x00\x00'))+ch(b'IEND',b''))"
KA="$(uuid)"
a1="$(curl -sS -w '\n%{http_code}' -X POST -u "talus:$DEV" -H "Idempotency-Key: $KA" -F "file=@/tmp/inv41-photo.png;type=image/png" "$API/layers/$LA/features/$F1/attachments")"
leg "upload with Idempotency-Key" 201 "$(printf '%s' "$a1" | tail -1)"
A1="$(printf '%s' "$a1" | sed '$d' | jget 'd["id"]')"
a2="$(curl -sS -w '\n%{http_code}' -X POST -u "talus:$DEV" -H "Idempotency-Key: $KA" -F "file=@/tmp/inv41-photo.png;type=image/png" "$API/layers/$LA/features/$F1/attachments")"
leg "replayed upload" 200 "$(printf '%s' "$a2" | tail -1)"
leg "replayed upload returns the SAME id" "$A1" "$(printf '%s' "$a2" | sed '$d' | jget 'd["id"]')"
n="$(curl -sS -u "talus:$DEV" "$API/layers/$LA/features/$F1/attachments" | jget 'len(d["attachments"])')"
leg "exactly one attachment" 1 "$n"

echo "inv41 leg 5 — the owner's rights bound the token (a viewer's device token cannot edit)"
GRP="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"name":"inv41 crew '"$RANDOM"'"}' "$API/groups" | jget 'd.get("groupId") or d.get("group_id","")')"
curl -sS -o /dev/null -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"userIds":["'"$TV_VIEWER_ID"'"]}' "$API/groups/$GRP/members"
curl -sS -o /dev/null -X PUT -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"organizationOnly":false,"groups":["'"$GRP"'"],"publicAccess":false}' "$API/web-maps/$M1/shares"
curl -sS -o /dev/null -X PUT -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"organizationOnly":false,"groups":["'"$GRP"'"],"publicAccess":false}' "$API/layers/$LA/shares"; sleep 0.3
VDEV="$(curl -sS -X POST -H "Authorization: Bearer $VIEWER" -H "$JSON" -d "$MB" "$MINT" | jget 'd["token"]')"
[ -n "$VDEV" ] && echo "  PASS viewer mint after share (201)" || { echo "  FAIL viewer mint after share"; fail=1; }
leg "viewer's device token POST LA (no edit tuple)" 403 "$(code -X POST -u "talus:$VDEV" -H "$JSON" -d "$NEW" "$API/layers/$LA/features")"

echo "inv41 leg 5b — the Esri write doors refuse a device token, inside its pack and outside it (once-over A4)"
# The devices page says the credential "can create and edit features on the layers in that pack and
# nothing else; it can never delete". The Esri edit doors have deletes and never looked at the pack's
# layer list, so a device token could add, change and DELETE on every layer its owner may edit. The
# ruling (F1) is the narrow one: a token that carries a layer list does not edit through these doors
# at all. The refusal is a PER-EDIT result at HTTP 200 (desktop clients show nothing else).
# Every edit below names OBJECTID 999999999, which no fixture layer holds: refused or not, nothing
# is written. DEV is still live here (leg 6 retires it); RW is leg 2's ordinary read_write token.
ESRI="$API/esri-auth"
DEVICE_SENTENCE="This is a Talus Field device credential."
# Payloads are built by ASSIGNMENT first: in bash 3.2 a quoted JSON literal inside $( ) in argument
# position loses its quoting (inv46 carries the same note).
ESRI_UPD='updates=[{"attributes":{"OBJECTID":999999999,"notes":"inv41 esri"}}]'
ESRI_DEL='objectIds=999999999'
# esri_verdict <result-array> <device|past-gate> <response-with-code>: prints "ok" or "no: <why>".
#   device    — HTTP 200, result[0] success:false, error.code 403, description BEGINS with the sentence
#   past-gate — HTTP 200, result[0] present, and NO result carries the sentence or the gate's 403
esri_verdict() {
  printf '%s' "$3" | python3 -c '
import json, sys
arr, mode, sentence = sys.argv[1], sys.argv[2], sys.argv[3]
raw = sys.stdin.read()
body, _, code = raw.rpartition("\n")
if code.strip() != "200":
    print("no: http " + code.strip() + ": " + body[:200]); sys.exit(0)
try:
    res = json.loads(body)[arr]
    first = res[0]
except Exception as exc:
    print("no: unreadable result (" + exc.__class__.__name__ + "): " + body[:200]); sys.exit(0)
err = first.get("error") or {}
desc = str(err.get("description") or "")
if mode == "device":
    ok = first.get("success") is False and err.get("code") == 403 and desc.startswith(sentence)
else:
    ok = sentence not in body and err.get("code") != 403
print("ok" if ok else "no: " + body[:200])
' "$1" "$2" "$DEVICE_SENTENCE"
}
esri_leg() { # <label> <result-array> <device|past-gate> <response-with-code>
  _v="$(esri_verdict "$2" "$3" "$4")"
  if [ "$_v" = "ok" ]; then echo "  PASS $1"; else echo "  FAIL $1 — $_v"; fail=1; fi
}
r="$(curl -sS -w '\n%{http_code}' -u "talus:$DEV" -X POST "$ESRI/rest/services/$LA/FeatureServer/0/applyEdits" --data-urlencode 'f=json' --data-urlencode "$ESRI_UPD")"
esri_leg "device token applyEdits on LA (in the pack) → per-edit refusal, the device sentence" updateResults device "$r"
r="$(curl -sS -w '\n%{http_code}' -u "talus:$DEV" -X POST "$ESRI/rest/services/$LB/FeatureServer/0/applyEdits" --data-urlencode 'f=json' --data-urlencode "$ESRI_UPD")"
esri_leg "device token applyEdits on LB (not in the pack) → per-edit refusal, the device sentence" updateResults device "$r"
r="$(curl -sS -w '\n%{http_code}' -u "talus:$DEV" -X POST "$ESRI/rest/services/$LA/FeatureServer/0/deleteFeatures" --data-urlencode 'f=json' --data-urlencode "$ESRI_DEL")"
esri_leg "device token deleteFeatures on LA → per-edit refusal, the device sentence" deleteResults device "$r"
# the attachment chute has no per-edit channel: its refusal is a plain 403 carrying the sentence
r="$(curl -sS -w '\n%{http_code}' -u "talus:$DEV" -X PUT --data-binary 'inv41' "$ESRI/layers/$LA/attachments/999999999/inv41.txt")"
case "$(printf '%s' "$r" | tail -1):$(printf '%s' "$r" | sed '$d')" in
  403:*"$DEVICE_SENTENCE"*) echo "  PASS device token on the attachment chute (403, the device sentence)" ;;
  2*) echo "  FAIL device token on the attachment chute — LEAKED ALLOW: $(printf '%s' "$r" | cut -c1-200)"; fail=1 ;;
  *)  echo "  FAIL device token on the attachment chute (expected 403 + the device sentence): $(printf '%s' "$r" | cut -c1-200)"; fail=1 ;;
esac
# Control: the SAME applyEdits with the owner's ordinary read_write token is not stopped at the gate —
# it reaches the edit and reports the missing object. So the refusal above belongs to the device
# credential, not to every access token, and desktop editing by "Read & edit" token still works.
r="$(curl -sS -w '\n%{http_code}' -u "talus:$RW" -X POST "$ESRI/rest/services/$LA/FeatureServer/0/applyEdits" --data-urlencode 'f=json' --data-urlencode "$ESRI_UPD")"
esri_leg "control: an ordinary read_write token, the same applyEdits on LA → past the gate, no device sentence" updateResults past-gate "$r"

echo "inv41 leg 6 — lifecycle: tokens are per DEVICE (f5), a renewal retires only what it replaces, DELETE-by-pack revokes all, never admin"
# a second device of the same owner mints for the same pack: the first device's token stays live
DEV2="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d "$MB" "$MINT" | jget 'd["token"]')"
leg "first device's token still writes after a second device minted" 201 "$(code -X POST -u "talus:$DEV" -H "$JSON" -d "$NEW" "$API/layers/$LA/features")"
leg "second device's token writes" 201 "$(code -X POST -u "talus:$DEV2" -H "$JSON" -d "$NEW" "$API/layers/$LA/features")"
# the first device RENEWS (replaces its own): the old one is retired by a short EXPIRY, not revoked
# (once-over 2026-11-15 T2) — a phone whose renewal reply was lost keeps working and renews again, and
# when the grace ends it hears token_expired (re-mint / sign in), never token_revoked (which wipes the
# pack). The second device's token is untouched.
resp="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"packId":"'"$P1"'","replaces":"'"$DEVID"'"}' "$MINT")"
DEV3="$(printf '%s' "$resp" | jget 'd["token"]')"
leg "renewal mints" 201 "$([ -n "$DEV3" ] && echo 201 || echo "$resp")"
leg "the replaced token still writes during its grace (a lost reply wipes nothing)" 201 "$(code -X POST -u "talus:$DEV" -H "$JSON" -d "$NEW" "$API/layers/$LA/features")"
grace="$(psqlw "SELECT (revoked_at IS NULL AND expires_at <= now() + interval '24 hours 1 minute' AND expires_at > now() + interval '23 hours')::text FROM platform.personal_access_tokens WHERE id = '$DEVID'")"
leg "the replaced token is not revoked and now expires within 24 hours" true "$grace"
psqlw "UPDATE platform.personal_access_tokens SET expires_at = now() - interval '1 minute' WHERE id = '$DEVID'" >/dev/null
leg "the replaced token is dead once its grace ends" 401 "$(code -X POST -u "talus:$DEV" -H "$JSON" -d "$NEW" "$API/layers/$LA/features")"
leg "…and says token_expired, which never wipes the pack" token_expired "$(hdr -X POST -u "talus:$DEV" -H "$JSON" -d "$NEW" "$API/layers/$LA/features")"
leg "the renewed token writes" 201 "$(code -X POST -u "talus:$DEV3" -H "$JSON" -d "$NEW" "$API/layers/$LA/features")"
leg "the other device's token is untouched" 201 "$(code -X POST -u "talus:$DEV2" -H "$JSON" -d "$NEW" "$API/layers/$LA/features")"
leg "replaces a token that is not the caller's for this pack → mint still 201, nothing revoked" 201 "$(code -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"packId":"'"$P1"'","replaces":"'"$(uuid)"'"}' "$MINT")"
leg "DELETE /me/field-tokens/{pack} (every device)" 204 "$(code -X DELETE -H "Authorization: Bearer $OWNER" "$MINT/$P1")"
leg "revoked token (device 2)" 401 "$(code -X POST -u "talus:$DEV2" -H "$JSON" -d "$NEW" "$API/layers/$LA/features")"
leg "revoked token (device 1, renewed)" 401 "$(code -X POST -u "talus:$DEV3" -H "$JSON" -d "$NEW" "$API/layers/$LA/features")"
if tv_mint admin 2>/dev/null; then
  ADMIN="$TV_ADMIN_TOKEN"
  # "Never admin" (decision-talus-field-f4.md §4.2): a device token resolves with is_admin=False, so the one
  # thing an admin may override with a Bearer — the owner's editing-off switch (admin-edit-all,
  # decision-editing-toggle.md F2) — must NOT be overridable with the admin's device token. The admin joins
  # leg 5's crew group (view + edit ACCESS on LA via the group; the role decides who may use it), mints, then
  # the owner switches editing off: Bearer admin write = 201 (the override exists HERE — the control), device
  # token write = 403 (the property). Until 2026-10-04 this sub-leg never ran: the admin had no share, so the
  # mint was refused (decision-paid-invariants-live.md §10).
  leg "add the admin to the crew group" 200 "$(code -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"userIds":["'"$TV_ADMIN_ID"'"]}' "$API/groups/$GRP/members")"
  sleep 0.3
  resp="$(curl -sS -w '\n%{http_code}' -X POST -H "Authorization: Bearer $ADMIN" -H "$JSON" -d "$MB" "$MINT")"
  leg "admin mints a device token for the shared pack" 201 "$(printf '%s' "$resp" | tail -1)"
  ADEV="$(printf '%s' "$resp" | sed '$d' | jget 'd["token"]')"
  if [ -n "$ADEV" ]; then
    leg "the owner switches editing OFF on LA" 200 "$(code -X PUT -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"editingEnabled":false}' "$API/layers/$LA/editing")"
    sleep 0.3
    leg "admin's BEARER write with editing off (the admin override — control)" 201 "$(code -X POST -H "Authorization: Bearer $ADMIN" -H "$JSON" -d "$NEW" "$API/layers/$LA/features")"
    leg "admin's DEVICE TOKEN write with editing off (never admin)" 403 "$(code -X POST -u "talus:$ADEV" -H "$JSON" -d "$NEW" "$API/layers/$LA/features")"
    leg "the owner switches editing back on" 200 "$(code -X PUT -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"editingEnabled":true}' "$API/layers/$LA/editing")"
    curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $ADMIN" "$MINT/$P1"
  else
    echo "  FAIL the admin could not mint despite viewing the map — the sub-leg cannot test what it exists for"; fail=1
  fi
else
  tv_env_leg "inv41 leg 6 admin device token" "no admin principal can be minted on this box"
fi

if tv_dev_leg "inv41 leg 7 (licence WITHOUT field)" "it swaps in a licence without field"; then
echo "inv41 leg 7 — licence WITHOUT field"
use without_field
leg "owner mint" 404 "$(code -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d "$MB" "$MINT")"
use with_field
fi

leg_cleanup   # the EXIT trap (tv_on_exit) runs tv_cleanup and re-runs this harmlessly
tv_partial_report
if [ "$fail" = 0 ]; then echo "inv41: PASS"; else echo "inv41: FAIL"; exit 1; fi
