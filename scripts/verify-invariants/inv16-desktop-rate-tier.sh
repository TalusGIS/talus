#!/usr/bin/env bash
# Invariant (desktop rate-limit tiering) — the ELEVATED bucket is unreachable without a VALIDATED PAT.
#
# The threat this closes by construction. `AnonRateLimitMiddleware` runs BEFORE auth, so it cannot ask
# the database whether a credential is real. The tempting fix for the desktop-throttling bug was to
# exempt (or elevate) any request carrying a `Basic` header — which would let an UNAUTHENTICATED
# attacker opt out of the anonymous throttle by sending a junk `Basic` header, silently removing the
# bound on credential-probing AND bulk egress from the public surface (T23 / T33). The shipped design
# instead elevates only on a sha256 the DB-side validator (auth.py::_pat_user) has vouched for.
#
# THIS TEST MANUFACTURES THE THROTTLE. A rate-limit check that only shows requests succeeding proves
# nothing — it is indistinguishable from a limiter that never fires. Every leg therefore asserts a
# TRANSITION: at least one non-429 followed by at least one 429. A leg that is 429 from its first
# response is INCONCLUSIVE (an already-drained bucket), not a pass — it waits for refill and retries.
#
# Legs (each on a DIFFERENT anon cost class, so each starts with its own live bucket):
#   1. no Authorization header      -> tile class (600/min)   -- the anon surface is UNCHANGED
#   2. junk non-PAT `Basic`         -> download class (10/min) -- the header-presence hole stays shut
#   3. PAT-SHAPED but never-issued  -> read class (120/min)    -- gated on VALIDATION, not on SHAPE
#
# Leg 3 is the sharp one: the secret is indistinguishable from a real PAT by inspection, so only an
# actual database lookup can tell them apart. It must still land on the anonymous bucket.
#
# Leg 4 (once-over A7, decision-once-over-security-fixes.md) is the same hole on the OTHER scheme. The
# limiter used to exempt any request whose Authorization header merely BEGAN with "Bearer " — so a
# junk `Bearer x` bought an unthrottled path to every door. Now a Bearer is exempt only once the api
# has VALIDATED it (auth.py records the token's hash for 60 s); every other Bearer is charged to a
# per-address class of its own (settings.bearer_bootstrap_rate_per_min, 600/min):
#   4. ~700 junk `Bearer junk-<n>` (a different one each time) -> rises to 429   (the hole is shut)
#      and, in the SAME burst, a real signed-in session token      -> 200        (a proven session is
#      never charged to the junk budget, even while that budget is spent)
# Leg 4 is the one leg here that needs a credential: a throwaway viewer via _principals.sh.
#
# Token-free by design (inv10/inv15 style) so it never flakes on an expired credential. The POSITIVE
# half of the proof — a real, validated PAT sailing past 120/min — needs a live token and is
# not part of this suite.
#
# NOTE: this drains three anon buckets for the test's own IP. It is deliberately the LAST invariant
# (inv16 sorts after inv15) so nothing downstream in run.sh inherits a spent bucket.
# ★ That stopped being true long ago (inv17 onward sort after it), and leg 4 drains a bucket the rest
# of the suite DOES use: every later script's first request with a freshly minted session token is an
# unproven Bearer. So leg 4 ends by WAITING ~20 s for that class to refill (10 a second) — see there.
set -u

API_BASE="${API_BASE:-http://localhost:8000/api/v1}"
JUNK_B64="$(printf 'talus:not-a-real-token' | base64)"
# Correctly SHAPED (^tpat_[A-Za-z0-9_-]{20,64}$) but never minted — the whole point of leg 3.
FAKE_PAT="tpat_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
FAKE_B64="$(printf 'talus:%s' "$FAKE_PAT" | base64)"

fail=0
echo "inv16: rate-limit tiering = $API_BASE (elevated bucket requires a VALIDATED PAT)"

# fire <count> <url> [header] -> prints one HTTP status per line.
# One curl process with N URLs: connection reuse keeps ~660 requests under a second, which matters
# because the bucket refills while we fire (a slow loop would never catch up to the capacity).
fire() {
  _n="$1"; _url="$2"; _hdr="${3:-}"
  _list=""; _i=0
  while [ "$_i" -lt "$_n" ]; do _list="$_list -o /dev/null $_url"; _i=$((_i + 1)); done
  if [ -n "$_hdr" ]; then
    curl -sS -H "$_hdr" -w '%{http_code}\n' $_list 2>/dev/null
  else
    curl -sS -w '%{http_code}\n' $_list 2>/dev/null
  fi
}

