#!/usr/bin/env bash
# apply-realm-settings.sh — SAFELY apply the Talus-48 realm settings (login theme selection + brute-force +
# password policy + OTP/MFA baseline) to a RUNNING Keycloak, via the Admin REST API.
#
# WHY NOT re-import / `--force-recreate`: the compose runs `start --import-realm`, which imports only on FIRST
# boot and SKIPS an already-existing realm — so a recreate applies NOTHING. And a forced *overwrite* import
# RESETS the realm and DELETES any users not in keycloak/import/<realm>-realm.json (the export is a seed, not a full
# mirror). So neither is the right tool for a live realm.
#
# This instead PATCHes ONLY the realm-level settings fields via `PUT /admin/realms/{realm}` — a representation
# that does NOT include users or clients (they are separate sub-resources), so it cannot touch them. As a
# belt-and-suspenders it asserts the user count is unchanged. Idempotent; safe to re-run.
#
# The branded login THEME *files* are delivered by a compose VOLUME mount, not by this script — if the running
# keycloak container predates that mount, follow up with `docker compose up -d keycloak` (safe: the realm +
# these settings persist in the Keycloak DB across a container recreate). See docs/sso-runbook.md.
#
# Once-over 2026-11-15 E5/E6 add, on every run:
#   - an offline session's maximum lifespan, and sign-in and admin events (kept 30 days; admin events
#     without their request bodies), copied from the template like the fields above;
#   - `offline_access` detached from the three Talus clients' optional scopes and from the realm's
#     default optional scopes. Nothing in Talus asks for it, and it let anyone holding a password
#     mint a token that renewed with no end and outlived a password reset;
#   - the MASTER realm's brute-force protection (temporary lockout only, never permanent, so a
#     mistyped script cannot lock the operator out for good) and its events.
# These are client-scope links and realm settings: still no user, credential or client body is touched.
#
# bash 3.2-safe; jq-free (python3). Run with the stack up: ./scripts/apply-realm-settings.sh
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
. ./.env

# The published Keycloak port (install.sh may move it off 8080); the sibling sync scripts read it the same way.
KC="${KEYCLOAK_URL:-http://localhost:${KEYCLOAK_PORT:-8080}}"
REALM="${KEYCLOAK_REALM:-talus}"
KCU="$(grep -E '^KEYCLOAK_ADMIN_USER=' .env | head -1 | cut -d= -f2-)"; KCU="${KCU:-admin}"
KCP="$(grep -E '^KEYCLOAK_ADMIN_PASSWORD=' .env | head -1 | cut -d= -f2-)"
# The settings are read from the TRACKED template, not the rendered keycloak/import/<realm>-realm.json:
# every field copied below is a fixed value there (no placeholder), and a rendered copy can predate
# the template (found 2026-11-15 on dev: a file rendered before E5/E6 carried none of their fields,
# so the PUT silently applied nothing new).
EXPORT="keycloak/realm-export.template.json"
[ -f "$EXPORT" ] || { echo "FATAL: $EXPORT not found"; exit 1; }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

echo "== Apply realm settings to '$REALM' via Admin REST (users untouched) =="
ATOK="$(curl -sS -X POST "$KC/realms/master/protocol/openid-connect/token" \
  -d grant_type=password -d client_id=admin-cli -d username="$KCU" -d password="$KCP" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("access_token",""))' 2>/dev/null)"
[ -n "$ATOK" ] || { echo "FATAL: master-realm admin token failed (KEYCLOAK_ADMIN_USER/_PASSWORD)"; exit 1; }
AUTH="Authorization: Bearer $ATOK"

BEFORE="$(curl -sS -H "$AUTH" "$KC/admin/realms/$REALM/users/count")"
echo "  users before: $BEFORE"

