#!/usr/bin/env bash
# sync-user-profile.sh — a LAST NAME is optional in the Talus realm
# (decision-one-word-names.md).
#
# Why: Talus keeps a person's name as one field; Keycloak keeps two and, by default, REQUIRES
# the second. An account whose name is one word — "Dispatch", a shared counter sign-in, a
# person with a single name — reaches Keycloak with no last name, is treated as "not fully
# set up", and is sent to a Keycloak profile page Talus never designed at its first sign-in.
# Nothing in Talus reads Keycloak's first or last name, so the requirement is lifted where it
# lives instead of being worked around at every door that creates a user.
#
# How: GET the realm's user profile, remove ONLY the `required` member of the `lastName`
# attribute, PUT it back — and only when that member was there. Never a profile built from
# scratch: every other attribute, validation and permission (including anything an
# administrator added) goes back exactly as it was read. Afterwards the profile is read AGAIN
# and compared, whole, with what was read first minus that one member — the script reports
# what Keycloak holds, not what it sent.
#
# ★ That one member is Talus's to set. An administrator who makes the
# last name required again in the Keycloak console will find it optional after the next
# install or upgrade: a required last name breaks every one-word account, so this runs on
# every install.sh run, and a fresh install and an upgraded one converge.
#
# ★ A second member is Talus's to set (once-over 2026-11-15 E4): a person cannot change their OWN
# email. Keycloak's default lets every user edit it in the account console, and an email is what
# single sign-on's "link to an existing account" matches on, so any local user could set a
# colleague's address (or the administrator's) before that colleague first signed in through the
# organization's identity provider. `email` keeps its view permissions; its `edit` becomes
# ["admin"]. Talus itself sets every account's email (Admin -> Users, bulk import) and marks it
# verified, so no Talus-created account is left needing to type one.
#
# Credential posture: the master admin credential from ./.env, used against localhost — the
# pattern of sync-portal-client.sh and create-initial-admin.sh. No new secret, no new holder,
# nothing steerable by untrusted input.
#
# Idempotent; safe to re-run anytime. bash 3.2-safe. Requires curl + python3 (preflight).
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] || { echo "FAIL: no .env here ($PWD)" >&2; exit 1; }

env_get() { sed -n "s/^$1=//p" .env | tail -1 | sed 's/^"//; s/"$//'; }

KC_PORT="$(env_get KEYCLOAK_PORT)"; KC_PORT="${KC_PORT:-8080}"
# The port goes into the address the admin credential is sent to: digits, or nothing is sent.
case "$KC_PORT" in *[!0-9]*) echo "FAIL: KEYCLOAK_PORT in .env is not a number" >&2; exit 1 ;; esac
KC="${KC_ADMIN_URL:-http://localhost:${KC_PORT}}"
REALM="$(env_get KEYCLOAK_REALM)"; REALM="${REALM:-talus}"
KC_USER="$(env_get KEYCLOAK_ADMIN_USER)"; KC_USER="${KC_USER:-admin}"
KC_PW="$(env_get KEYCLOAK_ADMIN_PASSWORD)"
[ -n "$KC_PW" ] || { echo "FAIL: KEYCLOAK_ADMIN_PASSWORD missing from .env" >&2; exit 1; }

# Every request: a time limit (a Keycloak that accepts the connection and stalls must not hang
# the installer — it warns and goes on), and the status code on the last line so a refusal's
# BODY can be printed (the house rule: never swallow what the server said).
CURL="curl -sS --max-time 30"
code_of() { printf '%s' "${1##*$'\n'}"; }
body_of() { printf '%s' "${1%$'\n'*}"; }
ok()      { c="$(code_of "$1")"; [ "${c#2}" != "$c" ]; }

# The password goes to curl on stdin — never in its argument list — and curl URL-encodes it
# (a hand-set password with & + or % would otherwise be mangled in the form body).
out="$(printf '%s' "$KC_PW" | $CURL -X POST "$KC/realms/master/protocol/openid-connect/token" \
  -d grant_type=password -d client_id=admin-cli \
  --data-urlencode "username=$KC_USER" --data-urlencode password@- -w $'\n%{http_code}' 2>&1)" || true
if ! ok "$out"; then
  echo "FAIL: could not get a Keycloak admin token -> HTTP $(code_of "$out") (is keycloak up? KEYCLOAK_ADMIN_* in .env?)" >&2
  echo "  body: $(body_of "$out" | head -c 400)" >&2
  exit 1
