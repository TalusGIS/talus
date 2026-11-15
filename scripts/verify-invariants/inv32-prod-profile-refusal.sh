#!/usr/bin/env bash
# Invariant C8 (partial) — a staging/production deployment REFUSES TO START carrying a published
# placeholder secret, and a REAL install still boots.
#
# The threat (T9 / S-3): .env.example ships `dev-...-not-for-production` client secrets. Anyone who
# has read this repo knows them. A deployment running one is forgeable while serving traffic
# perfectly well, so the control is a startup refusal (api/app/config.py
# `_forbid_dev_posture_in_prod`) and PART A proves the sad path fails.
#
# ★★ PART B is the half this test was missing the first time, and it is the more important half.
# The original guard also refused on the `talus-dev` realm and a `localhost` issuer — and BOTH were
# what a correct install actually carried (the shipped realm WAS named talus-dev until the
# 2026-09-09 rename, decision-realm-rename.md; KEYCLOAK_REALM was set nowhere; generate-secrets.sh
# writes ENVIRONMENT=production and a localhost issuer). So the guard refused every clean install.
# The original positive control passed anyway, because it was built from a hand-picked ideal value
# (KEYCLOAK_REALM=talus) that nothing in the product set at the time.
#
# So PART B asserts the postures a REAL ./install.sh produces — both the plain and the --edge
# shapes — still boot. A positive control assembled from what you wish the input were is not a
# control at all; it must be assembled from what the installer actually writes.
#
# Runs Settings() in a THROWAWAY container off the same image (`docker compose run --rm --no-deps`).
# Never touches the running api, writes nothing, needs no stack: read-only and safe (Tier 1).
#
# bash 3.2-safe. Exit: 0 pass · 2 fail · 3 skip.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv32: FAIL — cd"; exit 2; }

command -v docker >/dev/null 2>&1 || { echo "inv32: SKIP — docker not available on this box"; exit 3; }
docker compose config --services 2>/dev/null | grep -qx api \
  || { echo "inv32: SKIP — no 'api' service in this compose project"; exit 3; }
docker image inspect "$(docker compose config --images api 2>/dev/null | head -1)" >/dev/null 2>&1 \
  || { echo "inv32: SKIP — api image not built yet (docker compose build api)"; exit 3; }

fail=0
checked=0

try_boot() { # <label> <expect: refuse|boot> <env pairs...>
  label="$1"; expect="$2"; shift 2
  args=""
  for kv in "$@"; do args="$args -e $kv"; done
  # shellcheck disable=SC2086
  out="$(docker compose run --rm --no-deps -T $args api \
          python -c 'from app.config import Settings; Settings(); print("BOOTED")' 2>&1)"
  rc=$?
  checked=$((checked + 1))
  case "$expect" in
    refuse)
      if [ $rc -eq 0 ] && printf '%s' "$out" | grep -q BOOTED; then
        echo "  FAIL  $label — BOOTED with a published placeholder secret"; fail=$((fail + 1))
      elif printf '%s' "$out" | grep -q "refusing to start"; then
        echo "  PASS  $label — refused at startup"
      else
        echo "  FAIL  $label — did not boot, but NOT via the C8 refusal (masked by another error?)"
        printf '%s\n' "$out" | tail -3 | sed 's/^/          /'; fail=$((fail + 1))
      fi ;;
    boot)
      if [ $rc -eq 0 ] && printf '%s' "$out" | grep -q BOOTED; then
        echo "  PASS  $label — boots"
      else
        echo "  FAIL  $label — a REAL INSTALL'S POSTURE WAS REFUSED. The guard would brick it."
        printf '%s\n' "$out" | tail -3 | sed 's/^/          /'; fail=$((fail + 1))
      fi ;;
  esac
}

# ★ GENERATED, never literal. Two reasons, and the second is the one that bit:
#   1. it is what generate-secrets.sh actually does (`rand_hex`), so the test's "real install"
#      case is faithful rather than a hand-typed lookalike;
#   2. a 32-hex literal in a tracked file is credential-SHAPED, and the gitleaks gate flags it as
#      a generic-api-key — correctly. A test fixture must not look like a leaked secret.
REAL_API="TALUS_API_CLIENT_SECRET=$(openssl rand -hex 16)"
REAL_PROV="TALUS_PROVISIONER_CLIENT_SECRET=$(openssl rand -hex 16)"
# ★ NOT overridden below: KEYCLOAK_REALM. Post-rename, generate-secrets.sh writes it explicitly and
# config.py defaults to the same `talus`, so the container sees what a real install sees either
# way. Injecting a value the installer does not write would be the original mistake.
PLAIN_ISSUER="KEYCLOAK_ISSUER=http://localhost:8080/realms/talus"   # generate-secrets.sh default
EDGE_ISSUER="KEYCLOAK_ISSUER=https://auth.example.gov/realms/talus" # what --edge rewrites it to

echo "--- PART A: a published placeholder secret must not boot in production ---"

try_boot "dev-known TALUS_API_CLIENT_SECRET" refuse \
  ENVIRONMENT=production "$PLAIN_ISSUER" \
  "TALUS_API_CLIENT_SECRET=dev-talus-api-secret-not-for-production" "$REAL_PROV"

try_boot "dev-known TALUS_PROVISIONER_CLIENT_SECRET" refuse \
  ENVIRONMENT=production "$PLAIN_ISSUER" \
  "$REAL_API" "TALUS_PROVISIONER_CLIENT_SECRET=dev-prov-secret-not-for-production"

echo "--- PART B: the postures a REAL ./install.sh writes must still boot ---"

try_boot "clean install (plain): ENVIRONMENT=production, realm unset, localhost issuer" boot \
  ENVIRONMENT=production "$PLAIN_ISSUER" "$REAL_API" "$REAL_PROV"

try_boot "clean install (--edge): ENVIRONMENT=production, realm unset, https issuer" boot \
  ENVIRONMENT=production "$EDGE_ISSUER" "$REAL_API" "$REAL_PROV"

try_boot "dev box: ENVIRONMENT=development with the dev placeholders" boot \
  ENVIRONMENT=development "$PLAIN_ISSUER" \
  "TALUS_API_CLIENT_SECRET=dev-talus-api-secret-not-for-production" \
  "TALUS_PROVISIONER_CLIENT_SECRET=dev-prov-secret-not-for-production"

echo
if [ "$fail" -eq 0 ]; then
  echo "inv32: PASS ($checked checks)"; exit 0
else
  echo "inv32: FAIL — $fail of $checked checks"; exit 2
fi
