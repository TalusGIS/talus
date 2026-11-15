#!/usr/bin/env bash
# Invariant — every party that names the Keycloak realm names the SAME realm
# (decision-realm-rename.md §5).
#
# The realm rename's entire failure mode is "the stack comes up and nobody can log in": Keycloak
# serving one realm while the api validates tokens against another, or the SPA sending the
# browser to a third. No scanner sees that; a grep sees only the tree. This leg sees both.
#
# PART A (tree, no stack): the tree names exactly ONE realm — the template's "realm" key — in
#   every /realms/<name> URL, every REALM default, every config.py default, and (when a built
#   dist is present) the SPA's baked fallback. Catches a half-applied TREE.
# PART B (box, stack up): what the RUNNING api validates against (its effective keycloak_realm,
#   issuer and JWKS URL), the rendered realm-export.json, the realm Keycloak actually answers
#   for, Keycloak's advertised discovery issuer (the exact string the api compares `iss` to),
#   and on edge installs the runtime config.json the browser reads — all equal the same realm.
#   Catches a half-applied BOX: in particular a rebuild that kept an old .env, whose
#   KEYCLOAK_JWKS_URL nothing ever rewrites.
#
# ★ Bite-proven by sequencing, not by fixture: a renamed tree copied
# onto a not-yet-rebuilt box must read RED here; a box rebuilt with its old .env must read RED on
# the JWKS leg; only a clean rebuild reads GREEN. Do not skip those reds — they are the proof.
#
# Read-only: mints nothing, writes nothing. bash 3.2-safe; jq-free (python3); -sS curl.
# Exit: 0 pass · 2 fail · 3 skip (PART A passed, PART B had no stack to look at).
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv33: FAIL — cd"; exit 2; }

TEMPLATE="keycloak/realm-export.template.json"
[ -f "$TEMPLATE" ] || { echo "inv33: FAIL — $TEMPLATE missing"; exit 2; }
EXPECT="$(python3 -c "import json;print(json.load(open('$TEMPLATE')).get('realm',''))" 2>/dev/null)"
[ -n "$EXPECT" ] || { echo "inv33: FAIL — $TEMPLATE has no \"realm\" key"; exit 2; }
echo "  the realm the tree intends (template \"realm\"): $EXPECT"

fail=0; checked=0
ok()  { echo "  PASS  $1"; checked=$((checked + 1)); }
bad() { echo "  FAIL  $1"; checked=$((checked + 1)); fail=$((fail + 1)); }

# ---------------------------------------------------------------------------------------------
echo "--- PART A: the tree names ONE realm ---"
# Files to sweep: everything a deployment or a dev box executes or reads — never docs/ (history
# stays history), never the gitignored render (PART B reads that), never node_modules, never a
# dot-directory (.git, .github — tool state, not product).
# Comment lines are dropped so a comment recounting the old name is not a false red.
sweep_files() {
  # (no -type f before the prune clause — that would test the DIRECTORY against -type f first,
  # fail, and never prune it: node_modules would then be swept, 18k files, minutes not seconds)
  find . \
    \( -path ./docs -o -path './.*' -o -path ./web/node_modules -o -path ./api/.venv \
       -o -path ./web/dist -o -path ./keycloak/import -o -name '*.pyc' \) -prune \
    -o -type f \( -name '*.sh' -o -name '*.py' -o -name '*.ts' -o -name '*.tsx' -o -name '*.js' \
       -o -name '*.yml' -o -name '*.yaml' -o -name '*.json' -o -name '*.example' -o -name '*.conf' \
       -o -name 'Caddyfile*' -o -name 'install.sh' \) -print 2>/dev/null
}
names="$(sweep_files | while read -r f; do
  grep -I -v -E '^[[:space:]]*(#|//|\*)' "$f" 2>/dev/null \
    | grep -oE '(/realms/[A-Za-z0-9_-]+|KEYCLOAK_REALM:-[A-Za-z0-9_-]+|[^A-Z_]REALM:-[A-Za-z0-9_-]+|[^A-Z_]REALM="[A-Za-z0-9_-]+"|keycloak_realm: str = "[A-Za-z0-9_-]+"|KC_REALM \|\| '"'"'[A-Za-z0-9_-]+'"'"')' \
    | sed -E 's/.*[:=/|] ?"?'"'"'?//; s/["'"'"']$//; s/^-//' \
    | grep -vx master | sed "s|^|$f: |"
