#!/usr/bin/env bash
# Invariant — the public-sharing MASTER toggle (decision-public-sharing-reachable.md).
#
# This slice moves control of anonymous data exposure from an environment variable to a web form.
# That IS the security content of the slice, so this leg proves the control by REQUEST, never by
# reading a flag: the assertions below fetch actual public content and check whether bytes come
# back.
#
#   [1]  anon    GET  /org-settings/public-sharing -> 401   (anon cannot read the setting)
#   [2]  anon    PUT  /org-settings/public-sharing -> 401
#   [3]  viewer  PUT  /org-settings/public-sharing -> 403   (threat 1: non-admin cannot flip it)
#   [4]  admin   PUT {enabled:true}  -> 200, enabled=true
#   [5]  anon    GET the public fixture map           -> 200 (positive control: ON really serves)
#   [6]  admin   PUT {enabled:false} -> 200, enabled=false
#   [7]  anon    GET the SAME map                     -> 404 (threat 3: OFF stops serving an
#        already-public item — the READ gate, not just the write gate)
#   [8]  anon    GET /public/config  -> publicAnonAccessEnabled=false while OFF
#   [9]  anon    GET /open-data/dcat -> empty dataset list while OFF (the Open Data half)
#   [10] admin   PUT the fixture map's shares publicAccess=true while OFF -> 403 (the WRITE gate)
#   [11] UPGRADE ROW (threat 4): column NULL + env -> resolves to the ENV value, so an existing
#        public deployment is not silently un-published by V092. Set NULL by psql (there is no API
#        that writes NULL — the admin control is deliberately two-state), then prove the resolved
#        value by anon request and compare against PUBLIC_ANON_ACCESS_ENABLED in ./.env. Whichever
#        of the decision doc's two NULL rows this box embodies is the one proven; the leg says which.
#   [12] threat 6 (divergence): no call site outside the resolver module reads
#        settings.public_anon_access_enabled directly, so the read gate and the write gate can
#        never resolve differently.
#
# FIXTURE: the leg mints an admin, creates its own throwaway web map and shares it publicly, and
# tears both down in leg_cleanup — so it proves the gate against a REAL public item on any box,
# clean or dev, without depending on pre-existing content. The original toggle state is captured
# up front and restored in leg_cleanup.
#
# Needs the running stack + ./.env (throwaways self-provisioned via _principals.sh).
# bash 3.2-safe; jq-free.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv29: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
URL="$API/org-settings/public-sharing"
jget(){ python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }
fail=0

# State the leg mutates; leg_cleanup puts every one of them back.
INV29_MAP=""          # fixture web map id
INV29_ORIG=""         # the toggle's value before this leg ran ('True'/'False'), for restore
INV29_ORIG_COL=""     # the raw column before this leg ran ('t'/'f'/'' for NULL), for restore

# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh

psql_q(){ # <sql> -> tuples-only single value
  docker compose exec -T postgres psql -U "${TV_PGUSER:-talus}" -d "${TV_PGDB:-talusgis}" -tA \
    -c "$1" < /dev/null 2>/dev/null | tr -d '[:space:]'
}

leg_cleanup(){
  # Restore the column EXACTLY as found (NULL stays NULL — the upgrade row must not be
  # manufactured by this leg), then drop the fixture map.
  if [ -n "$INV29_ORIG_COL" ] || [ "$INV29_ORIG_COL" = "" ]; then
    case "$INV29_ORIG_COL" in
      t) psql_q "UPDATE platform.org_settings SET public_sharing_enabled = true WHERE id = true;" >/dev/null ;;
      f) psql_q "UPDATE platform.org_settings SET public_sharing_enabled = false WHERE id = true;" >/dev/null ;;
      *) psql_q "UPDATE platform.org_settings SET public_sharing_enabled = NULL WHERE id = true;" >/dev/null ;;
    esac
  fi
  if [ -n "$INV29_MAP" ] && [ -n "${TV_ADMIN_TOKEN:-}" ]; then
    curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $TV_ADMIN_TOKEN" "$API/web-maps/$INV29_MAP"
  fi
}

# ---- [1][2] anonymous ------------------------------------------------------------------------
rc=$(curl -sS -o /dev/null -w '%{http_code}' "$URL")
echo "  [1] anon GET  -> $rc (expect 401)"
case "$rc" in
  401) : ;;
  2*) echo "      FAIL: anon can read the public-sharing setting ($rc)"; fail=1 ;;
  *)  echo "      FAIL: expected 401, got $rc"; fail=1 ;;
esac

rc=$(curl -sS -o /dev/null -w '%{http_code}' -X PUT -H 'Content-Type: application/json' \
     -d '{"enabled":true}' "$URL")