fi
TOKEN="$(body_of "$out" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("access_token",""))' 2>/dev/null || true)"
[ -n "$TOKEN" ] || { echo "FAIL: Keycloak answered the token request without a token" >&2; exit 1; }

URL="$KC/admin/realms/$REALM/users/profile"
get_profile() { # -> the profile JSON on stdout; a refusal's body on stderr
  o="$($CURL -H "Authorization: Bearer $TOKEN" "$URL" -w $'\n%{http_code}' 2>&1)" || true
  if ! ok "$o"; then
    echo "FAIL: could not read the realm's user profile ($URL) -> HTTP $(code_of "$o")" >&2
    echo "  body: $(body_of "$o" | head -c 400)" >&2
    return 1
  fi
  body_of "$o"
}
BEFORE="$(get_profile)" || exit 1

# Prints the profile to write. Exit 0 = a change is needed, 10 = already as wanted,
# 2 = a shape this script does not recognise (nothing is written).
EDIT='
import sys, json
try:
    p = json.load(sys.stdin)
    attrs = p["attributes"]
    assert isinstance(attrs, list) and all(isinstance(a, dict) for a in attrs)
    last = [a for a in attrs if a.get("name") == "lastName"]
    email = [a for a in attrs if a.get("name") == "email"]
    assert last and email
    for a in email:
        assert isinstance(a.get("permissions", {}), dict)
except Exception:
    sys.exit(2)
changed = False
for a in last:
    if a.get("required") is not None:
        changed = True
    a.pop("required", None)
for a in email:
    perms = a.setdefault("permissions", {})
    if perms.get("edit") != ["admin"]:
        changed = True
    perms["edit"] = ["admin"]
if not changed:
    sys.exit(10)
json.dump(p, sys.stdout)
'
rc=0
AFTER="$(printf '%s' "$BEFORE" | python3 -c "$EDIT")" || rc=$?
if [ "$rc" -eq 10 ]; then
  echo "    user profile: a last name is already optional and email is administrator-edited — nothing to change"
  exit 0
fi
[ "$rc" -eq 0 ] || { echo "FAIL: the realm's user profile is not in a shape this script recognises — nothing was written" >&2; exit 1; }

out="$(printf '%s' "$AFTER" | $CURL -X PUT "$URL" -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d @- -w $'\n%{http_code}' 2>&1)" || true
if ! ok "$out"; then
  echo "FAIL: writing the user profile -> HTTP $(code_of "$out")" >&2
  echo "  body: $(body_of "$out" | head -c 400)" >&2
  exit 1
fi

# Read it back and compare the WHOLE profile with what was read first, minus the one member.
# This is the check that the write changed that and nothing else — it looks at what Keycloak
# now holds, independently of what was sent.
CHECK="$(get_profile)" || exit 1
verdict=0
SYNC_BEFORE="$BEFORE" SYNC_CHECK="$CHECK" python3 -c '
import os, sys, json
before = json.loads(os.environ["SYNC_BEFORE"]); now = json.loads(os.environ["SYNC_CHECK"])
def strip(p):
    for a in p.get("attributes", []):
        if isinstance(a, dict) and a.get("name") == "lastName":
            a.pop("required", None)
        if isinstance(a, dict) and a.get("name") == "email":
            a.setdefault("permissions", {})["edit"] = ["admin"]
    return p
still = [a for a in now.get("attributes", []) if isinstance(a, dict) and a.get("name") == "lastName" and a.get("required") is not None]
still += [a for a in now.get("attributes", []) if isinstance(a, dict) and a.get("name") == "email" and (a.get("permissions") or {}).get("edit") != ["admin"]]
if still: sys.exit(1)
sys.exit(0 if strip(before) == strip(now) else 3)
' || verdict=$?
case "$verdict" in
  0) echo "    user profile: a last name is optional and email is administrator-edited" ;;
  1) echo "FAIL: after the write the user profile still requires a last name or lets a user edit their email" >&2; exit 1 ;;
  *) echo "FAIL: the last name is optional now, but the profile Keycloak holds differs from the one that was read in MORE than that — compare it in the Keycloak console (Realm settings > User profile)" >&2; exit 1 ;;
esac
