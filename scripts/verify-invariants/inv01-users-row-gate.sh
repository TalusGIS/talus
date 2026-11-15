#!/usr/bin/env bash
# Invariant C1 — a valid Keycloak token ALONE grants nothing; a platform.users row is required for
# EVERY request (Talus 48: the row is the gate, provisioned explicitly — never auto-created on login).
#
# Fixture (via _principals.sh, decision-clean-box-verification.md): a throwaway KC user with NO
# platform.users row (`norow` shape), whose fully valid, correctly-audienced token must be DENIED.
# The positive control — a throwaway provisioned viewer hitting the SAME endpoint via the SAME
# client → 200 — proves the client/audience path is fine, so the no-row 401 is definitively the
# users-row gate, not an aud/JWT rejection. Self-provisioned from .env's KC admin credential; runs
# on clean boxes with zero demo fixtures; both throwaways are deleted (verified) on exit.
#
# Needs the running stack + .env beside the repo. bash 3.2-safe; -sS curl.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv01: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
ME="$API/me/layers"
fail=0

# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh

# the no-row fixture: KC user + valid token, NO platform.users row
tv_mint norow || { echo "inv01: FAIL — could not provision the no-row fixture"; exit 1; }

# the fixture is only valid if NO platform.users row exists for it
rows=$(docker compose exec -T postgres psql -U talus -d talusgis -tA < /dev/null \
  -c "SELECT count(*) FROM platform.users WHERE username='$TV_NOROW_USER';" | tr -d '[:space:]')
echo "  platform.users rows for $TV_NOROW_USER -> $rows (expect 0)"
[ "$rows" = "0" ] || { echo "      FAIL: fixture invalid — a row exists for the throwaway user"; fail=1; }

# [1] valid token, NO row -> DENIED
c1=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TV_NOROW_TOKEN" "$ME")
echo "  [1] no-row token   -> /me/layers -> $c1 (expect 401)"
case "$c1" in
  401|403) : ;;
  2*) echo "      FAIL: a valid token with NO platform.users row was GRANTED access — C1 violated"; fail=1 ;;
  *)  echo "      FAIL: expected 401, got $c1"; fail=1 ;;
esac

# [2] positive control: a provisioned throwaway (has a row), SAME endpoint + client -> 200
tv_mint viewer || { echo "inv01: FAIL — could not provision the viewer control"; exit 1; }
c2=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TV_VIEWER_TOKEN" "$ME")
echo "  [2] provisioned    -> /me/layers -> $c2 (expect 200 — proves client/audience is fine)"
[ "$c2" = "200" ] || { echo "      FAIL: a provisioned user was denied ($c2)"; fail=1; }

if [ "$fail" -eq 0 ]; then echo "inv01: PASS"; else echo "inv01: FAIL"; fi
exit "$fail"
