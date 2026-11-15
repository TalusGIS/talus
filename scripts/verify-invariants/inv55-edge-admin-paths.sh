#!/usr/bin/env bash
# Invariant (once-over S2, decision-once-over-edge-deployment.md E1) — the sign-in server's OWN
# administration can be closed at the edge, and closing it never touches sign-in.
#
# proxy/Caddyfile answers Keycloak's admin console, admin API and `master` realm only to the
# addresses in KEYCLOAK_ADMIN_ALLOW_FROM. The restriction is OFF BY DEFAULT:
# the default list is every address. This script proves both halves on the edge
# configuration that ships:
#   ON  — a caller who is not on the list gets 404 on every spelling of those paths, while the
#         portal realm's paths and the root redirect are untouched;
#   OFF — with the default, and with the setting absent, nothing is refused.
#
# SELF-CONTAINED: it never touches the running stack. It starts a throwaway container of the
# edge image pinned in docker-compose.yml, with the real proxy/Caddyfile and no sign-in server
# behind it, on a loopback port. So "passed on to the sign-in server" reads as 502 here (the
# upstream is absent) and "refused at the edge" as 404 — two different answers, which is what
# lets the legs tell them apart. bash 3.2-safe.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv55: FAIL — cd"; exit 2; }

command -v docker >/dev/null 2>&1 || { echo "inv55: SKIP — docker not available on this box"; exit 3; }
command -v curl >/dev/null 2>&1 || { echo "inv55: SKIP — curl not available on this box"; exit 3; }
IMG="$(grep -oE 'caddy@sha256:[0-9a-f]{64}' docker-compose.yml | head -1)"
[ -n "$IMG" ] || { echo "inv55: FAIL — no digest-pinned caddy image found in docker-compose.yml"; exit 1; }
docker image inspect "$IMG" >/dev/null 2>&1 || {
  echo "inv55: SKIP — the pinned edge image is not on this box (the stack has never been pulled here)"; exit 3; }
