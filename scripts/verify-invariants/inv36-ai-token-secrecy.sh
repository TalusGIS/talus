#!/usr/bin/env bash
# Invariant (Talus+ AI, decision-ai-substrate.md §4) — the Talus AI token, once pasted, is never
# echoed by any door, never appears in the api log, and rests as Fernet ciphertext.
#
# Setup: a REAL token minted on the local gateway (the sibling repo's CLI, stub backend), pasted
# through PUT /ai-settings by the admin. Legs:
#   1. PUT → 200, tokenSet:true, fingerprint == last 4 chars, body does NOT contain the token
#   2. GET /ai-settings → body does not contain the token
#   3. POST /ai/helper as a viewer → 200 with an answer + provenance; body does not contain the token
#   4. platform.ai_settings.token_ct is NOT the plaintext (ciphertext, Fernet 'gAAAA' prefix)
#   5. the api container log since the PUT does not contain the token
#   6. DELETE /ai-settings → tokenSet:false; POST /ai/helper → 404 (absent = fail-closed)
#   -- revoke the token gateway-side, re-paste the OLD token --
#   7. PUT with a revoked token → 422 (never stored unvalidated)
#
# Deny rule: the token string in ANY response body or the log = FAIL. Requires the local gateway
# (TALUS_AI_GATEWAY_REPO, default ../talus-ai-gateway) up on the stub backend and
# TALUS_AI_GATEWAY_URL pointing at it from the api container. bash 3.2-safe; -sS curl.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv36: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
GW="${TALUS_AI_GATEWAY_REPO:-$ROOT/../talus-ai-gateway}"
fail=0

# SKIP = exit 3 (run.sh's contract; 2 reads as a FAIL). ★ Leg 6 removes the api's pasted token as part of the
# test and cleanup removes it again — a suite run leaves the dev stack WITHOUT its Admin → AI token (a plaintext
# nobody can restore); re-paste one afterwards. inv37 mints its own for the same reason.
# ★★ DEV RIG ONLY, as a whole (decision-paid-invariants-live.md §3): every leg pastes a throwaway token and
# the test DELETEs /ai-settings — on a customer's box that would erase their activation. Before 2026-10-04
# this held only by accident (the .env grep happened to miss on bundle installs); now it is the rule.
# shellcheck disable=SC1091
. scripts/verify-invariants/_paid.sh
tv_paid_mode ai inv36
[ "$TV_PAID_MODE" = dev ] || { echo "inv36: SKIP — dev rig only, by design: it pastes a token and DELETEs /ai-settings, which on this box would erase the AI activation"; exit 3; }
[ -x "$GW/scripts/gateway-admin" ] || { echo "inv36: SKIP — gateway repo not at $GW"; exit 3; }

# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh
tv_mint admin || { echo "inv36: FAIL — no admin token"; exit 1; }
ADMIN="$TV_ADMIN_TOKEN"
tv_mint viewer || { echo "inv36: FAIL — no viewer token"; exit 1; }
VIEWER="$TV_VIEWER_TOKEN"
JSON='Content-Type: application/json'
leg() { if [ "$2" = "$3" ]; then echo "  PASS $1 ($3)"; else echo "  FAIL $1 (expected $2, got $3)"; fail=1; fi; }
absent() { # <label> <haystack-file> — the token must NOT appear
  if grep -q -- "$TOK" "$2"; then echo "  FAIL $1 — token present"; fail=1; else echo "  PASS $1 — token absent"; fi
}

MINT=$("$GW/scripts/gateway-admin" mint --plain-token --customer "inv36 throwaway" --term-days 30 --included-usd 1 --ops helper.docs 2>/dev/null)
TOK=$(printf '%s\n' "$MINT" | grep -E '^tai_' | tail -1)
TID=$(printf '%s\n' "$MINT" | python3 -c "import sys,json,re; s=sys.stdin.read(); m=re.search(r'\{.*?\n\}', s, re.S); print(json.loads(m.group(0))['id'])" 2>/dev/null)
[ -n "$TOK" ] && [ -n "$TID" ] || { echo "inv36: FAIL — could not mint a gateway token"; exit 1; }
cleanup() { "$GW/scripts/gateway-admin" revoke --token-id "$TID" >/dev/null 2>&1; curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $ADMIN" "$API/ai-settings"; tv_cleanup; }
trap cleanup EXIT
SINCE=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

echo "inv36 leg 1 — paste"
R1=$(mktemp); C=$(curl -sS -o "$R1" -w '%{http_code}' -X PUT -H "Authorization: Bearer $ADMIN" -H "$JSON" -d "{\"token\":\"$TOK\"}" "$API/ai-settings")
leg "PUT /ai-settings" 200 "$C"
leg "tokenSet" true "$(python3 -c "import json;print(str(json.load(open('$R1'))['tokenSet']).lower())")"
leg "fingerprint" "${TOK#"${TOK%????}"}" "$(python3 -c "import json;print(json.load(open('$R1'))['fingerprint'])")"
absent "PUT body" "$R1"

echo "inv36 leg 2 — read-back"
R2=$(mktemp); curl -sS -o "$R2" -H "Authorization: Bearer $ADMIN" "$API/ai-settings"; absent "GET body" "$R2"

echo "inv36 leg 3 — a viewer asks"
R3=$(mktemp); C=$(curl -sS -o "$R3" -w '%{http_code}' -X POST -H "Authorization: Bearer $VIEWER" -H "$JSON" \
  -d '{"question":"How do I take a backup before upgrading?"}' "$API/ai/helper")
leg "POST /ai/helper" 200 "$C"
leg "provenance present" true "$(python3 -c "import json;d=json.load(open('$R3'));print(str('request_id' in d.get('provenance',{})).lower())")"
absent "helper body" "$R3"

echo "inv36 leg 4 — at rest"
CT=$(docker compose exec -T postgres psql -U talus -d talusgis -tAc "SELECT convert_from(token_ct,'UTF8') FROM platform.ai_settings WHERE id = true" < /dev/null)
case "$CT" in gAAAA*) echo "  PASS ciphertext is Fernet";; *) echo "  FAIL token_ct is not Fernet ciphertext"; fail=1;; esac
[ "$CT" = "$TOK" ] && { echo "  FAIL plaintext at rest"; fail=1; } || echo "  PASS plaintext not at rest"

echo "inv36 leg 5 — the api log"
R5=$(mktemp); docker compose logs --since "$SINCE" api > "$R5" 2>&1; absent "api log" "$R5"

echo "inv36 leg 6 — remove → absent"
C=$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE -H "Authorization: Bearer $ADMIN" "$API/ai-settings"); leg "DELETE" 200 "$C"
leg "helper after remove" 404 "$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $VIEWER" -H "$JSON" -d '{"question":"x"}' "$API/ai/helper")"

echo "inv36 leg 7 — a revoked token is never stored"
"$GW/scripts/gateway-admin" revoke --token-id "$TID" >/dev/null 2>&1
leg "PUT revoked" 422 "$(curl -sS -o /dev/null -w '%{http_code}' -X PUT -H "Authorization: Bearer $ADMIN" -H "$JSON" -d "{\"token\":\"$TOK\"}" "$API/ai-settings")"
leg "tokenSet stays false" false "$(curl -sS -H "Authorization: Bearer $ADMIN" "$API/ai-settings" | python3 -c "import sys,json;print(str(json.load(sys.stdin)['tokenSet']).lower())")"

rm -f "$R1" "$R2" "$R3" "$R5"
[ "$fail" = 0 ] && echo "inv36: PASS" || echo "inv36: FAIL"
exit $fail
