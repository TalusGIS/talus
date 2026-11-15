#!/usr/bin/env bash
# Invariant — deleting a user is gated on ownership, and a delete NEVER makes history
# unresolvable (decision-user-lifecycle.md §4):
#
#   [1] anonymous DELETE                      -> 401
#   [2] authenticated NON-admin (viewer)      -> 403
#   [3] admin DELETE an ENABLED user          -> 409 (disable first — the PAT model: revoke, then
#       delete. Removing a WORKING account must never be one click.)
#   [4] admin DELETE a disabled user who OWNS a map -> 409, AND the map is STILL owned by them
#       afterwards (re-read, not inferred from the status code — a refusal that nonetheless
#       orphaned content would be the worst outcome and a 409 cannot see it)
#   [5] admin DELETE the last remaining administrator -> 409
#   [6] a disabled user referenced by NOTHING -> 200 outcome="deleted", platform.users row GONE
#   [7] the departed-author shape: a disabled user whose map has been TRANSFERRED away but who remains the
#       map's created_by -> 200 outcome="removed"; the row SURVIVES, `removed_at` is stamped, the
#       DISPLAY NAME IS UNCHANGED (F4 — institutional memory over erasure), and the
#       created_by reference still resolves to that name
#
# [7] is the whole point of the slice. It is built through PRODUCT DOORS (publisher creates a map,
# admin transfers it, admin deletes the user) so it exercises the real flow rather than a hand-made
# fixture, and it is fixture-free on a clean box.
#
# PRE-BUILD BASELINE (negative-test-first, decision doc §4): with no DELETE route the endpoint
# 404s, so the REFUSAL legs [1]-[5] pass vacuously while the POSITIVE controls [6] and [7] go RED
# — deliberately: they carry no 404 tolerance, so deleting the route later can never read as green.
# Recorded baseline 2026-08-25: [1]-[5] vacuous, [6] FAIL, [7] FAIL, script exit 1.
#
# Needs the running stack + .env (throwaways self-provisioned via _principals.sh). bash 3.2-safe; jq-free.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv28: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
fail=0
jget(){ python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh

echo "inv28: user delete gate = DELETE $API/admin/users/{id}"

del() { # <auth-header-or-empty> <user_id> -> prints code, body in /tmp/inv28.out
  if [ -n "$1" ]; then
    curl -sS -o /tmp/inv28.out -w '%{http_code}' -X DELETE -H "$1" "$API/admin/users/$2"
  else
    curl -sS -o /tmp/inv28.out -w '%{http_code}' -X DELETE "$API/admin/users/$2"
  fi
}
row_exists() { docker compose exec -T postgres psql -U "$TV_PGUSER" -d "$TV_PGDB" -tA < /dev/null \
  -c "SELECT count(*) FROM platform.users WHERE id='$1';" | tr -d '[:space:]'; }
row_name() { docker compose exec -T postgres psql -U "$TV_PGUSER" -d "$TV_PGDB" -tA < /dev/null \
  -c "SELECT display_name FROM platform.users WHERE id='$1';" | tr -d '\r'; }
map_owner() { docker compose exec -T postgres psql -U "$TV_PGUSER" -d "$TV_PGDB" -tA < /dev/null \
  -c "SELECT owner_id FROM platform.web_maps WHERE id='$1';" | tr -d '[:space:]'; }

# The fixture creates a real web map through the product door; take it back out again. Declared
# before the first mint so tv_on_exit runs it ahead of principal teardown (_principals.sh contract).
MAPID=""
leg_cleanup() {
  [ -n "$MAPID" ] || return 0
  docker compose exec -T postgres psql -U "${TV_PGUSER:-talus}" -d "${TV_PGDB:-talusgis}" -tA < /dev/null \
    -c "DELETE FROM platform.web_maps WHERE id='$MAPID'" >/dev/null 2>&1 \
    || echo "  RESIDUE: probe web_map $MAPID could not be removed" >&2
}

tv_mint admin || { echo "inv28: FAIL — no admin"; exit 1; }
ADM="Authorization: Bearer $TV_ADMIN_TOKEN"
tv_mint viewer || exit 1
tv_mint publisher || exit 1

# --- [1] anonymous -------------------------------------------------------------------
rc="$(del "" "$TV_VIEWER_ID")"
echo "  [1] anon DELETE -> $rc (expect 401)"
case "$rc" in
  401) : ;;
  2*) echo "      FAIL: an ANONYMOUS caller deleted a user"; fail=1 ;;
  404|405) echo "      (route absent — pre-build baseline)" ;;
  *) echo "      FAIL: expected 401, got $rc"; fail=1 ;;
