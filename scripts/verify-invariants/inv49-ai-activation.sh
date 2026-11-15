#!/usr/bin/env bash
# Invariant (Talus+ AI activation codes, decision-ai-substrate.md §11) — what Talus emails a customer
# is a ONE-TIME code inside an activation file. The product trades it at the PINNED gateway for the
# real token; the code is never stored, logged or echoed; a file can never say where it is sent; and
# the core licence door stores only the licence member of an activation file.
#
# Legs (the local gateway; this script mints its own throwaway with `mint --activation`):
#   1. anon   POST /ai-settings/activate                      -> 401
#   2. viewer POST /ai-settings/activate                      -> 403
#   3. admin, body carrying a `gateway` member                -> 422  (A.1: the destination is config)
#   4. admin, a malformed code                                -> 422, and the body does not echo it
#   5. admin, a well-formed code the gateway never issued     -> 422 with the fixed sentence
#   6. admin, the real code                                   -> 200; tokenSet, status ok; the response
#                                                                carries neither the code nor a tai_ token
#   7. admin GET /ai/usage                                    -> 200  (an authenticated gateway call: proves
#                                                                the install id travels with the token)
#   8. the SAME code again, same install                      -> 200 and a NEW fingerprint (A9: a lost
#                                                                reply is retried, the secret rotates)
#   9. the api log since the start carries neither the code nor any tai_ token
#  10. core licence door, an activation file wrapping the live licence -> 409 on this file-managed dev
#      stack (the unwrap worked: without it the wrapper fails verification = 422), or 200 on an
#      upload-managed one, where the stored file must then hold no `tac_` and no `talus_activation`
#  11. core licence door, an activation file wrapping a TAMPERED licence -> 422
#  12. after `gateway-admin reissue-activation`, GET /ai/usage -> 404 (the old secret is dead; absent)
#
# Deny rule: any 2xx on legs 1–5 or 11 = FAIL; the code or a token in any body or log = FAIL.
# Modes (_paid.sh, decision-paid-invariants-live.md): DEV mints its own code on the local gateway and runs
# every leg. LIVE — a real box licensed for ai — runs legs 1–4 with a deliberately MALFORMED code, so even
# a regressed door stays on the box (the format check refuses it before any gateway call), and skips 5–12
# by name: they reach a gateway, mint and reissue codes, and rewrite the licence file. UNLICENSED SKIPs
# (inv35 checks the closed doors). bash 3.2-safe.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv49: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
fail=0
GW="${TALUS_AI_GATEWAY_REPO:-$ROOT/../talus-ai-gateway}"
# shellcheck disable=SC1091
. scripts/verify-invariants/_paid.sh
tv_paid_mode ai inv49
[ "$TV_PAID_MODE" = unlicensed ] && { echo "inv49: SKIP — ai is not licensed on this box (inv35 checks the closed doors)"; exit 3; }
if [ "$TV_PAID_MODE" = dev ]; then
  [ -x "$GW/scripts/gateway-admin" ] || { echo "inv49: SKIP — gateway repo not at $GW"; exit 3; }
fi

# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh
tv_mint admin || { echo "inv49: FAIL — no admin token"; exit 1; }
ADMIN="$TV_ADMIN_TOKEN"
tv_mint viewer || { echo "inv49: FAIL — no viewer token"; exit 1; }
VIEWER="$TV_VIEWER_TOKEN"
JSON='Content-Type: application/json'
code() { curl -sS -o /dev/null -w '%{http_code}' "$@"; }
leg() { if [ "$2" = "$3" ]; then echo "  PASS $1 ($3)"; else echo "  FAIL $1 (expected $2, got $3)"; fail=1; fi; }
ACT="$API/ai-settings/activate"

