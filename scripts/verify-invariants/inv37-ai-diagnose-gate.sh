#!/usr/bin/env bash
# Invariant (Talus+ AI ai4, decision-ai-substrate.md §4) — diagnose mode sends operational data off
# the box, so: admin-only, server-assembled, previewed, redacted, and never stored.
#
# Legs (a valid AI token must be pasted; the local gateway on the stub backend):
#   1. viewer POST /ai/snapshot                 -> 403   (the preview is admin-only)
#   2. anon   POST /ai/snapshot                 -> 401
#   3. viewer POST /ai/helper {mode:diagnose}   -> 403   (admin-only; before any assembly)
#   4. admin  POST /ai/helper with a `snapshot` key in the body -> 422 (client snapshots are refused,
#             never merged — the server assembles from section NAMES only)
#   5. admin  POST /ai/snapshot -> 200 (5b: with request_id -> the `request` section, bound; a short code -> 422); names ⊆ the eight sections (failed_runs and request since 2026-10-13, items 2 and 4 of the diagnostics slice); bytes ≤ 32768; the body carries no
#             Talus AI token / PAT / Fernet / JWT shape (the redaction pass)
#   6. admin  POST /ai/helper {mode:diagnose, sections:[probes,environment]} -> 200 with provenance
#             (the token is this leg's own throwaway, minted on the local gateway — never a pasted one)
#             prompt_version helper-diagnose-v3 (v1→v2 2026-09-10; v2→v3 2026-10-13: the addendum names failed_runs and request, decision-ai-substrate.md); platform.ai_calls row has snapshot_hash (64 hex) and
#             snapshot_sections = {probes,environment}; the row holds no snapshot content
#   7. the api log since leg 5 carries no probe error strings from the snapshot body — a
#             cheap check that the snapshot is never logged
#
# Deny rule: any 2xx on leg 1–4 = FAIL; a secret shape in leg 5's body = FAIL. bash 3.2-safe.
# Modes (_paid.sh, decision-paid-invariants-live.md): DEV mints and pastes its own throwaway token on the
# local gateway and runs every leg. LIVE — a real box licensed for ai — runs legs 1–5 and 7 against the
# box's OWN activation (nothing pasted, nothing deleted) and skips leg 6 by name: a real diagnose call
# would spend the customer's allowance. A box whose AI is not activated yet SKIPs (ruling F2), and so does
# an unlicensed one (inv35 owns the closed doors).
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv37: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
fail=0
# SKIP = exit 3 (run.sh's contract; 2 reads as a FAIL). The token is this leg's OWN throwaway, minted on the local
# gateway like inv36's — inv36's leg 6 removes whatever was pasted, so a suite run can never rely on one.
GW="${TALUS_AI_GATEWAY_REPO:-$ROOT/../talus-ai-gateway}"
# shellcheck disable=SC1091
. scripts/verify-invariants/_paid.sh
tv_paid_mode ai inv37
[ "$TV_PAID_MODE" = unlicensed ] && { echo "inv37: SKIP — ai is not licensed on this box (inv35 checks the closed doors)"; exit 3; }
if [ "$TV_PAID_MODE" = dev ]; then
  [ -x "$GW/scripts/gateway-admin" ] || { echo "inv37: SKIP — gateway repo not at $GW"; exit 3; }
fi

# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh
tv_mint admin || { echo "inv37: FAIL — no admin token"; exit 1; }
ADMIN="$TV_ADMIN_TOKEN"
tv_mint viewer || { echo "inv37: FAIL — no viewer token"; exit 1; }
VIEWER="$TV_VIEWER_TOKEN"
JSON='Content-Type: application/json'
code() { curl -sS -o /dev/null -w '%{http_code}' "$@"; }
leg() { if [ "$2" = "$3" ]; then echo "  PASS $1 ($3)"; else echo "  FAIL $1 (expected $2, got $3)"; fail=1; fi; }

