#!/usr/bin/env bash
# talus-doctor.sh — a read-only report on this Talus install, for the day the portal cannot answer
# (decision-system-health.md §8). Admin → System and the Helper's diagnostics are served by the api,
# so a failure that keeps the api from serving — Docker not started, a full disk, the edge down, a
# certificate problem, a wrong DNS answer, a clock that no token survives — is invisible to them by
# construction. This script is the floor under that ceiling: what the administrator's own shell can
# see, read in one pass and printed in words, with the runbook's next step on every FAIL line.
#
# It changes nothing: no restart, no write, nothing sent anywhere. Every fact comes from docker,
# df/free/timedatectl, openssl and curl against 127.0.0.1, two read-only SELECTs through
# `docker compose exec postgres psql`, and the local resolver for this box's own name.
#
# THE REPORT IS MEANT TO BE PASTED (a support request, the Helper's question box), so every line is
# scrubbed before it prints: Talus AI tokens, PATs, activation codes, Fernet ciphertext, JWTs,
# Bearer/Basic credentials and any `<…password|secret|token|key>=<value>` become `***` — the same
# shapes the Helper's snapshot redacts. `.env` is checked for its KEY NAMES and its mode, never a
# value. The api-log check prints counts and event NAMES, never a record's fields.
#
# Usage:  scripts/talus-doctor.sh            (from the install directory; it cd's there itself)
#         scripts/talus-doctor.sh --scrub    (filter stdin through the scrub — the self-test uses it)
# Exit:   0 = nothing failed · 1 = at least one FAIL · 2 = usage
# bash 3.2-safe (no declare -A, mapfile, ${var,,}); needs docker, docker compose, jq, python3,
# openssl, curl — the same tools scripts/preflight.sh requires.

set -u

# ---------------------------------------------------------------------------------------------------
# The scrub: python3 so the patterns are the snapshot's own (BSD sed has no case-insensitive flag).
# Everything this script prints goes through it; `--scrub` exposes it for the self-test.
# ---------------------------------------------------------------------------------------------------
scrub() {
  python3 -c '
import re, sys
R = [
    (re.compile(r"\btai_[A-Za-z0-9_\-]{8,}"), "tai_***"),
    (re.compile(r"\btpat_[A-Za-z0-9_\-]{8,}"), "tpat_***"),
    (re.compile(r"\btac_[A-Za-z0-9_\-]{8,}"), "tac_***"),
    (re.compile(r"\bgAAAA[A-Za-z0-9_\-=]{20,}"), "***"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{6,}"), "jwt***"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._\-=+/]{8,}"), r"\1 ***"),
    (re.compile(r"(?i)\b([a-z0-9_\-]*(?:password|passwd|pwd|secret|token|api[_-]?key|encryption[_-]?key|private[_-]?key|key))\s*[=:]\s*[^\s,;&\"\x27}\]]+"), r"\1=***"),
]
for line in sys.stdin:
    for pat, rep in R:
        line = pat.sub(rep, line)
    sys.stdout.write(line)
'
}

