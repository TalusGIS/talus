#!/usr/bin/env bash
# Invariant — streamed basemaps (decision-streamed-basemap.md §14, §15). A streamed basemap makes
# every viewer's browser fetch tiles from a public publisher (OpenStreetMap, USGS), so two things
# must hold on the server: only an administrator chooses which streamed maps a portal offers and
# which basemap new maps open on, and what they choose is a NAME from a closed list — never an
# address. The client holds the addresses as constants; nothing a caller sends can become one.
#
#   [1] anon      PUT /admin/basemap/streamed -> 401
#   [2] non-admin PUT /admin/basemap/streamed -> 403 (role-gated, not just signed-in)
#   [3] anon      PUT /admin/basemap/default  -> 401   (V093's door; its first standing test)
#   [4] non-admin PUT /admin/basemap/default  -> 403
#   [5] admin PUT streamed with a tile ADDRESS in the list -> 422, and the offered list is
#       unchanged afterwards (the closed-list gate: an address is refused, not stored)
#   [6] admin PUT streamed with an unknown name -> 422
#   [7] admin PUT default = a streamed map this portal does NOT offer -> 4xx, default unchanged
#       (a default the switcher will not show is refused). SKIPPED, and said so, on a box that
#       offers every streamed map — there is then no not-offered name to try.
#   [8] anon GET /basemap carries `streamed`: a list drawn only from osm|usgstopo|imagery,
#       `defaultBasemap` is one of the eight known names, and `downloadedListed` is a boolean
#       (the shape every map surface trusts)
#   [9] positive control: admin GET /admin/basemap -> 200 and carries `streamed` (proves the
#       admin token is good, so [5]-[7]'s refusals are the validator's and not a dead route)
#
# V116 (decision-streamed-basemap.md §16): a portal chooses whether it LISTS its downloaded
# looks, and gains two OpenStreetMap looks (osm-gray, osm-dark) that are offered when osm is.
#
#  [10] admin PUT streamed that would list NOTHING (no publisher, downloaded looks not listed)
#       -> 422, and both settings are unchanged afterwards (a portal must list something)
#  [11] admin PUT streamed with `downloaded` that is not a boolean -> 422 (a string is not
#       quietly read as true)
#  [12] admin PUT default = an unknown look ("osm-sepia") -> 422
#  [13] admin PUT default = an OpenStreetMap look when osm is NOT offered -> 4xx, default
#       unchanged. SKIPPED, and said so, on a portal that offers osm.
#
# [10] and [11] are sent ONLY to an api that knows the setting (the admin status carries
# `downloadedListed`). An older api would ignore the extra field and ACCEPT the write, changing
# the portal's offered list; on such an api the two legs FAIL without sending anything.
#
# This leg changes NO setting: every admin write it sends is one that must be refused.
#
# PRE-SLICE BASELINE: to be recorded in the decision doc §15 when first run, before V115 and the
# api change, per the negative-test-first discipline.
#
# Needs the running stack + .env (throwaways self-provisioned via _principals.sh). bash 3.2-safe; jq-free.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv51: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
STREAMED="$API/admin/basemap/streamed"
DEFAULT="$API/admin/basemap/default"
ADMIN_STATUS="$API/admin/basemap"
ANON_STATUS="$API/basemap"
jget(){ python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }
# The two listing settings as ONE comparable string, e.g. "[osm,imagery]/True". It is never empty
# for a readable status — a portal that offers nothing reads "[]/True" — so an EMPTY result means
# only "could not be read", and a before/after comparison cannot mistake one for the other.
sig(){ python3 -c 'import sys,json;d=json.load(sys.stdin);print("["+",".join(d["streamed"])+"]/"+str(d.get("downloadedListed")))' 2>/dev/null; }
put(){ # <url> <json> [token] -> http code
  if [ -n "${3:-}" ]; then
    curl -sS -o /dev/null -w '%{http_code}' -X PUT -H "Authorization: Bearer $3" \
      -H 'Content-Type: application/json' -d "$2" "$1"
  else
    curl -sS -o /dev/null -w '%{http_code}' -X PUT -H 'Content-Type: application/json' -d "$2" "$1"
  fi
}
fail=0
OK_LIST='{"value":["imagery"]}'

# [1] anon streamed
rc=$(put "$STREAMED" "$OK_LIST")
echo "  [1] anon PUT streamed -> $rc (expect 401)"
[ "$rc" = "401" ] || { echo "      FAIL: anon must be 401, got $rc"; fail=1; }

# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh
tv_mint viewer || { echo "inv51: FAIL — no viewer token"; exit 1; }
VWR="$TV_VIEWER_TOKEN"

