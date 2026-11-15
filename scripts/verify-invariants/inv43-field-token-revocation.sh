#!/usr/bin/env bash
# Invariant (Talus Field f5, decision-talus-field-f5.md §4.1 / §6) — WHY a once-valid device token is
# refused rides the PAT path's 401 as `X-Talus-Auth-Code`, and ONLY for a once-valid token:
#   token_revoked  — revoked_at set, OR the owner was disabled / removed (the phone wipes the pack)
#   token_expired  — expires_at passed                             (the phone re-mints or asks for a sign-in)
#   (no header)    — an unknown or malformed credential            (no existence signal)
# Status, body and the Basic challenge never change. The same header rides every PAT door, read or
# write (the phone's probe uses the changes feed; the drain uses the write doors).
#
# Fixtures (throwaway publisher = OWNER): layer LA, a Field-ready map M1 over LA, a pack P1 built by
# the worker, device tokens minted through the f4 door (inv41's recipe). The expired and the
# disabled-owner legs flip the throwaway rows with psql and restore them before teardown.
# Legs (licence WITH field):
#   1. controls: the device token writes (201); an unknown PAT → 401 with NO reason header
#   2. the owner revokes the token in Profile → Access tokens (DELETE /me/tokens/{id}) → the write
#      door AND the changes feed answer 401 + token_revoked; the Basic challenge is unchanged
#   3. a fresh token, expires_at moved into the past → 401 + token_expired
#   4. a fresh token, the OWNER disabled (users.disabled_at) → 401 + token_revoked; owner re-enabled
#      → the same token writes again (the deny was the disable, nothing else)
#   5. (f5c) the admin doors: anon 401 / a publisher 403 on list and revoke; admin lists the live device
#      token and NOT a plain PAT; admin revoke → 204, again → 404, a plain PAT → 404; the phone hears
#      token_revoked; the revoke is audited — SKIPPED when no admin principal can be minted
#   6. (f5c) DELETE /me/field-tokens/self: anon 401, a Bearer 401, a plain PAT 403, the device token
#      204 and token_revoked on its next write
# Requires the dev entitlement mount + paid.field + V108 (SKIP otherwise).
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv43: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
ENT_DIR=".dev-entitlement"
fail=0
# shellcheck disable=SC1091
. scripts/verify-invariants/_paid.sh
tv_paid_mode field inv43
[ "$TV_PAID_MODE" = unlicensed ] && { echo "inv43: SKIP — field is not licensed on this box: revocation needs a built pack (inv38 and inv40 check the closed doors)"; exit 3; }
docker compose exec -T postgres psql -U talus -d talusgis -tA < /dev/null -c "SELECT 1 FROM information_schema.columns WHERE table_schema='platform' AND table_name='personal_access_tokens' AND column_name='layer_ids'" | grep -q 1 || { echo "inv43: SKIP — V108 not applied"; exit 3; }

# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh
tv_mint publisher || { echo "inv43: FAIL — no owner token"; exit 1; }
OWNER="$TV_PUBLISHER_TOKEN"
JSON='Content-Type: application/json'
code() { curl -sS -o /dev/null -w '%{http_code}' "$@"; }
# the reason header of a response, or "-" when absent
hdr() { curl -sS -o /dev/null -D - "$@" | tr -d '\r' | awk 'tolower($1)=="x-talus-auth-code:"{print $2; f=1} END{if(!f)print "-"}'; }
chal() { curl -sS -o /dev/null -D - "$@" | tr -d '\r' | awk 'tolower($1)=="www-authenticate:"{print $2; f=1} END{if(!f)print "-"}'; }
jget() { python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }
leg() { if [ "$2" = "$3" ]; then echo "  PASS $1 ($3)"; else echo "  FAIL $1 (expected $2, got $3)"; fail=1; fi; }
psqlw() { docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "$TV_PGUSER" -d "$TV_PGDB" -tA -c "$1" < /dev/null; }