if [ "${1:-}" = "--scrub" ]; then scrub; exit 0; fi
if [ $# -ne 0 ]; then echo "usage: scripts/talus-doctor.sh [--scrub]" >&2; exit 2; fi

cd "$(dirname "$0")/.." || exit 2

OK=0; WARN=0; FAIL=0
ok()   { printf '  ok    %s\n' "$1"; OK=$((OK+1)); }
warn() { printf '  WARN  %s\n' "$1"; WARN=$((WARN+1)); }
fail() { printf '  FAIL  %s\n' "$1"; FAIL=$((FAIL+1)); }
info() { printf '  info  %s\n' "$1"; }
na()   { printf '  n/a   %s\n' "$1"; }
head_() { printf '\n== %s ==\n' "$1"; }

# A value from .env, read into a variable only — never echoed. Used for NON-secret keys only
# (names, ports, the database user/name the psql reads need).
envval() { grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' ; }

# The whole report is assembled in a subshell and scrubbed once on the way out.
report() {

echo "talus-doctor — $(date -u '+%Y-%m-%d %H:%M:%SZ') — $(pwd)"
echo "(read-only; nothing was changed. Paste this whole report into a support request or the Helper.)"

# ---- 1. tools --------------------------------------------------------------------------------
head_ "tools"
for t in docker jq python3 openssl curl; do
  if command -v "$t" >/dev/null 2>&1; then ok "$t"; else fail "$t missing — install it (scripts/preflight.sh lists the hints)"; fi
done
if docker compose version >/dev/null 2>&1; then ok "docker compose"; else fail "docker compose (v2 plugin) missing"; fi
if docker info >/dev/null 2>&1; then ok "docker daemon answers"; else
  fail "docker daemon not answering — start Docker (systemctl start docker), then run docker compose up -d"
  echo; echo "summary: $OK ok · $WARN warn · $FAIL fail"; return
fi

# ---- 2. install directory ---------------------------------------------------------------------
head_ "install directory"
[ -f docker-compose.yml ] && ok "docker-compose.yml present" || fail "docker-compose.yml not found here — run from the Talus install directory"
if [ -f .env ]; then
  # GNU stat first: on Linux `stat -f` means "file-system status", so the BSD form does not fail
  # cleanly there — it prints a file-system block for .env and exits non-zero, and the fallback
  # appended the real mode to that block, so every Linux box warned ".env is mode   File: ..."
  # whatever the mode was (found on a Linux rehearsal; macOS never showed it).
  mode=$(stat -c '%a' .env 2>/dev/null || stat -f '%Lp' .env 2>/dev/null)
  if [ "$mode" = "600" ]; then ok ".env present, mode 600"; else warn ".env is mode ${mode:-?} — should be 600 (chmod 600 .env)"; fi
  # The keys scripts/generate-secrets.sh's manifest marks `warn` (a secret with a stateful counterpart
  # — nothing can mint it later) plus the two OpenFGA ids load-openfga-model.sh writes. Names only.
  missing=""
  for k in POSTGRES_PASSWORD TALUS_APP_PASSWORD TALUS_TILES_PASSWORD KEYCLOAK_ADMIN_PASSWORD KEYCLOAK_DB_PASSWORD \
           TALUS_API_CLIENT_SECRET TALUS_PROVISIONER_CLIENT_SECRET OPENFGA_DB_PASSWORD TALUS_ENCRYPTION_KEY \
           OPENFGA_STORE_ID OPENFGA_MODEL_ID; do
    grep -qE "^$k=." .env || missing="$missing $k"
  done
  if [ -z "$missing" ]; then ok ".env has every required key (names checked, values never read)"; else fail ".env is missing:$missing — the install did not finish; run scripts/talus-setup.sh again"; fi
else
  fail ".env not found — the install never completed; run scripts/talus-setup.sh"
fi
if grep -qE '^\s+build:' docker-compose.yml 2>/dev/null; then info "api image: built from source (a development tree, not a release)"
else
  pin=$(grep -oE 'talus-api[@:][^ ]+' docker-compose.yml 2>/dev/null | head -1)
  [ -n "$pin" ] && info "api image pinned: $pin" || warn "no talus-api image reference found in docker-compose.yml"
fi
[ -f talus-summary.txt ] && info "talus-summary.txt present (from the install, $(date -r talus-summary.txt '+%Y-%m-%d' 2>/dev/null))"

# ---- 3. services ------------------------------------------------------------------------------
head_ "services (docker compose ps)"
ps_json=$(docker compose --profile edge ps -a --format json 2>/dev/null)
if [ -z "$ps_json" ]; then
  fail "no containers for this project — run docker compose up -d (or scripts/talus-setup.sh)"
else
  for svc in postgres api caddy pg_tileserv keycloak keycloak-postgres openfga openfga-postgres; do
    line=$(printf '%s\n' "$ps_json" | jq -r --arg s "$svc" 'if type=="array" then .[] else . end | select(.Service==$s) | "\(.State)|\(.Health)|\(.Status)"' 2>/dev/null | head -1)
    state=${line%%|*}; rest=${line#*|}; health=${rest%%|*}; status=${rest#*|}
    case "$state" in
      running)
        case "$health" in
          healthy|"") ok "$svc running${health:+ ($health)} — $status" ;;
          starting)   warn "$svc running but health is starting — $status; give it a minute, then run this again" ;;
          *)          fail "$svc running but $health — docker compose logs $svc" ;;
        esac ;;
      "")  fail "$svc has no container — docker compose up -d" ;;
      *)   fail "$svc is $state — $status; run docker compose up -d $svc, then docker compose logs $svc if it stops again" ;;
    esac
  done
