#!/usr/bin/env bash
# verify-clean-install.sh — the A2 negative check (decision-deployment-appliance.md §7):
# assert a CLEAN install carries no known dev secret and no dev login. Run on the fresh
# box in A5 / by install.sh; it MUST FAIL against a demo/dev stack (that failure is the
# proof the checks bite — a demo stack IS non-clean, by design).
#
# Read-only: file inspection + (if Keycloak is reachable) login ATTEMPTS that must be
# rejected. No state changes.
#
# Checks:
#   1. rendered realm has zero users, no talus-sa-demo client, no dev-convention secrets
#   2. .env contains no dev-convention (not-for-production) secret values
#   3. live Keycloak (when reachable) REJECTS formula-password dev logins
#
# Exit: 0 = clean; 1 = dev residue found. bash 3.2-safe.

set -u
cd "$(dirname "$0")/.."

FAILS=0
pass() { printf '  PASS  %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; FAILS=$((FAILS+1)); }

REALM_FILE="$(ls keycloak/import/*-realm.json 2>/dev/null | head -1)"   # rendered by render-realm.sh, named <realm>-realm.json
KC_PORT="$(grep -E '^KEYCLOAK_PORT=' .env 2>/dev/null | cut -d= -f2)"
KC="http://localhost:${KC_PORT:-8080}"

echo "== 1. rendered realm (${REALM_FILE}) =="
if [ ! -f "$REALM_FILE" ]; then
  fail "rendered realm not found — run scripts/render-realm.sh"
else
  users="$(python3 -c "import json;print(len(json.load(open('$REALM_FILE')).get('users',[])))")"
  if [ "$users" = "0" ]; then pass "realm carries zero users"; else fail "realm carries $users users (demo render?)"; fi
  # Parse, don't grep: a client-scope DESCRIPTION legitimately mentions talus-sa-demo in prose.
  sa_demo="$(python3 -c "import json;print(any(c['clientId']=='talus-sa-demo' for c in json.load(open('$REALM_FILE'))['clients']))")"
  if [ "$sa_demo" = "True" ]; then fail "talus-sa-demo client present (demo-only surface)"; else pass "no talus-sa-demo client"; fi
  if grep -q 'not-for-production' "$REALM_FILE"; then fail "dev-convention secret in rendered realm"; else pass "no dev-convention secret in realm"; fi
fi

echo "== 2. .env =="
if [ ! -f .env ]; then
  fail ".env not found"
elif grep -q 'not-for-production' .env; then
  fail "dev-convention secret value(s) in .env (generate-secrets.sh not used?)"
else
  pass "no dev-convention secrets in .env"
fi

echo "== 3. database carries no demo seeds (V088/V095) =="
# A clean install's catalog must be truly empty and its sharing picker free of demo teams
# (backlog 9b/9c; V095 finished the retirement — the built-ins cannot exist at all now).
# Registry rows may only be slots or Talus 38 views; the three teams are demo-seed surface.
# Skipped when postgres isn't up (file-only mode) — the checks above still bite.
if docker compose exec -T postgres pg_isready -q >/dev/null 2>&1; then
  nonslot="$(docker compose exec -T postgres psql -U talus -d talusgis -tAc \
    "SELECT count(*) FROM platform.layers WHERE layer_id NOT LIKE 'tal\_%' AND layer_id NOT LIKE 'vw\_%'" 2>/dev/null | tr -d '[:space:]')"
  demogroups="$(docker compose exec -T postgres psql -U talus -d talusgis -tAc \
    "SELECT count(*) FROM platform.groups WHERE group_id IN ('signs_team','street_lights_team','county_boundary_team')" 2>/dev/null | tr -d '[:space:]')"
  if [ "$nonslot" = "0" ]; then pass "no non-slot/non-view registry rows"; else fail "unexpected registry rows present ($nonslot — demo seed on a clean install?)"; fi
  if [ "$demogroups" = "0" ]; then pass "no demo groups"; else fail "demo groups present ($demogroups)"; fi
else
  echo "  SKIP  postgres not reachable — file checks only"
fi

echo "== 4. live Keycloak dev-login rejection =="
# The probe uses talus-api (the one client with directAccessGrants) + this box's OWN
# secret from .env — NOT talus-portal, whose password grants are disabled and would make
# the check vacuously pass. On a clean box the users don't exist -> rejected. On a demo
# stack the login SUCCEEDS -> this check fails, which is correct.
API_SECRET="$(grep -E '^TALUS_API_CLIENT_SECRET=' .env 2>/dev/null | cut -d= -f2-)"
if [ -z "$API_SECRET" ]; then
  fail "TALUS_API_CLIENT_SECRET missing from .env — cannot probe dev logins"
elif curl -sf -o /dev/null --max-time 5 "$KC/realms/talus" 2>/dev/null; then
  for cred in "both-editor:both-editor-pw" "validate-phase1:dev-validate-phase1-pw"; do
    u="${cred%%:*}"; p="${cred##*:}"
    body="$(curl -s --max-time 10 -X POST "$KC/realms/talus/protocol/openid-connect/token" \
      -d grant_type=password -d client_id=talus-api -d "client_secret=$API_SECRET" \
      -d "username=$u" -d "password=$p" 2>/dev/null)"
    if printf '%s' "$body" | grep -q '"access_token"'; then
      fail "dev login SUCCEEDED for $u (formula password accepted)"
    else
      pass "dev login rejected for $u"
    fi
  done
else
  echo "  SKIP  Keycloak not reachable at $KC — file checks only"
fi

echo
if [ "$FAILS" -eq 0 ]; then
  echo "CLEAN-INSTALL CHECK: PASS — no dev residue found."
  exit 0
else
  echo "CLEAN-INSTALL CHECK: FAIL — $FAILS finding(s). (Expected on a dev/demo stack.)"
  exit 1
fi