# leg <label> <count> <url> <header-or-empty>
# PASS requires BOTH halves of a real transition. Retries once after a refill pause if the bucket was
# already spent, so a back-to-back run reports honestly instead of passing on a stuck-429 sequence.
leg() {
  _label="$1"; _n="$2"; _url="$3"; _hdr="${4:-}"
  _attempt=1
  while [ "$_attempt" -le 3 ]; do
    _out="$(fire "$_n" "$_url" "$_hdr")"
    _first="$(printf '%s\n' "$_out" | head -1)"
    _n429="$(printf '%s\n' "$_out" | grep -c '^429$' || true)"
    _nok="$(printf '%s\n' "$_out" | grep -vc '^429$' || true)"
    if [ "$_first" = "429" ]; then
      echo "  [$_label] bucket already spent on attempt $_attempt — waiting 15s for refill"
      sleep 15
      _attempt=$((_attempt + 1))
      continue
    fi
    break
  done
  echo "  [$_label] $_n requests -> first=$_first, non-429=$_nok, 429=$_n429"
  if [ "$_first" = "429" ]; then
    echo "      FAIL: INCONCLUSIVE — throttled from the first request, so no transition was"
    echo "            manufactured. Another sweep is running, or the bucket never refilled."
    fail=1
  elif [ "$_n429" -eq 0 ]; then
    echo "      FAIL: NEVER THROTTLED — this caller reached the elevated bucket without a"
    echo "            validated PAT. The anonymous bound is off for it (leaked exemption)."
    fail=1
  else
    echo "      ok: rose to 429 after $_nok allowed — this caller is on the ANONYMOUS bucket"
  fi
}

# Belt-and-braces on leg 3, run FIRST while the read bucket is still live so this reads a true 401
# rather than a 429: an unissued PAT-shaped credential must be REJECTED by auth, which is why it can
# never enter the validated cache. If this ever returned 2xx the throttle question would be moot.
AUTHCODE="$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Basic $FAKE_B64" "$API_BASE/me/layers")"
echo "  [PAT-shaped, never issued -> auth] /me/layers -> $AUTHCODE"
case "$AUTHCODE" in
  401) echo "      ok: rejected (never resolves to a principal, so it can never be cached)" ;;
  429) echo "      ok(weak): throttled before auth could answer — bucket spent by a prior run" ;;
  2*)  echo "      FAIL: an unissued PAT-shaped credential AUTHENTICATED"; fail=1 ;;
  *)   echo "      FAIL: expected 401, got $AUTHCODE (stack down? wrong API_BASE?)"; fail=1 ;;
esac

# Paths need not exist: the limiter is middleware and charges the bucket BEFORE routing, so a 404
# still spends a token. That keeps this invariant free of any dependency on seeded data.
leg "anon: no Authorization header" 660 "$API_BASE/inv16-probe/tiles/1/1/1.pbf" ""
leg "junk non-PAT Basic"             16 "$API_BASE/inv16-probe/export"          "Authorization: Basic $JUNK_B64"
leg "PAT-shaped, never issued"      140 "$API_BASE/inv16-probe"                 "Authorization: Basic $FAKE_B64"

