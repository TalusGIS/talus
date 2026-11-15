#!/usr/bin/env bash
# Invariant — join views (V080) never widen access and never let a non-owner republish a table.
#
# Three properties (decision-relates-and-join-views.md, Security §3/§4/§5):
#   [1] AUTHORING (Fork 6): owner-of-the-SOURCE-only is NOT enough — joining a table republishes
#       its columns, so create must 403 unless the author owns BOTH ends (or is admin).
#   [2] NO-WIDEN (DB level): the PG view physically contains ONLY the requested, prefixed joined
#       columns — un-requested table columns are absent from information_schema, not just from
#       API responses.
#   [3] SHARING INDEPENDENCE (T38 doctrine, both directions): a PUBLIC join view over a PRIVATE
#       table serves anon — including the joined values (that is the designed republish, gated at
#       authoring) — while the table itself stays anon-denied; unsharing the view re-denies anon.
#   [4] WHO MAY CHANGE WHAT A VIEW EXPOSES:
#       the source layer's owner or an administrator, and NOT
#       the view's own owner. Owner A narrows a view of A's layer; an admin transfers the VIEW to
#       publisher B, who cannot see the source. B's PUT with every column -> 403 and the PG view's
#       column set is unchanged; A's same PUT -> 200 and the column set DOES change (the control: the
#       body is a real widening); B may still DELETE the view (204) — deleting only narrows.
#
# Needs the running stack + .env (throwaways via _principals.sh) + V080. bash 3.2-safe; -sS curl.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv14: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
fail=0

# The throwaway publisher owns the SOURCE only; the throwaway admin owns the table + authors the
# real view (the author must own BOTH ends or be admin — a second publisher couldn't author it).
# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh
tv_mint publisher || { echo "inv14: FAIL — no publisher token"; exit 1; }
PUB="$TV_PUBLISHER_TOKEN"
tv_mint admin || { echo "inv14: FAIL — no admin token"; exit 1; }
ADM="$TV_ADMIN_TOKEN"
JSON='Content-Type: application/json'

