#!/usr/bin/env bash
# Invariant — self-service password change proves POSSESSION, and an app password can never
# become account takeover (decision-first-ten-minutes.md §3):
#
#   [1] anonymous POST /me/password                 -> 401 (never reachable unauthenticated)
#   [2] a REAL, minted PAT sent as Basic            -> 401 on /me/password, while the SAME
#       credential is 200 on /ogc-auth/collections  (the sharp leg: the PAT is genuinely valid,
#       and it is still refused here — get_current_user is Bearer-only, so a leaked app password
#       cannot rotate the account credential out from under its owner)
#   [3] Bearer + WRONG currentPassword              -> 401/403, and the OLD password STILL WORKS
#       (the refusal is real, not cosmetic: proven by re-minting a token with it afterwards)
#   [4] Bearer + policy-violating newPassword       -> 400, and the OLD password STILL WORKS
#       (three probes: too short · no digit · equals the username — the realm policy is
#       length(12)+upper+lower+digit+notUsername+notEmail)
#   [5] the endpoint takes NO user id — a body naming another user changes nothing about who is
#       affected (structural fence; the admin reset endpoint owns cross-user resets)
#   [6] positive control: correct currentPassword + compliant newPassword -> 204, the NEW
#       password mints a token, and the response body carries NO password string
#
# WHY [3]/[4] re-mint instead of trusting the status code: a 4xx that nonetheless wrote the
# password would be the worst possible outcome (owner locked out by a rejected request), and a
# status code alone cannot see that. Verify, don't assert from a glance.
#
# NOTE (brute force): the realm is bruteForceProtected with failureFactor 10. Leg [3] spends ONE
# failed attempt against a THROWAWAY principal that is deleted at teardown, so it can never lock
# out a real account. Do not add retry loops here.
#
# PRE-BUILD BASELINE (negative-test-first, decision doc §3): with no endpoint deployed every leg
# returns 404/405, so [1]-[5] "pass vacuously" and [6] FAILS. That is the expected red state —
# the suite is green on this leg only once the endpoint exists AND refuses correctly.
#
# Needs the running stack + .env (throwaways self-provisioned via _principals.sh). bash 3.2-safe; jq-free.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv27: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
URL="$API/me/password"
fail=0

jget(){ python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh

echo "inv27: self-service password change = $URL"

# A compliant password for the positive leg: upper+lower+digit, 24 chars, not the username.
NEWPW="Tv27Bb-$(openssl rand -hex 12)"

body() { # <current> <new>
  python3 -c "import json,sys;print(json.dumps({'currentPassword':sys.argv[1],'newPassword':sys.argv[2]}))" "$1" "$2"
}

post() { # <auth-header-or-empty> <json-body> -> prints http_code, sets BODY
  _hdr="$1"; _b="$2"
  if [ -n "$_hdr" ]; then
    BODY="$(curl -sS -o /tmp/inv27.out -w '%{http_code}' -X POST "$URL" \
      -H "$_hdr" -H 'Content-Type: application/json' -d "$_b")"
  else
    BODY="$(curl -sS -o /tmp/inv27.out -w '%{http_code}' -X POST "$URL" \
      -H 'Content-Type: application/json' -d "$_b")"
  fi
  printf '%s' "$BODY"
}

# A token minted from a username+password — the "does this password still work?" oracle.
mint_with() { # <username> <password>
  curl -sS -X POST "$TV_KC/realms/$TV_REALM/protocol/openid-connect/token" \
    -d grant_type=password -d client_id="$TV_CLIENT" -d "client_secret=$TV_SECRET" \
    -d username="$1" -d "password=$2" | tv_acc
}

tv_mint viewer || { echo "inv27: FAIL — no viewer principal"; exit 1; }
U="$TV_VIEWER_USER"; PW="$TV_VIEWER_PW"; TOK="$TV_VIEWER_TOKEN"

# --- [1] anonymous ------------------------------------------------------------------
rc="$(post "" "$(body "$PW" "$NEWPW")")"
echo "  [1] anon POST -> $rc (expect 401)"
case "$rc" in
  401) : ;;
  2*)  echo "      FAIL: an ANONYMOUS caller changed a password ($rc)"; fail=1 ;;
  404|405) echo "      (endpoint absent — pre-build baseline)" ;;
  *)   echo "      FAIL: expected 401, got $rc"; fail=1 ;;
esac

# --- [2] a real PAT, as Basic -------------------------------------------------------
PATJSON="$(curl -sS -X POST "$API/me/tokens" -H "Authorization: Bearer $TOK" \
  -H 'Content-Type: application/json' -d '{"label":"inv27-probe","scope":"read_write"}')"
PAT="$(printf '%s' "$PATJSON" | jget "d.get('token','')")"
if [ -z "$PAT" ]; then
  echo "  [2] FAIL — could not mint a PAT (needed for the sharp leg); body: $(printf '%s' "$PATJSON" | head -c 200)"
  fail=1