if [ "$TV_PAID_MODE" = dev ]; then
MINT=$("$GW/scripts/gateway-admin" mint --plain-token --customer "inv37 throwaway" --term-days 30 --included-usd 1 --ops helper.docs,helper.diagnose 2>/dev/null)
TOK=$(printf '%s\n' "$MINT" | grep -E '^tai_' | tail -1)
TID=$(printf '%s\n' "$MINT" | python3 -c "import sys,json,re; s=sys.stdin.read(); m=re.search(r'\{.*?\n\}', s, re.S); print(json.loads(m.group(0))['id'])" 2>/dev/null)
[ -n "$TOK" ] && [ -n "$TID" ] || { echo "inv37: FAIL — could not mint a gateway token (is the gateway stack up?)"; exit 1; }
cleanup() { "$GW/scripts/gateway-admin" revoke --token-id "$TID" >/dev/null 2>&1; curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $ADMIN" "$API/ai-settings"; tv_cleanup; }
trap cleanup EXIT
leg "paste the throwaway token" 200 "$(code -X PUT -H "Authorization: Bearer $ADMIN" -H "$JSON" -d "{\"token\":\"$TOK\"}" "$API/ai-settings")"
else
  # LIVE: the box's own activation is the fixture. Nothing is pasted, nothing is deleted — the default
  # tv_cleanup trap (installed by tv_mint) removes only the throwaway principals.
  ACT=$(curl -sS -H "Authorization: Bearer $ADMIN" "$API/ai-settings" | python3 -c "import sys,json;print('yes' if json.load(sys.stdin).get('tokenSet') else 'no')" 2>/dev/null)
  case "$ACT" in
    yes) echo "inv37: this box's AI is activated — testing against its own token" ;;
    no)  echo "inv37: SKIP — AI is not activated on this box yet (upload the activation file; ruling F2)"; exit 3 ;;
    *)   echo "inv37: FAIL — could not read /ai-settings as an admin"; exit 1 ;;
  esac
fi

echo "inv37 legs 1–4 — the gates"
leg "viewer POST /ai/snapshot"   403 "$(code -X POST -H "Authorization: Bearer $VIEWER" "$API/ai/snapshot")"
leg "anon POST /ai/snapshot"     401 "$(code -X POST "$API/ai/snapshot")"
leg "viewer diagnose"            403 "$(code -X POST -H "Authorization: Bearer $VIEWER" -H "$JSON" -d '{"mode":"diagnose","question":"why"}' "$API/ai/helper")"
# The 422 must be the schema refusing `snapshot` (extra_forbidden, before the handler) — a bare 422 could
# come from deeper in, after a real diagnose call had already left the box.
R4=$(mktemp)
C4=$(curl -sS -o "$R4" -w '%{http_code}' -X POST -H "Authorization: Bearer $ADMIN" -H "$JSON" -d '{"mode":"diagnose","question":"why","snapshot":{"probes":[]}}' "$API/ai/helper")
leg "client snapshot refused"    422 "$C4"
if grep -q '"extra_forbidden"' "$R4" && grep -q '"snapshot"' "$R4"; then echo "  PASS the 422 is the schema refusing snapshot (extra_forbidden)"
else echo "  FAIL the 422 does not name snapshot as forbidden: $(head -c 300 "$R4")"; fail=1; fi
rm -f "$R4"

echo "inv37 leg 5 — the preview"
SINCE=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
R5=$(mktemp); C=$(curl -sS -o "$R5" -w '%{http_code}' -X POST -H "Authorization: Bearer $ADMIN" "$API/ai/snapshot")
leg "admin POST /ai/snapshot" 200 "$C"
python3 - "$R5" <<'PY' || fail=1
import json, re, sys
d = json.load(open(sys.argv[1]))
allowed = {"probes", "operational", "recent_errors", "request", "failed_runs", "service_logs", "admin_audit", "environment", "licence"}
ok = True
if not set(d["names"]) <= allowed: print("  FAIL unknown section names", d["names"]); ok = False
# item 6 (2026-10-21): the other services' error logs — five fixed services, two of them "not available" by design,
# and never a visitor address or a Postgres statement in what the file-backed three carry
svcs = {e.get("service"): e for e in d["sections"].get("service_logs", []) if isinstance(e, dict)}
if set(svcs) != {"caddy", "postgres", "keycloak", "openfga", "pg_tileserv"}: print("  FAIL service_logs services:", sorted(svcs)); ok = False
for s in ("openfga", "pg_tileserv"):
    if s in svcs and not str(svcs[s].get("note", "")).startswith("not available"): print(f"  FAIL {s} should be not available"); ok = False
