#!/usr/bin/env bash
# Invariant (Talus Field f4e, decision-talus-field-f4.md §4.5 / §6) — the changes feed
# GET /layers/{id}/changes?since=<audit_id>: the pull half of sync. It must be gated exactly like the
# feature door (anon on a private layer → 404 existence-hiding; a caller without view → 403), report a
# delete as a tombstone, page by watermark, and read cleanly through a PAT (the phone's device token
# rides the same optional-auth read seam as every read).
# Legs: 1. anon 404 · viewer (no view) 403 · owner since=0 lists the feature, nextWatermark > 0
#       2. an edit → since=<wm> has exactly that row (upserts 1, deletes 0); since=<new wm> → empty
#       3. a delete → deletes carries the id; count=true agrees
#       4. a read_only PAT reads the feed (200); a bbox that excludes the row reports it in `outside`
#       5. limit=1 with two changes → hasMore true, then the second page
#       6. a NATIVE (State Plane ftUS) layer: a 4326 bbox that contains the row lists it as an upsert, not
#          `outside` (the envelope is transformed into the storage srid — Refresh on the phone removed every
#          State Plane row before 2026-09-26); a bbox that excludes it still reports `outside`
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv42: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
fail=0
# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh
tv_mint publisher || { echo "inv42: FAIL — no owner token"; exit 1; }
OWNER="$TV_PUBLISHER_TOKEN"
tv_mint viewer || { echo "inv42: FAIL — no viewer token"; exit 1; }
VIEWER="$TV_VIEWER_TOKEN"
JSON='Content-Type: application/json'
code() { curl -sS -o /dev/null -w '%{http_code}' "$@"; }
jget() { python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }
leg() { if [ "$2" = "$3" ]; then echo "  PASS $1 ($3)"; else echo "  FAIL $1 (expected $2, got $3)"; fail=1; fi; }
LA=""; LB=""
leg_cleanup() {
  [ -n "$LA" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/layers/$LA"
  [ -n "$LB" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/layers/$LB"
}
LA="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"display_name":"inv42 changes","geom_type":"point","working_srid":4326,"native_storage":false}' "$API/layers" | jget 'd.get("layer_id","")')"
case "$LA" in tal_*) : ;; *) echo "inv42: FAIL — layer create ('$LA')"; exit 1 ;; esac
F1="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"lon":-82.53,"lat":27.34,"notes":"one"}' "$API/layers/$LA/features" | jget 'd["id"]')"
CH="$API/layers/$LA/changes"
echo "inv42 leg 1 — gates"
leg "anon (private layer)" 404 "$(code "$CH?since=0")"
leg "viewer without view" 403 "$(code -H "Authorization: Bearer $VIEWER" "$CH?since=0")"
r0="$(curl -sS -H "Authorization: Bearer $OWNER" "$CH?since=0")"
leg "owner since=0 upserts" 1 "$(printf '%s' "$r0" | jget 'len(d["upserts"])')"
leg "owner since=0 lists F1" "$F1" "$(printf '%s' "$r0" | jget 'd["upserts"][0]["id"]')"
WM0="$(printf '%s' "$r0" | jget 'd["nextWatermark"]')"
[ "${WM0:-0}" -gt 0 ] && echo "  PASS nextWatermark > 0 ($WM0)" || { echo "  FAIL nextWatermark"; fail=1; }
echo "inv42 leg 2 — an edit moves the watermark"
V="$(curl -sS -H "Authorization: Bearer $OWNER" "$API/layers/$LA/features/$F1" | jget 'd["version"]')"
leg "PATCH" 200 "$(code -X PATCH -H "Authorization: Bearer $OWNER" -H "$JSON" -H "If-Match: $V" -d '{"notes":"two"}' "$API/layers/$LA/features/$F1")"
r1="$(curl -sS -H "Authorization: Bearer $OWNER" "$CH?since=$WM0")"
leg "since=wm0 upserts" 1 "$(printf '%s' "$r1" | jget 'len(d["upserts"])')"
leg "since=wm0 deletes" 0 "$(printf '%s' "$r1" | jget 'len(d["deletes"])')"
leg "the upsert carries the new notes" two "$(printf '%s' "$r1" | jget 'd["upserts"][0]["properties"]["notes"]')"
WM1="$(printf '%s' "$r1" | jget 'd["nextWatermark"]')"
leg "since=wm1 is empty" 0 "$(curl -sS -H "Authorization: Bearer $OWNER" "$CH?since=$WM1" | jget 'd["changes"]')"
echo "inv42 leg 3 — a delete is a tombstone"
V2="$(curl -sS -H "Authorization: Bearer $OWNER" "$API/layers/$LA/features/$F1" | jget 'd["version"]')"
leg "DELETE" 204 "$(code -X DELETE -H "Authorization: Bearer $OWNER" -H "If-Match: $V2" "$API/layers/$LA/features/$F1")"
r2="$(curl -sS -H "Authorization: Bearer $OWNER" "$CH?since=$WM1")"
leg "deletes carries F1" "$F1" "$(printf '%s' "$r2" | jget 'd["deletes"][0]')"
leg "no upserts for a deleted row" 0 "$(printf '%s' "$r2" | jget 'len(d["upserts"])')"
leg "count=true agrees" 1 "$(curl -sS -H "Authorization: Bearer $OWNER" "$CH?since=$WM1&count=true" | jget 'd["changes"]')"
echo "inv42 leg 4 — a PAT reads; a bbox reports rows outside it"
F2="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"lon":-82.53,"lat":27.34,"notes":"three"}' "$API/layers/$LA/features" | jget 'd["id"]')"
RO="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"label":"inv42 ro","scope":"read_only"}' "$API/me/tokens" | jget 'd["token"]')"
leg "read_only PAT reads the feed" 200 "$(code -u "talus:$RO" "$CH?since=0")"
WM2="$(printf '%s' "$r2" | jget 'd["nextWatermark"]')"
r3="$(curl -sS -H "Authorization: Bearer $OWNER" "$CH?since=$WM2&bbox=0,0,1,1")"
leg "bbox excludes F2 → outside" "$F2" "$(printf '%s' "$r3" | jget 'd["outside"][0]')"
leg "bbox excludes F2 → no upserts" 0 "$(printf '%s' "$r3" | jget 'len(d["upserts"])')"
echo "inv42 leg 5 — paging by watermark"
F3="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"lon":-82.531,"lat":27.341,"notes":"four"}' "$API/layers/$LA/features" | jget 'd["id"]')"
p1="$(curl -sS -H "Authorization: Bearer $OWNER" "$CH?since=$WM2&limit=1")"
leg "page 1 hasMore" True "$(printf '%s' "$p1" | jget 'd["hasMore"]')"
leg "page 1 one row" 1 "$(printf '%s' "$p1" | jget 'd["changes"]')"
WMP="$(printf '%s' "$p1" | jget 'd["nextWatermark"]')"
p2="$(curl -sS -H "Authorization: Bearer $OWNER" "$CH?since=$WMP&limit=1")"
leg "page 2 lists F3" "$F3" "$(printf '%s' "$p2" | jget 'd["upserts"][0]["id"]')"
leg "page 2 hasMore" False "$(printf '%s' "$p2" | jget 'd["hasMore"]')"
echo "inv42 leg 6 — a native State Plane layer: the bbox is transformed into the storage srid"
LB="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"display_name":"inv42 native 6443","geom_type":"point","working_srid":6443,"native_storage":true}' "$API/layers" | jget 'd.get("layer_id","")')"
case "$LB" in tal_*) : ;; *) echo "  FAIL native layer create ('$LB')"; fail=1 ;; esac
if [ -n "$LB" ]; then
  # a native layer is written in its own CRS: the §0.1 point, −82.53 27.34 as a State Plane zone in US survey feet
  FN="$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d '{"x":484107.827,"y":1093132.248,"notes":"native"}' "$API/layers/$LB/features" | jget 'd["id"]')"
  leg "native create (x/y in feet)" 36 "$(printf '%s' "$FN" | wc -c | tr -d ' ')"
  rn="$(curl -sS -H "Authorization: Bearer $OWNER" "$API/layers/$LB/changes?since=0&bbox=-82.6,27.3,-82.46,27.38")"
  leg "bbox containing the row → upsert" "$FN" "$(printf '%s' "$rn" | jget 'd["upserts"][0]["id"]')"
  leg "bbox containing the row → nothing outside" 0 "$(printf '%s' "$rn" | jget 'len(d["outside"])')"
  leg "the upsert's geometry comes back in 4326" -82.53 "$(printf '%s' "$rn" | jget 'round(d["upserts"][0]["geometry"]["coordinates"][0],3)')"
  ro="$(curl -sS -H "Authorization: Bearer $OWNER" "$API/layers/$LB/changes?since=0&bbox=0,0,1,1")"
  leg "bbox excluding the row → outside" "$FN" "$(printf '%s' "$ro" | jget 'd["outside"][0]')"
  leg "/features?bbox on the native layer finds the row" 1 "$(curl -sS -H "Authorization: Bearer $OWNER" "$API/layers/$LB/features?bbox=-82.6,27.3,-82.46,27.38" | jget 'len(d["features"])')"
  leg "/features?bbox far from the layer's system → 200 and no rows (V118; it was a 500)" 0 "$(curl -sS -H "Authorization: Bearer $OWNER" "$API/layers/$LB/features?bbox=0,0,1,1" | jget 'len(d["features"])')"
  # Tiles on the natively stored layer (V118): the world tile and the zoom-1 and zoom-2 tiles that hold the
  # row must DRAW it, and a tile half a world away must be an empty tile, not an error. Before V118 the
  # widest tiles came back empty (old coordinate library) or as errors (current one).
  tbytes() { curl -sS -H "Authorization: Bearer $OWNER" "$API/tiles/$LB/$1.pbf" | wc -c | tr -d ' '; }
  tcode() { curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $OWNER" "$API/tiles/$LB/$1.pbf"; }
  for t in 0/0/0 1/0/0 2/1/1 10/277/431; do
    leg "native layer, tile $t holds the row → drawn" yes "$([ "$(tbytes "$t")" -gt 0 ] && echo yes || echo no)"
  done
  # (no `case` inside $(...): bash 3.2 misreads the pattern's closing parenthesis)
  far="$(tcode 1/1/0)"; farok="$far"
  if [ "$far" = 200 ] || [ "$far" = 204 ]; then farok=ok; fi
  leg "native layer, a tile half a world away → an empty tile, not an error" ok "$farok"
  leg "…and it is empty" 0 "$(tbytes 1/1/0)"
fi
leg_cleanup
if [ "$fail" = 0 ]; then echo "inv42: PASS"; else echo "inv42: FAIL"; exit 1; fi