# [2] non-admin streamed
rc=$(put "$STREAMED" "$OK_LIST" "$VWR")
echo "  [2] non-admin PUT streamed -> $rc (expect 403)"
case "$rc" in
  403) : ;;
  2*) echo "      FAIL: a non-admin changed which streamed maps are offered ($rc)"; fail=1 ;;
  *)  echo "      FAIL: expected 403, got $rc"; fail=1 ;;
esac

# [3] anon default
rc=$(put "$DEFAULT" '{"value":"light"}')
echo "  [3] anon PUT default -> $rc (expect 401)"
[ "$rc" = "401" ] || { echo "      FAIL: anon must be 401, got $rc"; fail=1; }

# [4] non-admin default
rc=$(put "$DEFAULT" '{"value":"light"}' "$VWR")
echo "  [4] non-admin PUT default -> $rc (expect 403)"
case "$rc" in
  403) : ;;
  2*) echo "      FAIL: a non-admin changed the default basemap ($rc)"; fail=1 ;;
  *)  echo "      FAIL: expected 403, got $rc"; fail=1 ;;
esac

tv_mint admin || { echo "inv51: FAIL — no admin token"; exit 1; }
ADM="$TV_ADMIN_TOKEN"
before="$(curl -sS "$ADMIN_STATUS" -H "Authorization: Bearer $ADM")"
before_list="$(printf '%s' "$before" | jget '",".join(d["streamed"])')"
before_default="$(printf '%s' "$before" | jget 'd["defaultBasemap"]')"
before_sig="$(printf '%s' "$before" | sig)"

# [5] an ADDRESS in the list is refused and nothing is stored
rc=$(put "$STREAMED" '{"value":["imagery","https://tiles.example.org/{z}/{x}/{y}.png"]}' "$ADM")
echo "  [5] admin PUT streamed with a tile address -> $rc (expect 422)"
[ "$rc" = "422" ] || { echo "      FAIL: an address was not refused by the closed list (got $rc)"; fail=1; }
after_sig="$(curl -sS "$ADMIN_STATUS" -H "Authorization: Bearer $ADM" | sig)"
[ "${before_sig:-ERR}" = "${after_sig:-ERR2}" ] || {
  echo "      FAIL: the refused write changed what the portal lists (${before_sig:-unreadable} -> ${after_sig:-unreadable})"; fail=1; }

# [6] an unknown name is refused
rc=$(put "$STREAMED" '{"value":["satellite"]}' "$ADM")
echo "  [6] admin PUT streamed with an unknown name -> $rc (expect 422)"
[ "$rc" = "422" ] || { echo "      FAIL: unknown name not refused (got $rc)"; fail=1; }

# [7] a default that is not offered is refused
missing=""
for name in osm usgstopo imagery; do
  case ",$before_list," in *",$name,"*) : ;; *) missing="$name"; break ;; esac
done
if [ -z "$before_default" ]; then
  echo "  [7] FAIL: admin status carries no default / streamed list to test against"; fail=1
elif [ -z "$missing" ]; then
  echo "  [7] SKIP: this portal offers every streamed map — no not-offered name to try"
else
  rc=$(put "$DEFAULT" "{\"value\":\"$missing\"}" "$ADM")
  echo "  [7] admin PUT default=$missing (not offered) -> $rc (expect 4xx)"
  case "$rc" in
    4*) : ;;
    *)  echo "      FAIL: a default the portal does not offer was accepted (got $rc)"; fail=1 ;;
  esac
  after_default="$(curl -sS "$ADMIN_STATUS" -H "Authorization: Bearer $ADM" | jget 'd["defaultBasemap"]')"
  [ "${before_default:-ERR}" = "${after_default:-ERR2}" ] || {
    echo "      FAIL: the refused write changed the default ($before_default -> $after_default)"; fail=1; }
fi

# [8] the anonymous projection: names only, from the closed lists
shape="$(curl -sS "$ANON_STATUS" | python3 -c '
import sys, json
d = json.load(sys.stdin)
s = d.get("streamed")
ok = isinstance(s, list) and all(x in ("osm", "usgstopo", "imagery") for x in s) and len(s) == len(set(s))
ok = ok and d.get("defaultBasemap") in (
    "light", "dark", "grayscale", "osm", "osm-gray", "osm-dark", "usgstopo", "imagery")
ok = ok and isinstance(d.get("downloadedListed"), bool)
print("ok" if ok else "bad:" + json.dumps({"streamed": s, "defaultBasemap": d.get("defaultBasemap"),
                                             "downloadedListed": d.get("downloadedListed")}))
' 2>/dev/null)"
echo "  [8] anon GET /basemap shape -> ${shape:-unreadable} (expect ok)"
[ "$shape" = "ok" ] || { echo "      FAIL: anonymous status does not carry a closed-list streamed set and a boolean downloadedListed"; fail=1; }

