#!/usr/bin/env bash
# Invariant (I-6) — WCS coverage VALUES never bypass API authz or the downloadable fence.
#
# The threat: GetCoverage serves raw pixel values (the raster's actual data, not a picture).
# If an anonymous or forged caller could pull values from a private raster, or the view-only
# (`downloadable=false`) knob didn't gate the new door, the raster governance model is
# bypassable through the WCS façade.
#
# DENY paths asserted against BOTH doors (token-free, like inv04):
#   1. anon GetCoverage for a private raster (public door)   -> NOT 2xx (404 expected)
#   2. anon GetCoverage on the sign-in door                  -> 401 + Basic challenge
#   3. forged `alg:none` bearer                              -> 401
#   4. anon capabilities must NOT list the private raster id
#
# Deny rule: any 2xx on private values = leaked allow = FAIL. 400/401/403/404 = deny = pass.
# (The downloadable=false 403 needs a credentialed caller -> covered in verify-coverages.sh §D;
# this invariant stays token-free and expiry-immune.)
#
# PRIVATE_RASTER defaults to the dev rig's Test COG; override for other environments.
# bash 3.2-safe. -sS curl, status printed on every call.
set -u

API_BASE="${API_BASE:-http://localhost:8000/api/v1}"
PRIVATE_RASTER="${PRIVATE_RASTER:-bd20f4af-8c48-4cda-89d4-46e613bf05c4}"
FORGED='eyJhbGciOiJub25lIn0.eyJzdWIiOiJoYWNrZXIifQ.'   # alg:none, unsigned

fail=0
GC_Q="SERVICE=WCS&VERSION=1.0.0&REQUEST=GetCoverage&COVERAGE=$PRIVATE_RASTER&FORMAT=GeoTIFF&BBOX=0,0,1,1&WIDTH=8&HEIGHT=8"

echo "inv09: private raster = $PRIVATE_RASTER"

# 1) anon values on the public door -> must be denied
code=$(curl -sS -o /dev/null -w '%{http_code}' "$API_BASE/wcs?$GC_Q")
echo "  [1] anon GetCoverage (public door)   -> $code"
case "$code" in
  400|401|403|404) : ;;
  2*) echo "      FAIL: anon retrieved private raster values (leaked allow)"; fail=1 ;;
  *)  echo "      FAIL: unexpected status $code (stack down? wrong API_BASE?)"; fail=1 ;;
esac

# 2) anon on the sign-in door -> 401 + Basic
code=$(curl -sS -o /dev/null -w '%{http_code}' "$API_BASE/wcs-auth?$GC_Q")
echo "  [2] anon GetCoverage (sign-in door)  -> $code"
case "$code" in
  401) : ;;
  2*) echo "      FAIL: anon retrieved values on the sign-in door"; fail=1 ;;
  *)  echo "      FAIL: expected 401, got $code"; fail=1 ;;
esac

# 3) forged alg:none bearer -> 401 (and proves the route is live, so #1 is an authz denial)
code=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $FORGED" "$API_BASE/wcs-auth?$GC_Q")
echo "  [3] forged alg:none token            -> $code"
case "$code" in
  401) : ;;
  2*) echo "      FAIL: forged token retrieved values"; fail=1 ;;
  *)  echo "      FAIL: expected 401 for a forged token, got $code"; fail=1 ;;
esac

# 4) anon capabilities must not advertise the private raster
if curl -sS "$API_BASE/wcs?SERVICE=WCS&VERSION=1.0.0&REQUEST=GetCapabilities" | grep -q "$PRIVATE_RASTER"; then
  echo "  [4] anon capabilities                -> LISTS PRIVATE RASTER"
  echo "      FAIL: private raster advertised to anonymous callers"
  fail=1
else
  echo "  [4] anon capabilities                -> private raster not listed"
fi

if [ "$fail" -eq 0 ]; then echo "inv09: PASS"; else echo "inv09: FAIL"; fi
exit "$fail"
