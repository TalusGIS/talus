#!/usr/bin/env bash
# create-initial-admin.sh — the CLEAN install's first login (decision-deployment-appliance.md
# §5.2): one admin user with a GENERATED policy-compliant password, printed once.
#
# What it ensures, idempotently (mirrors bootstrap-7b's detect-and-skip discipline):
#   1. The four talus-* realm roles. On a clean install NOTHING else creates them — the
#      shipped realm template carries no roles (they were always bootstrap-created), and
#      without them Admin -> Users onboarding (Talus 27) cannot assign a role.
#   2. The admin user (default username: admin; ADMIN_USERNAME overrides) with a generated
#      password satisfying the realm policy (length(12) + upper + lower + digit).
#      ★ If the user already EXISTS its password is left UNCHANGED — re-running an installer
#      must never silently rotate the admin credential (unlike the dev bootstrap, which
#      resets dev throwaways by design).
#   3. The talus-admin realm-role mapping (realm-role-only admin — no OpenFGA tuple, the
#      same shape as the dev admin-test login).
#   4. The platform.users row (api/app/auth.py 401s a sub with no row).
#   ★ Steps 3 and 4 run only while the account's setup is UNFINISHED: the user was created in
#     this run, or a first install stopped after creating it and before its platform.users row
#     existed. Once that row exists, an install, upgrade or rename never changes the account
#     again — not its role, not its email or display name (once-over 2026-11-15 E2: before,
#     every run re-granted talus-admin, so an account an organization had demoted rather than
#     disabled was an Administrator again after the next upgrade, with no audit record). If
#     the account is no longer an administrator the run says so; TALUS_RESTORE_ADMIN_ROLE=1
#     grants the role back, on purpose.
#
# The password is printed ONCE, to stdout, at the very end — nothing else writes it anywhere.
#
# Guided setup (decision-first-run-setup.md §12, w1) supplies the identity instead:
#   ADMIN_USERNAME / ADMIN_EMAIL / ADMIN_DISPLAY_NAME   from .env (talus-setup wrote them)
#   TALUS_ADMIN_PASSWORD                                from the ENVIRONMENT only, never .env
# A supplied password is checked against the realm policy here (this is the gate; the caller's
# check is a courtesy), is never printed, and — like a generated one — never replaces the
# password of a user that already exists. Typed values reach JSON through jq and SQL through
# psql variables, never by pasting strings: a name can be O'Brien.
#
# bash 3.2-safe. Requires: curl, jq, a healthy Keycloak + postgres (install.sh sequences this).

set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
. ./.env

command -v jq >/dev/null   || { echo "FAIL: jq not found on host"; exit 1; }
command -v curl >/dev/null || { echo "FAIL: curl not found on host"; exit 1; }

KC="${KC_ADMIN_URL:-http://localhost:${KEYCLOAK_PORT:-8080}}"
REALM="talus"
ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
ADMIN_EMAIL="${ADMIN_EMAIL:-${ADMIN_USERNAME}@talus.local.invalid}"
ADMIN_DISPLAY_NAME="${ADMIN_DISPLAY_NAME:-Talus Administrator}"
SUPPLIED_PW="${TALUS_ADMIN_PASSWORD:-}"
# First/last for the Keycloak console only, split the way the api does (keycloak_admin.create_user);
# platform.users.display_name is the label the portal renders.
ADMIN_FIRST="${ADMIN_DISPLAY_NAME%% *}"
ADMIN_LAST=""
case "$ADMIN_DISPLAY_NAME" in *" "*) ADMIN_LAST="${ADMIN_DISPLAY_NAME#* }" ;; esac
# A one-word name leaves the last name empty. That is fine: install.sh has already made the last
# name optional in the realm (scripts/sync-user-profile.sh, decision-one-word-names.md).

# The realm policy: length(12) + upper + lower + digit + notUsername + notEmail.
policy_ok() {
  [ "${#1}" -ge 12 ] || { echo "at least 12 characters are needed"; return 1; }
  case "$1" in *[[:upper:]]*) : ;; *) echo "an upper-case letter is needed"; return 1 ;; esac
  case "$1" in *[[:lower:]]*) : ;; *) echo "a lower-case letter is needed"; return 1 ;; esac
  case "$1" in *[[:digit:]]*) : ;; *) echo "a digit is needed"; return 1 ;; esac
  lc="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  [ "$lc" != "$(printf '%s' "$ADMIN_USERNAME" | tr '[:upper:]' '[:lower:]')" ] || { echo "the password cannot be the sign-in name"; return 1; }
  [ "$lc" != "$(printf '%s' "$ADMIN_EMAIL" | tr '[:upper:]' '[:lower:]')" ] || { echo "the password cannot be the email address"; return 1; }
  return 0
}