if [ "$TV_PAID_MODE" = dev ]; then
docker compose exec -T api python - <<'PY' > "$ENT_DIR/.inv43.json" || { echo "inv43: FAIL — licence fixtures"; exit 1; }
import json
from datetime import date, timedelta
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from app.entitlement import LicenceDocument, sign_licence
t = date.today()
doc = LicenceDocument(licence_id="inv43-field", customer="INV43 THROWAWAY", sku="talus-plus", issued_at=t, term_start=t, term_end=t + timedelta(days=365), modules=["dashboards","story_maps","reports","ai","field"])
k = Ed25519PrivateKey.generate()
pub = k.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
print(json.dumps({"public_pem": pub, "with_field": sign_licence(doc, k).model_dump(mode="json")}))
PY
cp "$ENT_DIR/public.pem" "$ENT_DIR/.inv43.pub.bak" 2>/dev/null || true
cp "$ENT_DIR/licence.json" "$ENT_DIR/.inv43.lic.bak" 2>/dev/null || true
fi
M1=""; LA=""; P1=""; OWNER_SUB=""
leg_cleanup() {
  [ -n "$OWNER_SUB" ] && psqlw "UPDATE platform.users SET disabled_at = NULL WHERE id = '$OWNER_SUB' AND display_name = 'Talus Verify Throwaway'" >/dev/null 2>&1
  [ -n "$P1" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/field-packs/$P1"
  [ -n "$M1" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/web-maps/$M1"
  [ -n "$LA" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/layers/$LA"
  if [ "$TV_PAID_MODE" = dev ]; then
    [ -f "$ENT_DIR/.inv43.pub.bak" ] && mv "$ENT_DIR/.inv43.pub.bak" "$ENT_DIR/public.pem"
    [ -f "$ENT_DIR/.inv43.lic.bak" ] && mv "$ENT_DIR/.inv43.lic.bak" "$ENT_DIR/licence.json"
    rm -f "$ENT_DIR/.inv43.json"
  fi
}
if [ "$TV_PAID_MODE" = dev ]; then
python3 - "$ENT_DIR" <<'PY' || { echo "inv43: FAIL — fixture write"; exit 1; }
import json, sys
d = json.load(open(f"{sys.argv[1]}/.inv43.json"))
open(f"{sys.argv[1]}/public.pem", "w").write(d["public_pem"])
json.dump(d["with_field"], open(f"{sys.argv[1]}/licence.json", "w"))
PY
sleep 0.3
fi   # LIVE: the box's own licence grants field; nothing is written to it
LA="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" \
  -d '{"display_name":"inv43 pack layer","geom_type":"point","working_srid":4326,"native_storage":false,"editable":true}' \
  "$API/layers" | jget 'd.get("layer_id","")')"
case "$LA" in tal_*) : ;; *) echo "inv43: FAIL — layer create ('$LA')"; leg_cleanup; exit 1 ;; esac
curl -sS -o /dev/null -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"lon":-82.53,"lat":27.34}' "$API/layers/$LA/features"
M1="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" \
  -d '{"title":"inv43 field-ready","definition":{"version":1,"basemap":"light","operationalLayers":[{"layerId":"'"$LA"'","visible":true}],"initialView":{"center":[-82.53,27.34],"zoom":15},"field":{"enabled":true}}}' \
  "$API/web-maps" | jget 'd["id"]')"
[ -n "$M1" ] || { echo "inv43: FAIL — map create"; leg_cleanup; exit 1; }
if tv_field_no_basemap "$OWNER" "$M1"; then
  echo "inv43: SKIP — no basemap archive is installed on this box (Admin ▸ Basemap): the product refuses to build packs, and a device token needs one"
  leg_cleanup; exit 3
fi
P1="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"title":"Ward 43","bbox":[-82.6,27.3,-82.4,27.4],"basemapMaxzoom":14,"ttlDays":7}' "$API/web-maps/$M1/field-packs" | jget 'd.get("id","")')"
[ -n "$P1" ] || { echo "inv43: FAIL — pack create"; leg_cleanup; exit 1; }
for i in $(seq 1 12); do st="$(curl -sS -H "Authorization: Bearer $OWNER" "$API/web-maps/$M1/field-packs" | jget 'd["packs"][0]["status"]')"; [ "$st" = ready ] && break; [ "$st" = failed ] && break; sleep 3; done
[ "$st" = ready ] || { echo "inv43: FAIL — pack never built ($st)"; leg_cleanup; exit 1; }
echo "inv43: fixtures LA=$LA M1=$M1 P1=$P1"
MINT="$API/me/field-tokens"; MB='{"packId":"'"$P1"'"}'
mint() { curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d "$MB" "$MINT"; }
NEW='{"lon":-82.531,"lat":27.341}'
WRITE="$API/layers/$LA/features"
PROBE="$API/layers/$LA/changes?since=0&count=true"

