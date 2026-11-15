#!/usr/bin/env bash
# Invariant C4 extension — the DYNAMIC tile path (/tiles-dyn, decision-dynamic-mvt.md) never
# bypasses authz, never lets a predicate near SQL unvalidated, and never lets a tile be cached.
#
# The threat: this is the one route where USER-INFLUENCED filter/field text travels to SQL next to
# tile generation. The route must (a) authz-gate exactly like the static tile proxy — anon 404
# existence-hiding, wrong-user 403, forged 401; (b) 422 anything the shared filter compiler doesn't
# prove (op whitelist, introspected-field whitelist), never 5xx (a 5xx on a probe = the payload
# REACHED SQL); (c) refuse the anon scrub set as tile attributes (Talus 23 — tiles must not become
# a created_by/provenance bypass); (d) mark every dynamic tile Cache-Control: no-store (a predicate
# in a shared cache key = poisoning).
#
# Setup mirrors inv07: owner creates a private layer (+1 feature so tiles have content), probes run,
# layer flips public for the anon legs, owner deletes (cleanup scrubs the tuple).
# Needs the running stack + .env (throwaways self-provisioned via _principals.sh). bash 3.2-safe; -sS curl.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv12: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
[ -n "${OPENFGA_STORE_ID:-}" ] || OPENFGA_STORE_ID=$(grep '^OPENFGA_STORE_ID=' .env | cut -d= -f2)
[ -n "${OPENFGA_MODEL_ID:-}" ] || OPENFGA_MODEL_ID=$(grep '^OPENFGA_MODEL_ID=' .env | cut -d= -f2)
OPENFGA="http://openfga:8080"
fga() { docker compose run --rm -T openfga-cli -c "$1" 2>/dev/null; }
FORGED='eyJhbGciOiJub25lIn0.eyJzdWIiOiJoYWNrZXIifQ.'   # alg:none, unsigned
# The z/x/y containing the seeded feature at (-82.5, 27.3), so the positive-control legs assert a
# 200 CONTENT tile, not just a 204 empty one.
read -r Z X Y <<EOF
$(python3 -c 'import math;z=12;lon=-82.5;lat=27.3;n=2**z
x=int((lon+180)/360*n);r=math.radians(lat)
y=int((1-math.log(math.tan(r)+1/math.cos(r))/math.pi)/2*n);print(z,x,y)')
EOF
fail=0

# owner token + a fresh private layer with one feature (so a 200 tile is even possible)
# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh
tv_mint publisher || { echo "inv12: FAIL — no owner token"; exit 1; }
OWNER="$TV_PUBLISHER_TOKEN"
LID=$(curl -sS -X POST -H "Authorization: Bearer $OWNER" -H 'Content-Type: application/json' \
  -d '{"display_name":"inv12 dyn tile test","geom_type":"point","working_srid":4326,"native_storage":false}' \
  "$API/layers" | python3 -c "import sys,json;print(json.load(sys.stdin).get('layer_id',''))" 2>/dev/null)
[ -n "$LID" ] || { echo "inv12: FAIL — layer create failed"; exit 1; }
curl -sS -o /dev/null -X POST -H "Authorization: Bearer $OWNER" -H 'Content-Type: application/json' \
  -d '{"lon":-82.5,"lat":27.3,"notes":"inv12"}' "$API/layers/${LID}/features"
echo "  layer $LID (private, owned by $TV_PUBLISHER_USER)"
dyn="$API/tiles-dyn/$LID/$Z/$X/$Y.pbf"

# --- authz legs (mirror inv04 on the dynamic route) --------------------------------------------
code=$(curl -sS -o /dev/null -w '%{http_code}' "$dyn")
echo "  [1] anon private dynamic tile        -> $code (expect 404 — existence-hiding)"
case "$code" in
  404) : ;;
  2*) echo "      FAIL: anon retrieved a private dynamic tile (leaked allow)"; fail=1 ;;
  *)  echo "      FAIL: expected 404, got $code"; fail=1 ;;
esac

tv_mint editor || { echo "inv12: FAIL — no stranger token"; exit 1; }
STRANGER="$TV_EDITOR_TOKEN"
code=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $STRANGER" "$dyn")
echo "  [2] wrong-user private dynamic tile  -> $code (expect 403)"
case "$code" in
  403) : ;;
  2*) echo "      FAIL: non-shared user retrieved a private dynamic tile"; fail=1 ;;
  *)  echo "      FAIL: expected 403, got $code"; fail=1 ;;
esac

code=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $FORGED" "$dyn")
echo "  [3] forged alg:none token            -> $code (expect 401)"
case "$code" in
  401) : ;;
  2*) echo "      FAIL: forged token retrieved a dynamic tile"; fail=1 ;;
  *)  echo "      FAIL: expected 401, got $code"; fail=1 ;;
esac

code=$(curl -sS -o /dev/null -w '%{http_code}' "$API/tiles-dyn/evilslot/$Z/$X/$Y.pbf")
echo "  [4] unregistered layer id            -> $code (expect 404)"
[ "$code" = "404" ] || { echo "      FAIL: expected 404 for an unregistered layer, got $code"; fail=1; }

