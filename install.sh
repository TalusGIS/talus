#!/usr/bin/env bash
#
# install.sh — Talus GIS from zero to a working portal, zero hand-edits.
# (decision-deployment-appliance.md §5 / A4; the pre-alpha installer the appliance image
# will bake. Development bring-up lives in the dev repo, not in this artifact.)
#
# What a CLEAN install (the default, the customer shape) does:
#   preflight -> generate all secrets (if no .env) -> render a realm with ZERO users ->
#   core services -> Flyway migrate -> OpenFGA store+model (IDs captured into .env) ->
#   api (force-recreated so it reads those IDs) -> initial admin (generated password,
#   shown once) -> clean-install negative check (no dev secret, no dev login).
#
# Flags:
#   --edge   also start the hardened Caddy front (TLS, one origin): requires web/dist
#            (npm --prefix web run build) and uses TALUS_HOSTNAME / KEYCLOAK_HOSTNAME
#            (defaults talus.localhost / auth.localhost -> Caddy's internal CA; set real
#            hostnames + DNS for ACME certs). Sets PORTAL_ORIGIN / KEYCLOAK_ISSUER /
#            RATE_LIMIT_TRUST_FORWARDED_FOR in .env accordingly — the hand-edits an edge
#            deploy used to require.
#
# Re-runnable: an existing .env is KEPT (generate-secrets.sh refuses to clobber it),
# every downstream step is idempotent, and an existing admin's password is never rotated.
#
# Ordering rules baked in (proven in Layer 8a, kept ever since):
#   1. OpenFGA loader BEFORE the api (the api reads the IDs from .env at process start)
#   2. RECREATE the api, never restart (restart reuses the container's baked-in env)
#
# bash 3.2-safe.

set -euo pipefail
cd "$(dirname "$0")"

EDGE=0
TLS_INTERNAL=0
for arg in "$@"; do
  case "$arg" in
    # --demo was RETIRED (decision-demo-seed-retirement.md §2): demo seeding is a
    # development-tree feature that never shipped usefully — in a release artifact the
    # flag's seed chain aborted partway, leaving dev logins and no administrator. It is
    # answered rather than left "unknown" because a runbook or shell history carrying it
    # deserves the reason at the point of asking.
    --demo)
      echo "install.sh: --demo was retired — demo seeding is a development-tree feature." >&2
      echo "  A Talus install is clean by design: empty catalog, your data." >&2
      exit 2 ;;
    --edge) EDGE=1 ;;
    --tls-internal) TLS_INTERNAL=1 ;;
    *) echo "unknown arg: $arg (usage: ./install.sh [--edge [--tls-internal]])" >&2; exit 2 ;;
  esac
done

say() { printf '\n=== %s ===\n' "$1"; }
T_START=$SECONDS

# The exact command to re-run to resume this install (F5 re-run finding #2: every failure that
# tells you a cause must also tell you the way back). Reconstructed from the PARSED flags rather
# than "$*" — reconstruction cannot carry an unparsed argument forward into advice we print.
# Plain `if` blocks, not `[ x ] && y`: under `set -e` a false test at statement level exits.
RESUME_CMD="./install.sh"
if [ "$EDGE" -eq 1 ]; then RESUME_CMD="$RESUME_CMD --edge"; fi
if [ "$TLS_INTERNAL" -eq 1 ]; then RESUME_CMD="$RESUME_CMD --tls-internal"; fi
# When talus-setup is driving this installer, the way back is talus-setup — never this script by
# name. A guided first run handed us the administrator's chosen password in the environment; a
# bare re-run of install.sh would not have it, would mint the administrator with a GENERATED
# password, and would leave the guided setup marked unfinished. Two different instructions on one
# screen was the trap (found writing the self-install run sheet, 2026-10-05). A fixed literal,
# switched by a flag — nothing from the environment is echoed into advice.
DRIVEN_BY_SETUP=0
if [ "${TALUS_DRIVEN_BY_SETUP:-}" = "1" ]; then DRIVEN_BY_SETUP=1; RESUME_CMD="scripts/talus-setup.sh"; fi

