#!/usr/bin/env bash
# Invariant (raster-public slice) — the anon raster read paths never leak a NON-public raster.
#
# The threat: the raster-public slice converted tilejson/tiles/point/thumbnail/export from
# Bearer-only to optional-auth. If the anonymous branch ever passes on anything but the direct
# public_shared probe (or the flat-404 discipline slips), private imagery/elevation leaks to the
# open internet.
#
# DENY paths asserted token-free (inv04 style) against a PRIVATE raster:
#   1. anon tilejson / tile / point / thumbnail / export  -> NOT 2xx (404 expected, no oracle)
#   2. anon Open Data raster detail                        -> NOT 2xx
#   3. forged `alg:none` bearer on tilejson                -> 401
#   4. anon WMTS + WCS capabilities must NOT list the id
#
# Deny rule: any 2xx on private raster bytes/values = leaked allow = FAIL.
# (Positive controls — public raster anon-ALLOW — live in verify-raster-public.sh, which needs
# tokens; this invariant stays token-free and expiry-immune.)
#
# PRIVATE_RASTER defaults to the dev rig's Test COG; override for other environments.
set -u

API_BASE="${API_BASE:-http://localhost:8000/api/v1}"
PRIVATE_RASTER="${PRIVATE_RASTER:-bd20f4af-8c48-4cda-89d4-46e613bf05c4}"
FORGED='eyJhbGciOiJub25lIn0.eyJzdWIiOiJoYWNrZXIifQ.'

fail=0
echo "inv10: private raster = $PRIVATE_RASTER"

paths="rasters/$PRIVATE_RASTER/tilejson.json
rasters/$PRIVATE_RASTER/tiles/10/1/1.png
rasters/$PRIVATE_RASTER/point?lon=0&lat=0
rasters/$PRIVATE_RASTER/thumbnail.png
rasters/$PRIVATE_RASTER/export?format=geotiff
open-data/rasters/$PRIVATE_RASTER"

i=0
for p in $paths; do
  i=$((i + 1))
  c=$(curl -sS -o /dev/null -w '%{http_code}' "$API_BASE/$p")
  echo "  [$i] anon /$p -> $c"
  case "$c" in
    400|401|403|404) : ;;
    2*) echo "      FAIL: anon retrieved private raster data (leaked allow)"; fail=1 ;;
    *)  echo "      FAIL: unexpected status $c (stack down? wrong API_BASE?)"; fail=1 ;;
  esac
done

c=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $FORGED" \
  "$API_BASE/rasters/$PRIVATE_RASTER/tilejson.json")
echo "  [7] forged alg:none token -> $c"
case "$c" in
  401) : ;;
  2*) echo "      FAIL: forged token retrieved raster data"; fail=1 ;;
  *)  echo "      FAIL: expected 401 for a forged token, got $c"; fail=1 ;;
esac

if curl -sS "$API_BASE/wmts/1.0.0/WMTSCapabilities.xml" | grep -q "$PRIVATE_RASTER"; then
  echo "  [8] anon WMTS capabilities -> LISTS PRIVATE RASTER"; echo "      FAIL"; fail=1
else
  echo "  [8] anon WMTS capabilities -> private raster not listed"
fi
if curl -sS "$API_BASE/wcs?SERVICE=WCS&VERSION=1.0.0&REQUEST=GetCapabilities" | grep -q "$PRIVATE_RASTER"; then
  echo "  [9] anon WCS capabilities  -> LISTS PRIVATE RASTER"; echo "      FAIL"; fail=1
else
  echo "  [9] anon WCS capabilities  -> private raster not listed"
fi

if [ "$fail" -eq 0 ]; then echo "inv10: PASS"; else echo "inv10: FAIL"; fi
exit "$fail"