fi

# ---- 4. host ----------------------------------------------------------------------------------
head_ "host"
docker_root=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)
for p in "$(pwd)" "${docker_root:-/var/lib/docker}"; do
  [ -d "$p" ] || continue
  use=$(df -P "$p" 2>/dev/null | awk 'NR==2 {gsub("%","",$5); print $5}')
  avail=$(df -Ph "$p" 2>/dev/null | awk 'NR==2 {print $4}')
  if [ -z "$use" ]; then na "disk at $p"; elif [ "$use" -ge 95 ]; then fail "disk at $p is ${use}% used (${avail} free) — free space; Postgres and the api stop writing when it fills"
  elif [ "$use" -ge 85 ]; then warn "disk at $p is ${use}% used (${avail} free)"; else ok "disk at $p ${use}% used (${avail} free)"; fi
done
if command -v free >/dev/null 2>&1; then
  mem=$(free -m | awk '/^Mem:/ {printf "%d MB total, %d MB available", $2, $7}')
  availmb=$(free -m | awk '/^Mem:/ {print $7}')
  if [ "${availmb:-9999}" -lt 400 ]; then warn "memory: $mem — Talus wants about 4 GB; the api and Keycloak are the first to suffer"; else ok "memory: $mem"; fi
else na "memory (free not available on this OS)"; fi
if command -v timedatectl >/dev/null 2>&1; then
  sync=$(timedatectl show -p NTPSynchronized --value 2>/dev/null)
  case "$sync" in yes) ok "clock is NTP-synchronized" ;; no) warn "clock is NOT NTP-synchronized — sign-in tokens fail when the clock drifts (timedatectl set-ntp true)" ;; *) na "NTP status" ;; esac
else na "NTP status (timedatectl not available on this OS)"; fi

# ---- 5. the edge and the api through it -------------------------------------------------------
head_ "edge (caddy) and the api through it"
host=$(envval TALUS_HOSTNAME); https_port=$(envval HTTPS_PORT); https_port=${https_port:-443}; http_port=$(envval HTTP_PORT); http_port=${http_port:-80}
API_HEALTH=/health   # the api's liveness door, passed through by the edge's @api matcher (proxy/Caddyfile)
# The edge's site is `{$TALUS_HOSTNAME}` (proxy/Caddyfile), defaulted by compose to talus.localhost
# when .env does not set it — so that is the name to ask it for.
[ -n "$host" ] && info "configured name: $host" || info "TALUS_HOSTNAME is not set in .env — the edge serves its default name (talus.localhost) or this host's address (IP mode)"
sni=${host:-talus.localhost}
listening=0
for port in "$http_port" "$https_port"; do
  if (command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -qE "[:.]$port[[:space:]]") || \
     (command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1); then ok "port $port is listening"; listening=$((listening+1))
  else fail "nothing listens on port $port — is caddy up? docker compose --profile edge up -d caddy"; fi
done
code=$(curl -sk -o /dev/null -m 8 -w '%{http_code}' --resolve "$sni:$https_port:127.0.0.1" "https://$sni:$https_port/" 2>/dev/null)
case "$code" in
  200|301|302|304) ok "portal answers locally as $sni (HTTP $code)" ;;
  000|"")
    # The port listens but TLS never completed: caddy is up and has NO SITE for this name (its alert
    # reads "internal error"). The name in .env and the name the edge was configured with disagree.
    http_code=$(curl -s -o /dev/null -m 5 -w '%{http_code}' "http://127.0.0.1:$http_port/" 2>/dev/null)
    if [ "$listening" -eq 2 ] && [ "$http_code" != "000" ] && [ -n "$http_code" ]; then
      fail "the edge is up (HTTP $http_code on $http_port) but refuses TLS for the name '$sni' — it serves no site by that name; TALUS_HOSTNAME in .env and the edge's configured address disagree (re-run scripts/talus-setup.sh, or scripts/talus-setup.sh --hostname <name>)"
    else
      fail "portal does not answer on https://127.0.0.1:$https_port — caddy down or the port blocked; docker compose --profile edge ps caddy"
    fi ;;
  *) warn "portal answers HTTP $code locally as $sni" ;;