curl -sS -H "$AUTH" "$KC/admin/realms/$REALM" > "$TMP/realm.json"
python3 - "$EXPORT" "$TMP/realm.json" > "$TMP/patched.json" <<'PY'
import json, sys
exp = json.load(open(sys.argv[1]))
rep = json.load(open(sys.argv[2]))
# ONLY these realm-level fields; everything else in the live rep is preserved verbatim.
# The otpPolicy* keys carry the TOTP baseline (MFA is *capable* but forced on no one — enforcement
# is scoped per-role/user in the console per docs/sso-runbook.md; these are just the OTP parameters).
keys = ["loginTheme", "bruteForceProtected", "passwordPolicy", "failureFactor", "waitIncrementSeconds",
        "maxFailureWaitSeconds", "quickLoginCheckMilliSeconds", "minimumQuickLoginWaitSeconds",
        "maxDeltaTimeSeconds", "permanentLockout",
        "otpPolicyType", "otpPolicyAlgorithm", "otpPolicyDigits", "otpPolicyPeriod",
        "otpPolicyLookAheadWindow", "otpPolicyInitialCounter", "otpPolicyCodeReusable",
        # Session lifetimes: a crew out of signal for a
        # morning must not come back to "Sign in" — idle 4 h (a half work day), max unchanged (10 h).
        # The refresh token lives as long as the idle window, so the field app's renewal cues can carry a
        # session across a whole outage shorter than that.
        "ssoSessionIdleTimeout", "ssoSessionMaxLifespan",
        # E5/E6 (once-over 2026-11-15): offline sessions end; sign-in and admin events are recorded.
        "offlineSessionMaxLifespanEnabled", "offlineSessionMaxLifespan",
        "eventsEnabled", "eventsExpiration", "adminEventsEnabled", "adminEventsDetailsEnabled"]
rep.update({k: exp[k] for k in keys if k in exp})
json.dump(rep, sys.stdout)
PY

PUT="$(curl -sS -o /dev/null -w '%{http_code}' -X PUT -H "$AUTH" -H 'Content-Type: application/json' \
  --data @"$TMP/patched.json" "$KC/admin/realms/$REALM")"
[ "$PUT" = "204" ] || { echo "FATAL: realm PUT returned $PUT (expected 204)"; exit 1; }

curl -sS -H "$AUTH" "$KC/admin/realms/$REALM" | python3 -c \
  "import sys,json;d=json.load(sys.stdin);print('  applied: loginTheme=%s bruteForce=%s passwordPolicy=%s ssoIdle=%ss ssoMax=%ss' % (d.get('loginTheme'), d.get('bruteForceProtected'), bool(d.get('passwordPolicy')), d.get('ssoSessionIdleTimeout'), d.get('ssoSessionMaxLifespan')))"
curl -sS -H "$AUTH" "$KC/admin/realms/$REALM" | python3 -c \
  "import sys,json;d=json.load(sys.stdin);print('  applied: offlineMax=%s (%ss) events=%s (%ss) adminEvents=%s details=%s' % (d.get('offlineSessionMaxLifespanEnabled'), d.get('offlineSessionMaxLifespan'), d.get('eventsEnabled'), d.get('eventsExpiration'), d.get('adminEventsEnabled'), d.get('adminEventsDetailsEnabled')))"

# E5: offline_access detached wherever Talus's realm offers it. A link that is already absent is fine.
OA_ID="$(curl -sS -H "$AUTH" "$KC/admin/realms/$REALM/client-scopes" \
  | python3 -c 'import sys,json;print(next((c["id"] for c in json.load(sys.stdin) if c.get("name")=="offline_access"),""))' 2>/dev/null)"