flat = "\n".join(l for e in svcs.values() for l in e.get("lines", []))
if re.search(r"\b(STATEMENT|QUERY):", flat) or re.search(r"remote_ip|client_ip", flat): print("  FAIL service_logs carries a statement or an address"); ok = False
if d["bytes"] > 32768: print("  FAIL snapshot exceeds 32 KiB:", d["bytes"]); ok = False
if len(d["hash"]) != 64: print("  FAIL hash shape"); ok = False
body = json.dumps(d["sections"])
for label, pat in (("Talus AI token", r"tai_[A-Za-z0-9_\-]{8,}"), ("PAT", r"tpat_[A-Za-z0-9_\-]{8,}"),
                   ("Fernet", r"gAAAA[A-Za-z0-9_\-=]{20,}"), ("JWT", r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.")):
    if re.search(pat, body): print(f"  FAIL {label} shape present in the preview"); ok = False
names = {p["name"] for p in d["sections"].get("probes", [])}
if "talus_ai" not in names or "postgres" not in names: print("  FAIL probes missing talus_ai/postgres:", names); ok = False
print("  PASS preview: sections", d["names"], "bytes", d["bytes"], "probes", sorted(names)) if ok else None
sys.exit(0 if ok else 1)
PY
# 5b (item 4, 2026-10-13): a pasted error code adds the `request` section — bound to THAT id, a list even
# when nothing matches; a code too short to be one is refused by the schema, never a wildcard.
R5B=$(mktemp); C=$(curl -sS -o "$R5B" -w '%{http_code}' -X POST -H "Authorization: Bearer $ADMIN" -H "$JSON" -d '{"request_id":"deadbeef00"}' "$API/ai/snapshot")
leg "admin POST /ai/snapshot with an error code" 200 "$C"
python3 - "$R5B" <<'PY' || fail=1
import json, sys
d = json.load(open(sys.argv[1]))
req = d["sections"].get("request")
ok = "request" in d["names"] and isinstance(req, dict) and req.get("request_id") == "deadbeef00" and isinstance(req.get("records"), list)
print("  PASS the request section is present and bound to the pasted code" if ok else f"  FAIL request section shape: {req}")
sys.exit(0 if ok else 1)
PY
leg "a 7-char code is refused (422)" 422 "$(code -X POST -H "Authorization: Bearer $ADMIN" -H "$JSON" -d '{"request_id":"abcdef1"}' "$API/ai/snapshot")"

if tv_dev_leg "inv37 leg 6 (diagnose end to end)" "a real diagnose call spends this box's AI allowance"; then
echo "inv37 leg 6 — diagnose end to end"
R6=$(mktemp); C=$(curl -sS -o "$R6" -w '%{http_code}' -X POST -H "Authorization: Bearer $ADMIN" -H "$JSON" \
  -d '{"mode":"diagnose","question":"Why is my service not working?","sections":["probes","environment"]}' "$API/ai/helper")
leg "admin diagnose" 200 "$C"
leg "prompt_version" helper-diagnose-v5 "$(python3 -c "import json;print(json.load(open('$R6'))['provenance']['prompt_version'])" 2>/dev/null)"
RID=$(python3 -c "import json;print(json.load(open('$R6'))['provenance']['request_id'])" 2>/dev/null)
ROW=$(docker compose exec -T postgres psql -U talus -d talusgis -tAc "SELECT length(snapshot_hash), array_to_string(snapshot_sections, ','), mode FROM platform.ai_calls WHERE request_id = '$RID'" < /dev/null)
leg "ai_calls row (hash len, sections, mode)" "64|probes,environment|diagnose" "$ROW"
COLS=$(docker compose exec -T postgres psql -U talus -d talusgis -tAc "SELECT count(*) FROM information_schema.columns WHERE table_schema='platform' AND table_name='ai_calls' AND column_name IN ('snapshot','snapshot_json','question','answer')" < /dev/null)
leg "no content column exists" 0 "$COLS"
fi

echo "inv37 leg 7 — the snapshot is not logged"
ERR=$(python3 -c "import json;d=json.load(open('$R5'));print(next((p.get('error') for p in d['sections'].get('probes',[]) if p.get('error')), 'no-probe-error-to-check'))")
if [ "$ERR" = "no-probe-error-to-check" ]; then echo "  PASS (vacuous: every probe up — no error string to look for)"; else
  if docker compose logs --since "$SINCE" api 2>&1 | grep -q -- "\"$ERR\""; then echo "  FAIL probe error string found in the api log"; fail=1; else echo "  PASS api log carries no snapshot strings"; fi; fi

rm -f "$R5" "${R6:-}"
tv_partial_report
[ "$fail" = 0 ] && echo "inv37: PASS" || echo "inv37: FAIL"
exit $fail
