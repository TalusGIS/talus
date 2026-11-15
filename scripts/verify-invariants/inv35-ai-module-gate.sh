#!/usr/bin/env bash
# Invariant (Talus+ AI, decision-ai-substrate.md §4) — the AI module is gated on the signed licence's
# MODULE LIST, never on within_term, and every AI door is authenticated.
#
# The trap this guards (kickoff §0.4): the licensing model says "AI service = term", and a builder
# reading that beside entitlement.py's bolded rule reaches for `within_term` to gate the module.
# Wrong twice — the module gates like every other module (signed_and_valid + "ai" in modules); the
# SERVICE ends because the gateway token expires. So an EXPIRED-term licence that grants "ai" must
# still serve the doors, and a valid-term licence WITHOUT "ai" must 404 them.
#
# Legs (throwaway keypair + licences written to the dev-entitlement mount, restored on exit):
#   1. licence WITH ai, term valid:   anon GET /ai-settings -> 401 · forged alg:none -> 401
#                                     viewer GET /ai-settings -> 403 · admin GET -> 200
#                                     viewer GET /ai/status -> 200 (any signed-in user)
#                                     viewer GET /ai/usage -> 403 · anon -> 401 (ai3: admin-only)
#   2. licence WITHOUT ai, term valid: admin GET /ai-settings -> 404 · viewer GET /ai/status -> 404
#                                     (indistinguishable from the feature not existing)
#   3. licence WITH ai, term EXPIRED:  admin GET /ai-settings -> 200 (perpetual fallback holds;
#                                     within_term is NOT the gate) · viewer GET /ai/status -> 200
#   4. no licence at all:              admin GET /ai-settings -> 404
#   5. PUT /ai-settings with a `gatewayUrl` field -> 422 (the pinning fence: the endpoint is not
#                                     a request value — A.1 negative test (a))
#
# Deny rule: any 2xx on leg 2/4, or a 404 on leg 3 = FAIL.
# Modes (_paid.sh, decision-paid-invariants-live.md): DEV runs every leg (legs 2–4 swap throwaway
# licences on the dev entitlement mount). LIVE — a real box licensed for ai — runs legs 1 and 5 against
# the box's OWN licence and skips 2–4 by name. UNLICENSED (paid.ai installed, not licensed) runs leg 2's
# closed-door checks live. Not installed = SKIP. bash 3.2-safe; -sS curl.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv35: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
ENT_DIR=".dev-entitlement"
FORGED='eyJhbGciOiJub25lIn0.eyJzdWIiOiJoYWNrZXIifQ.'
fail=0

# shellcheck disable=SC1091
. scripts/verify-invariants/_paid.sh
tv_paid_mode ai inv35

# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh
tv_mint admin || { echo "inv35: FAIL — no admin token"; exit 1; }
ADMIN="$TV_ADMIN_TOKEN"
tv_mint viewer || { echo "inv35: FAIL — no viewer token"; exit 1; }
VIEWER="$TV_VIEWER_TOKEN"

code() { curl -sS -o /dev/null -w '%{http_code}' "$@"; }
leg() { # <label> <expected> <actual>
  if [ "$2" = "$3" ]; then echo "  PASS $1 ($3)"; else echo "  FAIL $1 (expected $2, got $3)"; fail=1; fi
}

if [ "$TV_PAID_MODE" = dev ]; then
# Fixtures: one throwaway keypair, three licences. Written via the api container (it has the
# entitlement module + cryptography); the public key goes where the dev override mounts it.
docker compose exec -T api python - <<'PY' > "$ENT_DIR/.inv35.json" || { echo "inv35: FAIL — fixtures"; exit 1; }
import json
from datetime import date, timedelta
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from app.entitlement import LicenceDocument, sign_licence
def doc(s, e, lid, mods):
    return LicenceDocument(licence_id=lid, customer="INV35 THROWAWAY", sku="talus-plus",
                           issued_at=date.today(), term_start=s, term_end=e, modules=mods)
k = Ed25519PrivateKey.generate()
pub = k.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
t = date.today(); ALL = ["dashboards", "story_maps", "reports", "ai"]; NOAI = ["dashboards", "story_maps", "reports"]
print(json.dumps({
    "public_pem": pub,
    "with_ai":    sign_licence(doc(t, t + timedelta(days=365), "inv35-ai", ALL), k).model_dump(mode="json"),
    "without_ai": sign_licence(doc(t, t + timedelta(days=365), "inv35-noai", NOAI), k).model_dump(mode="json"),
    "expired_ai": sign_licence(doc(t - timedelta(days=400), t - timedelta(days=35), "inv35-exp", ALL), k).model_dump(mode="json"),
}))
PY
# Preserve whatever was there; restore on exit (the other suites expect licence-valid.json to hold).
cp "$ENT_DIR/public.pem" "$ENT_DIR/.inv35.pub.bak" 2>/dev/null || true
cp "$ENT_DIR/licence.json" "$ENT_DIR/.inv35.lic.bak" 2>/dev/null || true
restore() {
  [ -f "$ENT_DIR/.inv35.pub.bak" ] && mv "$ENT_DIR/.inv35.pub.bak" "$ENT_DIR/public.pem"
  [ -f "$ENT_DIR/.inv35.lic.bak" ] && mv "$ENT_DIR/.inv35.lic.bak" "$ENT_DIR/licence.json"
  rm -f "$ENT_DIR/.inv35.json"
  tv_cleanup
}
trap restore EXIT
python3 - "$ENT_DIR" <<'PY' || { echo "inv35: FAIL — fixture write"; exit 1; }
import json, sys
d = json.load(open(f"{sys.argv[1]}/.inv35.json"))
open(f"{sys.argv[1]}/public.pem", "w").write(d["public_pem"])
for n in ("with_ai", "without_ai", "expired_ai"):
    json.dump(d[n], open(f"{sys.argv[1]}/licence-{n}.json", "w"))