# Portable .env upsert (no sed -i: BSD/GNU differ). Values here are URLs/hostnames/flags —
# no sed-special characters.
upsert_env() {
  key="$1"; val="$2"
  # .env is BOTH sourced by shell (`. ./.env`) and read by compose, so a value containing
  # whitespace must be quoted or the shell parses it as `VAR=word command…` (B5 edge
  # finding: TALUS_TLS_DIRECTIVE="tls internal" → "internal: command not found").
  # Compose strips the quotes; the shell keeps the value intact.
  case "$val" in
    *[[:space:]]*) val="\"$val\"" ;;
  esac
  touch .env
  if grep -q "^${key}=" .env; then
    tmp="$(mktemp)"
    sed "s|^${key}=.*|${key}=${val}|" .env > "$tmp" && mv "$tmp" .env
  else
    printf '%s=%s\n' "$key" "$val" >> .env
  fi
}

# --- preflight ---------------------------------------------------------------
say "Preflight: host tools"
./scripts/preflight.sh

if [ "$EDGE" -eq 1 ] && [ ! -f web/dist/index.html ]; then
  echo "FAIL: --edge serves the built SPA, but web/dist is missing." >&2
  echo "  Build it first:  npm --prefix web install && npm --prefix web run build" >&2
  exit 1
fi

# --- secrets -------------------------------------------------------------------
if [ -f .env ]; then
  say "Secrets: keeping existing .env + topping up keys this version added"
  # Upgrade path (decision-phase1-packaging-closeout.md §3.3/§3.4): generate-class keys are
  # appended, warn-class keys (stateful counterparts) are reported and NEVER minted.
  ./scripts/generate-secrets.sh --top-up
else
  say "Secrets: generating .env (secrets, clean-install defaults)"
  ./scripts/generate-secrets.sh
fi

# Stamp what this stack was installed with (the same-flags upgrade rule, §3.3): an upgrade
# re-run MUST use the same flags, or an --edge stack half-upgrades (Caddy up, wiring skipped).
MODE="clean"
[ "$EDGE" -eq 1 ] && MODE="${MODE},edge"
[ "$TLS_INTERNAL" -eq 1 ] && MODE="${MODE},tls-internal"
PREV_MODE="$(grep -E '^TALUS_INSTALL_MODE=' .env | cut -d= -f2- | tr -d '"' || true)"
case "$PREV_MODE" in
  # A stack stamped 'demo' predates the --demo retirement, and MODE can never be 'demo'
  # again — so the generic same-flags advice below would tell the operator to re-run with
  # a flag this installer now refuses. Never print a cause without a way back (F5 re-run
  # finding #2), so say the true thing instead. This population is real: the stamp lands
  # here, early, and the artifact's demo chain aborted much later (at verify-9).
  demo|demo,*)
    echo "  ⚠ this stack was installed with the retired --demo flag." >&2
    echo "    Demo seeding is a development-tree feature, and a demo stack cannot be" >&2
    echo "    upgraded into a clean one — its realm still carries dev logins, and the" >&2
    echo "    clean-install check below will say so. Install clean on a fresh box." >&2
    ;;
  *)
    if [ -n "$PREV_MODE" ] && [ "$PREV_MODE" != "$MODE" ]; then
      echo "  ⚠ install mode changed: this stack was installed with '${PREV_MODE}', this run is '${MODE}'." >&2
      echo "    Upgrades should re-run with the SAME flags (see docs/upgrade-runbook.md)." >&2
    fi
    ;;
esac
upsert_env TALUS_INSTALL_MODE "$MODE"

