#!/usr/bin/env bash
# Security invariant suite runner. Runs every inv*.sh in this directory.
#
# Leg exit codes (decision-clean-box-verification.md F7 — honest semantics on any box):
#   0 = PASS
#   3 = SKIP — a genuine environmental prerequisite is missing on this box; the leg printed a
#       one-line reason. SKIPs never block but are always listed loudly.
#   anything else = FAIL. A test whose basename (minus .sh) is in KNOWN_GAPS may fail without
#       failing the build — reported loudly as a KNOWN GAP. Any OTHER failing test -> overall
#       exit 1 (release blocker). BLOCKED is reserved for real reds, never missing fixtures.
#   PARTIAL — not an exit code: a paid-module leg that passed with some legs skipped BY NAME because
#       they need the dev rig, or a prerequisite the product itself reports missing (_paid.sh). It counts as a PASS and is ALSO listed on its own summary
#       line, so a run that could not try everything never reads as a plain OK
#       (decision-paid-invariants-live.md).
#
# After the loop, the residue sweep (F8): the run FAILs if any tverify-* throwaway principal
# survived — a Keycloak user present, or a platform.users row still ENABLED. (Disabled,
# audit-anchored tverify-* rows are REMOVED from the roster (V091 removed_at) rather than merely
# disabled — the sweep's bar is roster presence, not the enabled flag. Before 2026-08-25 it checked
# `disabled_at IS NULL`, which let 133 disabled throwaways accumulate on the Users page unnoticed.)
#
# Requires the full stack up + ./.env beside the repo (principals self-provision from it).
# bash 3.2-safe.
set -u

# Space-separated ids (basename without .sh) allowed to fail. Move an id OUT when fixed.
# inv06-audit-role graduated to REQUIRED with slice S-1a (least-privilege runtime role) — its RED
# is now a real release blocker. (Runs against DB_ROLE=talus_app; passes once S-1a is applied.)
KNOWN_GAPS=""

here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/../.." && pwd)"
overall=0
gap_hits=""
pass_hits=""
skip_hits=""

is_known_gap() { case " $KNOWN_GAPS " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }

# _paid.sh appends `<inv>(<n>)` here when a leg passes with dev-only legs skipped.
TV_PARTIAL_FILE="$(mktemp "${TMPDIR:-/tmp}/tv-partial.XXXXXX")" || TV_PARTIAL_FILE=""
export TV_PARTIAL_FILE

for t in "$here"/inv*.sh; do
  [ -f "$t" ] || continue
  name="$(basename "$t" .sh)"
  echo "==================================================================="
  echo ">> $name"
  bash "$t"
  rc=$?
  if [ "$rc" -eq 0 ]; then
    pass_hits="$pass_hits $name"
  elif [ "$rc" -eq 3 ]; then
    echo ">> $name: SKIP (reason above) — not a failure; unavailable on this box"
    skip_hits="$skip_hits $name"
  elif is_known_gap "$name"; then
    echo ">> $name: KNOWN GAP (rc=$rc) — not failing the build"
    gap_hits="$gap_hits $name"
  else
    echo ">> $name: FAIL (rc=$rc) — RELEASE BLOCKER"
    overall=1
  fi
  echo ""
done

# ---- residue sweep (F8): no throwaway principal may survive the suite -------------------------
echo "==================================================================="
echo ">> residue sweep (tverify-*)"
res_fail=0
cd "$root" || { echo "   sweep: FAIL — cannot cd to repo root"; res_fail=1; }
if [ "$res_fail" -eq 0 ] && [ -f ./.env ]; then
  _rv() { v="$(grep -E "^$1=" ./.env 2>/dev/null | head -1 | cut -d= -f2-)"; v="${v%\"}"; v="${v#\"}"; printf '%s' "$v"; }
  KP="$(_rv KEYCLOAK_PORT)"; KP="${KP:-8080}"
  KC="${KEYCLOAK_URL:-http://localhost:$KP}"
  REALM="${KEYCLOAK_REALM:-talus}"
  AU="$(_rv KEYCLOAK_ADMIN_USER)"; AU="${AU:-admin}"
  AP="$(_rv KEYCLOAK_ADMIN_PASSWORD)"
  AT=$(curl -sS -X POST "$KC/realms/master/protocol/openid-connect/token" \
    -d grant_type=password -d client_id=admin-cli -d username="$AU" -d "password=$AP" \
    | python3 -c "import sys,json;print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null)
  if [ -n "$AT" ]; then
    kc_res=$(curl -sS "$KC/admin/realms/$REALM/users?username=tverify-&max=100" -H "Authorization: Bearer $AT" \
      | python3 -c "import sys,json;u=json.load(sys.stdin);print('\n'.join(x['username'] for x in u))" 2>/dev/null)
    if [ -n "$kc_res" ]; then
      echo "   RESIDUE: surviving tverify-* Keycloak users (credentials still live!):"
      printf '%s\n' "$kc_res" | sed 's/^/     /'
      res_fail=1
    else
      echo "   Keycloak: no tverify-* users (clean)"
    fi
  else
    echo "   sweep: FAIL — could not mint a KC admin token to audit residue"
    res_fail=1
  fi
  PU="$(_rv POSTGRES_USER)"; PU="${PU:-talus}"; PD="$(_rv POSTGRES_DB)"; PD="${PD:-talusgis}"
  db_res=$(docker compose exec -T postgres psql -U "$PU" -d "$PD" -tA < /dev/null \
    -c "SELECT username FROM platform.users WHERE username LIKE 'tverify-%' AND removed_at IS NULL;" 2>/dev/null)
  if [ -n "$db_res" ]; then
    echo "   RESIDUE: tverify-* platform.users rows still ON THE ROSTER:"
    printf '%s\n' "$db_res" | sed 's/^/     /'
    res_fail=1
  else
    echo "   platform.users: no tverify-* rows on the roster (clean)"
  fi
else
  [ "$res_fail" -eq 0 ] && { echo "   sweep: FAIL — ./.env not found"; res_fail=1; }
fi
if [ "$res_fail" -ne 0 ]; then
  echo ">> residue sweep: FAIL — throwaway principals survived (see above) — RELEASE BLOCKER"
  overall=1
else
  echo ">> residue sweep: clean"
fi
echo ""

echo "==================================================================="
partial_hits=""
if [ -n "$TV_PARTIAL_FILE" ] && [ -f "$TV_PARTIAL_FILE" ]; then
  partial_hits="$(tr '\n' ' ' < "$TV_PARTIAL_FILE")"; rm -f "$TV_PARTIAL_FILE"
fi
echo "PASS:      ${pass_hits:- (none)}"
echo "PARTIAL:   ${partial_hits:- (none)}  (passed; (n) legs could not run on this box — dev rig only, or a missing prerequisite — each named above)"
echo "SKIP:      ${skip_hits:- (none)}"
echo "KNOWN GAP: ${gap_hits:- (none)}"
if [ "$overall" -eq 0 ]; then
  echo "RESULT: OK (SKIPs and PARTIALs are loud but never block; known gaps do not block; fix them to graduate)"
else
  echo "RESULT: BLOCKED — a required invariant failed"
fi
exit "$overall"
