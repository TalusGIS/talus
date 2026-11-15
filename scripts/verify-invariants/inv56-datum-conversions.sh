#!/usr/bin/env bash
# Invariant — the database and the api put the same point in the same place, and both use the datum
# correction files (api/datum_grids/).
#
# Why this exists. Coordinates are converted in two places: the database (PostGIS, for every layer:
# import, edits, tiles, GeoJSON) and the api (its own two copies of the PROJ library, for imagery).
# Each finds its correction files its own way. If one of them loses the files, nothing fails: the
# library quietly falls back to "no correction" and older data lands in the wrong place — tens of
# metres for NAD27, up to a few feet for original NAD83 — and the two halves of the product
# disagree about where the same point is. That was the state of the stack before these files were
# added (the database had some files, the api none), and a change of database image would have moved
# customers' coordinates without a word. This is the tripwire for both.
#
# Five conversions with known answers, asked of BOTH sides:
#   [a] NAD27 -> NAD83, a point in the central United States   needs us_noaa_conus.tif
#   [b] lat/long -> a plain NAD83 State Plane zone (EPSG:3418)  needs that state's HARN file
#   [c] lat/long -> the same zone on NAD83(2011) (EPSG:6465)    must NOT be adjusted (projection only)
#   [d] a NAD83(HARN) position -> WGS 84                        must come back UNCHANGED, as a
#       NAD83(2011) one does. The register holds two shifts for HARN and for no other realization of
#       NAD83 used in the United States; left in force, a HARN layer is drawn about 0.6 m from every
#       other layer of the same place (measured on two survey marks, 2026-11-14). They are withdrawn
#       in the database's reference file (db/proj/proj.db, mounted over the image's own) and in both
#       of the api's copies.
#   [e] a NAD83(CSRS) position -> WGS 84, a point in Canada    must come back UNCHANGED too. The
#       register holds one shift for Canada's original NAD83(CSRS); left in force, a layer on it is
#       drawn about 1.2 m from the same numbers on a later CSRS version, on NAD83(2011) or on GPS.
#       Withdrawn in the same three places.
# The expected numbers are the same ones api/datum_grids/check.py holds; change them together.
#
# Leg 3 proves the database's reference file is the pinned image's own with those three records
# changed and nothing else (scripts/derive-proj-db.sh --check): a file left behind by a change of
# image pin fails here, not on a customer's map.
#
# Read-only: SELECTs in the database, one python run in the api container, one read of the pinned
# image's own file with a one-off container. Nothing is created.
# Needs the running stack. bash 3.2-safe; stdin fed /dev/null so a compose exec never steals it.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv56: FAIL — cd"; exit 2; }
fail=0

q() { docker compose exec -T postgres psql -U talus -d talusgis -tA -F ' ' -c "$1" < /dev/null 2>&1; }
near() { # <got x> <got y> <want x> <want y> <tolerance> -> exit 0 when both are within tolerance
  awk -v gx="$1" -v gy="$2" -v wx="$3" -v wy="$4" -v t="$5" 'BEGIN { dx = gx - wx; if (dx < 0) dx = -dx; dy = gy - wy; if (dy < 0) dy = -dy; exit !(dx <= t && dy <= t) }'
}
dbleg() { # <label> <sql giving "x y"> <want x> <want y> <tolerance>
  got="$(q "$2")"
  gx="$(printf '%s' "$got" | awk '{print $1}')"; gy="$(printf '%s' "$got" | awk '{print $2}')"
  case "$gx" in ''|*[!0-9.-]*) echo "  FAIL database $1: no answer ($(printf '%s' "$got" | head -c 120))"; fail=1; return ;; esac
  if near "$gx" "$gy" "$3" "$4" "$5"; then echo "  ok   database $1: ($gx, $gy)"
  else echo "  FAIL database $1: ($gx, $gy), expected ($3, $4)"; fail=1; fi
}

echo "inv56 leg 1 — the database"
n="$(q "SELECT count(*) FROM pg_ls_dir('/var/lib/postgresql/.local/share/proj') f WHERE f LIKE 'us_noaa_%.tif'")"
echo "  correction files the database's library can see: $n (61 ship)"
case "$n" in ''|*[!0-9]*) echo "  FAIL: the folder is not mounted (docker-compose.yml, the postgres service)"; fail=1 ;; esac
dbleg "[a] NAD27 -> NAD83" \
  "SELECT round(ST_X(g)::numeric, 7), round(ST_Y(g)::numeric, 7) FROM (SELECT ST_Transform(ST_SetSRID(ST_MakePoint(-98.5, 39.5), 4267), 4269) g) s" \
  -98.5003411 39.5000104 0.0000002
dbleg "[b] lat/long -> plain NAD83 State Plane, ftUS (EPSG:3418)" \
  "SELECT round(ST_X(g)::numeric, 4), round(ST_Y(g)::numeric, 4) FROM (SELECT ST_Transform(ST_SetSRID(ST_MakePoint(-93.6, 41.6), 4326), 3418) g) s" \
  1613065.4125 582957.6562 0.002
dbleg "[c] lat/long -> NAD83(2011) State Plane, ftUS (EPSG:6465)" \
  "SELECT round(ST_X(g)::numeric, 4), round(ST_Y(g)::numeric, 4) FROM (SELECT ST_Transform(ST_SetSRID(ST_MakePoint(-93.6, 41.6), 4326), 6465) g) s" \
  1613065.9033 582957.3186 0.002
dbleg "[d] NAD83(HARN) lat/long -> WGS 84, unchanged" \
  "SELECT round(ST_X(g)::numeric, 9), round(ST_Y(g)::numeric, 9) FROM (SELECT ST_Transform(ST_SetSRID(ST_MakePoint(-98.5, 39.5), 4152), 4326) g) s" \
  -98.5 39.5 0.000000001
dbleg "[e] NAD83(CSRS) lat/long -> WGS 84, unchanged" \
  "SELECT round(ST_X(g)::numeric, 9), round(ST_Y(g)::numeric, 9) FROM (SELECT ST_Transform(ST_SetSRID(ST_MakePoint(-100.0, 55.0), 4617), 4326) g) s" \
  -100.0 55.0 0.000000001

echo "inv56 leg 2 — the api (both of its coordinate libraries)"
out="$(docker compose exec -T api python /usr/share/talus/datum_grids/check.py < /dev/null 2>&1)"; rc=$?
printf '%s\n' "$out" | head -12
if [ "$rc" -ne 0 ]; then
  case "$out" in
    *"No such file"*|*"can't open file"*) echo "  FAIL: this api image has no datum correction files built in (it predates them — rebuild the image)" ;;
    *) echo "  FAIL: the api's libraries do not give the expected coordinates" ;;
  esac
  fail=1
fi

echo "inv56 leg 3 — the database's reference file is the pinned image's own, three records changed"
if out="$(scripts/derive-proj-db.sh --check < /dev/null 2>&1)"; then
  printf '%s\n' "$out" | tail -3
else
  printf '%s\n' "$out" | tail -6
  echo "  FAIL: db/proj/proj.db is missing, altered, or belongs to another database image"
  fail=1
fi

if [ "$fail" -eq 0 ]; then echo "inv56: PASS"; else echo "inv56: FAIL"; fi
exit "$fail"