# --- realm coherence (decision-realm-rename.md §2; inv33-realm-consistency) ---------
# The realm is named in THREE .env keys. generate-secrets.sh writes them once, on a fresh
# .env, and --top-up never revisits them; the edge wiring below rewrites KEYCLOAK_ISSUER
# only. So a stack re-installed over an existing .env kept whatever realm that .env was
# born with — and across the talus-dev -> talus rename that is an api fetching signing
# keys from a realm that no longer exists: the stack comes up and nobody can log in.
# Retarget the realm SEGMENT only, keeping scheme/host/port (a plain install's localhost
# issuer and an edge install's public one both survive), and make KEYCLOAK_REALM explicit.
# The name comes from the shipped template — the one source of truth — not a literal here.
REALM_NAME="$(python3 -c 'import json;print(json.load(open("keycloak/realm-export.template.json"))["realm"])')"
[ -n "$REALM_NAME" ] || { echo "FAIL: keycloak/realm-export.template.json has no realm name" >&2; exit 1; }
say "Realm coherence: KEYCLOAK_REALM / KEYCLOAK_ISSUER / KEYCLOAK_JWKS_URL -> realm '${REALM_NAME}'"
retarget_realm() { # <KEY> <value-if-absent> — rewrite only the /realms/<name> segment of an existing value
  cur="$(grep -E "^$1=" .env | head -1 | cut -d= -f2- | tr -d '"' || true)"
  if [ -z "$cur" ]; then upsert_env "$1" "$2"; echo "  $1: absent, set to $2"; return 0; fi
  new="$(printf '%s' "$cur" | sed "s|/realms/[A-Za-z0-9_-]*|/realms/${REALM_NAME}|")"
  if [ "$new" != "$cur" ]; then upsert_env "$1" "$new"; echo "  $1: realm retargeted -> $new"; fi
  return 0
}
upsert_env KEYCLOAK_REALM "$REALM_NAME"
retarget_realm KEYCLOAK_ISSUER   "http://localhost:8080/realms/${REALM_NAME}"
retarget_realm KEYCLOAK_JWKS_URL "http://keycloak:8080/realms/${REALM_NAME}/protocol/openid-connect/certs"