# Policy-compliant generation: base64 alphabet gives upper+lower+digit with overwhelming
# likelihood; loop until all three classes are present so compliance is checked, not hoped.
gen_password() {
  pw=""
  i=0
  while [ "$i" -lt 20 ]; do
    pw="$(openssl rand -base64 18 | tr -d '+/=')"
    if printf '%s' "$pw" | grep -q '[A-Z]' \
       && printf '%s' "$pw" | grep -q '[a-z]' \
       && printf '%s' "$pw" | grep -q '[0-9]' \
       && [ "${#pw}" -ge 16 ]; then
      printf '%s' "$pw"
      return 0
    fi
    i=$((i+1))
  done
  echo "FAIL: could not generate a policy-compliant password in 20 tries" >&2
  return 1
}

echo "==> Obtaining Keycloak admin token..."
TOKEN="$(curl -sf -X POST "$KC/realms/master/protocol/openid-connect/token" \
  -d grant_type=password -d client_id=admin-cli \
  -d "username=${KEYCLOAK_ADMIN_USER:-admin}" \
  -d "password=${KEYCLOAK_ADMIN_PASSWORD}" | jq -r .access_token)"
[ -n "$TOKEN" ] && [ "$TOKEN" != "null" ] || { echo "FAIL: could not get admin token (check KEYCLOAK_ADMIN_* in .env; is keycloak up?)"; exit 1; }
AUTH=(-H "Authorization: Bearer $TOKEN")

echo "==> Ensuring the four talus-* realm roles (the clean path has no other creator)..."
for r in talus-viewer talus-data-editor talus-publisher talus-admin; do
  if curl -sf -o /dev/null "${AUTH[@]}" "$KC/admin/realms/$REALM/roles/$r"; then
    echo "    role $r — exists"
  else
    curl -sf -X POST "${AUTH[@]}" -H 'content-type: application/json' \
      "$KC/admin/realms/$REALM/roles" -d "{\"name\":\"$r\",\"description\":\"Talus role\"}"
    echo "    role $r — created"
  fi
done

user_id() {
  # -G + --data-urlencode: a sign-in name that is an email carries '@' and may carry '+'.
  curl -sf -G "${AUTH[@]}" "$KC/admin/realms/$REALM/users" \
    --data-urlencode "username=${ADMIN_USERNAME}" --data-urlencode "exact=true" | jq -r '.[0].id // empty'
}

CREATED=0
ADMIN_PW=""
ID="$(user_id)"
if [ -z "$ID" ]; then
  if [ -n "$SUPPLIED_PW" ]; then
    why="$(policy_ok "$SUPPLIED_PW")" || { echo "FAIL: the supplied administrator password does not meet the password policy: $why"; exit 1; }
    ADMIN_PW="$SUPPLIED_PW"
  else
    ADMIN_PW="$(gen_password)"
  fi
  # The body is built by jq (the password from jq's environment, so it is in no argument list) and
  # sent on curl's stdin for the same reason.
  body_json="$(TALUS_PW="$ADMIN_PW" jq -n --arg u "$ADMIN_USERNAME" --arg e "$ADMIN_EMAIL" \
    --arg f "$ADMIN_FIRST" --arg l "$ADMIN_LAST" \
    '{username:$u, email:$e, firstName:$f, lastName:$l, enabled:true, emailVerified:true,
      credentials:[{type:"password", value:env.TALUS_PW, temporary:false}]}')"
  # -sS (not -f): keep the body on a 4xx so a policy rejection is visible, not silent.
  out="$(printf '%s' "$body_json" | curl -sS -X POST "${AUTH[@]}" -H 'content-type: application/json' \
    "$KC/admin/realms/$REALM/users" -w $'\n%{http_code}' -d @-)"
  body_json=""
  code="${out##*$'\n'}"; body="${out%$'\n'*}"
  if [ "${code#2}" = "$code" ]; then   # not 2xx
    echo "FAIL: create ${ADMIN_USERNAME} -> HTTP $code"
    echo "  body: $body"
    exit 1
  fi
  ID="$(user_id)"
  [ -n "$ID" ] || { echo "FAIL: created ${ADMIN_USERNAME} but could not read its id back"; exit 1; }
  CREATED=1
  echo "    user ${ADMIN_USERNAME} — created ($ID)"
else
  echo "    user ${ADMIN_USERNAME} — exists ($ID); password UNCHANGED (installer never rotates it)"
fi