done)"
distinct="$(printf '%s\n' "$names" | sed 's/^.*: //' | sort -u | grep -v '^$')"
if [ -z "$distinct" ]; then
  bad "tree: no realm reference found at all — sweep is broken, not the tree"
elif [ "$distinct" = "$EXPECT" ]; then
  ok "tree: every realm reference reads '$EXPECT' ($(printf '%s\n' "$names" | grep -c . ) sites)"
else
  bad "tree names MORE than one realm — expected only '$EXPECT':"
  printf '%s\n' "$names" | grep -v ": $EXPECT\$" | sed 's/^/          /'
fi

# The Keycloak import must be a DIRECTORY mount (see docker-compose.yml): a file mount named
# after the realm leaves stubs in the kcdata volume when the realm is renamed.
if grep -qE '^\s*- \./keycloak/import:/opt/keycloak/data/import:ro' docker-compose.yml; then
  ok "docker-compose.yml mounts keycloak/import as a directory"
else
  bad "docker-compose.yml does not mount ./keycloak/import as a directory at /opt/keycloak/data/import"
fi

# The built SPA bakes oidcConfig.ts's fallback authority; a stale dist sends a dev/no-config.json
# deployment to a realm that no longer exists. Checked only when a dist is present.
if [ -d web/dist ]; then
  dn="$(grep -rIoh '/realms/[A-Za-z0-9_-]*' web/dist 2>/dev/null | sed 's|/realms/||' | grep -vx master | sort -u)"
  if [ -z "$dn" ]; then ok "web/dist: no baked realm fallback found (nothing to disagree)"
  elif [ "$dn" = "$EXPECT" ]; then ok "web/dist: baked fallback reads '$EXPECT'"
  else bad "web/dist: baked fallback reads '$(printf '%s' "$dn" | tr '\n' ' ')' — rebuild the SPA (npm --prefix web run build)"; fi
fi

# ---------------------------------------------------------------------------------------------
echo "--- PART B: the RUNNING box agrees with the tree ---"
live_skip() { echo "inv33: SKIP (live) — $1"; }
if ! command -v docker >/dev/null 2>&1; then
  [ "$fail" -eq 0 ] && { live_skip "docker not available on this box"; echo "inv33: PASS (tree) · SKIP (live)"; exit 3; }
elif [ -z "$(docker compose ps -q api 2>/dev/null)" ]; then
  [ "$fail" -eq 0 ] && { live_skip "api container not up"; echo "inv33: PASS (tree) · SKIP (live)"; exit 3; }
elif [ ! -f ./.env ]; then
  [ "$fail" -eq 0 ] && { live_skip "./.env not found beside the compose file"; echo "inv33: PASS (tree) · SKIP (live)"; exit 3; }
