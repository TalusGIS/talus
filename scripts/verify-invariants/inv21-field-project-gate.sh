#!/usr/bin/env bash
# Invariant — field-project gates (decision-field-projects.md §5). Field projects are
# credential-bearing opaque artifacts (a .qgz may embed per-crew PATs), so BOTH new doors
# fail closed: upload is publisher-gated, serving is credentialed with no anon path at all
# (401 + Basic challenge on every id — no existence signal), wrong-user reads are hidden
# (404), manage ops by non-owners are refused (403, the rasters _load_for_manage shape),
# and a revoked PAT — the field-device credential — dies at the door:
#
#   [1]  anon GET  archive (random id)    -> 401 (never 2xx, never 404 — challenge posture)
#   [2]  anon POST /field-projects        -> 401
#   [3]  non-publisher POST               -> 403 (role-gated, not just signed-in)
#   [4]  publisher POST tiny valid .qgz   -> 201 fixture (positive create control)
#   [5]  viewer Bearer GET archive        -> 403 (no share; house _gate_read posture)
#   [6]  viewer PUT  archive (replace)    -> 403 (manage is owner-or-admin)
#   [7]  viewer DELETE                    -> 403
#   [8]  forged alg:none JWT GET archive  -> 401
#   [9]  viewer PAT (live, unshared) GET  -> 401 (the NO_VIEW invite — never bytes)
#   [10] owner PAT REVOKED         GET    -> 401 (revocation cuts field access immediately)
#   [11] publisher garbage-bytes upload   -> 4xx rejected AND owned-list count unchanged
#   [12] archive suffix/traversal probes  -> never 2xx
#   [13] positive control: owner Bearer GET archive -> 200
#   [14] a deleted group's share does not pass to the next group of the same name (once-over A9,
#        decision-once-over-security-fixes.md): the project is shared to a throwaway group (control:
#        a member opens it, 200; OpenFGA holds the shared_group tuple); the group is deleted ->
#        OpenFGA holds NO shared_group tuple from it on the project; a NEW group with the same name
#        (so the same id) gains the viewer — who never had access — as a member -> the viewer is
#        still refused the project and its archive (403, the same answer as [5]; never 2xx)
#   cleanup: fixture deleted (204), viewer PAT revoked
#
# PRE-SLICE BASELINE (recorded 2026-08-16): routes absent — [1]/[2] returned 404 (wrong code
# for the contract), [4]/[13] FAIL outright; recorded per the negative-test-first discipline.
#
# Needs the running stack + .env (throwaways self-provisioned via _principals.sh). bash 3.2-safe; jq-free.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv21: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
FP="$API/field-projects"
jget(){ python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }
fail=0
RANDOM_ID="00000000-0000-4000-8000-0000deadbeef"
FORGED='eyJhbGciOiJub25lIn0.eyJzdWIiOiJoYWNrZXIifQ.'

