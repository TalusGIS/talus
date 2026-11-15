#!/usr/bin/env bash
# generate-secrets.sh — mint a complete .env for a CLEAN Talus install, every secret generated,
# zero hand-edits (decision-deployment-appliance.md §5.5; installer discipline 3 of 4).
#
# What it writes: every deployment secret (CSPRNG via openssl) + safe clean-install config
# defaults. What it does NOT write: OPENFGA_STORE_ID / OPENFGA_MODEL_ID (captured by
# load-openfga-model.sh at first bring-up — left blank here on purpose).
#
# Secrets are 48-char hex (192 bits) — hex on purpose: safe in .env values, in DSN/URL
# contexts (no '+', '/', '=' to percent-encode), and in JSON. The Fernet key is the one
# exception: its format is 32 url-safe-base64 bytes by definition.
#
# Refuses to overwrite an existing .env unless --force (which first copies it aside) — a
# deployment's .env is its restore master key (see dr-restore-runbook.md); clobbering one
# silently would be the worst kind of helpful.
#
# Dev boxes do NOT use this: dev keeps its known .env (values match the dev conventions).
# This is the customer/appliance path, wired into install.sh (A4).
#
# Usage:
#   ./scripts/generate-secrets.sh            # writes ./.env (fails if one exists)
#   ./scripts/generate-secrets.sh --force    # backs up existing .env, then writes fresh
#   ./scripts/generate-secrets.sh --top-up   # EXISTING .env: append missing generate-class
#                                            # keys; WARN (never generate) for keys with a
#                                            # stateful counterpart. See the manifest below.
#
# Ports: POSTGRES_PORT / API_PORT / KEYCLOAK_PORT honor pre-set
# environment variables (e.g. `API_PORT=8001 ./scripts/generate-secrets.sh`) so a second
# stack on one host — or a host where 5432 is taken — needs no hand-edit.
#
# bash 3.2-safe.

set -euo pipefail
cd "$(dirname "$0")/.."

command -v openssl >/dev/null || { echo "FAIL: openssl not found on host" >&2; exit 1; }

FORCE=0
TOPUP=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    --top-up) TOPUP=1 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

# --- the top-up manifest -------------------------------------------------------------
# Every REQUIRED .env key, with its top-up class (decision-phase1-packaging-closeout.md §3.4).
# A version that ADDS an .env key MUST add it here in the same commit, having answered:
# does this key have a stateful counterpart that already holds the OLD/derived value?
#   generate_hex    no counterpart — safe to mint into an existing .env (48-char hex)
#   generate_fernet no counterpart — safe to mint (Fernet-format key)
#   default:<val>   plain config with a safe default — appended verbatim, not a secret
#   warn            HAS a counterpart (a Postgres role minted at initdb, a Keycloak client
#                   imported with the realm, existing Fernet ciphertexts, …). Minting a new
#                   value would silently MISMATCH the counterpart — the V070/TALUS_APP_PASSWORD
#                   failure class. Print the remediation pointer; generate NOTHING.
# bash 3.2: plain "key:class" word list, no associative arrays.
TOPUP_MANIFEST="
POSTGRES_DB:default:talusgis
POSTGRES_USER:default:talus
POSTGRES_PASSWORD:warn
TALUS_APP_PASSWORD:warn
TALUS_TILES_PASSWORD:warn
POSTGRES_PORT:default:5432
API_PORT:default:8000
AUTH_MODE:default:keycloak
ENVIRONMENT:default:production
PUBLIC_ANON_ACCESS_ENABLED:default:false
KEYCLOAK_PORT:default:8080
KEYCLOAK_ADMIN_USER:default:admin
KEYCLOAK_ADMIN_PASSWORD:warn
KEYCLOAK_DB_NAME:default:keycloak
KEYCLOAK_DB_USER:default:keycloak
KEYCLOAK_DB_PASSWORD:warn
KEYCLOAK_AUDIENCE:default:talus-api
TALUS_API_CLIENT_SECRET:warn
TALUS_SA_DEMO_CLIENT_SECRET:warn
TALUS_PROVISIONER_CLIENT_SECRET:warn
OPENFGA_DB_NAME:default:openfga
OPENFGA_DB_USER:default:openfga
OPENFGA_DB_PASSWORD:warn
OPENFGA_API_URL:default:http://openfga:8080
MEDIA_BUCKET:default:talus-media
RASTER_BUCKET:default:talus-rasters
TALUS_ENCRYPTION_KEY:warn
"
# Deliberately NOT in the manifest: OPENFGA_STORE_ID/MODEL_ID (load-openfga-model.sh owns
# them), KEYCLOAK_ISSUER/JWKS_URL/PORTAL_ORIGIN and the edge keys (install.sh derives them
# per deployment shape), GEOCODERS (absent-by-default is the design).

