#!/usr/bin/env bash
# ★ umask FIRST (security review 2026-09-09): the .env upsert below rewrites the file via shell
# redirection (`grep -v ... > "$ENV_FILE.tmp" && mv`). A redirect creates the temp at 0666 & ~umask,
# so under the default 022 the mv silently replaced a 0600 .env with a WORLD-READABLE one — handing
# every local account the postgres superuser password, the Keycloak admin password, the provisioner
# client secret and the Fernet key. This ran on every install. install.sh re-asserts mode + owner
# after this script for belt and braces; this line is the actual fix.
umask 077

# Layer 7b — load the OpenFGA store + authorization model.
#
# Re-runnable. NOT baked into docker compose (so it does not run on every `up`).
# Runs against the already-running `openfga` service. OpenFGA has no host port
# (internal-only), so every API call is made from INSIDE the compose network via
# the on-demand `openfga-cli` helper service (curl image), not from your Mac.
#
# What it does:
#   (a) finds-or-creates a store named `talus`
#   (b) writes the authorization model from db/openfga/model.json (== D2)
#   (c) records OPENFGA_STORE_ID and OPENFGA_MODEL_ID into ./.env (gitignored),
#       matching how every other service config/secret is stored in this repo.
#
# Prereqs: the stack is up and openfga is healthy. Bring it up with:
#   docker compose up -d openfga
# (that pulls in openfga-postgres + openfga-migrate via depends_on). Then run:
#   ./scripts/load-openfga-model.sh
#
# Requires `jq` on the host (already used by the other verify scripts).

set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE=".env"
OPENFGA="http://openfga:8080"      # service name on the compose network
MODEL_FILE="/work/model.json"      # db/openfga/model.json, bind-mounted into openfga-cli

command -v jq >/dev/null || { echo "FAIL: jq not found on host (needed to parse OpenFGA responses)"; exit 1; }

# Run a curl command inside the compose network via the openfga-cli helper.
# Stdout is the response body; compose's own chatter goes to stderr.
fga_curl() {
  docker compose run --rm -T openfga-cli -c "$1"
}

upsert_env() {
  local key="$1" val="$2"
  touch "$ENV_FILE"
  if grep -q "^${key}=" "$ENV_FILE"; then
    grep -v "^${key}=" "$ENV_FILE" > "${ENV_FILE}.tmp" && mv "${ENV_FILE}.tmp" "$ENV_FILE"
  fi
  printf '%s=%s\n' "$key" "$val" >> "$ENV_FILE"
}

echo "==> Finding-or-creating store 'talus'..."
# Reuse an existing store named talus if present (keeps the script idempotent;
# OpenFGA happily creates duplicate-named stores otherwise, leaving orphans).
stores_json=$(fga_curl "curl -sS ${OPENFGA}/stores")
store_id=$(printf '%s' "$stores_json" | jq -r '.stores[]? | select(.name=="talus") | .id' | head -n1)

if [[ -n "${store_id}" && "${store_id}" != "null" ]]; then
  echo "    reusing existing store: ${store_id}"
else
  create_json=$(fga_curl "curl -sS -X POST ${OPENFGA}/stores -H 'content-type: application/json' -d '{\"name\":\"talus\"}'")
  store_id=$(printf '%s' "$create_json" | jq -r '.id')
  if [[ -z "${store_id}" || "${store_id}" == "null" ]]; then
    echo "FAIL: could not create store. Response was:"; printf '%s\n' "$create_json"; exit 1
  fi
  echo "    created store: ${store_id}"
fi

echo "==> Writing authorization model from db/openfga/model.json..."
# Models are immutable/versioned in OpenFGA — each write yields a new model id;
# the latest is what the api will pin. Re-running simply adds a fresh version.
model_resp=$(fga_curl "curl -sS -X POST ${OPENFGA}/stores/${store_id}/authorization-models -H 'content-type: application/json' -d @${MODEL_FILE}")
model_id=$(printf '%s' "$model_resp" | jq -r '.authorization_model_id')
if [[ -z "${model_id}" || "${model_id}" == "null" ]]; then
  echo "FAIL: could not write model. Response was:"; printf '%s\n' "$model_resp"; exit 1
fi
echo "    wrote model: ${model_id}"

echo "==> Recording IDs into ${ENV_FILE}..."
upsert_env OPENFGA_STORE_ID "${store_id}"
upsert_env OPENFGA_MODEL_ID "${model_id}"

echo
echo "PASS — OpenFGA store + model loaded."
echo "  OPENFGA_STORE_ID=${store_id}"
echo "  OPENFGA_MODEL_ID=${model_id}"
echo
echo "Next: ./scripts/verify-openfga.sh"