else
  PATB64="$(printf 'talus:%s' "$PAT" | base64 | tr -d '\n')"
  # positive control FIRST: the credential really is valid on the surface PATs ride.
  ok="$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Basic $PATB64" "$API/ogc-auth/collections")"
  echo "  [2a] PAT on /ogc-auth/collections -> $ok (expect 200 — the PAT is genuinely valid)"
  [ "$ok" = "200" ] || { echo "      FAIL: the PAT is not valid where it should be; leg [2b] would prove nothing"; fail=1; }
  rc="$(post "Authorization: Basic $PATB64" "$(body "$PW" "$NEWPW")")"
  echo "  [2b] PAT on /me/password -> $rc (expect 401)"
  case "$rc" in
    401) : ;;
    2*)  echo "      FAIL: an APP PASSWORD rotated the ACCOUNT password — takeover path ($rc)"; fail=1 ;;
    404|405) echo "      (endpoint absent — pre-build baseline)" ;;
    *)   echo "      FAIL: expected 401, got $rc"; fail=1 ;;
  esac
fi

# --- [3] wrong current password -----------------------------------------------------
rc="$(post "Authorization: Bearer $TOK" "$(body "definitely-not-$PW" "$NEWPW")")"
echo "  [3] Bearer + WRONG currentPassword -> $rc (expect 401/403)"
case "$rc" in
  401|403) : ;;
  2*) echo "      FAIL: the current password was NOT verified — a lifted token is a takeover ($rc)"; fail=1 ;;
  404|405) echo "      (endpoint absent — pre-build baseline)" ;;
  *)  echo "      FAIL: expected 401/403, got $rc"; fail=1 ;;
esac
still="$(mint_with "$U" "$PW")"
if [ -n "$still" ]; then
  echo "      old password still works (the refusal did not write)"
else
  echo "      FAIL: a REJECTED request changed the password anyway — owner locked out"; fail=1
fi

# --- [4] policy-violating new passwords ---------------------------------------------
for bad in "Short1a" "NoDigitsAtAllHere" "$U"; do
  rc="$(post "Authorization: Bearer $TOK" "$(body "$PW" "$bad")")"
  echo "  [4] Bearer + weak newPassword ($(printf '%s' "$bad" | head -c 12)…) -> $rc (expect 400)"
  case "$rc" in
    400|422) : ;;
    2*) echo "      FAIL: the realm password policy was bypassed ($rc)"; fail=1 ;;
    404|405) echo "      (endpoint absent — pre-build baseline)" ;;
    *)  echo "      FAIL: expected 400/422, got $rc"; fail=1 ;;
  esac
done
still="$(mint_with "$U" "$PW")"
[ -n "$still" ] || { echo "      FAIL: a policy-rejected request changed the password anyway"; fail=1; }

# --- [5] no user id is accepted -----------------------------------------------------
# A body naming another principal must not steer the write. Extra keys are ignored by the model;
# what matters is that the ADMIN's password is untouched afterwards.
tv_mint admin || { echo "inv27: FAIL — no admin principal"; exit 1; }
OTHER_U="$TV_ADMIN_USER"; OTHER_PW="$TV_ADMIN_PW"
steer="$(python3 -c "import json,sys;print(json.dumps({'currentPassword':sys.argv[1],'newPassword':sys.argv[2],'userId':sys.argv[3],'username':sys.argv[4],'sub':sys.argv[3]}))" \
  "$PW" "$NEWPW" "$TV_ADMIN_ID" "$OTHER_U")"
rc="$(post "Authorization: Bearer $TOK" "$steer")"
echo "  [5] Bearer + body naming ANOTHER user -> $rc"
other_tok="$(mint_with "$OTHER_U" "$OTHER_PW")"
if [ -n "$other_tok" ]; then
  echo "      the other principal's password is untouched (no user id is honoured)"
else
  echo "      FAIL: a body-supplied user id steered the write — cross-user password change"; fail=1
fi

# --- [6] positive control -----------------------------------------------------------
# Leg [5] may legitimately have changed THIS caller's own password (it carried a valid pair).
# Establish the current one before the final leg rather than assuming.
CUR="$PW"
if [ -z "$(mint_with "$U" "$PW")" ] && [ -n "$(mint_with "$U" "$NEWPW")" ]; then CUR="$NEWPW"; fi
FINALPW="Tv27Cc-$(openssl rand -hex 12)"
TOK2="$(mint_with "$U" "$CUR")"
rc="$(post "Authorization: Bearer $TOK2" "$(body "$CUR" "$FINALPW")")"
echo "  [6] Bearer + correct pair -> $rc (expect 204)"
case "$rc" in
  204|200) : ;;
  *) echo "      FAIL: a legitimate password change was refused ($rc): $(head -c 200 /tmp/inv27.out)"; fail=1 ;;
esac
if [ -n "$(mint_with "$U" "$FINALPW")" ]; then
  echo "      the NEW password mints a token (and no forced reset blocks it)"
else
  echo "      FAIL: the change reported success but the new password does not work"; fail=1
fi
# The response must never echo a password.
for probe in "$FINALPW" "$CUR"; do
  if grep -qF "$probe" /tmp/inv27.out 2>/dev/null; then
    echo "      FAIL: a password string appears in the response body"; fail=1
  fi
done
rm -f /tmp/inv27.out

if [ "$fail" -eq 0 ]; then echo "inv27: PASS"; else echo "inv27: FAIL"; fi
exit "$fail"
