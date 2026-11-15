#!/usr/bin/env bash
# Talus 19 c4 — provision the server-side PROVISIONER identity (Keycloak Admin REST API),
# idempotently. Mirrors scripts/bootstrap-19-service-accounts.sh.
#
# WHY a script (not just realm-export.json): `start --import-realm` imports only on FIRST
# boot. Editing realm-export.json does NOT re-apply to a running Keycloak without wiping its
# DB (a `down -v`). This script applies the same config to the RUNNING instance via the admin
# API — additive, no realm re-import, safe to re-run.
#
# Ensures THREE things the c4 admin flow needs:
#   1. A confidential client `talus-provisioner` (serviceAccountsEnabled, client-credentials
#      only; secret = $TALUS_PROVISIONER_CLIENT_SECRET from .env). The portal API authenticates
#      AS this client to call the Keycloak Admin API.
#   2. A shared client scope `talus-api-audience` carrying an oidc-audience-mapper (aud=talus-api).
#      The provisioner attaches this to each minted SA client at create time (replacing the
#      hand-attached inline mapper). Defined here; NOT added to any realm default scope list, so
#      it never auto-applies to talus-api / talus-portal.
#   3. LEAST-PRIVILEGE realm-management client roles on the provisioner's service-account user:
#        manage-clients  (create / rotate-secret / enable-disable SA clients)
#        manage-users    (assign the talus-data-editor realm role to an SA's service-account user)
#        view-realm      (read the realm role object before assigning it)
#      NOT realm-admin — the provisioner can manage clients + users, nothing else.
#
# This is the Admin-API seam the future user-admin slice (Talus 20) reuses.
#
# Secrets (admin creds, the provisioner secret) come from ./.env — nothing hardcoded.
# Runs from the HOST (Keycloak publishes 8080). Run: ./scripts/bootstrap-19c-provisioner.sh

set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
. ./.env

command -v jq   >/dev/null || { echo "FAIL: jq not found on host"; exit 1; }
command -v curl >/dev/null || { echo "FAIL: curl not found on host"; exit 1; }

KC="${KC_ADMIN_URL:-http://localhost:8080}"
REALM="talus"
CLIENT_ID="talus-provisioner"
SECRET="${TALUS_PROVISIONER_CLIENT_SECRET:?missing from .env}"
SCOPE_NAME="talus-api-audience"
RM_ROLES="manage-clients manage-users view-realm"

# Confidential client representation. No protocol mappers: the Admin API authorizes on the
# realm-management roles in the token (resource_access), not a custom audience.
read -r -d '' CLIENT_JSON <<JSON || true
{
  "clientId": "$CLIENT_ID",
  "name": "Talus provisioner (Keycloak Admin API)",
  "description": "Talus 19 c4 server-side provisioning identity (client-credentials).",
  "enabled": true,
  "protocol": "openid-connect",
  "publicClient": false,
  "secret": "$SECRET",
  "standardFlowEnabled": false,
  "implicitFlowEnabled": false,
  "directAccessGrantsEnabled": false,
  "serviceAccountsEnabled": true,
  "redirectUris": [],
  "webOrigins": [],
  "attributes": { "access.token.lifespan": "300" }
}
JSON

# Shared audience client scope (created if absent). Inline mapper injects aud=talus-api.
read -r -d '' SCOPE_JSON <<JSON || true
{
  "name": "$SCOPE_NAME",
  "description": "Talus 19 c4 shared audience scope (aud=talus-api). Attached to minted SA clients.",
  "protocol": "openid-connect",
  "attributes": { "include.in.token.scope": "true", "display.on.consent.screen": "false" },
  "protocolMappers": [
    {
      "name": "$SCOPE_NAME",
      "protocol": "openid-connect",
      "protocolMapper": "oidc-audience-mapper",
      "consentRequired": false,
      "config": {
        "included.client.audience": "talus-api",
        "id.token.claim": "false",
        "access.token.claim": "true",
        "userinfo.token.claim": "false"
      }
    }
  ]
}
JSON

echo "==> Obtaining admin token from ${KC} (realm master, admin-cli)..."
TOKEN="$(curl -sf -X POST "$KC/realms/master/protocol/openid-connect/token" \
  -d grant_type=password -d client_id=admin-cli \
  -d "username=${KEYCLOAK_ADMIN_USER:-admin}" \
  -d "password=${KEYCLOAK_ADMIN_PASSWORD}" | jq -r .access_token)"
[[ -n "$TOKEN" && "$TOKEN" != "null" ]] || { echo "FAIL: could not get admin token (check KEYCLOAK_ADMIN_* in .env and that keycloak is up)"; exit 1; }
AUTH=(-H "Authorization: Bearer $TOKEN")

client_uuid() {
  curl -sf "${AUTH[@]}" "$KC/admin/realms/$REALM/clients?clientId=$CLIENT_ID" | jq -r '.[0].id // empty'
}

# --- 1. provisioner client -----------------------------------------------------
echo "==> Ensuring client ${CLIENT_ID} (serviceAccountsEnabled, confidential)..."
ID="$(client_uuid)"
if [[ -z "$ID" ]]; then
  # -sS (not -f): keep the body on a 4xx so the failure is visible.
  out="$(curl -sS -X POST "${AUTH[@]}" -H 'content-type: application/json' \
    "$KC/admin/realms/$REALM/clients" -w $'\n%{http_code}' -d "$CLIENT_JSON")"
  code="${out##*$'\n'}"; body="${out%$'\n'*}"
  [[ "$code" == 2* ]] || { echo "FAIL: create $CLIENT_ID -> HTTP $code"; echo "  body: $body"; exit 1; }
  ID="$(client_uuid)"
  [[ -n "$ID" ]] || { echo "FAIL: created $CLIENT_ID but could not read its id back"; exit 1; }
  echo "    client $CLIENT_ID — created ($ID)"