# --- edge wiring (the hand-edits an edge deploy used to require) ---------------
if [ "$EDGE" -eq 1 ]; then
  say "Edge wiring: hostnames -> .env (PORTAL_ORIGIN, KEYCLOAK_ISSUER, forwarded-for trust)"
  # shellcheck disable=SC1091
  # Every grep here tolerates an absent line (|| true): a generated clean .env carries
  # none of these keys, and under `set -e` a bare failing grep kills the whole install
  # (edge finding #1, 2026-08-07 — A5 never exercised --edge, so this stayed latent).
  TALUS_HOSTNAME="${TALUS_HOSTNAME:-$(grep -E '^TALUS_HOSTNAME=' .env | cut -d= -f2- || true)}"
  KEYCLOAK_HOSTNAME="${KEYCLOAK_HOSTNAME:-$(grep -E '^KEYCLOAK_HOSTNAME=' .env | cut -d= -f2- || true)}"
  TALUS_HOSTNAME="${TALUS_HOSTNAME:-talus.localhost}"
  KEYCLOAK_HOSTNAME="${KEYCLOAK_HOSTNAME:-auth.localhost}"
  HTTPS_PORT_VAL="$(grep -E '^HTTPS_PORT=' .env | cut -d= -f2- || true)"
  HTTPS_PORT_VAL="${HTTPS_PORT_VAL:-443}"
  if [ "$HTTPS_PORT_VAL" = "443" ]; then PORT_SUFFIX=""; else PORT_SUFFIX=":${HTTPS_PORT_VAL}"; fi
  upsert_env TALUS_HOSTNAME "$TALUS_HOSTNAME"
  upsert_env KEYCLOAK_HOSTNAME "$KEYCLOAK_HOSTNAME"
  upsert_env PORTAL_ORIGIN "https://${TALUS_HOSTNAME}${PORT_SUFFIX}"
  upsert_env KEYCLOAK_ISSUER "https://${KEYCLOAK_HOSTNAME}${PORT_SUFFIX}/realms/talus"
  upsert_env CORS_ALLOW_ORIGINS "https://${TALUS_HOSTNAME}${PORT_SUFFIX}"
  upsert_env RATE_LIMIT_TRUST_FORWARDED_FOR "true"
  # Keycloak must advertise its PUBLIC url and trust the proxy's forwarded headers, or
  # OIDC discovery/redirects carry localhost and browser login fails.
  upsert_env KC_PUBLIC_URL "https://${KEYCLOAK_HOSTNAME}${PORT_SUFFIX}"
  upsert_env KC_PROXY_HEADERS "xforwarded"
  if [ "$TLS_INTERNAL" -eq 1 ]; then
    upsert_env TALUS_TLS_DIRECTIVE "tls internal"
    echo "  TLS: Caddy INTERNAL CA (LAN / air-gapped; browsers will warn until the CA is trusted)"
  else
    upsert_env TALUS_TLS_DIRECTIVE ""
    echo "  TLS: automatic (ACME for public names). Use --tls-internal when this host has no inbound internet."
  fi
  # IP mode's ONE global Caddy option rides in .env (2026-10-11): bare-IP clients send no SNI, and
  # fallback_sni routes them to the IP certificate (packaging/firstboot/Caddyfile.ip-mode explains).
  # Derived HERE — the one place the edge is wired — so an upgrade's installer re-run keeps it and the
  # IP-mode compose override needs no edit (the override is the operator's file; a preserved old
  # copy must keep working). A hostname gets nothing: Caddy's global block then holds only the CA name.
  case "$TALUS_HOSTNAME" in
    *[!0-9.]*) upsert_env TALUS_CADDY_GLOBAL "" ;;
    *) upsert_env TALUS_CADDY_GLOBAL "fallback_sni ${TALUS_HOSTNAME}" ;;
  esac
  # An IP-mode box carries docker-compose.override.yml (the R6 overlay: the 8443 door + the ip-mode
  # Caddyfile). The overlay imports the release Caddyfile and must match its shape, and an appliance's
  # copy mounts the BAKED overlay — so an upgrade re-run refreshes the file from this release's own
  # copy (2026-10-12). Only an overlay this project wrote is touched (it names Caddyfile.ip-mode); an
  # operator's own override is left alone.
  if [ -f docker-compose.override.yml ] && grep -q 'Caddyfile.ip-mode' docker-compose.override.yml \
     && [ -f packaging/firstboot/compose.ip-mode.override.yml ]; then
    tmp="$(mktemp)"
    if sed 's|/opt/talus-firstboot/Caddyfile.ip-mode|./packaging/firstboot/Caddyfile.ip-mode|' \
         packaging/firstboot/compose.ip-mode.override.yml > "$tmp" && mv "$tmp" docker-compose.override.yml; then
      echo "  IP-mode overlay: docker-compose.override.yml refreshed from this release"
    else
      rm -f "$tmp"
      echo "  ⚠ could not refresh the IP-mode overlay; run scripts/talus-setup.sh (press Enter) after this install" >&2
    fi
  fi
  echo "  portal: https://${TALUS_HOSTNAME}${PORT_SUFFIX}   auth: https://${KEYCLOAK_HOSTNAME}${PORT_SUFFIX}"

  # 9a runtime config: ONE generic SPA dist works on every deployment — the portal
  # fetches same-origin /config.json at boot (Caddy serves this file no-store).
  # apiBase is RELATIVE on purpose: fetches stay same-origin through the edge, so the
  # portal hostname appears nowhere in the SPA's config. Only the OIDC authority (a
  # genuinely different origin) is configured.
  mkdir -p proxy/config
  printf '{"apiBase":"/api/v1","oidcAuthority":"https://%s%s/realms/talus"}\n' \
    "$KEYCLOAK_HOSTNAME" "$PORT_SUFFIX" > proxy/config/config.json
  echo "  runtime config: proxy/config/config.json written (apiBase=/api/v1, authority=${KEYCLOAK_HOSTNAME})"
fi

# --- realm render ---------------------------------------------------------------
# Always a clean render: zero users, and PUBLIC_ANON_ACCESS_ENABLED keeps its safe
# default (false). The demo posture that turned the anon master switch ON went with --demo.
say "Render Keycloak realm (CLEAN: zero users)"
./scripts/render-realm.sh

# --- core services ---------------------------------------------------------------
say "Core services (postgres + roles-init, keycloak, openfga)"
# Storage needs no bring-up: media/rasters/backups are plain files on the storagedata
# volume, and the api creates the subtrees on first use (the make_bucket pattern,
# decision-object-store-build.md B3). The A5-defect-#5 class (a service missing from
# this list) can't recur for storage — there is no storage service to forget.
# Retired services: on an UPGRADE, containers from services this compose no longer
# defines must not linger (the beta.2->beta.4 rehearsal found the retired object-store
# server still running after install.sh alone). An EXPLICIT name list, deliberately not
# compose's --remove-orphans: orphan detection interacts with profiles and varies across
# compose versions (bite-tested 2026-08-23), while `docker rm -f <name>` is deterministic
# and a no-op on fresh installs. Extend this list when a service is retired.
for retired in talusgis-minio talusgis-minio-setup; do
  if docker ps -a --format '{{.Names}}' | grep -qx "$retired"; then
    say "Removing retired service container: $retired (superseded in v0.9.0-beta.3)"
    docker rm -f "$retired" >/dev/null
  fi
