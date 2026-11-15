#!/usr/bin/env bash
# Invariant — a SQL-source pull never runs for an owner who is no longer an active publisher
# (decision-sql-source-pull.md §11.2, L1–L5). The lifecycle doors are the mechanism of record; the
# worker carries a no-network backstop. The posture: every background write has an accountable,
# CURRENT actor.
#
#   fixture: publisher A owns a connection (private literal, unreachable) and a layer; a pull row
#            ties them with an interval schedule that is already due (inserted directly — no live
#            source is needed to prove who may RUN a pull)
#   [0] positive control: with A active, the worker mints a trigger='schedule' run within a tick
#       (the scheduler is live, so a later "no run" means the gate, not a dead worker)
#   [1] admin disables A → the pull is manual, next_run_at NULL, stale with 'owner disabled on …',
#       no pending/running run survives; one sql_pull.deactivated_by_lifecycle audit row
#   [2] ★ L5 backstop: the schedule is forced back to due (a hook a future door forgot) while A is
#       still disabled → the worker mints NO run across two ticks
#   [3] admin re-enables A → still manual, the reason still on the badge (L2: no silent resume)
#   [4] the owner re-sets a schedule; admin sets A's role to viewer → manual + 'owner role changed';
#       role back to publisher → still manual
#   [5] admin transfers the layer to publisher B → the pull row is GONE, the layer is unlocked, B
#       can turn editing on through the settings door (200, not the 409 lock); A's connection stays;
#       the bulk door (reassign-content, the offboarding dialog) detaches a SECOND pulled layer the same way
#   [6] admin disables A, then DELETEs A → A's connection rows are gone; no run carries A's id
#   [7] cleanup: B deletes the layer
#
# Needs the running stack + .env (throwaways via _principals.sh). bash 3.2-safe; jq-free.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv48: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
JSON='Content-Type: application/json'
TICK="${SQL_PULL_WORKER_TICK_SECONDS:-3}"
jget(){ python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }
psql_q(){ docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "${TV_PGUSER:-talus}" -d "${TV_PGDB:-talusgis}" -tAq -c "$1" < /dev/null 2>/dev/null | tr -d '[:space:]'; }
fail=0
CID="" LID="" LID2="" PID="" PID2="" ADMIN="" PUBA="" PUBB=""
leg_cleanup(){
  [ -n "$LID" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer ${PUBB:-$ADMIN}" "$API/layers/$LID" || true
  [ -n "$LID2" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer ${PUBB:-$ADMIN}" "$API/layers/$LID2" || true
  [ -n "$CID" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer ${ADMIN:-$PUBA}" "$API/sql-connections/$CID" || true
}
# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh

pull_state(){ # -> kind|next_run_at|stale_since|last_error  (spaces kept: the reason is prose)
  docker compose exec -T postgres psql -U "${TV_PGUSER:-talus}" -d "${TV_PGDB:-talusgis}" -tAq -c "SELECT schedule_kind || '|' || COALESCE(next_run_at::text,'') || '|' || COALESCE(stale_since::text,'') || '|' || COALESCE(last_error,'') FROM platform.sql_pulls WHERE id = '$PID'" < /dev/null 2>/dev/null | tr -d '\n'
}
runs(){ psql_q "SELECT count(*) FROM platform.sql_pull_runs WHERE pull_id = '$PID'"; }
live_runs(){ psql_q "SELECT count(*) FROM platform.sql_pull_runs WHERE pull_id = '$PID' AND status IN ('pending','running')"; }
force_due(){ psql_q "UPDATE platform.sql_pulls SET schedule_kind='interval', schedule_hour=NULL, schedule_minutes=60, next_run_at = now() - interval '1 minute' WHERE id = '$PID'" >/dev/null; }
wait_ticks(){ sleep $(( TICK * 2 + 2 )); }
wait_terminal(){ # the control run must reach a terminal state before the next leg (a late failure write would clobber the reason)
  i=0; while [ "$i" -lt 30 ]; do [ "$(live_runs)" = "0" ] && return 0; sleep 1; i=$((i+1)); done; return 1
}

# ---- fixture ----------------------------------------------------------------------------------
tv_mint publisher || { echo "inv48: FAIL — no publisher token"; exit 1; }
PUBA="$TV_PUBLISHER_TOKEN"; A_ID="$TV_PUBLISHER_ID"
tv_mint publisher || { echo "inv48: FAIL — no second publisher token"; exit 1; }
PUBB="$TV_PUBLISHER_TOKEN"; B_ID="$TV_PUBLISHER_ID"
tv_mint admin || { echo "inv48: FAIL — no admin token"; exit 1; }
ADMIN="$TV_ADMIN_TOKEN"

CID=$(curl -sS -X POST -H "Authorization: Bearer $PUBA" -H "$JSON" \
  --data "{\"name\":\"inv48-$(date +%s)\",\"dialect\":\"postgresql\",\"host\":\"10.255.255.1\",\"port\":5432,\"database\":\"billing\",\"username\":\"talus_ro\",\"password\":\"inv48-$$\",\"tlsMode\":\"disable\",\"timezone\":\"UTC\"}" \
  "$API/sql-connections" | jget 'd.get("id","")')
[ -n "$CID" ] || { echo "inv48: FAIL — connection create"; exit 1; }
LID=$(curl -sS -X POST -H "Authorization: Bearer $PUBA" -H "$JSON" \
  -d '{"display_name":"inv48 owner lifecycle","geom_type":"none","working_srid":4326,"native_storage":false}' \
  "$API/layers" | jget 'd.get("layer_id","")')
[ -n "$LID" ] || { echo "inv48: FAIL — layer create"; exit 1; }
PID=$(psql_q "INSERT INTO platform.sql_pulls (connection_id, target_layer_id, source_schema, source_table, columns, created_by, schedule_kind, schedule_minutes, next_run_at)
  VALUES ('$CID', '$LID', 'public', 'inv48', '[]'::jsonb, '$A_ID', 'interval', 60, now() - interval '1 minute') RETURNING id")
[ -n "$PID" ] || { echo "inv48: FAIL — pull row insert"; exit 1; }
psql_q "UPDATE platform.layers SET editing_enabled=false, edit_add=false, edit_attributes=false, edit_geometry=false, edit_delete=false WHERE layer_id='$LID'" >/dev/null
LID2=$(curl -sS -X POST -H "Authorization: Bearer $PUBA" -H "$JSON" \
  -d '{"display_name":"inv48 owner lifecycle (bulk)","geom_type":"none","working_srid":4326,"native_storage":false}' \
  "$API/layers" | jget 'd.get("layer_id","")')
[ -n "$LID2" ] || { echo "inv48: FAIL — second layer create"; exit 1; }
PID2=$(psql_q "INSERT INTO platform.sql_pulls (connection_id, target_layer_id, source_schema, source_table, columns, created_by)
  VALUES ('$CID', '$LID2', 'public', 'inv48b', '[]'::jsonb, '$A_ID') RETURNING id")
[ -n "$PID2" ] || { echo "inv48: FAIL — second pull row insert"; exit 1; }
psql_q "UPDATE platform.layers SET editing_enabled=false, edit_add=false, edit_attributes=false, edit_geometry=false, edit_delete=false WHERE layer_id='$LID2'" >/dev/null
echo "inv48: fixture — connection $CID, layer $LID, pull $PID (owner $TV_PUBLISHER_USER's predecessor A=$A_ID)"

# [0] positive control
wait_ticks
n=$(runs); sched=$(psql_q "SELECT count(*) FROM platform.sql_pull_runs WHERE pull_id='$PID' AND trigger='schedule' AND created_by='$A_ID'")
echo "  [0] active owner, due schedule -> $n run(s), $sched scheduled under A (expect >= 1)"
[ "${sched:-0}" -ge 1 ] 2>/dev/null || { echo "      FAIL: the scheduler never fired for an ACTIVE owner — the later legs would prove nothing"; fail=1; }
wait_terminal || echo "      note: the control run is still live after 30 s"
BASE=$(runs)

# [1] disable
rc=$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $ADMIN" "$API/admin/users/$A_ID/disable")
st=$(pull_state); kind=${st%%|*}; rest=${st#*|}; nxt=${rest%%|*}; rest=${rest#*|}; stale=${rest%%|*}; err=${rest#*|}
echo "  [1] disable A -> $rc; pull: kind=$kind next=${nxt:-NULL} stale=${stale:+set} error='$err'"
[ "$rc" = "200" ] || { echo "      FAIL: disable -> $rc"; fail=1; }
[ "$kind" = "manual" ] || { echo "      FAIL: schedule not flipped to manual"; fail=1; }
[ -z "$nxt" ] || { echo "      FAIL: next_run_at still set"; fail=1; }
[ -n "$stale" ] || { echo "      FAIL: stale_since not set"; fail=1; }
case "$err" in "owner disabled on "*) : ;; *) echo "      FAIL: last_error is not the lifecycle reason"; fail=1 ;; esac
[ "$(live_runs)" = "0" ] || { echo "      FAIL: a live run survived the disable"; fail=1; }
au=$(psql_q "SELECT count(*) FROM platform.admin_audit WHERE action='sql_pull.deactivated_by_lifecycle' AND target_id='$PID' AND detail->>'event'='disabled'")
[ "${au:-0}" -ge 1 ] 2>/dev/null && echo "      audit: sql_pull.deactivated_by_lifecycle (disabled) recorded" || { echo "      FAIL: no lifecycle audit row"; fail=1; }

# [2] ★ L5 backstop — the schedule forced due while the owner is disabled
force_due; wait_ticks
n=$(runs)
echo "  [2] forced-due pull, owner DISABLED -> runs $BASE -> $n (expect unchanged)"
[ "$n" = "$BASE" ] || { echo "      FAIL: the worker minted a run for a disabled owner"; fail=1; }
psql_q "UPDATE platform.sql_pulls SET schedule_kind='manual', schedule_minutes=NULL, next_run_at=NULL WHERE id='$PID'" >/dev/null   # undo the simulation

# [3] enable: nothing resumes
rc=$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $ADMIN" "$API/admin/users/$A_ID/enable")
st=$(pull_state); kind=${st%%|*}; err=${st##*|}
echo "  [3] enable A -> $rc; pull: kind=$kind error='$err' (expect manual, reason kept)"
[ "$rc" = "200" ] && [ "$kind" = "manual" ] || { echo "      FAIL"; fail=1; }
case "$err" in "owner disabled on "*) : ;; *) echo "      FAIL: the reason was cleared by enable"; fail=1 ;; esac

# [4] demotion, then promotion
force_due
rc=$(curl -sS -o /dev/null -w '%{http_code}' -X PUT -H "Authorization: Bearer $ADMIN" -H "$JSON" -d '{"role":"talus-viewer"}' "$API/admin/users/$A_ID/role")
st=$(pull_state); kind=${st%%|*}; err=${st##*|}
echo "  [4a] role -> viewer: $rc; pull: kind=$kind error='$err'"
[ "$rc" = "200" ] && [ "$kind" = "manual" ] || { echo "      FAIL: a demoted owner's pull kept its schedule"; fail=1; }
case "$err" in "owner role changed to "*) : ;; *) echo "      FAIL: last_error is not the demotion reason"; fail=1 ;; esac
[ "$(live_runs)" = "0" ] || { echo "      FAIL: a live run survived the demotion"; fail=1; }
rc=$(curl -sS -o /dev/null -w '%{http_code}' -X PUT -H "Authorization: Bearer $ADMIN" -H "$JSON" -d '{"role":"talus-publisher"}' "$API/admin/users/$A_ID/role")
kind=$(pull_state); kind=${kind%%|*}
echo "  [4b] role -> publisher: $rc; pull: kind=$kind (expect manual: no silent resume)"
[ "$rc" = "200" ] && [ "$kind" = "manual" ] || { echo "      FAIL"; fail=1; }
BASE=$(runs)

# [5] transfer detaches
rc=$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $ADMIN" -H "$JSON" -d "{\"newOwnerId\":\"$B_ID\"}" "$API/catalog/layer/$LID/transfer")
left=$(psql_q "SELECT count(*) FROM platform.sql_pulls WHERE target_layer_id='$LID'")
unlocked=$(psql_q "SELECT editing_enabled FROM platform.layers WHERE layer_id='$LID'")
conns=$(psql_q "SELECT count(*) FROM platform.sql_connections WHERE id='$CID'")
echo "  [5a] transfer layer A -> B: $rc; pull rows left=$left layer editing_enabled=$unlocked A's connection rows=$conns"
[ "$rc" = "200" ] && [ "$left" = "0" ] && [ "$unlocked" = "t" ] && [ "$conns" = "1" ] || { echo "      FAIL"; fail=1; }
rc=$(curl -sS -o /dev/null -w '%{http_code}' -X PUT -H "Authorization: Bearer $PUBB" -H "$JSON" -d '{"editingEnabled":true}' "$API/layers/$LID/editing")
echo "  [5b] successor B turns editing on through the settings door -> $rc (expect 200, not the 409 lock)"
[ "$rc" = "200" ] || { echo "      FAIL"; fail=1; }
au=$(psql_q "SELECT count(*) FROM platform.admin_audit WHERE action='sql_pull.deactivated_by_lifecycle' AND target_id='$PID' AND detail->>'event'='transferred'")
[ "${au:-0}" -ge 1 ] 2>/dev/null || { echo "      FAIL: no 'transferred' audit row"; fail=1; }
OUT=$(curl -sS -w '\n%{http_code}' -X POST -H "Authorization: Bearer $ADMIN" -H "$JSON" -d "{\"successor_id\":\"$B_ID\"}" "$API/admin/users/$A_ID/reassign-content")
rc=$(printf '%s' "$OUT" | tail -1); moved=$(printf '%s' "$OUT" | sed '$d' | jget 'len(d.get("moved_layers",[]))')
left=$(psql_q "SELECT count(*) FROM platform.sql_pulls WHERE target_layer_id='$LID2'")
unlocked=$(psql_q "SELECT editing_enabled FROM platform.layers WHERE layer_id='$LID2'")
echo "  [5c] reassign-content A -> B (the bulk door): $rc, moved $moved layer(s); second pull rows left=$left editing_enabled=$unlocked"
[ "$rc" = "200" ] && [ "$left" = "0" ] && [ "$unlocked" = "t" ] || { echo "      FAIL: the bulk door left the pull attached"; fail=1; }

# [6] offboard: the credential dies with its person
curl -sS -o /dev/null -X POST -H "Authorization: Bearer $ADMIN" "$API/admin/users/$A_ID/disable"
OUT=$(curl -sS -w '\n%{http_code}' -X DELETE -H "Authorization: Bearer $ADMIN" "$API/admin/users/$A_ID")
rc=$(printf '%s' "$OUT" | tail -1); outcome=$(printf '%s' "$OUT" | sed '$d' | jget 'd.get("outcome","")')
conns=$(psql_q "SELECT count(*) FROM platform.sql_connections WHERE created_by='$A_ID'")
aruns=$(psql_q "SELECT count(*) FROM platform.sql_pull_runs WHERE created_by='$A_ID'")
echo "  [6] delete A -> $rc ($outcome); A's connections=$conns, runs carrying A's id=$aruns (expect 0, 0)"
[ "$rc" = "200" ] && [ "$conns" = "0" ] && [ "$aruns" = "0" ] || { echo "      FAIL"; fail=1; }
[ "$conns" = "0" ] && CID=""

# [7] cleanup
rc=$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE -H "Authorization: Bearer $PUBB" "$API/layers/$LID")
rc2=$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE -H "Authorization: Bearer $PUBB" "$API/layers/$LID2")
echo "  [7] B deletes both layers -> $rc, $rc2"
case "$rc" in 2*) LID="" ;; *) echo "      note: layer delete -> $rc (leg_cleanup retries)" ;; esac
case "$rc2" in 2*) LID2="" ;; *) echo "      note: second layer delete -> $rc2 (leg_cleanup retries)" ;; esac

if [ "$fail" = "0" ]; then echo "inv48: PASS"; else echo "inv48: FAIL"; fi
exit $fail