echo "  [2] anon PUT  -> $rc (expect 401)"
case "$rc" in
  401) : ;;
  2*) echo "      FAIL: ANON FLIPPED THE MASTER SWITCH ($rc)"; fail=1 ;;
  *)  echo "      FAIL: expected 401, got $rc"; fail=1 ;;
esac

# ---- [3] authenticated NON-admin (threat 1) ---------------------------------------------------
tv_mint viewer || { echo "inv29: FAIL — no viewer token"; exit 1; }
rc=$(curl -sS -o /dev/null -w '%{http_code}' -X PUT -H "Authorization: Bearer $TV_VIEWER_TOKEN" \
     -H 'Content-Type: application/json' -d '{"enabled":true}' "$URL")
echo "  [3] viewer PUT -> $rc (expect 403)"
case "$rc" in
  403) : ;;
  2*) echo "      FAIL: a NON-ADMIN turned public sharing on ($rc)"; fail=1 ;;
  *)  echo "      FAIL: expected 403, got $rc"; fail=1 ;;
esac

# ---- admin + fixture --------------------------------------------------------------------------
tv_mint admin || { echo "inv29: FAIL — no admin token"; exit 1; }
ADM="$TV_ADMIN_TOKEN"
tv_init || exit 1

INV29_ORIG_COL="$(psql_q "SELECT CASE WHEN public_sharing_enabled IS NULL THEN '' ELSE public_sharing_enabled::text END FROM platform.org_settings WHERE id = true;")"
INV29_ORIG="$(curl -sS -H "Authorization: Bearer $ADM" "$URL" | jget 'd["enabled"]')"
echo "  ... captured original state: column='${INV29_ORIG_COL:-NULL}' resolved=${INV29_ORIG:-?}"

# The toggle must be ON to create a public fixture (the write gate is the thing under test).
curl -sS -o /dev/null -X PUT -H "Authorization: Bearer $ADM" -H 'Content-Type: application/json' \
  -d '{"enabled":true}' "$URL"

MAP_BODY='{"title":"inv29 public sharing fixture","definition":{"version":1,"basemap":"light","operationalLayers":[],"initialView":{"center":[-83.0,42.3],"zoom":10}}}'
INV29_MAP="$(curl -sS -X POST -H "Authorization: Bearer $ADM" -H 'Content-Type: application/json' \
             -d "$MAP_BODY" "$API/web-maps" | jget 'd["id"]')"
if [ -z "$INV29_MAP" ]; then
  echo "inv29: FAIL — could not create the fixture web map (admin POST /web-maps)"
  exit 1
fi
sc=$(curl -sS -o /dev/null -w '%{http_code}' -X PUT -H "Authorization: Bearer $ADM" \
     -H 'Content-Type: application/json' -d '{"organizationOnly":false,"groups":[],"publicAccess":true}' \
     "$API/web-maps/$INV29_MAP/shares")
if [ "$sc" != "200" ]; then
  echo "inv29: FAIL — could not share the fixture map publicly (PUT shares -> $sc)"
  exit 1
fi
echo "  ... fixture public web map $INV29_MAP created + shared"

# ---- [4][5] ON serves ------------------------------------------------------------------------
BODY="$(curl -sS -X PUT -H "Authorization: Bearer $ADM" -H 'Content-Type: application/json' \
        -d '{"enabled":true}' "$URL")"
EN="$(printf '%s' "$BODY" | jget 'd["enabled"]')"
echo "  [4] admin PUT enabled=true -> enabled=${EN:-?} (expect True)"
[ "$EN" = "True" ] || { echo "      FAIL: toggle did not turn on ($(printf '%.200s' "$BODY"))"; fail=1; }

rc=$(curl -sS -o /dev/null -w '%{http_code}' "$API/web-maps/$INV29_MAP")
echo "  [5] anon GET the public map (toggle ON) -> $rc (expect 200)"
[ "$rc" = "200" ] || { echo "      FAIL: ON does not actually serve public content ($rc)"; fail=1; }

# ---- [6][7] OFF stops serving (threat 3) ------------------------------------------------------
BODY="$(curl -sS -X PUT -H "Authorization: Bearer $ADM" -H 'Content-Type: application/json' \
        -d '{"enabled":false}' "$URL")"
EN="$(printf '%s' "$BODY" | jget 'd["enabled"]')"
echo "  [6] admin PUT enabled=false -> enabled=${EN:-?} (expect False)"
[ "$EN" = "False" ] || { echo "      FAIL: toggle did not turn off ($(printf '%.200s' "$BODY"))"; fail=1; }

rc=$(curl -sS -o /dev/null -w '%{http_code}' "$API/web-maps/$INV29_MAP")
echo "  [7] anon GET the SAME map (toggle OFF) -> $rc (expect 404)"
case "$rc" in
  404) : ;;
  2*) echo "      FAIL: AN ALREADY-PUBLIC ITEM IS STILL SERVED AFTER THE ADMIN TURNED SHARING OFF ($rc)"; fail=1 ;;
  *)  echo "      FAIL: expected 404, got $rc"; fail=1 ;;