done
docker compose up -d postgres roles-init keycloak-postgres keycloak openfga-postgres openfga

say "Waiting for Keycloak realm import"
# shellcheck disable=SC1091
. ./.env
kc_ready=0
i=0
while [ "$i" -lt 90 ]; do
  if curl -sf "http://localhost:${KEYCLOAK_PORT:-8080}/realms/talus" >/dev/null 2>&1; then
    kc_ready=1; break
  fi
  i=$((i+1)); sleep 2
done
if [ "$kc_ready" -ne 1 ]; then
  echo "  Keycloak realm talus not reachable after ~180s — aborting." >&2
  exit 1
fi
echo "  realm talus is up."

# --- provisioner grants (D5, Stage C 2026-08-19) ----------------------------------
# The realm import ships the talus-provisioner CLIENT, but Keycloak imports do not carry
# service-account ROLE GRANTS — without them every server-side Admin-API call (Admin → Users,
# reset-password, service-account provisioning) 403s. The bootstrap is idempotent and applies
# to the RUNNING realm; it was historically run by hand, which is why clean installs shipped
# with Admin → Users half-broken until a clean-box fresh-install test caught it.
say "Provisioner grants (talus-provisioner realm-management roles)"
./scripts/bootstrap-19c-provisioner.sh

# --- user profile (decision-one-word-names.md) ------------------------------------
# Keycloak's default profile REQUIRES a last name; Talus keeps a name as one field. Without this
# an account named "Dispatch" is sent to a Keycloak profile page at its first sign-in. Runs on
# every install, so an upgrade repairs existing realms. A failure here costs that detour page and
# nothing else, so it warns and the install goes on (F2).
say "User profile (a last name is optional)"
if ! ./scripts/sync-user-profile.sh; then
  echo "  ⚠ could not make the last name optional in the sign-in system (see above)." >&2
  echo "    The install continues. Until it is fixed, an account with a one-word name is" >&2
  echo "    asked for a last name at its first sign-in. To retry:  ./scripts/sync-user-profile.sh" >&2
fi

# --- realm settings (2026-10-12) ----------------------------------------------------
# The realm imports on FIRST boot only; a later release that changes a realm-level setting
# (the 2026-10-11 ruling: session idle 30 min → 4 h for field crews) would never reach an
# upgraded box unless someone remembered the script. So the installer runs it: it PATCHes only
# the realm-level fields via the Admin API — users and clients untouched, user count asserted.
# A failure costs that setting and nothing else, so it warns and the install goes on.
say "Realm settings (session lifetimes, login theme, password policy)"
if ! ./scripts/apply-realm-settings.sh; then
  echo "  ⚠ could not apply the realm settings to the sign-in system (see above)." >&2
  echo "    The install continues with the realm's current settings. To retry:" >&2
  echo "      ./scripts/apply-realm-settings.sh" >&2
fi

# --- portal client sync (decision-install-reachability.md F1 — F5 finding #11) -----
# The realm import bakes PORTAL_ORIGIN into the talus-portal client at IMPORT time;
# a later rename (talus-setup re-runs this installer) would leave login pointing at a
# dead origin. Reconcile the client to the CURRENT origin on every edge run.
if [ "$EDGE" -eq 1 ]; then
  say "Portal client sync (redirect URIs -> current PORTAL_ORIGIN)"
  ./scripts/sync-portal-client.sh
fi

# --- schema -----------------------------------------------------------------------