# [9] positive control
rc=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $ADM" "$ADMIN_STATUS")
has="$(printf '%s' "$before" | jget '"streamed" in d')"
echo "  [9] admin status -> $rc, carries streamed: ${has:-?} (expect 200, True)"
{ [ "$rc" = "200" ] && [ "$has" = "True" ]; } || { echo "      FAIL: admin status control"; fail=1; }

# --- V116 ---------------------------------------------------------------------------------
knows="$(printf '%s' "$before" | jget 'isinstance(d.get("downloadedListed"), bool)')"

# [10] a write that would list nothing is refused, and nothing is stored
if [ "$knows" != "True" ]; then
  echo " [10] FAIL: the api does not carry downloadedListed — not sending a write it would accept"; fail=1
else
  rc=$(put "$STREAMED" '{"value":[],"downloaded":false}' "$ADM")
  echo " [10] admin PUT streamed listing nothing -> $rc (expect 422)"
  [ "$rc" = "422" ] || { echo "      FAIL: a portal was allowed to list no basemap (got $rc)"; fail=1; }
  after_sig="$(curl -sS "$ADMIN_STATUS" -H "Authorization: Bearer $ADM" | sig)"
  [ "${before_sig:-ERR}" = "${after_sig:-ERR2}" ] || {
    echo "      FAIL: the refused write changed what the portal lists (${before_sig:-unreadable} -> ${after_sig:-unreadable})"; fail=1; }
fi

# [11] `downloaded` must be a boolean
if [ "$knows" != "True" ]; then
  echo " [11] FAIL: the api does not carry downloadedListed — not sending a write it would accept"; fail=1
else
  # The body repeats the portal's CURRENT settings, with the boolean as a string: if an api ever
  # read "true"/"false" as a boolean and accepted this, the write would change nothing.
  same_body="$(printf '%s' "$before" | python3 -c '
import sys, json
d = json.load(sys.stdin)
print(json.dumps({"value": d["streamed"], "downloaded": "true" if d["downloadedListed"] else "false"}))
' 2>/dev/null)"
  rc=$(put "$STREAMED" "$same_body" "$ADM")
  echo " [11] admin PUT streamed with a non-boolean downloaded -> $rc (expect 422)"
  [ "$rc" = "422" ] || { echo "      FAIL: a non-boolean was accepted (got $rc)"; fail=1; }
  after_sig="$(curl -sS "$ADMIN_STATUS" -H "Authorization: Bearer $ADM" | sig)"
  [ "${before_sig:-ERR}" = "${after_sig:-ERR2}" ] || {
    echo "      FAIL: the refused write changed what the portal lists (${before_sig:-unreadable} -> ${after_sig:-unreadable})"; fail=1; }
fi

# [12] an unknown look is refused as a default (safe on any api: the name is in no closed list)
rc=$(put "$DEFAULT" '{"value":"osm-sepia"}' "$ADM")
echo " [12] admin PUT default=osm-sepia (unknown look) -> $rc (expect 422)"
[ "$rc" = "422" ] || { echo "      FAIL: an unknown look was not refused (got $rc)"; fail=1; }

# [13] an OpenStreetMap look is refused as a default when osm is not offered. Sent ONLY when the
# baseline status was actually read: an unreadable status leaves the offered list empty, and an
# empty list alone cannot tell "offers nothing" from "could not be read" — on a portal that does
# offer osm the write would then SUCCEED and change what new maps open on.
if [ -z "$before_sig" ] || [ -z "$before_default" ]; then
  echo " [13] FAIL: the admin status could not be read — not sending a write that might succeed"; fail=1
else case ",$before_list," in
  *",osm,"*)
    echo " [13] SKIP: this portal offers osm — its looks are allowed as a default here" ;;
  *)
    rc=$(put "$DEFAULT" '{"value":"osm-dark"}' "$ADM")
    echo " [13] admin PUT default=osm-dark (osm not offered) -> $rc (expect 4xx)"
    case "$rc" in
      4*) : ;;
      *)  echo "      FAIL: a look of a publisher the portal does not offer was accepted (got $rc)"; fail=1 ;;
    esac
    after_default="$(curl -sS "$ADMIN_STATUS" -H "Authorization: Bearer $ADM" | jget 'd["defaultBasemap"]')"
    [ "${before_default:-ERR}" = "${after_default:-ERR2}" ] || {
      echo "      FAIL: the refused write changed the default ($before_default -> $after_default)"; fail=1; } ;;
esac; fi

if [ "$fail" = "0" ]; then echo "inv51: PASS"; else echo "inv51: FAIL"; exit 1; fi