# [14] reads OpenFGA directly (inv05's helper) and owns a throwaway group. The group is removed by
# leg_cleanup — _principals.sh runs it from its EXIT trap — if the leg stops before its own cleanup.
OPENFGA="http://openfga:8080"
[ -n "${OPENFGA_STORE_ID:-}" ] || OPENFGA_STORE_ID=$(grep '^OPENFGA_STORE_ID=' .env | cut -d= -f2)
OPENFGA_STORE_ID="${OPENFGA_STORE_ID%\"}"; OPENFGA_STORE_ID="${OPENFGA_STORE_ID#\"}"
fga() { docker compose run --rm -T openfga-cli -c "$1" 2>/dev/null; }
group_share_count() {  # <group-id> <project-id> -> how many shared_group tuples that group holds on the project (or ERR)
  # An answer without a `tuples` list (an OpenFGA error document, or nothing) is ERR, never 0.
  _gresp=$(fga "curl -sS -X POST ${OPENFGA}/stores/${OPENFGA_STORE_ID}/read -H 'content-type: application/json' -d '{\"tuple_key\":{\"object\":\"field_project:$2\",\"relation\":\"shared_group\"}}'")
  printf '%s' "$_gresp" | python3 -c "import sys,json;t=json.load(sys.stdin)['tuples'];print(len([x for x in t if x.get('key',{}).get('user')==sys.argv[1]]))" "group:$1" 2>/dev/null || echo "ERR"
}
GRP=""
leg_cleanup() {
  [ -n "$GRP" ] && [ -n "${PUB:-}" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $PUB" "$API/groups/$GRP"
  return 0
}

# fixture bytes: a minimal genuine .qgz (zip containing a .qgs) + a garbage non-zip file
QGZ="$(mktemp "${TMPDIR:-/tmp}/inv21-fixture.XXXXXX")"
GARBAGE="$(mktemp "${TMPDIR:-/tmp}/inv21-garbage.XXXXXX")"
python3 - "$QGZ" <<'PYEOF'
import sys, zipfile
with zipfile.ZipFile(sys.argv[1], "w") as z:
    z.writestr("project.qgs", '<qgis projectname="inv21" version="3.44"><projectlayers/></qgis>')
PYEOF
printf 'not a qgz at all' > "$GARBAGE"
cleanup_files(){ rm -f "$QGZ" "$GARBAGE"; }

# [1] anon serve — challenge, never bytes, never a 404 existence hint
rc=$(curl -sS -o /dev/null -w '%{http_code}' "$FP/$RANDOM_ID/archive")
echo "  [1] anon archive -> $rc (expect 401)"
[ "$rc" = "401" ] || { echo "      FAIL: anon archive must be 401, got $rc"; fail=1; }

# [2] anon upload
rc=$(curl -sS -o /dev/null -w '%{http_code}' -X POST --data-binary "@$QGZ" "$FP?title=inv21")
echo "  [2] anon upload -> $rc (expect 401)"
[ "$rc" = "401" ] || { echo "      FAIL: anon upload must be 401, got $rc"; fail=1; }

# [3] non-publisher upload (throwaway viewer — no publisher role)
# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh
tv_mint viewer || { echo "inv21: FAIL — no viewer token"; cleanup_files; exit 1; }
VWR="$TV_VIEWER_TOKEN"
rc=$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $VWR" \
  --data-binary "@$QGZ" "$FP?title=inv21")
echo "  [3] non-publisher upload -> $rc (expect 403)"
case "$rc" in
  403) : ;;
  2*) echo "      FAIL: non-publisher can upload ($rc)"; fail=1 ;;
  *)  echo "      FAIL: expected 403, got $rc"; fail=1 ;;
esac

# [9-prep] viewer PAT minted while the viewer token is fresh
VPAT_JSON=$(curl -sS -X POST -H "Authorization: Bearer $VWR" -H 'Content-Type: application/json' \
  -d '{"label":"inv21-viewer","scope":"read_only"}' "$API/me/tokens")
VPAT=$(printf '%s' "$VPAT_JSON" | jget 'd["token"]')
VPAT_ID=$(printf '%s' "$VPAT_JSON" | jget 'd["id"]')

# [4] publisher creates the fixture (positive create control — the pre-build red)
tv_mint publisher || { echo "inv21: FAIL — no publisher token"; cleanup_files; exit 1; }
PUB="$TV_PUBLISHER_TOKEN"
CREATE_JSON=$(curl -sS -X POST -H "Authorization: Bearer $PUB" --data-binary "@$QGZ" \
  "$FP?title=inv21%20fixture")
FPID=$(printf '%s' "$CREATE_JSON" | jget 'd["id"]')
if [ -n "${FPID:-}" ]; then
  echo "  [4] publisher create -> id $FPID (ok)"
else
  echo "  [4] publisher create FAILED: $CREATE_JSON"; fail=1
fi