# Fork-B pre-flight (decision-retire-builtins.md): refuse BEFORE Flyway if the pilot
# built-ins still hold features — V095 drops their tables and its RAISE is the fence that
# cannot be bypassed; this check is the friendly refusal that keeps the one-command
# upgrade from dying mid-migration. Fresh installs pass (the tables don't exist until
# Flyway creates them, and V095 then drops them empty); used-then-emptied boxes pass.
say "Pre-flight: pilot built-ins must hold no data (V095 retirement)"
preflight_out="$(docker compose exec -T postgres psql -U talus -d talusgis -v ON_ERROR_STOP=1 -q 2>&1 <<'SQL'
DO $$
DECLARE
    t text; n bigint; total bigint := 0;
BEGIN
    FOREACH t IN ARRAY ARRAY['assets.signs','assets.street_lights','assets.county_boundary'] LOOP
        IF to_regclass(t) IS NOT NULL THEN
            EXECUTE format('SELECT count(*) FROM %s', t) INTO n;
            total := total + n;
        END IF;
    END LOOP;
    IF total > 0 THEN
        RAISE EXCEPTION 'the pilot built-ins still hold % feature row(s)', total;
    END IF;
END $$;
SQL
)" || {
  case "$preflight_out" in
    *"pilot built-ins"*)
      cat >&2 <<'EOF'
This stack still holds features in the retired pilot built-ins (signs / street_lights /
county_boundary). The upgrade stops HERE, before any migration ran — nothing changed.
Manual path (decision-retire-builtins.md, fork B), on the CURRENT pre-upgrade version:
  1. Export each built-in's features via the OGC / export doors.
  2. Re-import them through the bulk-import wizard (9d) into hosted layers.
  3. TRUNCATE assets.signs, assets.street_lights, assets.county_boundary.
  4. Re-run this upgrade with the SAME flags.
EOF
      ;;
    *)
      echo "$preflight_out" >&2
      echo "Pre-flight query failed (postgres not ready?) — see the output above." >&2
      ;;
  esac
  exit 1
}

mig_head="$(ls db/migrations/V*.sql | tail -1 | sed 's|.*/||; s|__.*||')"
say "Flyway migrate (expect through ${mig_head})"
docker compose run --rm flyway migrate

# --- OpenFGA loader BEFORE the api (ordering rule #1) ------------------------------
say "OpenFGA store + model load (captures IDs into .env)"
./scripts/load-openfga-model.sh

# --- .env ownership + mode — deliberately the LAST thing that touches .env -------------------
# ★ The ordering is the whole point (security review 2026-09-09): an earlier version of this block
# ran right after generate-secrets, and load-openfga-model.sh below it rewrote .env and silently
# undid both the mode and the owner. A permissions fix has to be the last word, not the first.
#
# WHY at all: the api runs as NON-ROOT uid 10001 (api/Dockerfile), .env is mounted read-only at
# /secrets/.env, and backup.py reads it for the Talus 45 lifeboat artifact — so that uid needs read
# access. And the file must NOT be world-readable: it holds the postgres superuser password, the
# Keycloak admin password, the provisioner client secret (Admin API — can reset any password) and
# TALUS_ENCRYPTION_KEY (decrypts stored geocoder tokens and field-project PATs).
chmod 600 .env 2>/dev/null || true
# ★ Never hand the deployment master key to an EXISTING host principal. uid 10001 sits inside the
# canonical Samba/winbind `idmap config * : range = 10000-20000`, so on an AD-joined municipal
# server it can be a real interactive domain user — who would then OWN this file and could make it
# writable at will. If the uid is taken, refuse the chown and say why.
if id 10001 >/dev/null 2>&1; then
  echo "  ⚠ uid 10001 already exists on this host — NOT changing .env ownership." >&2
  echo "    That uid is the api container's user; giving it to an existing host account would" >&2
  echo "    hand that account every deployment secret. .env stays root-owned, mode 600." >&2
  echo "    Consequence: the OPTIONAL include-.env backup artifact cannot be produced (it is" >&2
  echo "    off by default). Nothing else is affected." >&2
elif chown 10001:10001 .env 2>/dev/null; then
  echo "  .env -> mode 600, owner uid 10001 (the api's non-root user)"
else
  echo "  ⚠ could not chown .env to uid 10001 (needs root); .env stays mode 600." >&2
  echo "    Everything works EXCEPT the optional include-.env backup artifact, which is off by" >&2
  echo "    default. To enable it later:  sudo chown 10001:10001 .env" >&2