# Is the account's setup finished? Its platform.users row is the last thing this script writes.
HAS_ROW="$(docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -At \
  -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -v id="$ID" <<'SQL'
SELECT count(*) FROM platform.users WHERE id = :'id';
SQL
)"
case "$HAS_ROW" in 0|1) : ;; *) echo "FAIL: could not read platform.users for ${ADMIN_USERNAME} (got: $HAS_ROW)"; exit 1 ;; esac
SETUP_DONE=0
[ "$CREATED" -eq 0 ] && [ "$HAS_ROW" = "1" ] && SETUP_DONE=1

roleobj="$(curl -sf "${AUTH[@]}" "$KC/admin/realms/$REALM/roles/talus-admin")"
rid="$(printf '%s' "$roleobj" | jq -r .id)"
map_admin_role() {   # re-POST is a no-op if already mapped
  curl -sf -X POST "${AUTH[@]}" -H 'content-type: application/json' \
    "$KC/admin/realms/$REALM/users/$ID/role-mappings/realm" \
    -d "[{\"id\":\"$rid\",\"name\":\"talus-admin\"}]"
}
if [ "$SETUP_DONE" -eq 0 ]; then
  map_admin_role
  echo "    role talus-admin — mapped"
else
  has_admin="$(curl -sf "${AUTH[@]}" "$KC/admin/realms/$REALM/users/$ID/role-mappings/realm" \
    | jq -r '[.[] | select(.name == "talus-admin")] | length')"
  if [ "${has_admin:-0}" != "0" ]; then
    echo "    role talus-admin — already held (unchanged)"
  elif [ "${TALUS_RESTORE_ADMIN_ROLE:-0}" = "1" ]; then
    map_admin_role
    echo "    role talus-admin — granted again (TALUS_RESTORE_ADMIN_ROLE=1)"
  else
    echo "    NOTE: ${ADMIN_USERNAME} is no longer an administrator, and this run leaves it that way."
    echo "          To make it one again: Admin -> Users in the portal (as another administrator), or"
    echo "          re-run with TALUS_RESTORE_ADMIN_ROLE=1."
  fi
fi

echo "==> Ensuring platform.users row..."
# psql substitutes :'var' only in statements read from stdin (never in -c), hence the heredoc.
docker compose exec -T postgres psql -v ON_ERROR_STOP=1 \
  -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" \
  -v id="$ID" -v un="$ADMIN_USERNAME" -v em="$ADMIN_EMAIL" -v dn="$ADMIN_DISPLAY_NAME" >/dev/null <<'SQL'
INSERT INTO platform.users (id, username, email, display_name)
VALUES (:'id', :'un', :'em', :'dn')
ON CONFLICT (id) DO NOTHING;
SQL
echo "    platform.users <- ${ADMIN_USERNAME} ($ID)"

# The guided setup asks to be told which it was (talus-setup.sh points this at a private file):
# its closing words differ when the administrator already existed and the password typed in this
# run is therefore NOT the one in force.
if [ -n "${TALUS_ADMIN_RESULT_FILE:-}" ]; then
  if [ "$CREATED" -eq 1 ]; then echo created; else echo existed; fi > "$TALUS_ADMIN_RESULT_FILE"
fi

echo
if [ "$CREATED" -eq 1 ] && [ -n "$SUPPLIED_PW" ]; then
  echo "=============================================================="
  echo "  Administrator created"
  echo "    sign in as: ${ADMIN_USERNAME}"
  case "$ADMIN_EMAIL" in *@talus.local.invalid) : ;; *) echo "                (or by email: ${ADMIN_EMAIL})" ;; esac
  echo "    password:   the one you chose (not stored or printed by the installer)"
  echo "=============================================================="
elif [ "$CREATED" -eq 1 ]; then
  echo "=============================================================="
  echo "  Initial admin credential — shown ONCE, stored nowhere else"
  echo "    username: ${ADMIN_USERNAME}"
  echo "    password: ${ADMIN_PW}"
  echo "  Change it after first sign-in."
  echo "=============================================================="
else
  # A re-run is the COMMON path, not the edge case: the installer is idempotent precisely so a
  # failed install can be resumed by running it again. A bare "no credential to show" at the very
  # end of that run reads like a broken install to someone who is already recovering from one.
  # Say plainly that nothing was rotated, and where the original came from.
  echo "=============================================================="
  echo "  ${ADMIN_USERNAME} already exists — password NOT changed."
  echo "  Keep using the credential from the first install."
  echo "  Lost it? See admin-password recovery in docs/install-runbook.md."
  echo "=============================================================="
fi