# Keycloak answers 204 to a DELETE of a link that is not there, so what is linked is READ first:
# the report says what changed, and a second run says "unchanged".
has_scope() {  # $1 = URL of a scope list; prints 1 if offline_access is in it
  curl -sS -H "$AUTH" "$1" | python3 -c 'import sys,json;print(1 if any(c.get("name")=="offline_access" for c in json.load(sys.stdin)) else 0)' 2>/dev/null
}
if [ -n "$OA_ID" ]; then
  for cid in talus-portal talus-api talus-provisioner; do
    CUUID="$(curl -sS -G -H "$AUTH" "$KC/admin/realms/$REALM/clients" --data-urlencode "clientId=$cid" \
      | python3 -c 'import sys,json;l=json.load(sys.stdin);print(l[0]["id"] if l else "")' 2>/dev/null)"
    [ -n "$CUUID" ] || { echo "  offline_access: client $cid not found (skipped)"; continue; }
    LIST="$KC/admin/realms/$REALM/clients/$CUUID/optional-client-scopes"
    if [ "$(has_scope "$LIST")" != "1" ]; then echo "  offline_access: not offered to $cid (unchanged)"; continue; fi
    code="$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE -H "$AUTH" "$LIST/$OA_ID")"
    { [ "$code" = "204" ] && [ "$(has_scope "$LIST")" = "0" ]; } \
      || { echo "FATAL: detaching offline_access from $cid returned $code, or it is still offered"; exit 1; }
    echo "  offline_access: detached from $cid"
  done
  LIST="$KC/admin/realms/$REALM/default-optional-client-scopes"
  if [ "$(has_scope "$LIST")" != "1" ]; then
    echo "  offline_access: not a realm default optional scope (unchanged)"
  else
    code="$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE -H "$AUTH" "$LIST/$OA_ID")"
    { [ "$code" = "204" ] && [ "$(has_scope "$LIST")" = "0" ]; } \
      || { echo "FATAL: removing offline_access from the realm defaults returned $code, or it is still there"; exit 1; }
    echo "  offline_access: removed from the realm's default optional scopes"
  fi
fi

# E6: the master realm — brute-force protection (temporary lockout only) and events, from the
# template's own values. Only these fields; the rest of the master realm is preserved verbatim.
curl -sS -H "$AUTH" "$KC/admin/realms/master" > "$TMP/master.json"
python3 - "$EXPORT" "$TMP/master.json" > "$TMP/master-patched.json" <<'PY'
import json, sys
exp = json.load(open(sys.argv[1]))
rep = json.load(open(sys.argv[2]))
keys = ["bruteForceProtected", "failureFactor", "waitIncrementSeconds", "maxFailureWaitSeconds",
        "quickLoginCheckMilliSeconds", "minimumQuickLoginWaitSeconds", "maxDeltaTimeSeconds",
        "eventsEnabled", "eventsExpiration", "adminEventsEnabled", "adminEventsDetailsEnabled"]
rep.update({k: exp[k] for k in keys if k in exp})
rep["permanentLockout"] = False
json.dump(rep, sys.stdout)
PY
PUT="$(curl -sS -o /dev/null -w '%{http_code}' -X PUT -H "$AUTH" -H 'Content-Type: application/json' \
  --data @"$TMP/master-patched.json" "$KC/admin/realms/master")"
[ "$PUT" = "204" ] || { echo "FATAL: master realm PUT returned $PUT (expected 204)"; exit 1; }
curl -sS -H "$AUTH" "$KC/admin/realms/master" | python3 -c \
  "import sys,json;d=json.load(sys.stdin);print('  master: bruteForce=%s permanentLockout=%s events=%s adminEvents=%s' % (d.get('bruteForceProtected'), d.get('permanentLockout'), d.get('eventsEnabled'), d.get('adminEventsEnabled')))"

AFTER="$(curl -sS -H "$AUTH" "$KC/admin/realms/$REALM/users/count")"
echo "  users after:  $AFTER"
[ "$BEFORE" = "$AFTER" ] || { echo "  ✗ USER COUNT CHANGED ($BEFORE→$AFTER) — investigate immediately"; exit 1; }
echo "  ✓ users unchanged ($AFTER)"
echo ""
echo "Settings applied. If the running keycloak predates the theme volume mount, run:"
echo "    docker compose up -d keycloak      # mounts ./keycloak/themes/talus; realm + settings persist in the DB"
echo "DONE."
