#!/usr/bin/env bash
# Invariant — every request body
# has a ceiling, and it applies BEFORE the caller is known. Until the fix nothing bounded a body: not
# the edge, not the api. FastAPI reads a JSON body before it resolves who is calling, and the Esri
# doors read a form before the principal is known, so one anonymous request could make the api hold
# whatever it was sent. Now api/app/body_limit.py refuses any body over 32 MiB with 413 — before a
# byte is read when the length is declared, and as it arrives when it is not — and the query-type
# Esri doors carry their own 4 MiB form cap (geoservices.py _QUERY_FORM_MAX_BYTES, once-over A2).
#
# Token-free: every request here is ANONYMOUS, which is the point — the refusal needs no credential.
#   [1] control: a tiny JSON POST to the web-maps create door, no credential      -> 401
#       (the door is live and wants a sign-in; so a 413 below is about SIZE, not a dead route)
#   [2] 34 MiB JSON POST, declared Content-Length, no credential                  -> 413, not 401
#   [3] 34 MiB JSON POST, chunked (no declared length), no credential             -> 413, not 401
#   [4] control: a tiny form POST to the Esri discovery document /esri/rest/info  -> 200
#   [5] 5 MiB form POST to the same Esri door, declared length                    -> 413
#   [6] 5 MiB form POST to the same Esri door, chunked                            -> 413
#
# The bodies are STREAMED into curl (head -c … | curl --data-binary @-): never a shell variable.
# curl itself buffers what it reads from stdin, so the test costs this machine ~34 MiB for a moment.
#
# Read against the base URL the suite uses (API_BASE). The ceiling lives in the api, so the answer
# is the same through the edge as long as the edge passes the request on; the edge has no body rule
# of its own (decision F4).
#
# A `000` from curl is a connection the server closed while curl was still sending (the server
# answers and hangs up without reading the rest). It is retried, twice at most, and said so: it is
# never counted as a pass.
#
# PRE-FIX BASELINE: not run (written on the fix branch). Expected on the old code, from reading it:
# [2] and [3] answer 401 after the whole body has been buffered; [5] and [6] answer 200.
#
# Needs only the running stack. bash 3.2-safe; -sS curl.
set -u
API_BASE="${API_BASE:-http://localhost:8000/api/v1}"
JSON_DOOR="$API_BASE/web-maps"
ESRI_DOOR="$API_BASE/esri/rest/info"
BIG=$((34 * 1024 * 1024))        # over the 32 MiB default (settings.request_body_max_mb)
ESRI_BIG=$((5 * 1024 * 1024))    # over the Esri query-form cap of 4 MiB
fail=0
OUT="$(mktemp "${TMPDIR:-/tmp}/inv54-body.XXXXXX")" || { echo "inv54: FAIL — mktemp"; exit 1; }
ERR="$(mktemp "${TMPDIR:-/tmp}/inv54-err.XXXXXX")" || { echo "inv54: FAIL — mktemp"; exit 1; }
trap 'rm -f "$OUT" "$ERR"' EXIT

echo "inv54: request-body ceiling = $API_BASE (anonymous throughout)"

# big_post <bytes> <fill: zero|a> <url> <content-type> <chunked: yes|no> -> http code on stdout.
# The response body is left in $OUT and curl's own complaint in $ERR, for the caller to print.
big_post() {
  _bytes="$1"; _fill="$2"; _url="$3"; _ctype="$4"; _chunked="$5"
  _try=1
  while :; do
    if [ "$_chunked" = "yes" ]; then
      _c="$(head -c "$_bytes" /dev/zero | { if [ "$_fill" = "a" ]; then tr '\0' 'a'; else cat; fi; } \
        | curl -sS --max-time 120 -o "$OUT" -w '%{http_code}' -X POST \
            -H "Content-Type: $_ctype" -H 'Transfer-Encoding: chunked' --data-binary @- "$_url" 2>"$ERR")"
    else
      _c="$(head -c "$_bytes" /dev/zero | { if [ "$_fill" = "a" ]; then tr '\0' 'a'; else cat; fi; } \
        | curl -sS --max-time 120 -o "$OUT" -w '%{http_code}' -X POST \
            -H "Content-Type: $_ctype" --data-binary @- "$_url" 2>"$ERR")"
    fi
    if [ "$_c" = "000" ] && [ "$_try" -lt 3 ]; then
      echo "      note: no status (attempt $_try) — $(head -1 "$ERR" | cut -c1-160) — retrying" >&2
      _try=$((_try + 1)); sleep 1
      continue
    fi
    break
  done
  printf '%s' "$_c"
}
show() { # print what came back when a leg is not what it should be
  echo "      body: $(cut -c1-300 "$OUT" 2>/dev/null)"
  [ -s "$ERR" ] && echo "      curl: $(head -1 "$ERR" | cut -c1-200)"
  return 0
}
# want413 <label> <code> <what a non-413 means>
want413() {
  echo "  [$1] -> $2 (expect 413)"
  case "$2" in
    413) : ;;
    401) echo "      FAIL: 401 — the body was read in full before the size was looked at"; show; fail=1 ;;
    2*)  echo "      FAIL: $2 — an over-size body was ACCEPTED ($3)"; show; fail=1 ;;
    000) echo "      FAIL: no status from the server after three attempts (stack down? wrong API_BASE?)"; show; fail=1 ;;
    *)   echo "      FAIL: expected 413, got $2"; show; fail=1 ;;
  esac
}

# [1] control — the JSON door is live and gated
c="$(curl -sS -o "$OUT" -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d '{}' "$JSON_DOOR" 2>"$ERR")"
echo "  [1 control: tiny JSON POST, no credential] -> $c (expect 401)"
[ "$c" = "401" ] || { echo "      FAIL: the control door did not answer 401 — the 413 legs below would prove nothing"; show; fail=1; }

# [2] over the ceiling, length declared: refused before the body is read
want413 "2 34 MiB JSON, declared length, no credential" \
  "$(big_post "$BIG" zero "$JSON_DOOR" application/json no)" "no ceiling"

# [3] over the ceiling, no declared length: cut off as it arrives
want413 "3 34 MiB JSON, chunked, no credential" \
  "$(big_post "$BIG" zero "$JSON_DOOR" application/json yes)" "no ceiling on an undeclared length"

# [4] control — the Esri discovery document answers an anonymous form POST
c="$(curl -sS -o "$OUT" -w '%{http_code}' -X POST --data-urlencode 'f=json' "$ESRI_DOOR" 2>"$ERR")"
echo "  [4 control: tiny form POST to /esri/rest/info, no credential] -> $c (expect 200)"
[ "$c" = "200" ] || { echo "      FAIL: the Esri control door did not answer 200 — legs 5 and 6 would prove nothing"; show; fail=1; }

# [5]/[6] the Esri query-form cap (4 MiB), on the same anonymous door
want413 "5 5 MiB form to /esri/rest/info, declared length" \
  "$(big_post "$ESRI_BIG" a "$ESRI_DOOR" application/x-www-form-urlencoded no)" "the Esri form cap did not apply"
want413 "6 5 MiB form to /esri/rest/info, chunked" \
  "$(big_post "$ESRI_BIG" a "$ESRI_DOOR" application/x-www-form-urlencoded yes)" "the Esri form cap did not apply to an undeclared length"

if [ "$fail" -eq 0 ]; then echo "inv54: PASS"; else echo "inv54: FAIL"; fi
exit "$fail"