# ---- leg 4: a junk Bearer is throttled; a proven session is not (once-over A7) -------------------
# One curl process, one connection, ~720 requests from a config file: 700 junk Bearers (each a
# DIFFERENT token, so the budget is shown to be per ADDRESS, not per token), then the real session
# token, then 20 more junk. The real one sits INSIDE the burst on purpose: a check made a moment
# after the burst could pass on a bucket that had simply refilled by one token (10 a second).
bearer_leg() {
  tv_mint viewer || { echo "  [junk Bearer] FAIL — no throwaway session (needs ./.env + Keycloak)"; fail=1; return; }
  _real="$TV_VIEWER_TOKEN"
  _junk_url="$API_BASE/inv16-probe/bearer"     # need not exist: the limiter charges before routing
  _real_url="$API_BASE/me/layers"
  BEARER_CFG="$(mktemp "${TMPDIR:-/tmp}/inv16-bearer.XXXXXX")" || { echo "  [junk Bearer] FAIL — mktemp"; fail=1; return; }
  # The config holds the throwaway's session token for a few seconds (mktemp: owner-only); it is
  # removed below and again by leg_cleanup, which _principals.sh runs from its EXIT trap.
  # One block per request; `next` SEPARATES blocks (curl resets url/header/output/write-out at each),
  # so it is written before every block but the first — never trailing, which would be an empty request.
  {
    _i=1
    while [ "$_i" -le 700 ]; do
      [ "$_i" -gt 1 ] && printf 'next\n'
      printf 'url = "%s"\nheader = "Authorization: Bearer junk-%s"\noutput = "/dev/null"\nwrite-out = "J %%{http_code}\\n"\n' "$_junk_url" "$_i"
      _i=$((_i + 1))
    done
    printf 'next\nurl = "%s"\nheader = "Authorization: Bearer %s"\noutput = "/dev/null"\nwrite-out = "R %%{http_code}\\n"\n' "$_real_url" "$_real"
    while [ "$_i" -le 720 ]; do
      printf 'next\nurl = "%s"\nheader = "Authorization: Bearer junk-%s"\noutput = "/dev/null"\nwrite-out = "T %%{http_code}\\n"\n' "$_junk_url" "$_i"
      _i=$((_i + 1))
    done
  } > "$BEARER_CFG"

  _attempt=1
  while :; do
    # Control, and the step that PROVES the token to the api: a real session on a normal door is 200.
    # (Before each attempt, so the 60 s record is fresh however long a refill wait took.)
    _pre="$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $_real" "$_real_url")"
    _out="$(curl -sS -K "$BEARER_CFG" 2>/dev/null)"
    _first="$(printf '%s\n' "$_out" | grep '^J ' | head -1)"
    if [ "$_first" = "J 429" ] && [ "$_attempt" -lt 3 ]; then
      echo "  [junk Bearer] bucket already spent on attempt $_attempt — waiting 15s for refill"
      sleep 15
      _attempt=$((_attempt + 1))
      continue
    fi
    break
  done
  rm -f "$BEARER_CFG"

  _j429="$(printf '%s\n' "$_out" | grep -c '^J 429$' || true)"
  _jok="$(printf '%s\n' "$_out" | grep '^J ' | grep -vc '^J 429$' || true)"
  _jlast="$(printf '%s\n' "$_out" | grep '^J ' | tail -1)"
  _r="$(printf '%s\n' "$_out" | grep '^R ' | head -1)"
  _t429="$(printf '%s\n' "$_out" | grep -c '^T 429$' || true)"
  echo "  [real session before the burst] /me/layers -> $_pre (expect 200)"
  [ "$_pre" = "200" ] || { echo "      FAIL: the control session did not get 200 — nothing below can be read"; fail=1; }
  echo "  [junk Bearer] 700 requests -> first=${_first:-none}, non-429=$_jok, 429=$_j429"
  if [ "$((_jok + _j429))" -lt 700 ]; then
    echo "      FAIL: only $((_jok + _j429)) of 700 answers came back (stack down? curl too old for a config with 'next'?)"
    fail=1
  elif [ "$_first" = "J 429" ]; then
    echo "      FAIL: INCONCLUSIVE — throttled from the first request, so no transition was"
    echo "            manufactured. Another sweep is running, or the bucket never refilled."
    fail=1
  elif [ "$_j429" -eq 0 ]; then
    echo "      FAIL: NEVER THROTTLED — a junk Bearer header is exempt from the rate limiter."
    echo "            Anyone can opt out of every anonymous bound by sending 'Bearer x'."
    fail=1
  else
    echo "      ok: rose to 429 after $_jok allowed — an unproven Bearer is on a per-address budget"
  fi
  echo "  [real session INSIDE the burst] /me/layers -> ${_r:-none} (expect R 200); junk just before: ${_jlast:-none}; 429s in the 20 junk after: $_t429"
  case "$_r" in
    "R 200")
      if [ "$_jlast" = "J 429" ] || [ "$_t429" -gt 0 ]; then
        echo "      ok: a proven session is served while the junk budget around it is spent"
      else
        echo "      FAIL: INCONCLUSIVE — the junk budget was not spent around the real request, so its 200"
        echo "            does not show an exemption"; fail=1
      fi ;;
    "R 429") echo "      FAIL: a VALIDATED session was throttled by the junk-Bearer budget — a flood of junk"
             echo "            from one address locks that address's real users out"; fail=1 ;;
    *)       echo "      FAIL: expected 200 for the real session, got '${_r:-nothing}'"; fail=1 ;;
  esac

  # ★ Leave the class as we found it. The burst emptied this address's unproven-Bearer budget, and
  # every script run.sh starts after this one presents freshly minted session tokens — unproven on
  # their first request, so charged to the same budget. It refills at 10 a second; 20 s restores 200
  # requests' worth, far more first-presentations than the rest of the suite makes at once.
  echo "  [junk Bearer] waiting 20s for the unproven-Bearer budget to refill before the next script"
  sleep 20
}
BEARER_CFG=""
leg_cleanup() { [ -n "$BEARER_CFG" ] && rm -f "$BEARER_CFG"; return 0; }
# Legs 1-3 above are token-free and run from anywhere; this one needs ./.env for the throwaway.
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
if cd "$ROOT"; then
  # shellcheck disable=SC1091
  . scripts/verify-invariants/_principals.sh
  bearer_leg
else
  echo "  [junk Bearer] FAIL — cannot cd to the repo root for _principals.sh"; fail=1
fi

if [ "$fail" -eq 0 ]; then echo "inv16: PASS"; else echo "inv16: FAIL"; fi
exit "$fail"