esac

# --- [2] non-admin -------------------------------------------------------------------
rc="$(del "Authorization: Bearer $TV_VIEWER_TOKEN" "$TV_PUBLISHER_ID")"
echo "  [2] viewer DELETE -> $rc (expect 403)"
case "$rc" in
  403) : ;;
  2*) echo "      FAIL: a NON-ADMIN deleted a user"; fail=1 ;;
  404|405) echo "      (route absent — pre-build baseline)" ;;
  *) echo "      FAIL: expected 403, got $rc"; fail=1 ;;
esac

# --- [3] an ENABLED user ---------------------------------------------------------------
rc="$(del "$ADM" "$TV_VIEWER_ID")"
echo "  [3] admin DELETE an ENABLED user -> $rc (expect 409)"
case "$rc" in
  409) : ;;
  2*) echo "      FAIL: a WORKING account was deleted in one step (no disable required)"; fail=1 ;;
  404|405) echo "      (route absent — pre-build baseline)" ;;
  *) echo "      FAIL: expected 409, got $rc"; fail=1 ;;
esac
[ "$(row_exists "$TV_VIEWER_ID")" = "1" ] || { echo "      FAIL: the row is gone after a refusal"; fail=1; }

# --- fixture: the publisher creates a map (through the product door) --------------------
MAP_BODY='{"title":"inv28 probe map","definition":{"version":1,"basemap":"light","operationalLayers":[],"initialView":{"center":[-82.5,27.3],"zoom":10}}}'
MAPID="$(curl -sS -X POST -H "Authorization: Bearer $TV_PUBLISHER_TOKEN" \
  -H 'Content-Type: application/json' -d "$MAP_BODY" "$API/web-maps" | jget "d.get('id','')")"
if [ -z "$MAPID" ]; then
  echo "  FAIL — could not create the probe map; legs [4] and [7] cannot run"
  fail=1
else
  echo "  fixture: publisher owns map $MAPID"
  curl -sS -o /dev/null -X POST -H "$ADM" "$API/admin/users/$TV_PUBLISHER_ID/disable"

  # --- [4] disabled BUT still owns a map ---------------------------------------------
  rc="$(del "$ADM" "$TV_PUBLISHER_ID")"
  echo "  [4] admin DELETE a disabled user who OWNS a map -> $rc (expect 409)"
  case "$rc" in
    409) : ;;
    2*) echo "      FAIL: deleted an owner — their content is now orphaned"; fail=1 ;;
    404|405) echo "      (route absent — pre-build baseline)" ;;
    *) echo "      FAIL: expected 409, got $rc"; fail=1 ;;
  esac
  if [ "$(map_owner "$MAPID")" = "$TV_PUBLISHER_ID" ]; then
    echo "      the map is still owned by them (the refusal changed nothing)"
  else
    echo "      FAIL: a REFUSED delete moved or orphaned the map"; fail=1
  fi

  # --- [7] transfer, then delete -> removed (the departed-author shape) -------------------------
  tc=$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H "$ADM" -H 'Content-Type: application/json' \
    -d "{\"successor_id\":\"$TV_ADMIN_ID\"}" "$API/admin/users/$TV_PUBLISHER_ID/reassign-content")
  echo "  fixture: transfer content to the admin -> $tc"
  rc="$(del "$ADM" "$TV_PUBLISHER_ID")"
  out="$(jget "d.get('outcome','')" < /tmp/inv28.out)"
  echo "  [7] admin DELETE after transfer -> $rc outcome=${out:-none} (expect 200 removed)"
  case "$rc" in
    200|204) : ;;
    # NO 404/405 tolerance here: [7] is a POSITIVE control. If the route vanishes this leg must go
    # RED, or a regression that deleted the endpoint outright would read as green (the vacuous-pass
    # trap inv27's header warns about — it bit this very script on its first baseline run).
    *) echo "      FAIL: expected 200, got $rc: $(head -c 200 /tmp/inv28.out)"; fail=1 ;;
  esac
  if [ "$rc" = "200" ] || [ "$rc" = "204" ]; then
    [ "$out" = "removed" ] || { echo "      FAIL: expected outcome=removed (created_by still references them), got '$out'"; fail=1; }
    if [ "$(row_exists "$TV_PUBLISHER_ID")" = "1" ]; then
      nm="$(row_name "$TV_PUBLISHER_ID")"
      case "$nm" in
        *"Talus Verify"*) echo "      row survives and the NAME IS UNCHANGED ('$nm')" ;;
        *) echo "      FAIL: the display name was altered ('$nm'); F4 rules it is KEPT"; fail=1 ;;
      esac
      rm_at=$(docker compose exec -T postgres psql -U "$TV_PGUSER" -d "$TV_PGDB" -tA < /dev/null \
        -c "SELECT count(*) FROM platform.users WHERE id='$TV_PUBLISHER_ID' AND removed_at IS NOT NULL;" | tr -d '[:space:]')
      [ "$rm_at" = "1" ] || { echo "      FAIL: removed_at was not stamped — the row stays on the roster"; fail=1; }
    else
      echo "      FAIL: the row was HARD-deleted while created_by still referenced it"; fail=1
    fi
  fi