OUT=$(mktemp)
if [ "$TV_PAID_MODE" = dev ]; then
MINT=$("$GW/scripts/gateway-admin" mint --activation --customer "inv49 throwaway" --term-days 30 --included-usd 1 --ops helper.docs 2>/dev/null)
CODE=$(printf '%s\n' "$MINT" | grep -E '^tac_' | tail -1)
TID=$(printf '%s\n' "$MINT" | python3 -c "import sys,json,re; s=sys.stdin.read(); m=re.search(r'\{.*?\n\}', s, re.S); print(json.loads(m.group(0))['id'])" 2>/dev/null)
[ -n "$CODE" ] && [ -n "$TID" ] || { echo "inv49: FAIL — could not mint an activation code (is the gateway stack up, and on the build with activation codes?)"; exit 1; }
cleanup() { "$GW/scripts/gateway-admin" revoke --token-id "$TID" >/dev/null 2>&1; curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $ADMIN" "$API/ai-settings"; rm -f "$OUT"; tv_cleanup; }
trap cleanup EXIT
else
  # LIVE: no code is minted and nothing reaches a gateway. A malformed code (it fails the door's
  # format check) keeps legs 1–3 on the box even if the door they test has regressed.
  CODE='tac_live-probe-not-a-code'
  leg_cleanup() { rm -f "$OUT"; }   # tv_on_exit runs this, then removes the throwaway principals
fi
SINCE=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
BODY="{\"code\":\"$CODE\"}"

echo "inv49 legs 1–5 — who may knock, and what the door refuses"
leg "anon activate"   401 "$(code -X POST -H "$JSON" -d "$BODY" "$ACT")"
leg "viewer activate" 403 "$(code -X POST -H "Authorization: Bearer $VIEWER" -H "$JSON" -d "$BODY" "$ACT")"
GWBODY="{\"code\":\"$CODE\",\"gateway\":\"https://evil.example\"}"
C=$(curl -sS -o "$OUT" -w '%{http_code}' -X POST -H "Authorization: Bearer $ADMIN" -H "$JSON" -d "$GWBODY" "$ACT")
leg "a gateway member is refused" 422 "$C"
# The status alone proves nothing (an unknown or malformed code is a 422 too): it must be the schema
# refusing the member itself (pydantic extra_forbidden), before the door ever handles the code.
if grep -q '"extra_forbidden"' "$OUT" && grep -q '"gateway"' "$OUT"; then echo "  PASS the 422 is the schema refusing gateway (extra_forbidden)"
else echo "  FAIL the 422 does not name gateway as forbidden — the fence may be gone"; fail=1; fi
# Payloads are built in variables first: \" inside $(…) in argument position is the bash 3.2 trap.
BADBODY='{"code":"tac_not-a-real-code-MARKER49"}'
C=$(curl -sS -o "$OUT" -w '%{http_code}' -X POST -H "Authorization: Bearer $ADMIN" -H "$JSON" -d "$BADBODY" "$ACT")
leg "malformed code" 422 "$C"
if grep -q "MARKER49" "$OUT"; then echo "  FAIL the refusal echoes the submitted value"; fail=1; else echo "  PASS the refusal does not echo the submitted value"; fi
if tv_dev_leg "inv49 legs 5–12 (unknown code, redemption, rotation, log, licence door, re-issue)" "they reach a gateway, mint and reissue codes, and rewrite the licence file"; then
UNK="tac_$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")"
UNKBODY="{\"code\":\"$UNK\"}"
C=$(curl -sS -o "$OUT" -w '%{http_code}' -X POST -H "Authorization: Bearer $ADMIN" -H "$JSON" -d "$UNKBODY" "$ACT")
leg "unknown code" 422 "$C"
if grep -q "not a valid activation code" "$OUT" && ! grep -q "$UNK" "$OUT"; then echo "  PASS fixed sentence, no echo"; else echo "  FAIL unknown-code body"; fail=1; fi

