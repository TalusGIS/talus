#!/usr/bin/env bash
# Invariant — the Admin → System health endpoint is admin-only and leaks no connection detail
# (decision-system-health.md §5). It disCLOSES internal topology (service names, up/down, backup
# destination, migration head), so the gate IS the control:
#
#   [1] anonymous → 401 (never a probe run for an unauthenticated caller)
#   [2] authenticated NON-admin → 403 (role-gated, not just signed-in)
#   [3] admin → 200 with the probes array (positive control — [1]/[2] aren't a dead route)
#   [4] scrub: no "://" anywhere in probe error fields (no URLs/connection strings escape,
#       whatever state the stack is in)
#
# PRE-SLICE BASELINE (recorded 2026-07-23): [1] and [2] returned 404 (route absent) — WRONG codes
# for the contract — and [3]/[4] FAILED outright; recorded per the negative-test-first discipline.
#
# Needs the running stack + .env (throwaways self-provisioned via _principals.sh). bash 3.2-safe; jq-free.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv18: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
URL="$API/admin/system-health"
jget(){ python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }
fail=0

# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh

# [1] anonymous
rc=$(curl -sS -o /dev/null -w '%{http_code}' "$URL")
echo "  [1] anon -> $rc (expect 401)"
[ "$rc" = "401" ] || { echo "      FAIL: anon must be 401, got $rc"; fail=1; }

# [2] authenticated non-admin (throwaway viewer — no admin role)
tv_mint viewer || { echo "inv18: FAIL — no viewer token"; exit 1; }
rc=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TV_VIEWER_TOKEN" "$URL")
echo "  [2] non-admin -> $rc (expect 403)"
case "$rc" in
  403) : ;;
  2*) echo "      FAIL: non-admin can read system topology ($rc)"; fail=1 ;;
  *)  echo "      FAIL: expected 403, got $rc"; fail=1 ;;
esac

# [3] admin positive control + [4] scrub
tv_mint admin || { echo "inv18: FAIL — no admin token"; exit 1; }
ADM="$TV_ADMIN_TOKEN"
BODY="$(curl -sS -H "Authorization: Bearer $ADM" "$URL")"
OK="$(printf '%s' "$BODY" | jget '"ok" if isinstance(d.get("probes"), list) and len(d["probes"]) >= 6 else "no"')"
echo "  [3] admin -> probes present: ${OK:-no}"
[ "$OK" = "ok" ] || { echo "      FAIL: admin control ($(printf '%.200s' "$BODY"))"; fail=1; }
SCRUB="$(printf '%s' "$BODY" | jget '"leak" if any("://" in str(p.get("error","")) for p in d.get("probes",[])) else "clean"')"
echo "  [4] error-field scrub -> ${SCRUB:-unreadable} (expect clean)"
[ "$SCRUB" = "clean" ] || { echo "      FAIL: a probe error field carries a URL/connection string"; fail=1; }

if [ "$fail" = "0" ]; then echo "inv18: PASS"; else echo "inv18: FAIL"; exit 1; fi