esac
if [ "$code" = "000" ] || [ -z "$code" ]; then
  na "api through the edge — skipped, the edge refused the name above"
else
  body=$(curl -sk -m 8 --resolve "$sni:$https_port:127.0.0.1" "https://$sni:$https_port$API_HEALTH" 2>/dev/null)
  acode=$(curl -sk -o /dev/null -m 8 -w '%{http_code}' --resolve "$sni:$https_port:127.0.0.1" "https://$sni:$https_port$API_HEALTH" 2>/dev/null)
  case "$acode" in
    200) ok "api through the edge: $body" ;;
    502|503|504) fail "api through the edge: HTTP $acode — the api is not serving; docker compose ps api, then docker compose logs api" ;;
    000|"") fail "api through the edge: no answer" ;;
    *) warn "api through the edge: HTTP $acode — $body" ;;
  esac
fi

# ---- 6. certificate ---------------------------------------------------------------------------
head_ "certificate (as served on $https_port for $sni)"
cert=$(printf '' | openssl s_client -connect "127.0.0.1:$https_port" -servername "$sni" 2>/dev/null | openssl x509 -noout -enddate -issuer 2>/dev/null)
if [ -z "$cert" ]; then
  fail "no certificate could be read from 127.0.0.1:$https_port"
else
  issuer=$(printf '%s\n' "$cert" | sed -n 's/^issuer=//p')
  enddate=$(printf '%s\n' "$cert" | sed -n 's/^notAfter=//p')
  end_epoch=$(python3 -c 'import sys,email.utils,datetime; s=sys.argv[1]
try:
    print(int(datetime.datetime.strptime(s, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=datetime.timezone.utc).timestamp()))
except Exception: print("")' "$enddate")
  now_epoch=$(date -u +%s)
  if [ -n "$end_epoch" ]; then
    days=$(( (end_epoch - now_epoch) / 86400 ))
    case "$issuer" in
      *"Caddy Local"*|*"Talus GIS"*|*"ECC Root"*)
        kind="built-in certificate authority"
        if [ "$end_epoch" -le "$now_epoch" ]; then fail "certificate EXPIRED ($enddate; $kind) — caddy renews its own leaves; docker compose --profile edge restart caddy, then check again"
        else ok "$kind leaf, renews itself (expires $enddate); browsers must trust the root — install runbook §5"; fi ;;
      *)
        kind="public certificate ($issuer)"
        if [ "$end_epoch" -le "$now_epoch" ]; then fail "certificate EXPIRED ($enddate; $kind) — renewal failed; ports 80/443 must be reachable from the internet; docker compose logs caddy"
        elif [ "$days" -le 14 ]; then warn "certificate expires in $days days ($kind) — renewal should already have happened; docker compose logs caddy"
        else ok "certificate valid for $days more days ($kind)"; fi ;;
    esac
  else
    info "certificate: $cert"
  fi
fi