echo "inv49 legs 6–8 — the redemption"
C=$(curl -sS -o "$OUT" -w '%{http_code}' -X POST -H "Authorization: Bearer $ADMIN" -H "$JSON" -d "$BODY" "$ACT")
leg "admin activate" 200 "$C"
FP1=$(python3 -c "import json;d=json.load(open('$OUT'));print(d.get('fingerprint') if d.get('tokenSet') and d.get('status')=='ok' else 'NOT-SET')" 2>/dev/null)
if [ -n "$FP1" ] && [ "$FP1" != "NOT-SET" ]; then echo "  PASS tokenSet, status ok, fingerprint …$FP1"; else echo "  FAIL the settings card after activation"; fail=1; fi
if grep -q "$CODE" "$OUT" || grep -Eq 'tai_[A-Za-z0-9_-]{8,}' "$OUT"; then echo "  FAIL the response carries the code or a token"; fail=1; else echo "  PASS the response carries neither the code nor a token"; fi
leg "usage through the bound token" 200 "$(code -H "Authorization: Bearer $ADMIN" "$API/ai/usage")"
C=$(curl -sS -o "$OUT" -w '%{http_code}' -X POST -H "Authorization: Bearer $ADMIN" -H "$JSON" -d "$BODY" "$ACT")
leg "the same code again from this install" 200 "$C"
FP2=$(python3 -c "import json;print(json.load(open('$OUT')).get('fingerprint'))" 2>/dev/null)
if [ -n "$FP2" ] && [ "$FP2" != "$FP1" ]; then echo "  PASS the secret rotated (…$FP1 → …$FP2)"; else echo "  FAIL the retry did not rotate the secret"; fail=1; fi
leg "usage after the rotation" 200 "$(code -H "Authorization: Bearer $ADMIN" "$API/ai/usage")"

echo "inv49 leg 9 — the log"
LOGS=$(docker compose logs --since "$SINCE" api 2>&1 < /dev/null)
if printf '%s' "$LOGS" | grep -q "$CODE"; then echo "  FAIL the activation code is in the api log"; fail=1; else echo "  PASS the code is not in the api log"; fi
if printf '%s' "$LOGS" | grep -Eq 'tai_[A-Za-z0-9_-]{20,}'; then echo "  FAIL a token is in the api log"; fail=1; else echo "  PASS no token in the api log"; fi
if printf '%s' "$LOGS" | grep -q "ai_activated"; then echo "  PASS the activation itself was logged (fingerprint only)"; else echo "  FAIL no ai_activated line — is the log check looking at the right container?"; fail=1; fi

echo "inv49 legs 10–11 — the core licence door takes only the licence member"
LIC=$(docker compose exec -T api python -c "
import json
from app.entitlement_runtime import effective_licence_path
print(json.dumps(json.load(open(effective_licence_path()))))" 2>/dev/null < /dev/null | tail -1)
if [ -z "$LIC" ]; then echo "  FAIL could not read the live licence"; fail=1; else
  WRAP="{\"talus_activation\":1,\"licence\":$LIC,\"ai\":{\"code\":\"$UNK\",\"expires\":\"2099-01-01\"}}"
  C=$(code -X PUT -H "Authorization: Bearer $ADMIN" -H "$JSON" -d "$WRAP" "$API/admin/entitlement/licence")
  if grep -q '^TALUS_LICENCE_PATH=.' .env 2>/dev/null; then
    leg "activation file at the licence door (file-managed: verified, then refused)" 409 "$C"
  else
    leg "activation file at the licence door" 200 "$C"
    if docker compose exec -T api sh -c 'grep -Eq "tac_|talus_activation" /storage/talus-licence/licence.json' < /dev/null; then
      echo "  FAIL the stored licence file holds the wrapper or the code"; fail=1; else echo "  PASS the stored file is the licence alone"; fi
  fi
  TAMPERED=$(printf '%s' "$LIC" | python3 -c "
import json, sys
d = json.load(sys.stdin); d['licence']['customer'] = 'Somebody Else'
print(json.dumps({'talus_activation': 1, 'licence': d, 'ai': {'code': 'x'}}))")
  leg "a tampered licence inside an activation file" 422 "$(code -X PUT -H "Authorization: Bearer $ADMIN" -H "$JSON" -d "$TAMPERED" "$API/admin/entitlement/licence")"
fi

echo "inv49 leg 12 — a re-issue kills the old secret"
"$GW/scripts/gateway-admin" reissue-activation --token-id "$TID" >/dev/null 2>&1 || { echo "  FAIL reissue-activation"; fail=1; }
leg "usage after the re-issue (absent)" 404 "$(code -H "Authorization: Bearer $ADMIN" "$API/ai/usage")"
fi   # legs 5–12

tv_partial_report
[ "$fail" = 0 ] && echo "inv49: PASS" || echo "inv49: FAIL"
exit $fail
