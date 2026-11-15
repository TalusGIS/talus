#!/usr/bin/env bash
# Invariant (Talus Field f7b g1, decision-talus-field-f7b.md §6 / fork A) — the geocode READ doors take
# the optional-auth principal like every other read door: a session Bearer OR a personal access token
# sent as Basic (a desktop PAT, or a pack's device token — the only credential a crew phone holds once
# its portal session lapses). Anonymous is STILL refused: a locator proxy is never a public surface.
# Legs:
#   1. anon → 401 on /geocode/locators, /suggest, /search (the invariant that must never move)
#   2. a session Bearer → 200 on all three (unchanged)
#   3. a read_only PAT as Basic → 200 on all three (a PAT reads as its owner; fork A: ANY PAT)
#   4. the PAT revoked (DELETE /me/tokens/{id}) → 401 + X-Talus-Auth-Code: token_revoked; with
#      `X-Talus-Client: field` the answer carries NO WWW-Authenticate (WebKit's sheet never appears)
#   5. an unknown PAT → 401 with no reason header (no existence signal)
# Needs a working dev realm (tv_mint); no licence, no pack — the seam is core.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv44: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
fail=0
# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh
tv_mint publisher || { echo "inv44: FAIL — no principal"; exit 1; }
OWNER="$TV_PUBLISHER_TOKEN"
JSON='Content-Type: application/json'
code() { curl -sS -o /dev/null -w '%{http_code}' "$@"; }
hdr() { curl -sS -o /dev/null -D - "$@" | tr -d '\r' | awk 'tolower($1)=="x-talus-auth-code:"{print $2; f=1} END{if(!f)print "-"}'; }
chal() { curl -sS -o /dev/null -D - "$@" | tr -d '\r' | awk 'tolower($1)=="www-authenticate:"{print $2; f=1} END{if(!f)print "-"}'; }
jget() { python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }
leg() { if [ "$2" = "$3" ]; then echo "  PASS $1 ($3)"; else echo "  FAIL $1 (expected $2, got $3)"; fail=1; fi; }

echo "inv44: the geocode doors take a session OR a PAT; anonymous stays out"
DOORS="locators suggest?q=main search?q=main"
for d in $DOORS; do leg "anon /geocode/$d" 401 "$(code "$API/geocode/$d")"; done
for d in $DOORS; do leg "session /geocode/$d" 200 "$(code -H "Authorization: Bearer $OWNER" "$API/geocode/$d")"; done

MINTED="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"label":"inv44 ro","scope":"read_only"}' "$API/me/tokens")"
RO="$(printf '%s' "$MINTED" | jget 'd["token"]')"
RO_ID="$(printf '%s' "$MINTED" | jget 'd["id"]')"
[ -n "$RO" ] && [ -n "$RO_ID" ] || { echo "inv44: FAIL — could not mint a PAT"; exit 1; }
for d in $DOORS; do leg "read_only PAT (Basic) /geocode/$d" 200 "$(code -u "talus:$RO" "$API/geocode/$d")"; done

leg "revoke the PAT" 204 "$(code -X DELETE -H "Authorization: Bearer $OWNER" "$API/me/tokens/$RO_ID")"
leg "revoked PAT → 401" 401 "$(code -u "talus:$RO" "$API/geocode/suggest?q=main")"
leg "revoked PAT → token_revoked" token_revoked "$(hdr -u "talus:$RO" "$API/geocode/suggest?q=main")"
leg "revoked PAT + X-Talus-Client: field → no Basic challenge" - "$(chal -u "talus:$RO" -H 'X-Talus-Client: field' "$API/geocode/suggest?q=main")"
leg "revoked PAT, desktop → Basic challenge kept" Basic "$(chal -u "talus:$RO" "$API/geocode/suggest?q=main")"
leg "unknown PAT → 401" 401 "$(code -u "talus:tpat_inv44unknownunknownunknownunknown" "$API/geocode/suggest?q=main")"
leg "unknown PAT → no reason header" - "$(hdr -u "talus:tpat_inv44unknownunknownunknownunknown" "$API/geocode/suggest?q=main")"

if [ "$fail" = 0 ]; then echo "inv44: PASS"; else echo "inv44: FAIL"; fi
exit "$fail"