if [ -n "${FPID:-}" ]; then
  # [5] viewer read of an unshared project — refused (house _gate_read posture)
  rc=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $VWR" "$FP/$FPID/archive")
  echo "  [5] viewer archive -> $rc (expect 403)"
  case "$rc" in
    403) : ;;
    2*) echo "      FAIL: unshared project served to wrong user ($rc)"; fail=1 ;;
    *)  echo "      FAIL: expected 403, got $rc"; fail=1 ;;
  esac

  # [6] viewer replace
  rc=$(curl -sS -o /dev/null -w '%{http_code}' -X PUT -H "Authorization: Bearer $VWR" \
    --data-binary "@$QGZ" "$FP/$FPID/archive")
  echo "  [6] viewer replace -> $rc (expect 403)"
  case "$rc" in
    403) : ;;
    2*) echo "      FAIL: wrong user can replace the artifact ($rc)"; fail=1 ;;
    *)  echo "      FAIL: expected 403, got $rc"; fail=1 ;;
  esac

  # [7] viewer delete
  rc=$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE -H "Authorization: Bearer $VWR" "$FP/$FPID")
  echo "  [7] viewer delete -> $rc (expect 403)"
  case "$rc" in
    403) : ;;
    2*) echo "      FAIL: wrong user can delete ($rc)"; fail=1 ;;
    *)  echo "      FAIL: expected 403, got $rc"; fail=1 ;;
  esac

  # [8] forged identity
  rc=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $FORGED" "$FP/$FPID/archive")
  echo "  [8] forged JWT -> $rc (expect 401)"
  [ "$rc" = "401" ] || { echo "      FAIL: forged token not rejected ($rc)"; fail=1; }

  # [9] wrong user through the field-device credential path (live PAT, no share) — the
  # NO_VIEW 401 invite ("use another token"), never bytes
  if [ -n "${VPAT:-}" ]; then
    rc=$(curl -sS -o /dev/null -w '%{http_code}' -u "talus:$VPAT" "$FP/$FPID/archive")
    echo "  [9] viewer PAT archive -> $rc (expect 401)"
    case "$rc" in
      401) : ;;
      2*) echo "      FAIL: unshared project served to wrong-user PAT ($rc)"; fail=1 ;;
      *)  echo "      FAIL: expected 401 (invite), got $rc"; fail=1 ;;
    esac
  else
    echo "  [9] SKIP-FAIL: viewer PAT mint failed"; fail=1
  fi

  # [10] revoked owner PAT — mint as publisher, prove it works is NOT required; prove revoked dies
  OPAT_JSON=$(curl -sS -X POST -H "Authorization: Bearer $PUB" -H 'Content-Type: application/json' \
    -d '{"label":"inv21-owner","scope":"read_only"}' "$API/me/tokens")
  OPAT=$(printf '%s' "$OPAT_JSON" | jget 'd["token"]')
  OPAT_ID=$(printf '%s' "$OPAT_JSON" | jget 'd["id"]')
  if [ -n "${OPAT:-}" ] && [ -n "${OPAT_ID:-}" ]; then
    curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $PUB" "$API/me/tokens/$OPAT_ID"
    rc=$(curl -sS -o /dev/null -w '%{http_code}' -u "talus:$OPAT" "$FP/$FPID/archive")
    echo "  [10] revoked owner PAT -> $rc (expect 401)"
    [ "$rc" = "401" ] || { echo "      FAIL: revoked PAT still serves bytes ($rc)"; fail=1; }
  else
    echo "  [10] SKIP-FAIL: owner PAT mint failed"; fail=1
  fi

  # [11] malformed upload rejected AND owned-list state unchanged
  before=$(curl -sS -H "Authorization: Bearer $PUB" "$API/me/field-projects" | jget 'len(d.get("owned",[]))')
  rc=$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $PUB" \
    --data-binary "@$GARBAGE" "$FP?title=inv21-garbage")
  echo "  [11] garbage upload -> $rc (expect 4xx reject)"
  case "$rc" in
    4*) : ;;
    *)  echo "      FAIL: malformed upload not rejected (got $rc)"; fail=1 ;;
  esac
  after=$(curl -sS -H "Authorization: Bearer $PUB" "$API/me/field-projects" | jget 'len(d.get("owned",[]))')
  [ "${before:-ERR}" = "${after:-ERR2}" ] || { echo "      FAIL: rejected upload changed owned count ($before -> $after)"; fail=1; }

  # [12] archive addresses exactly one object — suffix/traversal probes never 2xx
  for probe in "$FP/$FPID/archive/../../../media" "$FP/$FPID/archive/extra" "$FP/$FPID/archive%2f..%2fsecret"; do
    rc=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $PUB" "$probe")
    case "$rc" in
      2*) echo "  [12] probe $probe -> $rc  FAIL: suffixed archive path returned bytes"; fail=1 ;;
      *)  echo "  [12] probe -> $rc (not 2xx — ok)" ;;
    esac
  done

  # [13] positive control — the deny legs aren't a dead route
  rc=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $PUB" "$FP/$FPID/archive")
  echo "  [13] owner archive -> $rc (expect 200)"
  [ "$rc" = "200" ] || { echo "      FAIL: owner cannot fetch own artifact ($rc)"; fail=1; }

  # [14] a deleted group's share is not inherited by the next group of the same name (once-over A9).
  # A group's id is derived from its NAME. Deleting a group used to remove its shares on three item
  # types and leave them on four — field projects among them, whose archive embeds an access token —
  # so "delete Field Crew, create Field Crew" handed the old shares to the new members.
  JSONH='Content-Type: application/json'
  GNAME="inv21 crew $(openssl rand -hex 4)"
  SHARE_OFF='{"organizationOnly":false,"groups":[],"publicAccess":false}'
  if ! tv_mint editor; then
    echo "  [14] FAIL: no throwaway member for the control"; fail=1
  else
    MEM="$TV_EDITOR_TOKEN"; MEM_ID="$TV_EDITOR_ID"
    GRP=$(curl -sS -X POST -H "Authorization: Bearer $PUB" -H "$JSONH" -d '{"name":"'"$GNAME"'"}' "$API/groups" \
      | jget 'd.get("groupId") or d.get("group_id","")')
    if [ -z "${GRP:-}" ]; then
      echo "  [14] FAIL: group create"; fail=1
    else
      G1="$GRP"
      SHARE_ON='{"organizationOnly":false,"groups":["'"$G1"'"],"publicAccess":false}'
      rc=$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $PUB" -H "$JSONH" \
        -d '{"userIds":["'"$MEM_ID"'"]}' "$API/groups/$G1/members")
      rs=$(curl -sS -o /dev/null -w '%{http_code}' -X PUT -H "Authorization: Bearer $PUB" -H "$JSONH" \
        -d "$SHARE_ON" "$FP/$FPID/shares")
      echo "  [14] fixture: group $G1, member added -> $rc, project shared to it -> $rs (expect 200, 200)"
      { [ "$rc" = "200" ] && [ "$rs" = "200" ]; } || { echo "      FAIL: fixture"; fail=1; }
      sleep 0.3
      # controls: the share really grants access through the group, and really is a tuple
      rc=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $MEM" "$FP/$FPID")
      echo "  [14a] control: a member of the group opens the project -> $rc (expect 200)"
      [ "$rc" = "200" ] || { echo "      FAIL: the group share grants nothing — the legs below would pass on anything"; fail=1; }
      n=$(group_share_count "$G1" "$FPID")
      echo "  [14b] control: shared_group tuples from group:$G1 on the project -> $n (expect 1)"
      [ "$n" = "1" ] || { echo "      FAIL: expected exactly one tuple before the delete (ERR = OpenFGA could not be read)"; fail=1; }
      rc=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $VWR" "$FP/$FPID")
      echo "  [14c] the viewer (never a member) before any of this -> $rc (expect 403)"
      [ "$rc" = "403" ] || { echo "      FAIL: the viewer was expected to have no access ($rc)"; fail=1; }

      # delete the group
      rc=$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE -H "Authorization: Bearer $PUB" "$API/groups/$G1")
      echo "  [14d] owner deletes the group -> $rc (expect 200)"
      if [ "$rc" = "200" ]; then GRP=""; else echo "      FAIL: group delete ($rc)"; fail=1; fi
      n=$(group_share_count "$G1" "$FPID")
      echo "  [14e] shared_group tuples from the DELETED group on the project -> $n (expect 0)"
      [ "$n" = "0" ] || { echo "      INVARIANT VIOLATED: the deleted group's share survives (ERR = OpenFGA could not be read)"; fail=1; }
      rc=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $MEM" "$FP/$FPID")
      echo "  [14f] the former member after the delete -> $rc (expect 403)"
      case "$rc" in
        403) : ;;
        2*) echo "      INVARIANT VIOLATED: access through a deleted group ($rc)"; fail=1 ;;
        *)  echo "      FAIL: expected 403, got $rc"; fail=1 ;;
      esac

      # a NEW group with the same name — and so the same id — holding a different user
      G2=$(curl -sS -X POST -H "Authorization: Bearer $PUB" -H "$JSONH" -d '{"name":"'"$GNAME"'"}' "$API/groups" \
        | jget 'd.get("groupId") or d.get("group_id","")')
      if [ -z "${G2:-}" ]; then
        echo "  [14g] FAIL: could not create the second group of the same name"; fail=1
      else
        GRP="$G2"
        echo "  [14g] a new group of the same name -> id $G2 (expect the same id, $G1)"
        [ "$G2" = "$G1" ] || { echo "      FAIL: the id differs — this leg can no longer test inheritance by name"; fail=1; }
        rc=$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $PUB" -H "$JSONH" \
          -d '{"userIds":["'"$TV_VIEWER_ID"'"]}' "$API/groups/$G2/members")
        sleep 0.3
        rg=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $VWR" "$API/groups/$G2")
        echo "  [14h] the viewer joins the new group -> $rc; the viewer reads the group -> $rg (expect 200, 200 — the membership is real)"
        { [ "$rc" = "200" ] && [ "$rg" = "200" ]; } || { echo "      FAIL: the viewer is not a member — [14i] would prove nothing"; fail=1; }
        for sfx in "" "/archive"; do
          R=$(curl -sS -w '\n%{http_code}' -H "Authorization: Bearer $VWR" "$FP/$FPID$sfx")
          rc=$(printf '%s' "$R" | tail -1)
          echo "  [14i] a member of the NEW group on the project${sfx:+ ($sfx)} -> $rc (expect 403)"
          case "$rc" in
            403) : ;;
            2*) echo "      INVARIANT VIOLATED: the new group's member inherited the deleted group's share ($rc)"; fail=1 ;;
            *)  echo "      FAIL: expected 403, got $rc"; echo "      body: $(printf '%s' "$R" | sed '$d' | cut -c1-200)"; fail=1 ;;
          esac
        done
        n=$(group_share_count "$G2" "$FPID")
        echo "  [14j] shared_group tuples from the new group on the project -> $n (expect 0)"
        [ "$n" = "0" ] || { echo "      INVARIANT VIOLATED: a share the new group never made (ERR = OpenFGA could not be read)"; fail=1; }
        rc=$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE -H "Authorization: Bearer $PUB" "$API/groups/$G2")
        echo "  [14 cleanup] delete the second group -> $rc (expect 200)"
        if [ "$rc" = "200" ]; then GRP=""; else echo "      FAIL: group cleanup ($rc)"; fail=1; fi
      fi
      # the project is deleted just below; clearing its shares first keeps a red run from leaving a tuple
      curl -sS -o /dev/null -X PUT -H "Authorization: Bearer $PUB" -H "$JSONH" -d "$SHARE_OFF" "$FP/$FPID/shares"
    fi
  fi

  # cleanup: fixture out, so reruns start clean
  rc=$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE -H "Authorization: Bearer $PUB" "$FP/$FPID")
  echo "  [cleanup] delete fixture -> $rc (expect 204)"
  [ "$rc" = "204" ] || { echo "      FAIL: fixture cleanup ($rc)"; fail=1; }
else
  echo "  [5]-[13] SKIPPED-FAIL: no fixture (create failed)"; fail=1
fi

# cleanup: viewer PAT revoked (soft — audit row stays, matching house posture)
if [ -n "${VPAT_ID:-}" ]; then
  curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $VWR" "$API/me/tokens/$VPAT_ID" || true
fi
cleanup_files

if [ "$fail" = "0" ]; then echo "inv21: PASS"; else echo "inv21: FAIL"; exit 1; fi