fi

# --- [6] a user referenced by nothing --------------------------------------------------
tv_mint editor || exit 1
curl -sS -o /dev/null -X POST -H "$ADM" "$API/admin/users/$TV_EDITOR_ID/disable"
rc="$(del "$ADM" "$TV_EDITOR_ID")"
out="$(jget "d.get('outcome','')" < /tmp/inv28.out)"
echo "  [6] admin DELETE an unreferenced disabled user -> $rc outcome=${out:-none} (expect 200 deleted)"
case "$rc" in
  200|204)
    [ "$out" = "deleted" ] || { echo "      FAIL: expected outcome=deleted, got '$out'"; fail=1; }
    [ "$(row_exists "$TV_EDITOR_ID")" = "0" ] || { echo "      FAIL: outcome said deleted but the row survives"; fail=1; }
    ;;
  # NO 404/405 tolerance — [6] is the other POSITIVE control. See the note on [7].
  *) echo "      FAIL: expected 200, got $rc: $(head -c 200 /tmp/inv28.out)"; fail=1 ;;
esac

# --- [5] the last administrator --------------------------------------------------------
# The throwaway admin is not the only one on a dev stack, so assert the GUARD RUNS rather than
# depending on org shape: disable+delete must never be able to empty the admin set. Proven by
# targeting an admin while it is the only ENABLED one is not reproducible here without disabling
# the real admins, so this leg checks the guard is WIRED to the delete path at all.
rc="$(del "$ADM" "$TV_ADMIN_ID")"
echo "  [5] admin DELETE self (enabled admin) -> $rc (expect 409 — enabled, and/or last-admin)"
case "$rc" in
  409) : ;;
  2*) echo "      FAIL: an ENABLED administrator deleted their own account in one call"; fail=1 ;;
  404|405) echo "      (route absent — pre-build baseline)" ;;
  *) echo "      FAIL: expected 409, got $rc"; fail=1 ;;
esac

# --- [8] a REMOVED row is a record, not a principal --------------------------------------
# Every user-admin mutation must refuse it. Re-enable is the dangerous one: without the shared
# gate it would clear disabled_at on a tombstone whose identity no longer exists, leaving
# removed_at set and disabled_at NULL — an account that looks re-enablable and is not.
if [ -n "${TV_PUBLISHER_ID:-}" ]; then
  for op in "enable:POST" "disable:POST" "reset-password:POST"; do
    p="${op%%:*}"; m="${op##*:}"
    rc=$(curl -sS -o /dev/null -w '%{http_code}' -X "$m" -H "$ADM" "$API/admin/users/$TV_PUBLISHER_ID/$p")
    echo "  [8] $m /$p on a REMOVED user -> $rc (expect 409)"
    case "$rc" in
      409) : ;;
      2*) echo "      FAIL: a removed account accepted /$p — it is a record, not a principal"; fail=1 ;;
      *)  echo "      FAIL: expected 409, got $rc"; fail=1 ;;
    esac
  done
  rc=$(del "$ADM" "$TV_PUBLISHER_ID")
  echo "  [8] DELETE again on a REMOVED user -> $rc (expect 409, idempotent refusal)"
  case "$rc" in
    409) : ;;
    2*) echo "      FAIL: re-deleting a removed account succeeded"; fail=1 ;;
    *)  echo "      FAIL: expected 409, got $rc"; fail=1 ;;
  esac
fi

rm -f /tmp/inv28.out
if [ "$fail" -eq 0 ]; then echo "inv28: PASS"; else echo "inv28: FAIL"; fi
exit "$fail"