[ -f proxy/Caddyfile ] || { echo "inv55: FAIL — proxy/Caddyfile missing"; exit 1; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/inv55.XXXXXX")" || { echo "inv55: FAIL — mktemp"; exit 2; }
mkdir -p "$WORK/www" "$WORK/config"; echo ok > "$WORK/www/index.html"
NAME=""
cleanup() {
  [ -n "$NAME" ] && docker rm -f "$NAME" >/dev/null 2>&1
  rm -rf "$WORK"
}
trap cleanup EXIT

AUTH_HOST="auth.localhost"
REFUSED_PATHS="/admin /admin/ /admin/master/console/ /admin/realms/talus/users /realms/master /realms/master/protocol/openid-connect/token /REALMS/MASTER/x /Admin/ /%61dmin/ /realms/%6daster/x"
# Keycloak drops a matrix parameter (";x") from a path segment before routing, so these ARE the
# admin API and the master realm to it — and must be refused too (measured on 26.0.8).
REFUSED_PATHS="$REFUSED_PATHS /realms/master;x/protocol/openid-connect/token /realms;x/master/x /admin;x/realms/talus/users /admin;/master/console/ /realms/master%3Bx/x /realms/talus;x/y"
# A realm whose name merely BEGINS with "master" must not be caught by the rule. Its path is put
# together here, not written out, because inv33 reads every /realms/<name> written in the tree as
# a claim about which realm this product uses (and would, rightly, object to a second one).
SIBLING_REALM_PATH="/realms/master""x/y"
OPEN_PATHS="/realms/talus/.well-known/openid-configuration /realms/talus/protocol/openid-connect/token $SIBLING_REALM_PATH /resources/x /administrator"
# (once-over 2026-11-15 E1) Paths whose RAW form, before decoding, holds an encoded slash or
# backslash or a dot segment. The `path` matcher reads them decoded and cleaned, so
# `/realms/x%2F../../master/…` read as `/master/…` and was passed on as it arrived. Sent with
# --path-as-is: without it curl itself would clean the dot segments before sending.
# Each odd path that begins with a name other than this product's realm is put together, not written
# out, for the same reason as SIBLING_REALM_PATH above (inv33 reads /realms/<name> as a claim).
ODD_PATHS="/realms/""x%2F../../master/.well-known/openid-configuration /realms/""x%2f../../master/x /realms/""x%5C..%5C..%5Cmaster/x /realms/talus/../master/x /realms/talus/./protocol/openid-connect/certs"
# Sign-in's own encoded redirect address is in the QUERY, which the rule never reads.
OPEN_QUERY_PATH="/realms/talus/protocol/openid-connect/auth?client_id=talus-portal&redirect_uri=https%3A%2F%2Ftalus.localhost%2F"

fail=0
PORT=""

start_edge() {  # $1 = how to pass the setting: "unset" | "value:<text>"
  NAME="tverify-inv55-$$-$RANDOM"
  if [ "$1" = "unset" ]; then
    docker run -d --rm --name "$NAME" -p 127.0.0.1::443 \
      -v "$ROOT/proxy/Caddyfile:/etc/caddy/Caddyfile:ro" -v "$WORK/www:/srv/www:ro" -v "$WORK/config:/srv/config:ro" \
      --tmpfs /servicelogs \
      -e TALUS_HOSTNAME=talus.localhost -e "KEYCLOAK_HOSTNAME=$AUTH_HOST" -e PORTAL_ORIGIN=https://talus.localhost \
      -e "TALUS_TLS_DIRECTIVE=tls internal" -e TALUS_CADDY_GLOBAL= \
      "$IMG" >/dev/null 2>"$WORK/err.txt"
  else
    docker run -d --rm --name "$NAME" -p 127.0.0.1::443 \
      -v "$ROOT/proxy/Caddyfile:/etc/caddy/Caddyfile:ro" -v "$WORK/www:/srv/www:ro" -v "$WORK/config:/srv/config:ro" \
      --tmpfs /servicelogs \
      -e TALUS_HOSTNAME=talus.localhost -e "KEYCLOAK_HOSTNAME=$AUTH_HOST" -e PORTAL_ORIGIN=https://talus.localhost \
      -e "TALUS_TLS_DIRECTIVE=tls internal" -e TALUS_CADDY_GLOBAL= \
      -e "KEYCLOAK_ADMIN_ALLOW_FROM=${1#value:}" \
      "$IMG" >/dev/null 2>"$WORK/err.txt"
  fi
  if [ $? -ne 0 ]; then
    echo "  FAIL — the throwaway edge did not start: $(head -3 "$WORK/err.txt")"; NAME=""; return 1
  fi
  PORT=""
  tries=0
  while [ "$tries" -lt 20 ]; do
    PORT="$(docker port "$NAME" 443/tcp 2>/dev/null | head -1 | sed 's/.*://')"
    if [ -n "$PORT" ]; then
      code="$(curl -sk -o /dev/null -w '%{http_code}' --resolve "$AUTH_HOST:$PORT:127.0.0.1" "https://$AUTH_HOST:$PORT/" --max-time 3)"
      [ "$code" = "302" ] && return 0
    fi
    tries=$((tries+1)); sleep 0.5
  done
  echo "  FAIL — the throwaway edge never answered (its error log follows)"
  docker logs "$NAME" 2>&1 | grep -i '"level":"error"' | head -5
  return 1
}

stop_edge() { [ -n "$NAME" ] && docker rm -f "$NAME" >/dev/null 2>&1; NAME=""; }

get() { curl -sk -o /dev/null -w '%{http_code}' --resolve "$AUTH_HOST:$PORT:127.0.0.1" "https://$AUTH_HOST:$PORT$1" --max-time 6; }
get_raw() { curl -sk --path-as-is -o /dev/null -w '%{http_code}' --resolve "$AUTH_HOST:$PORT:127.0.0.1" "https://$AUTH_HOST:$PORT$1" --max-time 6; }

expect_raw() {  # $1 = leg label, $2 = expected status, $3 = the paths (sent as they are)
  for p in $3; do
    got="$(get_raw "$p")"
    if [ "$got" = "$2" ]; then
      echo "  [$1] $p -> $got (expect $2)"
    else
      echo "  [$1] $p -> $got (expect $2) — FAIL"
      [ "$2" = "404" ] && [ "$got" != "404" ] && echo "       LEAKED ALLOW: the edge passed an odd path on to the sign-in server"
      fail=1
    fi
  done
}

expect_all() {  # $1 = leg label, $2 = expected status, $3 = the paths
  for p in $3; do
    got="$(get "$p")"
    if [ "$got" = "$2" ]; then
      echo "  [$1] $p -> $got (expect $2)"
    else
      echo "  [$1] $p -> $got (expect $2) — FAIL"
      [ "$2" = "404" ] && [ "$got" != "404" ] && echo "       LEAKED ALLOW: the edge passed an administration path on to the sign-in server"
      fail=1
    fi
  done
}

echo "inv55: the pinned edge image with the shipped proxy/Caddyfile, no sign-in server behind it"
echo "       404 = refused at the edge; 502 = passed on (the sign-in server is absent here)"

# ---- 1. ON: the caller is not on the list -------------------------------------------------
echo "inv55 leg 1 — the list names one other address (203.0.113.7): this caller is refused"
if start_edge "value:203.0.113.7"; then
  expect_all "1 refused" 404 "$REFUSED_PATHS"
  echo "inv55 leg 2 — …and sign-in is untouched by the same setting"
  expect_all "2 open" 502 "$OPEN_PATHS"
  got="$(get /)"; [ "$got" = "302" ] && echo "  [2 open] / -> 302 (expect 302, the redirect to the portal)" || { echo "  [2 open] / -> $got (expect 302) — FAIL"; fail=1; }
  echo "inv55 leg 2b — encoded-slash and dot-segment spellings are refused too (once-over 2026-11-15 E1)"
  expect_raw "2b refused" 404 "$ODD_PATHS"
  got="$(get_raw "$OPEN_QUERY_PATH")"
  if [ "$got" = "502" ]; then echo "  [2b open] sign-in with an encoded redirect in its query -> 502 (expect 502)"
  else echo "  [2b open] sign-in with an encoded redirect in its query -> $got (expect 502) — FAIL"; fail=1; fi
else
  fail=1
fi
stop_edge

# ---- 3. the caller IS on the list: the control that makes leg 1 mean something -------------
echo "inv55 leg 3 — control: private_ranges is on the list, and this caller's address is private"
if start_edge "value:private_ranges 203.0.113.7"; then
  expect_all "3 allowed" 502 "/admin/ /admin/master/console/ /realms/master/x"
else
  fail=1
fi
stop_edge

# ---- 4. OFF by default ----------------------------------------------------------------------
DEFAULT="$(grep -E '^[[:space:]]+KEYCLOAK_ADMIN_ALLOW_FROM:' docker-compose.yml | sed -E 's/.*:-([^}]*)\}.*/\1/')"
echo "inv55 leg 4 — the default docker-compose.yml hands the edge (\"$DEFAULT\"): nobody is refused"
if [ -z "$DEFAULT" ]; then
  echo "  FAIL — docker-compose.yml does not pass KEYCLOAK_ADMIN_ALLOW_FROM to the edge with a default"; fail=1
elif start_edge "value:$DEFAULT"; then
  expect_all "4 default" 502 "/admin/ /admin/master/console/ /realms/master/x /realms/talus/x"
  expect_raw "4 default" 502 "$ODD_PATHS"
else
  fail=1
fi
stop_edge

echo "inv55 leg 5 — the setting absent altogether (the Caddyfile's own default): nobody is refused"
if start_edge "unset"; then
  expect_all "5 absent" 502 "/admin/ /realms/master/x"
else
  fail=1
fi
stop_edge

if [ "$fail" -ne 0 ]; then echo "inv55: FAIL"; exit 1; fi
echo "inv55: PASS"