pyget(){ python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

LID=$(curl -sS -X POST -H "Authorization: Bearer $PUB" -H "$JSON" \
  -d '{"display_name":"inv14 source","geom_type":"point"}' "$API/layers" | pyget 'd.get("layer_id","")')
TID=$(curl -sS -X POST -H "Authorization: Bearer $ADM" -H "$JSON" \
  -d '{"display_name":"inv14 table","geom_type":"none"}' "$API/layers" | pyget 'd.get("layer_id","")')
[ -n "$LID" ] && [ -n "$TID" ] || { echo "inv14: FAIL — fixture create (L='$LID' T='$TID')"; exit 1; }
VID=""
V2=""   # leg [4]'s plain view; cleared once the leg has deleted it
leg_cleanup(){   # runs from the helper's EXIT trap, before principal teardown
  [ -n "$V2" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $ADM" "$API/layer-views/$V2"
  [ -n "$VID" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $ADM" "$API/layer-views/$VID"
  curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $ADM" "$API/layers/$TID"
  curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $PUB" "$API/layers/$LID"
}

curl -sS -o /dev/null -X POST -H "Authorization: Bearer $ADM" -H "$JSON" \
  -d '{"fieldKey":"status","label":"Status","dataType":"text"}' "$API/layers/$TID/custom-fields"
curl -sS -o /dev/null -X POST -H "Authorization: Bearer $PUB" -H "$JSON" \
  -d '{"lon":-82.5,"lat":27.3,"notes":"K1"}' "$API/layers/$LID/features"
curl -sS -o /dev/null -X POST -H "Authorization: Bearer $ADM" -H "$JSON" \
  -d '{"notes":"K1","props":{"status":"tbl-secret-val"}}' "$API/layers/$TID/features"
echo "  fixtures: L=$LID (publisher-owned) · T=$TID (admin-owned, private)"

JOIN='{"joinTableId":"'$TID'","layerKey":"notes","tableKey":"notes","alias":"insp","columns":["status"],"sortKey":"status","sortDir":"desc"}'

# [1] source-owner-only author → 403 (publisher owns L, does NOT own T)
rc=$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $PUB" -H "$JSON" \
  -d '{"sourceLayerId":"'$LID'","displayName":"inv14 probe","allowedColumns":["notes"],"filters":[],"join":'"$JOIN"'}' \
  "$API/layer-views")
echo "  [1] owner-of-source-only join create -> $rc (expect 403)"
[ "$rc" = "403" ] || { echo "      INVARIANT VIOLATED: a non-owner republished a table via a join"; fail=1; }

# admin (owner of the table; admin on the source) authors the real view
BODY=$(curl -sS -X POST -H "Authorization: Bearer $ADM" -H "$JSON" \
  -d '{"sourceLayerId":"'$LID'","displayName":"inv14 view","allowedColumns":["notes"],"filters":[],"join":'"$JOIN"'}' \
  "$API/layer-views")
VID=$(printf '%s' "$BODY" | pyget 'd.get("layer_id","")')
[ -n "$VID" ] || { echo "inv14: FAIL — admin view create: $BODY"; exit 1; }

# [2] DB-level no-widen: the PG view's real column set (read-only psql, Tier-1)
COLS=$(docker compose exec -T postgres psql -U talus -d talusgis -tA -c \
  "SELECT string_agg(column_name, ',') FROM information_schema.columns
   WHERE table_schema='assets' AND table_name='${VID}'" 2>/dev/null)
echo "  [2] view columns: $COLS"
case ",$COLS," in
  *,insp_status,*) : ;;
  *) echo "      INVARIANT VIOLATED: requested joined column missing"; fail=1 ;;
esac
case ",$COLS," in
  *,status,*|*,insp_created_by,*|*,insp_props,*)
    echo "      INVARIANT VIOLATED: un-requested table column present in the view def"; fail=1 ;;
esac

# [3] sharing independence — needs the anon surface enabled; skip legibly if it isn't
ANON_ON=$(grep -E '^PUBLIC_ANON_ACCESS_ENABLED=' .env | cut -d= -f2 | tr -d '"' )
if [ "$ANON_ON" = "true" ]; then
  curl -sS -o /dev/null -X PUT -H "Authorization: Bearer $ADM" -H "$JSON" \
    -d '{"organizationOnly":false,"groups":[],"publicAccess":true}' "$API/layers/$VID/shares"
  ANON_BODY=$(curl -sS "$API/layers/$VID/features")
  rc=$(curl -sS -o /dev/null -w '%{http_code}' "$API/layers/$VID/features")
  echo "  [3a] anon on the PUBLIC join view -> $rc (expect 200 — the view's own sharing governs)"
  [ "$rc" = "200" ] || fail=1
  case "$ANON_BODY" in
    *tbl-secret-val*) echo "       joined value served via the view (designed republish)" ;;
    *) echo "      note: joined value not present in anon body (check join)"; fail=1 ;;
  esac
  rc=$(curl -sS -o /dev/null -w '%{http_code}' "$API/layers/$TID/features")
  echo "  [3b] anon DIRECT on the private table -> $rc (expect 401/404)"
  case "$rc" in 401|404) : ;; *) echo "      INVARIANT VIOLATED: table reachable anon"; fail=1 ;; esac
  curl -sS -o /dev/null -X PUT -H "Authorization: Bearer $ADM" -H "$JSON" \
    -d '{"organizationOnly":false,"groups":[],"publicAccess":false}' "$API/layers/$VID/shares"
  rc=$(curl -sS -o /dev/null -w '%{http_code}' "$API/layers/$VID/features")
  echo "  [3c] anon after UNSHARE -> $rc (expect 401/404)"
  case "$rc" in 401|404) : ;; *) echo "      INVARIANT VIOLATED: unshared view still anon-readable"; fail=1 ;; esac
else
  echo "  [3] skipped: PUBLIC_ANON_ACCESS_ENABLED != true (anon surface off in this deploy)"
fi

# [4] who may change what a view exposes (once-over A5). A view is created by the source's owner or
# an admin, so at creation its owner is one of those; after a TRANSFER the view's owner can be someone
# who cannot see the source at all — and the edit door replaces the column list and the filters. Until
# the fix that door admitted the view's owner, who could set "every column, no filter" and so publish
# the whole source under the view's sharing.
view_cols(){   # the PG view's real column set, in order (read-only psql, as leg [2])
  docker compose exec -T postgres psql -U talus -d talusgis -tA < /dev/null -c \
    "SELECT string_agg(column_name, ',' ORDER BY ordinal_position) FROM information_schema.columns
     WHERE table_schema='assets' AND table_name='$1'" 2>/dev/null | tr -d '[:space:]'
}
tv_mint publisher || { echo "inv14: FAIL — no second publisher token"; exit 1; }   # publisher B (A's token is saved in PUB)
PUBB="$TV_PUBLISHER_TOKEN"; PUBB_ID="$TV_PUBLISHER_ID"
BODY=$(curl -sS -X POST -H "Authorization: Bearer $PUB" -H "$JSON" \
  -d '{"sourceLayerId":"'$LID'","displayName":"inv14 narrowed view","allowedColumns":["notes"],"filters":[]}' \
  "$API/layer-views")