PY
# Settle after every swap: the dev-entitlement dir is a Docker Desktop bind mount and a host-side
# cp can take a beat to be visible in the container (the paid verify suites sleep 0.2 for the same).
use() { cp "$ENT_DIR/licence-$1.json" "$ENT_DIR/licence.json"; sleep 0.3; }
else
  # LIVE / UNLICENSED: the box's own licence is the fixture. Nothing is written to it, ever.
  use() { :; }
fi

if [ "$TV_PAID_MODE" != unlicensed ]; then
echo "inv35 leg 1 — licence WITH ai, valid term"
use with_ai
leg "anon /ai-settings"          401 "$(code "$API/ai-settings")"
leg "forged /ai-settings"        401 "$(code -H "Authorization: Bearer $FORGED" "$API/ai-settings")"
leg "viewer /ai-settings"        403 "$(code -H "Authorization: Bearer $VIEWER" "$API/ai-settings")"
leg "admin /ai-settings"         200 "$(code -H "Authorization: Bearer $ADMIN" "$API/ai-settings")"
leg "viewer /ai/status"          200 "$(code -H "Authorization: Bearer $VIEWER" "$API/ai/status")"
leg "anon /ai/status"            401 "$(code "$API/ai/status")"
leg "viewer /ai/usage"           403 "$(code -H "Authorization: Bearer $VIEWER" "$API/ai/usage")"   # ai3: admin-only
leg "anon /ai/usage"             401 "$(code "$API/ai/usage")"
fi

if [ "$TV_PAID_MODE" = unlicensed ] || tv_dev_leg "inv35 leg 2 (licence WITHOUT ai)" "it swaps in a licence without ai"; then
echo "inv35 leg 2 — licence WITHOUT ai, valid term (or this box's own licence, UNLICENSED mode)"
use without_ai
leg "admin /ai-settings"         404 "$(code -H "Authorization: Bearer $ADMIN" "$API/ai-settings")"
leg "viewer /ai/status"          404 "$(code -H "Authorization: Bearer $VIEWER" "$API/ai/status")"
leg "viewer POST /ai/helper"     404 "$(code -X POST -H "Authorization: Bearer $VIEWER" -H 'Content-Type: application/json' -d '{"question":"x"}' "$API/ai/helper")"
leg "admin /ai/usage"            404 "$(code -H "Authorization: Bearer $ADMIN" "$API/ai/usage")"   # ai3: module gate
fi

if tv_dev_leg "inv35 leg 3 (EXPIRED term still serves)" "it swaps in an expired licence"; then
echo "inv35 leg 3 — licence WITH ai, EXPIRED term (perpetual fallback: still serves)"
use expired_ai
leg "admin /ai-settings"         200 "$(code -H "Authorization: Bearer $ADMIN" "$API/ai-settings")"
leg "viewer /ai/status"          200 "$(code -H "Authorization: Bearer $VIEWER" "$API/ai/status")"
fi

if tv_dev_leg "inv35 leg 4 (no licence)" "it removes the licence file"; then
echo "inv35 leg 4 — no licence"
rm -f "$ENT_DIR/licence.json"; sleep 0.3
leg "admin /ai-settings"         404 "$(code -H "Authorization: Bearer $ADMIN" "$API/ai-settings")"
fi

if [ "$TV_PAID_MODE" != unlicensed ]; then
echo "inv35 leg 5 — the pinning fence: gatewayUrl is not a request value"
use with_ai
# The status alone proves nothing: the body also carries a (fake) token, so with the fence gone the
# door would still answer 422 — token invalid — after first calling the gateway. The 422 must be the
# schema's own refusal of the field (pydantic extra_forbidden, raised before the handler runs).
R5=$(mktemp)
C5=$(curl -sS -o "$R5" -w '%{http_code}' -X PUT -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' \
  -d '{"token":"tai_abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJ","gatewayUrl":"https://evil.example.com"}' "$API/ai-settings")
leg "PUT gatewayUrl"             422 "$C5"
if grep -q '"extra_forbidden"' "$R5" && grep -q '"gatewayUrl"' "$R5"; then
  echo "  PASS the 422 is the schema refusing gatewayUrl (extra_forbidden)"
else
  echo "  FAIL the 422 does not name gatewayUrl as forbidden — the fence may be gone: $(head -c 300 "$R5")"; fail=1
fi
rm -f "$R5"
fi

tv_partial_report
[ "$fail" = 0 ] && echo "inv35: PASS" || echo "inv35: FAIL"
exit $fail
