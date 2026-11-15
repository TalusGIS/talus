#!/usr/bin/env bash
# Invariant — the provisioner's Keycloak grants are LIVE (the Stage C finding-D5 tripwire,
# decision-clean-box-verification.md §5).
#
# D5 (2026-08-19): clean installs shipped Admin → Users 403-broken because the talus-provisioner
# service account's realm-management roles (manage-clients / manage-users / view-realm) were only
# ever hand-run — the realm import does not carry service-account role grants. install.sh now runs
# bootstrap-19c-provisioner.sh in the common path; THIS leg makes the grants-missing class unable
# to ship silently again:
#
#   [1] client-credentials mint as talus-provisioner (secret from .env) -> a token
#   [2] the exact Admin-API surface the portal's user-admin flow needs (list realm users) -> 200
#       (403 = the D5 class: client exists but its service-account grants are missing = RED)
#
# Cheap by design: two HTTP calls, no fixtures, nothing created, nothing to tear down.
# Needs the running stack + .env. bash 3.2-safe; -sS curl; jq-free.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv23: FAIL — cd"; exit 2; }
REALM="${KEYCLOAK_REALM:-talus}"
fail=0

env_val() { v="$(grep -E "^$1=" ./.env 2>/dev/null | head -1 | cut -d= -f2-)"; v="${v%\"}"; v="${v#\"}"; printf '%s' "$v"; }
KP="$(env_val KEYCLOAK_PORT)"; KP="${KP:-8080}"
KC="${KEYCLOAK_URL:-http://localhost:$KP}"
SECRET="$(env_val TALUS_PROVISIONER_CLIENT_SECRET)"
[ -n "$SECRET" ] || { echo "inv23: FAIL — TALUS_PROVISIONER_CLIENT_SECRET missing from ./.env (provisioner never provisioned = the D5 class)"; exit 1; }

# [1] the provisioner's own credential mints
PT=$(curl -sS -X POST "$KC/realms/$REALM/protocol/openid-connect/token" \
  -d grant_type=client_credentials -d client_id=talus-provisioner -d "client_secret=$SECRET" \
  | python3 -c "import sys,json;print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null)
if [ -n "$PT" ]; then
  echo "  [1] talus-provisioner client-credentials mint -> ok"
else
  echo "  [1] FAIL: talus-provisioner could not mint (client missing/disabled or secret mismatch — the D5 class)"
  echo "inv23: FAIL"; exit 1
fi

# [2] the grants bite: the same Admin-API read the portal's Admin -> Users flow depends on
c=$(curl -sS -o /dev/null -w '%{http_code}' \
  "$KC/admin/realms/$REALM/users?max=1&briefRepresentation=true" -H "Authorization: Bearer $PT")
echo "  [2] provisioner Admin-API user list -> $c (expect 200; 403 = role grants missing)"
case "$c" in
  200) : ;;
  403) echo "      FAIL: provisioner token REFUSED by the Admin API — service-account role grants are missing (D5 shipped again)"; fail=1 ;;
  401) echo "      FAIL: provisioner token NOT ACCEPTED by the Admin API — on Keycloak 26.8.2 and later this is the kc-admin-api client scope missing from talus-provisioner (scripts/bootstrap-19c-provisioner.sh assigns it; the installer runs that script)"; fail=1 ;;
  *)   echo "      FAIL: expected 200, got $c"; fail=1 ;;
esac

if [ "$fail" -eq 0 ]; then echo "inv23: PASS"; else echo "inv23: FAIL"; fi
exit "$fail"
