#!/usr/bin/env bash
# Invariant C4 (tiles) — tile access never bypasses API authz.
#
# The threat (T1): pg_tileserv connects straight to PostGIS; authz lives in the
# FastAPI tile proxy. If a browser could reach a private layer's tiles without
# passing that proxy's OpenFGA check, the whole item-permission model is bypassable.
#
# This asserts the DENY paths against the API tile route:
#   1. anon request for a PRIVATE layer's tile         -> NOT 200 (401/403/404)
#   2. forged `alg:none` bearer                         -> 401  (also proves the route
#                                                          exists & runs auth, so #1's
#                                                          404 is an authz denial, not a
#                                                          missing route)
#   3. malformed slot (regex-fence)                     -> 404
#
# Deny rule: any 2xx on a private tile = leaked allow = FAIL. 401/403/404 = deny = pass.
#
# TODO (positive control): with a viewer token that HAS access, the same route -> 200,
# and with a token that does NOT -> 403. _principals.sh can now mint the principals;
# still deferred to keep this test token-free and immune to token expiry.
#
# bash 3.2-safe. -sS curl, status printed on every call.
set -u

API_BASE="${API_BASE:-http://localhost:8000/api/v1}"
PRIVATE_SLOT="${PRIVATE_SLOT:-tal_2c09dee7}"   # a layer the anon caller must NOT see
Z=10; X=279; Y=427                              # any in-range tile; content is irrelevant to authz
FORGED='eyJhbGciOiJub25lIn0.eyJzdWIiOiJoYWNrZXIifQ.'   # alg:none, unsigned

fail=0
tile_url="$API_BASE/tiles/$PRIVATE_SLOT/$Z/$X/$Y.pbf"

echo "inv04: private tile = $tile_url"

# 1) anon -> must be denied
code=$(curl -sS -o /dev/null -w '%{http_code}' "$tile_url")
echo "  [1] anon private tile           -> $code"
case "$code" in
  401|403|404) : ;;
  200) echo "      FAIL: anon retrieved a private tile (leaked allow)"; fail=1 ;;
  *)   echo "      FAIL: unexpected status $code (stack down? wrong API_BASE?)"; fail=1 ;;
esac

# 2) forged alg:none bearer -> 401 (and proves the route is live)
code=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $FORGED" "$tile_url")
echo "  [2] forged alg:none token       -> $code"
case "$code" in
  401) : ;;
  200) echo "      FAIL: forged token retrieved a tile"; fail=1 ;;
  *)   echo "      FAIL: expected 401 for a forged token, got $code"; fail=1 ;;
esac

# 3) malformed slot -> 404 (regex fence rejects anything not tal_<8hex>)
bad_url="$API_BASE/tiles/evilslot/$Z/$X/$Y.pbf"
code=$(curl -sS -o /dev/null -w '%{http_code}' "$bad_url")
echo "  [3] malformed slot              -> $code"
case "$code" in
  404) : ;;
  200) echo "      FAIL: malformed slot served a tile"; fail=1 ;;
  *)   echo "      FAIL: expected 404 for a malformed slot, got $code"; fail=1 ;;
esac

if [ "$fail" -eq 0 ]; then echo "inv04: PASS"; else echo "inv04: FAIL"; fi
exit "$fail"
