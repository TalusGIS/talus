#!/usr/bin/env bash
# sync-portal-client.sh — reconcile the talus-portal Keycloak client to the CURRENT
# PORTAL_ORIGIN (decision-install-reachability.md F1 — the F5 finding-#11 keystone).
#
# Why: the realm import bakes PORTAL_ORIGIN into the client's redirectUris/webOrigins at
# import time; renaming the portal later (talus-setup) rewrote .env but never the realm,
# so login died on `Invalid parameter: redirect_uri`. This script heals that on every
# install.sh --edge run: GET the FULL client representation, update the three origin
# fields, PUT the FULL representation back (never a partial PUT — Keycloak's PUT replaces
# the whole client, so a partial body would null what it omits).
#
# Credential posture: the master admin credential from ./.env, used against localhost —
# the exact pattern of create-initial-admin.sh and bootstrap-19c-provisioner.sh. No new
# secret, no new holder, nothing steerable by untrusted input.
#
# Idempotent; safe to re-run anytime. bash 3.2-safe.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] || { echo "FAIL: no .env here ($PWD)" >&2; exit 1; }

env_get() { sed -n "s/^$1=//p" .env | tail -1 | sed 's/^"//; s/"$//'; }

KC_PORT="$(env_get KEYCLOAK_PORT)"; KC_PORT="${KC_PORT:-8080}"
KC="http://localhost:${KC_PORT}"
KC_USER="$(env_get KEYCLOAK_ADMIN_USER)"; KC_USER="${KC_USER:-admin}"
KC_PW="$(env_get KEYCLOAK_ADMIN_PASSWORD)"
ORIGIN="$(env_get PORTAL_ORIGIN)"
[ -n "$KC_PW" ]  || { echo "FAIL: KEYCLOAK_ADMIN_PASSWORD missing from .env" >&2; exit 1; }
[ -n "$ORIGIN" ] || { echo "FAIL: PORTAL_ORIGIN missing from .env (edge wiring not run?)" >&2; exit 1; }

# Master token. Print the error body on failure (house rule) — a 401 here is almost
# always a rotated admin password.
TOK_BODY="$(curl -sS "${KC}/realms/master/protocol/openid-connect/token" \
  -d grant_type=password -d client_id=admin-cli \
  -d "username=${KC_USER}" -d "password=${KC_PW}")"
TOKEN="$(printf '%s' "$TOK_BODY" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("access_token",""))' 2>/dev/null || true)"
[ -n "$TOKEN" ] || { echo "FAIL: could not obtain admin token: ${TOK_BODY}" >&2; exit 1; }

CLIENT_JSON="$(curl -sS -H "Authorization: Bearer ${TOKEN}" \
  "${KC}/admin/realms/talus/clients?clientId=talus-portal")"

# JSON travels via the environment, not stdin — `python3 -` + a heredoc would consume
# stdin for the program text, leaving json.load(sys.stdin) an empty stream (bite-tested).
UPDATED="$(CLIENT_JSON="$CLIENT_JSON" ORIGIN="$ORIGIN" python3 -c '
import os, json, sys
origin = os.environ["ORIGIN"].rstrip("/")
clients = json.loads(os.environ["CLIENT_JSON"])
if not clients:
    sys.exit("no talus-portal client in realm talus")
c = clients[0]
c["redirectUris"] = [origin + "/*"]
c["webOrigins"] = [origin]
c.setdefault("attributes", {})["post.logout.redirect.uris"] = origin + "/*"
print(json.dumps({"id": c["id"], "body": c}))
')"

CID="$(UPDATED="$UPDATED" python3 -c 'import os,json;print(json.loads(os.environ["UPDATED"])["id"])')"
HTTP="$(UPDATED="$UPDATED" python3 -c 'import os,json;print(json.dumps(json.loads(os.environ["UPDATED"])["body"]))' | \
  curl -sS -o /tmp/sync-portal-client-err.$$ -w '%{http_code}' -X PUT \
    -H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json" \
    "${KC}/admin/realms/talus/clients/${CID}" --data @-)"
if [ "$HTTP" != "204" ]; then
  echo "FAIL: client update -> HTTP ${HTTP}: $(cat /tmp/sync-portal-client-err.$$)" >&2
  rm -f /tmp/sync-portal-client-err.$$
  exit 1
fi
rm -f /tmp/sync-portal-client-err.$$
echo "    talus-portal client -> ${ORIGIN} (redirect URIs, web origins, post-logout)"
