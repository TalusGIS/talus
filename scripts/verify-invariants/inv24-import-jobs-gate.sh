#!/usr/bin/env bash
# Invariant — the bulk-import door (9d) is publisher-gated, jobs are owner-scoped, and the
# archive path refuses traversal (decision-9d-bulk-import.md §8). A NEW DATA PATH: an upload
# endpoint accepting attacker-shaped archives feeding a DB-writing worker — the gate IS the
# control:
#
#   [1] anonymous POST /import-jobs → 401 (never a staged byte for an unauthenticated caller)
#   [2] viewer POST → 403 (role-gated, not just signed-in — same shape as the E4 door)
#   [3] publisher POST (tiny GeoJSON) → 2xx + job id (positive control — [1]/[2] aren't a
#       dead route) and the job reaches a terminal state
#   [4] ANOTHER user's GET of that job → 404 (owner-scoped; existence not disclosed)
#   [5] forged job id (random UUID) → 404
#   [6] zip-slip archive (entry named "../../evil-<nonce>.txt") → 422, refused by name
#       (jail-integrity + zip-bomb budget + corrupt-file 4xx are unit-test territory:
#        api/tests/test_import_jobs.py — this leg proves the DOOR refuses, per the gate)
#
# PRE-SLICE BASELINE (recorded 2026-08-21, route absent): every leg red — [1]/[2] returned
# 404 (WRONG codes for the contract), [3]–[6] failed outright. Recorded per the
# negative-test-first discipline; the 9d build must turn this green.
#
# Needs the running stack + .env (throwaways via _principals.sh). bash 3.2-safe; jq-free.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv24: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
URL="$API/import-jobs"
jget(){ python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }
fail=0
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/inv24.XXXXXX")"
JOB="" PUB="" TARGET_LAYER=""
# per the _principals.sh trap contract: declare leg_cleanup, never an own EXIT trap.
# The positive control mints a REAL job + layer — release both (owner token, still fresh at
# exit) or say so LOUDLY: a stray layer per run is the inv11 lesson.
leg_cleanup(){
  rm -rf "$SCRATCH"
  if [ -n "$JOB" ] && [ -n "$PUB" ]; then
    curl -sS -o /dev/null -X POST -H "Authorization: Bearer $PUB" "$URL/$JOB/cancel" || true
  fi
  if [ -n "$TARGET_LAYER" ] && [ -n "$PUB" ]; then
    rc=$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE -H "Authorization: Bearer $PUB" \
         "$API/layers/$TARGET_LAYER")
    case "$rc" in 2*) : ;; *) echo "  RESIDUE: inv24 fixture layer $TARGET_LAYER not released ($rc)" ;; esac
  fi
}

# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh

TINY='{"type":"FeatureCollection","features":[{"type":"Feature","geometry":{"type":"Point","coordinates":[-82.53,27.33]},"properties":{"name":"inv24"}}]}'

# [1] anonymous
rc=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$URL?format=geojson&target=new&name=inv24-anon" \
     -H 'Content-Type: application/octet-stream' --data-binary "$TINY")
echo "  [1] anon POST -> $rc (expect 401)"
[ "$rc" = "401" ] || { echo "      FAIL: anon must be 401, got $rc"; fail=1; }

# [2] viewer (authenticated, wrong role)
tv_mint viewer || { echo "inv24: FAIL — no viewer token"; exit 1; }
rc=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$URL?format=geojson&target=new&name=inv24-viewer" \
     -H "Authorization: Bearer $TV_VIEWER_TOKEN" -H 'Content-Type: application/octet-stream' --data-binary "$TINY")
echo "  [2] viewer POST -> $rc (expect 403)"
case "$rc" in
  403) : ;;
  2*) echo "      FAIL: a viewer can stage imports ($rc)"; fail=1 ;;
  *)  echo "      FAIL: expected 403, got $rc"; fail=1 ;;
esac

# [3] publisher positive control — create, then poll to a terminal state
tv_mint publisher || { echo "inv24: FAIL — no publisher token"; exit 1; }
PUB="$TV_PUBLISHER_TOKEN"
BODY=$(curl -sS -X POST "$URL?format=geojson&target=new&name=inv24-$(date +%s)" \
       -H "Authorization: Bearer $PUB" -H 'Content-Type: application/octet-stream' --data-binary "$TINY")
JOB=$(printf '%s' "$BODY" | jget 'd.get("id","")')
echo "  [3] publisher POST -> job id: ${JOB:-none}"
if [ -z "$JOB" ]; then
  echo "      FAIL: no job id ($(printf '%.200s' "$BODY"))"; fail=1
else
  TARGET_LAYER=$(printf '%s' "$BODY" | jget 'd.get("target_layer_id","")')
  st=""
  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    st=$(curl -sS -H "Authorization: Bearer $PUB" "$URL/$JOB" | jget 'd.get("status","")')
    case "$st" in completed|failed|canceled) break ;; esac
    sleep 2
  done
  echo "      job status -> ${st:-unknown} (expect completed)"
  [ "$st" = "completed" ] || { echo "      FAIL: tiny import did not complete"; fail=1; }
fi

# [4] owner-scoping — a DIFFERENT authenticated user must get 404, not the job
tv_mint editor || { echo "inv24: FAIL — no second principal"; exit 1; }
if [ -n "${JOB:-}" ]; then
  rc=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TV_EDITOR_TOKEN" "$URL/$JOB")
  echo "  [4] other user's GET -> $rc (expect 404)"
  [ "$rc" = "404" ] || { echo "      FAIL: cross-user job read must be 404, got $rc"; fail=1; }
fi

# [5] forged id
FORGED=$(python3 -c "import uuid;print(uuid.uuid4())")
rc=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $PUB" "$URL/$FORGED")
echo "  [5] forged job id -> $rc (expect 404)"
[ "$rc" = "404" ] || { echo "      FAIL: forged id must be 404, got $rc"; fail=1; }

# [6] zip-slip refusal — archive with a traversal entry name
python3 - "$SCRATCH/slip.zip" <<'PYEOF'
import sys, zipfile
with zipfile.ZipFile(sys.argv[1], "w") as z:
    z.writestr("../../evil-inv24.txt", "escape attempt")
    z.writestr("ok.shp", "not a real shapefile")
PYEOF
rc=$(curl -sS -o "$SCRATCH/slip-body" -w '%{http_code}' -X POST "$URL?format=shp-zip&target=new&name=inv24-slip" \
     -H "Authorization: Bearer $PUB" -H 'Content-Type: application/octet-stream' --data-binary "@$SCRATCH/slip.zip")
echo "  [6] zip-slip archive -> $rc (expect 422)"
if [ "$rc" = "422" ]; then
  grep -q "evil-inv24\|traversal\|entry" "$SCRATCH/slip-body" 2>/dev/null \
    || echo "      note: 422 but the refusal does not name the entry (cosmetic)"
else
  echo "      FAIL: traversal archive must be 422, got $rc"; fail=1
fi

if [ "$fail" = "0" ]; then echo "inv24: PASS"; else echo "inv24: FAIL"; exit 1; fi