# Remediation pointers printed for warn-class keys (one line per key family):
warn_hint() {
  case "$1" in
    TALUS_ENCRYPTION_KEY)
      echo "      existing Fernet ciphertexts (geocoder tokens, field-project PATs) need the ORIGINAL key;"
      echo "      a new key would silently orphan them. Recover the key from your .env copy / restore master." ;;
    POSTGRES_PASSWORD|TALUS_APP_PASSWORD|TALUS_TILES_PASSWORD)
      echo "      the Postgres role already exists with the original password (00-roles.sh at initdb)."
      echo "      Recover it, or ALTER ROLE in the DB and set the same value here. See dr-restore-runbook.md." ;;
    KEYCLOAK_ADMIN_PASSWORD|KEYCLOAK_DB_PASSWORD|*_CLIENT_SECRET)
      echo "      Keycloak already holds the original credential (imported realm / bootstrap)."
      echo "      Recover it, or reset it IN KEYCLOAK first and then set the same value here." ;;
    *)
      echo "      this key pairs with existing deployment state — recover the original value." ;;
  esac
}

if [ "$TOPUP" -eq 1 ]; then
  [ -f .env ] || { echo "FAIL: --top-up needs an existing ./.env (fresh installs: run without flags)." >&2; exit 1; }
  added=0; warned=0
  for entry in $TOPUP_MANIFEST; do
    key="${entry%%:*}"
    rest="${entry#*:}"; class="${rest%%:*}"
    grep -q "^${key}=" .env && continue
    case "$class" in
      generate_hex)    printf '%s=%s\n' "$key" "$(openssl rand -hex 24)" >> .env
                       echo "  ADDED   $key (generated)"; added=$((added+1)) ;;
      generate_fernet) printf '%s=%s\n' "$key" "$(openssl rand -base64 32 | tr '+/' '-_')" >> .env
                       echo "  ADDED   $key (generated)"; added=$((added+1)) ;;
      default)         printf '%s=%s\n' "$key" "${rest#*:}" >> .env
                       echo "  ADDED   $key=${rest#*:} (config default)"; added=$((added+1)) ;;
      warn)            echo "  MISSING $key — NOT generated (has a stateful counterpart):"
                       warn_hint "$key"; warned=$((warned+1)) ;;
    esac
  done
  echo "  top-up done: $added key(s) added, $warned warning(s)."
  [ "$warned" -gt 0 ] && echo "  ⚠ resolve the warning(s) above before relying on the affected features."
  exit 0
fi

if [ -f .env ]; then
  if [ "$FORCE" -ne 1 ]; then
    echo "FAIL: ./.env already exists. This script is for CLEAN installs." >&2
    echo "  An existing .env is a deployment's restore master key — not overwriting it." >&2
    echo "  If you really mean it: ./scripts/generate-secrets.sh --force  (backs it up first)" >&2
    exit 1
  fi
  ts="$(date +%Y%m%d-%H%M%S)"
  cp .env ".env.replaced-${ts}"
  chmod 600 ".env.replaced-${ts}"
  echo "  existing .env copied to .env.replaced-${ts} (mode 600)"
fi

rand_hex() { openssl rand -hex 24; }                                   # 48 chars, 192 bits
fernet_key() { openssl rand -base64 32 | tr '+/' '-_'; }               # 32B url-safe b64 (Fernet spec)