fi

# --- api: RECREATE so it reads the captured IDs (ordering rule #2) -----------------
# --- storage volume ownership (the UPGRADE case) -------------------------------------------
# ★ PROVEN on a clean box, 2026-09-10, and it is why this block exists rather than a runbook step.
# The api runs as non-root uid 10001 (api/Dockerfile). Docker seeds an EMPTY named volume from the
# image's content AND ownership, so a FRESH install's `storagedata` comes out talus-owned and this
# is a no-op. An EXISTING volume — any deployment installed before the non-root switch — gets no
# such copy: its directories stay root-owned and the non-root api cannot write media, rasters or
# backups.
#
# ★★ The failure is SILENT. The upgraded container reports `healthy` (its healthcheck only GETs
# /health) while every upload and every backup would fail. That is why this is repaired here
# automatically instead of being documented and forgotten: an operator has no signal to act on.
#
# Runs as root in a throwaway container off the api's OWN image — no extra image to pull, so this
# works on an air-gapped box. Guarded by a cheap probe (`-print -quit` stops at the first hit), so
# a correct volume costs one exec and a large one is never needlessly re-chowned.
# `< /dev/null` is load-bearing: `docker compose run` otherwise eats this script's stdin.
STORAGE_FIX_RUN="docker compose run --rm --no-deps --user 0 --entrypoint sh api"
if [ -n "$($STORAGE_FIX_RUN -c 'find /storage -maxdepth 1 ! -uid 10001 -print -quit' < /dev/null 2>/dev/null | tr -d '\r')" ]; then
  say "Storage volume: adopting to the api's non-root uid (pre-existing install)"
  if $STORAGE_FIX_RUN -c 'chown -R 10001:10001 /storage' < /dev/null >/dev/null 2>&1; then
    echo "  /storage -> uid 10001 (media, rasters, backups writable by the api again)"
  else
    echo "  ⚠ could not adopt /storage to uid 10001." >&2
    echo "    The api will start and report healthy, but uploads and backups WILL fail." >&2
    echo "    Fix with:  docker compose run --rm --no-deps --user 0 --entrypoint sh api \\" >&2
    echo "                 -c 'chown -R 10001:10001 /storage'" >&2
  fi
fi

say "API + tileserv (force-recreate so the api re-reads .env)"
if ! docker compose up -d --force-recreate api pg_tileserv; then
  # decision-install-reachability.md F4 (F5 finding #4): the raw registry error says only
  # "unauthorized" — name the image and the way out, and make resume-by-re-run explicit.
  API_IMG="$(grep -A2 '^  api:' docker-compose.yml | sed -n 's/^ *image: *//p' | head -1)"
  echo "" >&2
  echo "FAIL: could not start the api — most often the image pull failed." >&2
  echo "  Image: ${API_IMG:-ghcr.io/talusgis/talus-api}" >&2
  echo "  Likely causes:" >&2
  echo "    - this host cannot reach ghcr.io (proxy / egress filter / offline)" >&2
  echo "    - the package requires authentication (docker login ghcr.io)" >&2
  echo "" >&2
  echo "  To finish the install:" >&2
  echo "    1. Fix the cause above — for an auth failure:  docker login ghcr.io" >&2
  echo "    2. Re-run the same command:  ${RESUME_CMD}" >&2
  echo "       The install is idempotent and resumes where it stopped." >&2
  exit 1
fi

say "Waiting for API health (boot guard must pass)"
api_ready=0
i=0
while [ "$i" -lt 45 ]; do
  if curl -sf "http://localhost:${API_PORT:-8000}/health" >/dev/null 2>&1; then
    api_ready=1; break
  fi
  i=$((i+1)); sleep 2
done
if [ "$api_ready" -ne 1 ]; then
  echo "  API not healthy after ~90s." >&2
  echo "  Check 'docker compose logs --tail=30 api' — if the boot guard fired" >&2
  echo "  (openfga_store_absent / openfga_model_absent), re-run load-openfga-model.sh" >&2
  echo "  then 'docker compose up -d --force-recreate api'. A plain restart will NOT fix it." >&2
  echo "" >&2
  echo "  Then re-run:  ${RESUME_CMD}" >&2
  exit 1