# ---- 7. the name ------------------------------------------------------------------------------
head_ "name resolution"
if [ -z "$host" ]; then na "no TALUS_HOSTNAME (IP mode)"
elif printf '%s' "$host" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then info "$host is an address (IP mode); nothing to resolve"
else
  resolved=$(python3 -c 'import socket,sys
try: print(" ".join(sorted({a[4][0] for a in socket.getaddrinfo(sys.argv[1], None)})))
except Exception: print("")' "$host")
  if [ -z "$resolved" ]; then fail "$host does not resolve from this host — DNS: the A record is missing or not yet propagated"
  else
    local_addrs=$( (command -v ip >/dev/null 2>&1 && ip -o addr show 2>/dev/null | awk '{print $4}' | cut -d/ -f1) ; (command -v ifconfig >/dev/null 2>&1 && ifconfig 2>/dev/null | awk '/inet /{print $2}') )
    match=0; for a in $resolved; do printf '%s\n' "$local_addrs" | grep -qx "$a" && match=1; done
    if [ "$match" -eq 1 ]; then ok "$host → $resolved (one of this host's addresses)"
    else info "$host → $resolved — not an address on this host (normal behind NAT or a load balancer; wrong if this box should answer directly)"; fi
  fi
fi

# ---- 8. database reads (read-only) ------------------------------------------------------------
head_ "database (two read-only queries)"
pguser=$(envval POSTGRES_USER); pgdb=$(envval POSTGRES_DB)
psql_ro() { docker compose exec -T postgres psql -U "${pguser:-talus}" -d "${pgdb:-talusgis}" -tAX -v ON_ERROR_STOP=1 -c "$1" 2>/dev/null; }
if docker compose exec -T postgres pg_isready -q 2>/dev/null; then
  headv=$(psql_ro "SELECT version FROM public.flyway_schema_history WHERE success ORDER BY installed_rank DESC LIMIT 1")
  [ -n "$headv" ] && ok "migration head V$headv" || warn "migration head unreadable — the schema may not be migrated yet (the installer runs flyway)"
  dbnow=$(psql_ro "SELECT extract(epoch from now())::bigint")
  if [ -n "$dbnow" ]; then
    skew=$(( $(date -u +%s) - dbnow )); [ "$skew" -lt 0 ] && skew=$(( -skew ))
    if [ "$skew" -ge 60 ]; then fail "host and database clocks are ${skew}s apart — sign-in tokens fail past about a minute; fix the host clock"
    else ok "host and database clocks agree (${skew}s)"; fi
  fi
  last=$(psql_ro "SELECT to_char(created_at AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI')||'|'||coalesce(target,'?')||'|'||extract(epoch from now()-created_at)::bigint FROM platform.backups ORDER BY created_at DESC LIMIT 1")
  if [ -z "$last" ]; then warn "no backup has ever been recorded — Admin → System → Backups, or the upgrade runbook's first step"
  else
    when=${last%%|*}; r=${last#*|}; target=${r%%|*}; age=${r##*|}; days=$(( age / 86400 ))
    if [ "$days" -ge 7 ]; then warn "last backup ${days} days ago ($when UTC, to $target) — check Admin → System"
    else ok "last backup $when UTC, to $target"; fi
  fi
else
  fail "postgres is not accepting connections — docker compose ps postgres; docker compose logs postgres (a full disk is the usual cause)"
fi

# ---- 9. recent api log ------------------------------------------------------------------------
head_ "api log, last 24 h (counts and event names only)"
logs=$(docker compose logs --since 24h --no-log-prefix api 2>/dev/null)
if [ -z "$logs" ]; then info "no api log lines in the last 24 h (the api may not be running)"
else
  # JSON lines (production) carry "level": "…"; the development console renderer prints [level ].
  nerr=$(printf '%s\n' "$logs" | grep -cE '"level": ?"(error|critical)"|\[(error|critical) *\]')
  nwarn=$(printf '%s\n' "$logs" | grep -cE '"level": ?"warning"|\[warning *\]')
  events=$(printf '%s\n' "$logs" | python3 -c '
import sys, json, re
out = []
for line in sys.stdin:
    line = line.strip()
    ev = None
    if line.startswith("{"):
        try:
            d = json.loads(line)
            if d.get("level") in ("warning", "error", "critical"): ev = d.get("event")
        except Exception: pass
    else:
        m = re.search(r"\[(warning|error|critical)\s*\]\s+(\S+)", line)
        if m: ev = m.group(2)
    if ev: out.append(str(ev))
print(", ".join(out[-5:]))')
  if [ "$nerr" -gt 0 ]; then warn "$nerr error and $nwarn warning lines in 24 h; last events: ${events:-?} — Admin → System and the Helper (Include diagnostics) have the details"
  elif [ "$nwarn" -gt 0 ]; then info "$nwarn warning lines, no errors, in 24 h; last events: ${events:-?}"
  else ok "no warnings or errors in the api log in 24 h"; fi
fi

echo
echo "summary: $OK ok · $WARN warn · $FAIL fail"
[ "$FAIL" -gt 0 ] && echo "RESULT: ATTENTION — fix the FAIL lines above (each says what to do); re-run this script after." || echo "RESULT: OK"
}

out=$(report)
rc=0
printf '%s\n' "$out" | scrub
printf '%s\n' "$out" | grep -qE '^  FAIL ' && rc=1
exit $rc
