#!/usr/bin/env bash
# preflight.sh — host-tool preflight for the Talus bring-up / install paths (A3,
# decision-deployment-appliance.md §5.6). Reports EVERY missing tool at once (not
# fail-on-first), so one pass tells the operator the full shopping list.
#
# Core checks (always): what the stack bring-up itself needs on the host.
# --demo adds:          what the demo seed chain additionally needs (loaders). That chain
#                       is DEV-TREE ONLY (decision-demo-seed-retirement.md) — the flag is
#                       here for the dev bring-up, and a Talus install never passes it.
#
# The appliance image bakes all of these; this script is for plain hosts (a dev Mac,
# a fresh Ubuntu box) where install.sh runs directly (and, in the dev tree, fresh-bringup.sh).
#
# Usage:  ./scripts/preflight.sh [--demo]
# Exit:   0 = all present · 1 = something missing (each named, with install hints)
# bash 3.2-safe.

set -u

DEMO=0
for arg in "$@"; do
  case "$arg" in
    --demo) DEMO=1 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

MISSING=0
need() {  # $1 = command, $2 = install hint
  if command -v "$1" >/dev/null 2>&1; then
    printf '  ok      %s\n' "$1"
  else
    printf '  MISSING %s — %s\n' "$1" "$2"
    MISSING=$((MISSING+1))
  fi
}

echo "== preflight: core =="
need docker  "https://docs.docker.com/engine/install/ (Docker Engine; Docker Desktop is NOT required)"
# compose v2 is a docker plugin, not a standalone binary — probe it directly.
if docker compose version >/dev/null 2>&1; then
  printf '  ok      docker compose\n'
else
  printf '  MISSING docker compose (v2 plugin) — apt install docker-compose-plugin / included in Docker Engine installs\n'
  MISSING=$((MISSING+1))
fi
need curl    "apt install curl / preinstalled on macOS"
need jq      "apt install jq / brew install jq"
need python3 "apt install python3 / preinstalled on macOS"
need openssl "apt install openssl / preinstalled on macOS"

# .env is loaded as shell by the install and realm scripts (`. ./.env` under `set -e`). A line the
# shell cannot read — most often an unquoted value with a space in it, which the shell takes as
# "set a variable, then run a command" — stopped those scripts partway, after the run had begun
# (once-over 2026-11-15 E3: the documented two-address KEYCLOAK_ADMIN_ALLOW_FROM did exactly that).
# Checked here, before anything changes, by loading it the same way in a throwaway shell.
if [ -f .env ]; then
  echo "== preflight: .env =="
  env_err="$("${BASH:-bash}" -c 'set -e; set -a; . ./.env' 2>&1 >/dev/null)"
  env_rc=$?
  if [ "$env_rc" -eq 0 ] && [ -z "$env_err" ]; then
    printf '  ok      .env loads\n'
  else
    printf '  FAIL    .env does not load as written:\n'
    printf '%s\n' "$env_err" | head -5 | sed 's/^/            /'
    printf '          A value that contains a space must be in double quotes, for example:\n'
    printf '            KEYCLOAK_ADMIN_ALLOW_FROM="203.0.113.7 198.51.100.0/24"\n'
    MISSING=$((MISSING+1))
  fi
fi

if [ "$DEMO" -eq 1 ]; then
  echo "== preflight: demo seed chain =="
  # The demo chain needs nothing beyond the base tools above: the hosted demo layers seed
  # talks HTTP through the product doors, tuples ride the in-network openfga-cli container,
  # and the group seed rides psql-in-container. (The psql/ogr2ogr/psycopg loaders retired
  # with the pilot built-ins — decision-retire-builtins.md phase 2.)
  printf '  ok      no additional host tools required\n'
fi

echo
if [ "$MISSING" -eq 0 ]; then
  echo "PREFLIGHT: PASS — all host tools present."
  exit 0
else
  echo "PREFLIGHT: FAIL — $MISSING problem(s) (listed above)."
  exit 1
fi