else
  # rendered realm (what Keycloak imported on first boot). ★ The FILENAME must be
  # <realm>-realm.json: Keycloak 26.0 derives the realm name from it for the deferred
  # service-account setup (keycloak/keycloak#36284) — a mismatch is a fresh-install outage.
  if [ -d keycloak/import ]; then
    nfiles="$(ls keycloak/import/*-realm.json 2>/dev/null | wc -l | tr -d ' ')"
    rf="$(ls keycloak/import/*-realm.json 2>/dev/null | head -1)"
    if [ "$nfiles" != "1" ]; then
      bad "keycloak/import holds $nfiles *-realm.json files — Keycloak parses every one; expected exactly ${EXPECT}-realm.json"
    else
      rr="$(python3 -c "import json;print(json.load(open('$rf')).get('realm',''))" 2>/dev/null)"
      [ "$rr" = "$EXPECT" ] && ok "rendered $rf: realm '$rr'" \
        || bad "rendered $rf says realm '$rr' — re-render (scripts/render-realm.sh)"
      [ "$(basename "$rf")" = "${EXPECT}-realm.json" ] && ok "rendered filename matches <realm>-realm.json" \
        || bad "rendered file is $(basename "$rf"), Keycloak will look up realm '$(basename "$rf" -realm.json)' (#36284) — re-render"
    fi
  fi

  # the api's EFFECTIVE settings — env + config.py defaults, exactly as the running process sees them
  eff="$(docker compose exec -T api python -c \
    'from app.config import Settings; s=Settings(); print(s.keycloak_realm); print(s.keycloak_issuer); print(s.keycloak_jwks_url)' \
    2>/dev/null < /dev/null)"
  api_realm="$(printf '%s\n' "$eff" | sed -n 1p)"
  api_iss="$(printf '%s\n' "$eff" | sed -n 2p)"
  api_jwks="$(printf '%s\n' "$eff" | sed -n 3p)"
  if [ -z "$api_realm" ] || [ -z "$api_iss" ] || [ -z "$api_jwks" ]; then
    bad "could not read the api's effective settings (docker compose exec api python …)"
  else
    iss_realm="$(printf '%s' "$api_iss"  | sed -n 's|.*/realms/\([A-Za-z0-9_-]*\).*|\1|p')"
    jwks_realm="$(printf '%s' "$api_jwks" | sed -n 's|.*/realms/\([A-Za-z0-9_-]*\).*|\1|p')"
    [ "$api_realm" = "$EXPECT" ] && ok "api keycloak_realm (admin calls): '$api_realm'" \
      || bad "api keycloak_realm is '$api_realm' — KEYCLOAK_REALM in .env / compose disagrees with the tree"
    [ "$iss_realm" = "$EXPECT" ] && ok "api KEYCLOAK_ISSUER realm: '$iss_realm'" \
      || bad "api KEYCLOAK_ISSUER names realm '$iss_realm' ($api_iss) — every token would fail the iss check"
    [ "$jwks_realm" = "$EXPECT" ] && ok "api KEYCLOAK_JWKS_URL realm: '$jwks_realm'" \
      || bad "api KEYCLOAK_JWKS_URL names realm '$jwks_realm' ($api_jwks) — signing keys fetched from a realm that does not exist (stale .env?)"
  fi

  # Keycloak itself: does it SERVE the realm the tree intends, and what issuer does it advertise?
  kp="$(grep -E '^KEYCLOAK_PORT=' ./.env | head -1 | cut -d= -f2- | tr -d '"')"; kp="${kp:-8080}"
  KC="${KEYCLOAK_URL:-http://localhost:$kp}"
  kc_code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "$KC/realms/$EXPECT" 2>/dev/null)"
  if [ "$kc_code" = "200" ]; then
    kc_realm="$(curl -sS --max-time 10 "$KC/realms/$EXPECT" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("realm",""))' 2>/dev/null)"
    [ "$kc_realm" = "$EXPECT" ] && ok "Keycloak serves realm '$kc_realm'" \
      || bad "Keycloak answered for /realms/$EXPECT but reports realm '$kc_realm'"
    disc_iss="$(curl -sS --max-time 10 "$KC/realms/$EXPECT/.well-known/openid-configuration" \
      | python3 -c 'import sys,json;print(json.load(sys.stdin).get("issuer",""))' 2>/dev/null)"
    if [ -n "${api_iss:-}" ]; then
      [ "$disc_iss" = "$api_iss" ] && ok "Keycloak's advertised issuer == api KEYCLOAK_ISSUER ($disc_iss)" \
        || bad "Keycloak advertises issuer '$disc_iss' but the api expects '$api_iss' — the api's iss string-equality check fails on every token"
    fi
  else
    bad "Keycloak does NOT serve /realms/$EXPECT (HTTP $kc_code at $KC) — the realm the tree intends is not the realm Keycloak imported"
  fi

  # Edge installs: the SPA reads its authority from config.json, which must be the api's issuer.
  if [ -f proxy/config/config.json ]; then
    auth="$(python3 -c 'import json;print(json.load(open("proxy/config/config.json")).get("oidcAuthority",""))' 2>/dev/null)"
    auth_realm="$(printf '%s' "$auth" | sed -n 's|.*/realms/\([A-Za-z0-9_-]*\).*|\1|p')"
    [ "$auth_realm" = "$EXPECT" ] && ok "config.json oidcAuthority realm: '$auth_realm'" \
      || bad "config.json oidcAuthority names realm '$auth_realm' ($auth) — the browser is sent to the wrong realm"
    if [ -n "${api_iss:-}" ]; then
      [ "$auth" = "$api_iss" ] && ok "config.json oidcAuthority == api KEYCLOAK_ISSUER" \
        || bad "config.json oidcAuthority '$auth' != api KEYCLOAK_ISSUER '$api_iss' — browser tokens carry an iss the api rejects"
    fi
  fi
fi

echo
if [ "$fail" -eq 0 ]; then echo "inv33: PASS ($checked checks, realm '$EXPECT')"; exit 0
else echo "inv33: FAIL — $fail of $checked checks (realm the tree intends: '$EXPECT')"; exit 2; fi