echo "inv43 leg 1 — controls"
resp="$(mint)"; DEV="$(printf '%s' "$resp" | jget 'd["token"]')"; DEVID="$(printf '%s' "$resp" | jget 'd["tokenId"]')"
[ -n "$DEV" ] || { echo "  FAIL owner mint: $resp"; leg_cleanup; exit 1; }
OWNER_SUB="$(psqlw "SELECT owner_id FROM platform.personal_access_tokens WHERE id = '$DEVID'")"
leg "device token writes" 201 "$(code -X POST -u "talus:$DEV" -H "$JSON" -d "$NEW" "$WRITE")"
leg "device token reads the changes feed" 200 "$(code -u "talus:$DEV" "$PROBE")"
BOGUS="tal_pat_$(python3 -c "import secrets;print(secrets.token_hex(16))")"
leg "unknown PAT → 401" 401 "$(code -X POST -u "talus:$BOGUS" -H "$JSON" -d "$NEW" "$WRITE")"
leg "unknown PAT carries NO reason" "-" "$(hdr -X POST -u "talus:$BOGUS" -H "$JSON" -d "$NEW" "$WRITE")"
leg "malformed Basic carries NO reason" "-" "$(hdr -X POST -H "Authorization: Basic not-base64" -H "$JSON" -d "$NEW" "$WRITE")"

echo "inv43 leg 2 — revoked by its owner (Profile → Access tokens)"
leg "owner revokes" 204 "$(code -X DELETE -H "Authorization: Bearer $OWNER" "$API/me/tokens/$DEVID")"
leg "write door → 401" 401 "$(code -X POST -u "talus:$DEV" -H "$JSON" -d "$NEW" "$WRITE")"
leg "write door says token_revoked" token_revoked "$(hdr -X POST -u "talus:$DEV" -H "$JSON" -d "$NEW" "$WRITE")"
leg "changes feed says token_revoked (the probe path)" token_revoked "$(hdr -u "talus:$DEV" "$PROBE")"
leg "the Basic challenge is unchanged" Basic "$(chal -u "talus:$DEV" "$PROBE")"
leg "the field app opts out of the challenge (X-Talus-Client: field) — no native sign-in sheet" "-" "$(chal -u "talus:$DEV" -H "X-Talus-Client: field" "$PROBE")"
leg "and still hears token_revoked" token_revoked "$(hdr -u "talus:$DEV" -H "X-Talus-Client: field" "$PROBE")"

echo "inv43 leg 3 — expired"
resp="$(mint)"; DEV2="$(printf '%s' "$resp" | jget 'd["token"]')"; DEVID2="$(printf '%s' "$resp" | jget 'd["tokenId"]')"
[ -n "$DEV2" ] || { echo "  FAIL re-mint: $resp"; fail=1; }
leg "fresh token writes" 201 "$(code -X POST -u "talus:$DEV2" -H "$JSON" -d "$NEW" "$WRITE")"
psqlw "UPDATE platform.personal_access_tokens SET expires_at = now() - interval '1 minute' WHERE id = '$DEVID2'" >/dev/null
leg "expired → 401" 401 "$(code -X POST -u "talus:$DEV2" -H "$JSON" -d "$NEW" "$WRITE")"
leg "expired says token_expired" token_expired "$(hdr -X POST -u "talus:$DEV2" -H "$JSON" -d "$NEW" "$WRITE")"
leg "changes feed says token_expired" token_expired "$(hdr -u "talus:$DEV2" "$PROBE")"

echo "inv43 leg 4 — the owner disabled"
resp="$(mint)"; DEV3="$(printf '%s' "$resp" | jget 'd["token"]')"
[ -n "$DEV3" ] || { echo "  FAIL re-mint: $resp"; fail=1; }
leg "fresh token writes" 201 "$(code -X POST -u "talus:$DEV3" -H "$JSON" -d "$NEW" "$WRITE")"
psqlw "UPDATE platform.users SET disabled_at = now() WHERE id = '$OWNER_SUB' AND display_name = 'Talus Verify Throwaway'" >/dev/null
leg "disabled owner → 401" 401 "$(code -X POST -u "talus:$DEV3" -H "$JSON" -d "$NEW" "$WRITE")"
leg "disabled owner says token_revoked (not which)" token_revoked "$(hdr -X POST -u "talus:$DEV3" -H "$JSON" -d "$NEW" "$WRITE")"
leg "changes feed says token_revoked" token_revoked "$(hdr -u "talus:$DEV3" "$PROBE")"
psqlw "UPDATE platform.users SET disabled_at = NULL WHERE id = '$OWNER_SUB' AND display_name = 'Talus Verify Throwaway'" >/dev/null
leg "owner re-enabled → the same token writes again" 201 "$(code -X POST -u "talus:$DEV3" -H "$JSON" -d "$NEW" "$WRITE")"