esac

# ---- [8] the anon bootstrap agrees ------------------------------------------------------------
PC="$(curl -sS "$API/public/config" | jget 'd["publicAnonAccessEnabled"]')"
echo "  [8] anon /public/config publicAnonAccessEnabled -> ${PC:-?} (expect False)"
[ "$PC" = "False" ] || { echo "      FAIL: the anon bootstrap still advertises a public face"; fail=1; }

# ---- [9] the Open Data half -------------------------------------------------------------------
DS="$(curl -sS "$API/open-data/dcat" | jget 'len(d.get("dataset") or [])')"
echo "  [9] anon /open-data/dcat dataset count -> ${DS:-?} (expect 0)"
[ "$DS" = "0" ] || { echo "      FAIL: the DCAT feed still publishes datasets with sharing OFF"; fail=1; }

# ---- [10] the WRITE gate ----------------------------------------------------------------------
rc=$(curl -sS -o /dev/null -w '%{http_code}' -X PUT -H "Authorization: Bearer $ADM" \
     -H 'Content-Type: application/json' -d '{"organizationOnly":false,"groups":[],"publicAccess":true}' \
     "$API/web-maps/$INV29_MAP/shares")
echo "  [10] admin re-share public while OFF -> $rc (expect 403)"
case "$rc" in
  403) : ;;
  2*) echo "      FAIL: content can be made public while the master toggle is OFF ($rc)"; fail=1 ;;
  *)  echo "      FAIL: expected 403, got $rc"; fail=1 ;;
esac

# ---- [11] the UPGRADE row (threat 4) ----------------------------------------------------------
# Column NULL means "not decided here — use the deployment value". Prove the resolution by REQUEST.
ENV_VAL="$(tv_env PUBLIC_ANON_ACCESS_ENABLED)"
case "$ENV_VAL" in
  [Tt]rue|1|[Yy]es|[Oo]n) ENV_ON=1 ;;
  *) ENV_ON=0 ;;
esac
psql_q "UPDATE platform.org_settings SET public_sharing_enabled = NULL WHERE id = true;" >/dev/null
PC="$(curl -sS "$API/public/config" | jget 'd["publicAnonAccessEnabled"]')"
rc=$(curl -sS -o /dev/null -w '%{http_code}' "$API/web-maps/$INV29_MAP")
if [ "$ENV_ON" -eq 1 ]; then
  echo "  [11] column NULL + PUBLIC_ANON_ACCESS_ENABLED='$ENV_VAL' -> resolved=${PC:-?}, anon map GET=$rc"
  echo "       (the doc's UPGRADE row: an existing public deployment must NOT be un-published by V092)"
  [ "$PC" = "True" ] || { echo "      FAIL: V092 silently un-published a live public deployment"; fail=1; }
  [ "$rc" = "200" ] || { echo "      FAIL: public content stopped serving on upgrade ($rc)"; fail=1; }
else
  echo "  [11] column NULL + PUBLIC_ANON_ACCESS_ENABLED='$ENV_VAL' -> resolved=${PC:-?}, anon map GET=$rc"
  echo "       (the doc's FRESH-INSTALL row: closed by default)"
  [ "$PC" = "False" ] || { echo "      FAIL: a fresh install resolves OPEN"; fail=1; }
  [ "$rc" = "404" ] || { echo "      FAIL: public content served on a closed-by-default install ($rc)"; fail=1; }
fi

# ---- [12] one resolver, no divergence (threat 6) ----------------------------------------------
# Every gate must consult the resolver. A call site that reads the env setting directly could
# resolve differently from the one next to it — the read gate serving what the write gate refuses.
# --include='*.py': the api bind-mount means the container's __pycache__ lands on the host, and
# the resolver's own docstring (which names the setting) survives into its .pyc — without the
# filter this leg flakes on "Binary file ... matches" the moment the api has imported the module.
STRAY="$(grep -rn --include='*.py' "settings\.public_anon_access_enabled" api/app 2>/dev/null \
         | grep -v "^api/app/public_sharing.py:" | grep -v "^api/app/config.py:")"
if [ -n "$STRAY" ]; then
  echo "  [12] direct reads of settings.public_anon_access_enabled outside the resolver:"
  printf '%s\n' "$STRAY" | sed 's/^/       /'
  echo "      FAIL: a gate bypasses the resolver — the read and write gates can diverge"
  fail=1
else
  echo "  [12] no gate reads settings.public_anon_access_enabled directly (one resolver) -> OK"
fi

if [ "$fail" = "0" ]; then echo "inv29: PASS"; else echo "inv29: FAIL"; exit 1; fi