# --- positive control: owner CAN tile (so the denials above are authz, not a dead route) --------
code=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $OWNER" "$dyn")
echo "  [5] owner dynamic tile (control)     -> $code (expect 200/204)"
case "$code" in
  200|204) : ;;
  *) echo "      FAIL: owner cannot fetch own dynamic tile ($code)"; fail=1 ;;
esac

# --- injection probes (owner token, so the request REACHES the compiler) ------------------------
# A probe must die in validation (422). Any 5xx = the payload got past validation into SQL = FAIL.
probe() { curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $OWNER" "$1"; }

code=$(probe "$dyn?filter=notes:DROP%20TABLE:x")
echo "  [6] op smuggling (bad op)            -> $code (expect 422)"
[ "$code" = "422" ] || { echo "      FAIL: expected 422, got $code"; fail=1; }

code=$(probe "$dyn?filter=notes%22%3BDROP%20TABLE%20assets.signs--:eq:x")
echo "  [7] field smuggling (quoted field)   -> $code (expect 422 — not a real column)"
[ "$code" = "422" ] || { echo "      FAIL: expected 422, got $code"; fail=1; }

code=$(probe "$dyn?filter=notes:eq:%27%3B%20DROP%20TABLE%20assets.signs%3B--")
echo "  [8] value payload (bound param)      -> $code (expect 200/204 — value is DATA, never SQL)"
case "$code" in
  200|204) : ;;
  5*) echo "      FAIL: a VALUE payload produced $code — it reached SQL unbound"; fail=1 ;;
  *)  echo "      FAIL: expected 200/204, got $code"; fail=1 ;;
esac

code=$(probe "$dyn?fields=notes%22%2C(SELECT%201)--")
echo "  [9] fields smuggling                 -> $code (expect 422 — not a real column)"
[ "$code" = "422" ] || { echo "      FAIL: expected 422, got $code"; fail=1; }

# --- anon legs: public layer, scrub + cache-header ----------------------------------------------
# Deployment-gated (the inv14 [3] pattern): with PUBLIC_ANON_ACCESS_ENABLED=false the anon surface
# doesn't exist — every anon request 404s at the gate (fail-closed), so there is no anon path to
# assert scrub/cache on. Skip [10]/[11] legibly; [12] (authed no-store) stays unconditional.
# tail -1: dotenv semantics are LAST-occurrence-wins, and a duplicated line in .env used to make
# this grep return "true\ntrue" — failing the compare and silently skipping [10]/[11] on a box
# where they should run (caught 2026-08-25 while verifying the borrowed-slot tile fix).
ANON_ON=$(grep -E '^PUBLIC_ANON_ACCESS_ENABLED=' .env | tail -1 | cut -d= -f2 | tr -d '"')
if [ "$ANON_ON" = "true" ]; then
  fga "curl -sS -X POST ${OPENFGA}/stores/${OPENFGA_STORE_ID}/write -H 'content-type: application/json' -d '{\"authorization_model_id\":\"${OPENFGA_MODEL_ID}\",\"writes\":{\"tuple_keys\":[{\"user\":\"user:*\",\"relation\":\"public_shared\",\"object\":\"layer:${LID}\"}]}}'" >/dev/null 2>&1

  code=$(curl -sS -o /dev/null -w '%{http_code}' "$dyn?fields=created_by")
  echo "  [10] anon fields=created_by          -> $code (expect 422 — tile is not a scrub bypass)"
  [ "$code" = "422" ] || { echo "      FAIL: expected 422, got $code"; fail=1; }

  # anon valid request: allowed (public) AND Cache-Control: no-store on the response
  hdr_file=$(mktemp); code=$(curl -sS -D "$hdr_file" -o /dev/null -w '%{http_code}' "$dyn?filter=notes:eq:inv12&fields=notes")
  cc=$(grep -i '^cache-control:' "$hdr_file" | tr -d '\r' | cut -d' ' -f2-); rm -f "$hdr_file"
  echo "  [11] anon public dynamic tile        -> $code / Cache-Control: ${cc:-<missing>} (expect 200|204 + no-store)"
  case "$code" in
    200|204) : ;;
    *) echo "      FAIL: public dynamic tile not served anon ($code)"; fail=1 ;;
  esac
  case "$cc" in
    *no-store*) : ;;
    *) echo "      FAIL: dynamic tile is cacheable (Cache-Control: ${cc:-missing}) — poisoning surface"; fail=1 ;;
  esac
else
  echo "  [10]/[11] skipped: PUBLIC_ANON_ACCESS_ENABLED != true (anon surface off in this deploy)"
fi

# owner (authenticated) response must ALSO be no-store
hdr_file=$(mktemp); curl -sS -D "$hdr_file" -o /dev/null -H "Authorization: Bearer $OWNER" "$dyn"
cc=$(grep -i '^cache-control:' "$hdr_file" | tr -d '\r' | cut -d' ' -f2-); rm -f "$hdr_file"
echo "  [12] authed dynamic tile cache hdr   -> Cache-Control: ${cc:-<missing>} (expect no-store)"
case "$cc" in
  *no-store*) : ;;
  *) echo "      FAIL: authed dynamic tile is cacheable (${cc:-missing})"; fail=1 ;;
esac

# cleanup (owner deletes → also scrubs the public_shared tuple)
curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/layers/${LID}"

if [ "$fail" -eq 0 ]; then echo "inv12: PASS"; else echo "inv12: FAIL"; fi
exit "$fail"