POSTGRES_PASSWORD="$(rand_hex)"
TALUS_APP_PASSWORD="$(rand_hex)"
TALUS_TILES_PASSWORD="$(rand_hex)"
KEYCLOAK_ADMIN_PASSWORD="$(rand_hex)"
KEYCLOAK_DB_PASSWORD="$(rand_hex)"
OPENFGA_DB_PASSWORD="$(rand_hex)"
TALUS_API_CLIENT_SECRET="$(rand_hex)"
TALUS_SA_DEMO_CLIENT_SECRET="$(rand_hex)"      # dev tree only; no install creates talus-sa-demo
TALUS_PROVISIONER_CLIENT_SECRET="$(rand_hex)"
TALUS_ENCRYPTION_KEY="$(fernet_key)"

umask 077
cat > .env <<EOF
# Talus GIS deployment .env — GENERATED $(date '+%Y-%m-%d %H:%M:%S') by scripts/generate-secrets.sh
# Every secret below is unique to THIS deployment. This file is the restore "master key"
# (see docs/dr-restore-runbook.md). It is not in a backup unless an administrator turns that on
# in the portal, and then only encrypted — keep a copy somewhere safe and separate.
# Reference for every knob: .env.example.

# --- core database ---
POSTGRES_DB=talusgis
POSTGRES_USER=talus
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
TALUS_APP_PASSWORD=${TALUS_APP_PASSWORD}
TALUS_TILES_PASSWORD=${TALUS_TILES_PASSWORD}
POSTGRES_PORT=${POSTGRES_PORT:-5432}

# --- api ---
API_PORT=${API_PORT:-8000}
AUTH_MODE=keycloak
ENVIRONMENT=production
PUBLIC_ANON_ACCESS_ENABLED=false

# --- keycloak ---
KEYCLOAK_PORT=${KEYCLOAK_PORT:-8080}
KEYCLOAK_ADMIN_USER=admin
KEYCLOAK_ADMIN_PASSWORD=${KEYCLOAK_ADMIN_PASSWORD}
KEYCLOAK_DB_NAME=keycloak
KEYCLOAK_DB_USER=keycloak
KEYCLOAK_DB_PASSWORD=${KEYCLOAK_DB_PASSWORD}
KEYCLOAK_REALM=talus
KEYCLOAK_ISSUER=http://localhost:8080/realms/talus
KEYCLOAK_AUDIENCE=talus-api
KEYCLOAK_JWKS_URL=http://keycloak:8080/realms/talus/protocol/openid-connect/certs

# --- keycloak clients (rendered into the realm by scripts/render-realm.sh) ---
TALUS_API_CLIENT_SECRET=${TALUS_API_CLIENT_SECRET}
TALUS_SA_DEMO_CLIENT_SECRET=${TALUS_SA_DEMO_CLIENT_SECRET}
TALUS_PROVISIONER_CLIENT_SECRET=${TALUS_PROVISIONER_CLIENT_SECRET}

# --- portal origin (rendered into the realm's redirect URIs; install.sh sets the real one) ---
PORTAL_ORIGIN=http://localhost:5173

# --- openfga (STORE/MODEL IDs are captured by scripts/load-openfga-model.sh — leave blank) ---
OPENFGA_DB_NAME=openfga
OPENFGA_DB_USER=openfga
OPENFGA_DB_PASSWORD=${OPENFGA_DB_PASSWORD}
OPENFGA_API_URL=http://openfga:8080
OPENFGA_STORE_ID=
OPENFGA_MODEL_ID=

# --- object storage (plain files on the storagedata volume; no server, no creds) ---
MEDIA_BUCKET=talus-media
RASTER_BUCKET=talus-rasters

# --- secrets-at-rest (Fernet; see api/app/secrets.py + dr-restore-runbook.md) ---
TALUS_ENCRYPTION_KEY=${TALUS_ENCRYPTION_KEY}

# --- geocoders: none are set here. The portal starts with one (the public OpenStreetMap
#     geocoder); add, change or remove them in-portal via Admin -> Geocoders ---
# (deliberately not written as GEOCODERS= — the api treats empty as none, but an absent
#  var is the cleaner default; see .env.example for the JSON shape if bootstrapping one)
EOF
chmod 600 .env

echo "  wrote ./.env (mode 600) — secrets generated, clean-install defaults set."
echo "  NOT set here (by design): OPENFGA_STORE_ID / OPENFGA_MODEL_ID (load-openfga-model.sh captures them)."
echo "  Next: ./scripts/render-realm.sh  (then the bring-up / install flow)"