fi
echo "  API healthy (boot guard passed)."

# --- edge front --------------------------------------------------------------------
if [ "$EDGE" -eq 1 ]; then
  say "Edge: Caddy TLS front (serves the SPA, proxies api + keycloak)"
  # ALWAYS --force-recreate (decision-installer-edge-recreate.md): the Caddyfile is a single-file
  # bind mount, which follows the file's inode, and an upgrade's rsync or checkout writes a new
  # inode. A plain `up -d` leaves a running edge on the OLD file, so a release's edge changes
  # (headers, CSP, routes) never reach an upgraded box. On a first install nothing is running and
  # this is the same as `up -d`; certificates live in the caddydata volume, so none is re-issued.
  docker compose --profile edge up -d --force-recreate caddy
fi

# --- clean finish --------------------------------------------------------------------
# Unconditional now, which makes it strictly more load-bearing: it is the check that
# catches a realm rendered with dev users by any route (decision-demo-seed-retirement.md §4).
say "Clean-install negative check (no dev secret, no dev login)"
./scripts/verify-clean-install.sh
# The initial admin is minted at the VERY END, not here — see the closing block.

# --- done ------------------------------------------------------------------------------
ELAPSED=$((SECONDS - T_START))
say "Talus is ready  (install took $((ELAPSED / 60))m $((ELAPSED % 60))s)"
if [ "$EDGE" -eq 1 ]; then
  echo "  Portal:  https://${TALUS_HOSTNAME}${PORT_SUFFIX:-}"
  echo "  Auth:    https://${KEYCLOAK_HOSTNAME}${PORT_SUFFIX:-}"
else
  echo "  API:     http://localhost:${API_PORT:-8000}  (docs gated off in production mode)"
  echo "  Portal:  NOT STARTED — this run brought up the services without the web portal."
  echo "           For the portal, run:  ./install.sh --edge --tls-internal   (docs/install-runbook.md §4)"
fi
echo "  Sign in as the administrator — see the end of this output."

# decision-install-reachability.md F3 (F5 finding #5): the localhost default is correct on a
# desktop and a trap on a server — never end an edge install unreachable QUIETLY. Last words
# on purpose: this is the block a hurried operator actually reads.
if [ "$EDGE" -eq 1 ] && [ "$TALUS_HOSTNAME" = "talus.localhost" ]; then
  echo ""
  echo "  ============================================================================"
  echo "  ⚠  This portal is reachable ONLY FROM THIS MACHINE (https://talus.localhost)."
  echo "     Installing on a server? Give it a name your users can reach — one question:"
  echo ""
  echo "         scripts/talus-setup.sh"
  echo ""
  echo "     Enter a hostname you own, or just press Enter to use this machine's"
  echo "     network address. Safe to re-run anytime."
  echo "  ============================================================================"
fi

# --- initial admin — DELIBERATELY LAST (F5 re-run finding #1) --------------------------
# The generated credential is the one string in this whole output that cannot be recovered, and
# the terminal's resting position shows the END. It used to print before the closing blocks, so
# our own F3 warning box pushed it out of view. Minting it here — rather than capturing the
# password earlier and re-printing it — keeps "shown ONCE, stored nowhere else" literally true:
# no capture, no temp file, no second copy in a shell variable.
# The heading stays neutral: on a FIRST install the script prints the generated credential, on a
# re-run it prints "already provisioned; no credential to show". Promising a password in the
# heading made the re-run read as a broken install — the closing lines claimed a credential twice
# and then delivered none (clean-box run finding, 2026-08-25; the promise was this slice's own).
say "Initial admin"
if ! ./scripts/create-initial-admin.sh; then
  echo "" >&2
  echo "  The stack is UP, but no administrator was created — you cannot sign in yet." >&2
  echo "  Fix the cause above, then run:" >&2
  if [ "$DRIVEN_BY_SETUP" -eq 1 ]; then
    echo "    ${RESUME_CMD}" >&2
  else
    echo "    ./scripts/create-initial-admin.sh" >&2
  fi
  exit 1
fi