else
  curl -sf -X PUT "${AUTH[@]}" -H 'content-type: application/json' \
    "$KC/admin/realms/$REALM/clients/$ID" -d "$CLIENT_JSON"
  echo "    client $CLIENT_ID — exists ($ID), config ensured"
fi

# --- 2. shared audience client scope -------------------------------------------
echo "==> Ensuring client scope ${SCOPE_NAME} (oidc-audience-mapper aud=talus-api)..."
SCOPE_ID="$(curl -sf "${AUTH[@]}" "$KC/admin/realms/$REALM/client-scopes" \
  | jq -r --arg n "$SCOPE_NAME" '.[] | select(.name==$n) | .id' | head -1)"
if [[ -z "$SCOPE_ID" ]]; then
  out="$(curl -sS -X POST "${AUTH[@]}" -H 'content-type: application/json' \
    "$KC/admin/realms/$REALM/client-scopes" -w $'\n%{http_code}' -d "$SCOPE_JSON")"
  code="${out##*$'\n'}"; body="${out%$'\n'*}"
  [[ "$code" == 2* ]] || { echo "FAIL: create scope $SCOPE_NAME -> HTTP $code"; echo "  body: $body"; exit 1; }
  echo "    client scope $SCOPE_NAME — created"
else
  echo "    client scope $SCOPE_NAME — present ($SCOPE_ID)"
fi

# --- 3. least-privilege realm-management roles on the service account ----------
echo "==> Reading provisioner service-account user + realm-management client..."
PROV_SUB="$(curl -sf "${AUTH[@]}" "$KC/admin/realms/$REALM/clients/$ID/service-account-user" | jq -r '.id // empty')"
[[ -n "$PROV_SUB" ]] || { echo "FAIL: could not read service-account user id for $CLIENT_ID"; exit 1; }
RM_ID="$(curl -sf "${AUTH[@]}" "$KC/admin/realms/$REALM/clients?clientId=realm-management" | jq -r '.[0].id // empty')"
[[ -n "$RM_ID" ]] || { echo "FAIL: could not find the realm-management client"; exit 1; }

echo "==> Granting least-privilege roles to ${CLIENT_ID} service account: ${RM_ROLES}"
ROLES_JSON="$(for r in $RM_ROLES; do
  curl -sf "${AUTH[@]}" "$KC/admin/realms/$REALM/clients/$RM_ID/roles/$r"
done | jq -s '[ .[] | {id, name} ]')"
got="$(printf '%s' "$ROLES_JSON" | jq 'length')"
[[ "$got" == "3" ]] || { echo "FAIL: expected 3 realm-management roles, resolved $got (need: $RM_ROLES)"; exit 1; }
# POST is idempotent — re-mapping an already-assigned role is a no-op.
curl -sf -X POST "${AUTH[@]}" -H 'content-type: application/json' \
  "$KC/admin/realms/$REALM/users/$PROV_SUB/role-mappings/clients/$RM_ID" -d "$ROLES_JSON"
echo "    roles [$RM_ROLES] -> service-account user $PROV_SUB"

# --- 4. the Admin API's own client scope (Keycloak 26.8.2 and later) -------------
# From 26.8.2 the Admin API refuses (401) a token that does not carry the `kc-admin-api` client
# scope. Keycloak creates that scope in every realm, on upgrade and on a fresh import alike, and
# gives it to its own two clients only — so this client has to be given it here, or every user-
# administration call the portal makes fails. It adds no role: the three above still decide what the
# token may do. On an older Keycloak the scope does not exist and there is nothing to assign.
echo "==> Ensuring the Admin API client scope on ${CLIENT_ID}..."
ADMIN_SCOPE_ID="$(curl -sf "${AUTH[@]}" "$KC/admin/realms/$REALM/client-scopes" \
  | jq -r '.[] | select(.name=="kc-admin-api") | .id' | head -1)"
if [[ -n "$ADMIN_SCOPE_ID" ]]; then
  # PUT is idempotent — assigning a scope the client already has is a no-op (204).
  out="$(curl -sS -X PUT "${AUTH[@]}" -w $'\n%{http_code}' \
    "$KC/admin/realms/$REALM/clients/$ID/default-client-scopes/$ADMIN_SCOPE_ID")"
  code="${out##*$'\n'}"; body="${out%$'\n'*}"
  [[ "$code" == 2* ]] || { echo "FAIL: assign kc-admin-api to $CLIENT_ID -> HTTP $code"; echo "  body: $body"; exit 1; }
  echo "    client scope kc-admin-api -> $CLIENT_ID (default scope)"
else
  echo "    client scope kc-admin-api — not in this realm (a Keycloak older than 26.8.2): nothing to assign"
fi

echo
echo "PASS — provisioner ${CLIENT_ID} ready (client + secret + least-privilege roles + shared scope)."
echo "  service-account sub: ${PROV_SUB}"
echo "  The portal API uses TALUS_PROVISIONER_CLIENT_SECRET (.env) to mint a token for this"
echo "  client, then calls the Keycloak Admin API to create/rotate/disable SA clients (c4-2)."