echo "inv43 leg 5 — the admin's Field-devices doors (f5c)"
resp="$(mint)"; DEV4="$(printf '%s' "$resp" | jget 'd["token"]')"; DEVID4="$(printf '%s' "$resp" | jget 'd["tokenId"]')"
RO="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"label":"inv43 plain","scope":"read_write"}' "$API/me/tokens" | jget 'd["id"]')"
ALIST="$API/admin/users/$OWNER_SUB/field-tokens"
leg "anon list" 401 "$(code "$ALIST")"
leg "the owner (publisher, not admin) cannot list through the admin door" 403 "$(code -H "Authorization: Bearer $OWNER" "$ALIST")"
leg "anon revoke" 401 "$(code -X DELETE "$API/admin/field-tokens/$DEVID4")"
leg "a publisher cannot revoke through the admin door" 403 "$(code -X DELETE -H "Authorization: Bearer $OWNER" "$API/admin/field-tokens/$DEVID4")"
if tv_mint admin 2>/dev/null; then
  ADMIN="$TV_ADMIN_TOKEN"
  ids="$(curl -sS -H "Authorization: Bearer $ADMIN" "$ALIST" | jget '" ".join(t["id"] for t in d["tokens"])')"
  case " $ids " in *" $DEVID4 "*) echo "  PASS admin lists the live device token";; *) echo "  FAIL admin list lacks $DEVID4: $ids"; fail=1;; esac
  case " $ids " in *" $RO "*) echo "  FAIL admin list shows a PLAIN token"; fail=1;; *) echo "  PASS a plain PAT is not a device";; esac
  leg "admin cannot revoke a plain PAT through the field door" 404 "$(code -X DELETE -H "Authorization: Bearer $ADMIN" "$API/admin/field-tokens/$RO")"
  leg "admin revokes the device token" 204 "$(code -X DELETE -H "Authorization: Bearer $ADMIN" "$API/admin/field-tokens/$DEVID4")"
  leg "revoked again → 404 (already dead)" 404 "$(code -X DELETE -H "Authorization: Bearer $ADMIN" "$API/admin/field-tokens/$DEVID4")"
  leg "the phone hears token_revoked" token_revoked "$(hdr -X POST -u "talus:$DEV4" -H "$JSON" -d "$NEW" "$WRITE")"
  leg "the list no longer shows it" "" "$(curl -sS -H "Authorization: Bearer $ADMIN" "$ALIST" | jget '" ".join(t["id"] for t in d["tokens"] if t["id"]=="'"$DEVID4"'")')"
  n="$(psqlw "SELECT count(*) FROM platform.admin_audit WHERE action = 'field_token_revoked' AND target_id = '$DEVID4'")"
  leg "the revoke is audited" 1 "$n"
else
  tv_env_leg "inv43 leg 5 admin doors" "no admin principal can be minted on this box"
fi
echo "inv43 leg 6 — a device retires its own token (f5c: Remove / the expiry sweep, no session)"
resp="$(mint)"; DEV5="$(printf '%s' "$resp" | jget 'd["token"]')"
ROPLAIN="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"label":"inv43 plain2","scope":"read_write"}' "$API/me/tokens" | jget 'd["token"]')"
leg "anon" 401 "$(code -X DELETE "$API/me/field-tokens/self")"
leg "a Bearer session is not a device" 401 "$(code -X DELETE -H "Authorization: Bearer $OWNER" "$API/me/field-tokens/self")"
leg "a plain PAT is not a device" 403 "$(code -X DELETE -u "talus:$ROPLAIN" "$API/me/field-tokens/self")"
leg "the device token retires itself" 204 "$(code -X DELETE -u "talus:$DEV5" "$API/me/field-tokens/self")"
leg "and hears token_revoked next" token_revoked "$(hdr -X POST -u "talus:$DEV5" -H "$JSON" -d "$NEW" "$WRITE")"
leg "the plain PAT still reads" 200 "$(code -u "talus:$ROPLAIN" "$API/layers/$LA/features?limit=1")"
curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/me/tokens/$RO?purge=true"

leg_cleanup   # the EXIT trap (tv_on_exit) runs tv_cleanup and re-runs this harmlessly
tv_partial_report
if [ "$fail" = 0 ]; then echo "inv43: PASS"; else echo "inv43: FAIL"; exit 1; fi