V2=$(printf '%s' "$BODY" | pyget 'd.get("layer_id","")')
# The widest column list the picker offers for the source (everything not auto-included), as JSON.
WIDE=$(curl -sS -H "Authorization: Bearer $PUB" "$API/layer-views/sources/$LID/columns" \
  | pyget 'json.dumps([c["name"] for c in d["columns"] if not c.get("auto") and not c.get("geometry")])')
NWIDE=$(printf '%s' "${WIDE:-[]}" | pyget 'len(d)')
if [ -z "$V2" ]; then
  echo "  [4] FAIL — owner A could not create the narrowed view: $BODY"; fail=1
elif [ "${NWIDE:-0}" -lt 2 ] 2>/dev/null; then
  echo "  [4] FAIL — the source offers ${NWIDE:-no} column(s) to expose; a wider list cannot be built (got: $WIDE)"; fail=1
else
  WIDE_BODY='{"allowedColumns":'$WIDE',"filters":[]}'
  rc=$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $ADM" -H "$JSON" \
    -d '{"newOwnerId":"'$PUBB_ID'"}' "$API/catalog/layer/$V2/transfer")
  echo "  [4] fixture: admin transfers the VIEW $V2 to publisher B -> $rc (expect 200)"
  [ "$rc" = "200" ] || { echo "      FAIL: the transfer did not land — the leg cannot test what it exists for"; fail=1; }
  sleep 0.3
  rc=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $PUBB" "$API/layers/$LID/features")
  echo "  [4a] B on the SOURCE layer -> $rc (expect 403 — B cannot see what the view narrows)"
  case "$rc" in
    403) : ;;
    *) echo "      FAIL: B was expected to have no access to the source (got $rc)"; fail=1 ;;
  esac
  rc=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $PUBB" "$API/layers/$V2/features")
  echo "  [4b] B on the VIEW it now owns -> $rc (expect 200 — the transfer is real)"
  [ "$rc" = "200" ] || { echo "      FAIL: B cannot read the view it owns ($rc)"; fail=1; }

  before=$(view_cols "$V2")
  R=$(curl -sS -w '\n%{http_code}' -X PUT -H "Authorization: Bearer $PUBB" -H "$JSON" -d "$WIDE_BODY" "$API/layer-views/$V2")
  rc=$(printf '%s' "$R" | tail -1)
  after=$(view_cols "$V2")
  echo "  [4c] B (view owner, not source owner) PUT every column -> $rc (expect 403)"
  case "$rc" in
    403) : ;;
    2*) echo "      INVARIANT VIOLATED: the view's owner widened a view of a source they cannot see"
        echo "      body: $(printf '%s' "$R" | sed '$d' | cut -c1-300)"; fail=1 ;;
    *)  echo "      FAIL: expected 403, got $rc"; echo "      body: $(printf '%s' "$R" | sed '$d' | cut -c1-300)"; fail=1 ;;
  esac
  echo "       view columns before: ${before:-unreadable}"
  echo "       view columns after : ${after:-unreadable}"
  if [ -z "$before" ] || [ "$before" != "$after" ]; then
    echo "      INVARIANT VIOLATED: the refused edit changed the view's columns (or they could not be read)"; fail=1
  fi

  R=$(curl -sS -w '\n%{http_code}' -X PUT -H "Authorization: Bearer $PUB" -H "$JSON" -d "$WIDE_BODY" "$API/layer-views/$V2")
  rc=$(printf '%s' "$R" | tail -1)
  widened=$(view_cols "$V2")
  echo "  [4d] control: A (source owner) the SAME PUT -> $rc (expect 200), columns now: ${widened:-unreadable}"
  [ "$rc" = "200" ] || { echo "      FAIL: the source's owner could not edit the view"; echo "      body: $(printf '%s' "$R" | sed '$d' | cut -c1-300)"; fail=1; }
  [ -n "$widened" ] && [ "$widened" != "$before" ] || {
    echo "      FAIL: A's edit did not change the column set — the PUT body is not a widening, so [4c] proves nothing"; fail=1; }

  rc=$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE -H "Authorization: Bearer $PUBB" "$API/layer-views/$V2")
  echo "  [4e] B (view owner) DELETE the view -> $rc (expect 204 — deleting only narrows)"
  if [ "$rc" = "204" ]; then V2=""; else echo "      FAIL: the view's owner could not delete it ($rc)"; fail=1; fi
fi

if [ "$fail" -eq 0 ]; then echo "inv14: PASS"; exit 0; else echo "inv14: FAIL"; exit 1; fi
