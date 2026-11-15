#!/usr/bin/env bash
# Invariant — field-project GENERATION + the minted-credential lifecycle
# (decision-generate-field-project.md §6). The server now MINTS credentials: "Make field
# project" compiles a web map into a .qgz and embeds a dedicated per-project PAT. The gate
# must hold at every step: publisher-only mint, manage-shape regenerate, and — the crux —
# NO ORPHANED LIVE CREDENTIALS: deleting the item revokes the embedded token, live.
#
#   [1]  anon generate (random map id)       -> 401
#   [2]  viewer (non-publisher) generate     -> 403 (role gate fires before map load)
#   [3]  publisher generate, absent map id   -> 404
#   [4]  publisher: fixture map -> generate  -> 201 (positive mint control)
#   [5]  minted PAT visible in the owner's token list: 'field-project: …' label, the
#        derived scope echoed by the response, no expiry, not revoked
#   [6]  the EMBEDDED PAT (extracted from the artifact) reads the ESRI door -> 200
#   [6b] (once-over 2026-11-15 K6) the embedded PAT edits a layer its publisher owns but the
#        project does NOT hold -> refused per edit (HTTP 200, error 403, the project sentence).
#        Before K6 the token carried no layer list and edited every layer its publisher may edit
#   [6c] control: the same edit on the project's own layer gets past the gate (no 403, no sentence)
#        — so [6b] fails for the layer list and nothing else. Both name OBJECTID 999999999, which no
#        fixture holds: refused or not, nothing is written
#   [7]  viewer regenerate                   -> 403 (owner-or-admin, the manage shape)
#   [8]  owner regenerate -> 200 AND contentSha256 rolls (crews' next fetch misses its 304)
#   [9]  delete item -> 204 AND the token shows revoked in the list (the auto-revoke)
#   [10] the extracted PAT now dies at the ESRI door -> 401 (the retirement story, live)
#   [11] map holding only an INVISIBLE layer -> 400, and the live-token count is unchanged
#        (nothing minted on a refused generate)
#   cleanup: fixture maps deleted, the auto-revoked PAT purged
#
# PRE-SLICE BASELINE (recorded 2026-08-16): routes absent on main — [1] 404, [2]/[3] 404,
# [4] FAIL outright (405/404); recorded per the negative-test-first discipline.
#
# Needs the running stack + dev tokens + advertised base URL set (Admin ▸ Connections) + at
# least one ready hosted tal_ layer. bash 3.2-safe; jq-free.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv22: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
FP="$API/field-projects"
jget(){ python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }
fail=0
RANDOM_ID="00000000-0000-4000-8000-0000deadbeef"
ARTIFACT="$(mktemp "${TMPDIR:-/tmp}/inv22-artifact.XXXXXX")"
cleanup_files(){ rm -f "$ARTIFACT"; }

# [1] anon generate — 401 (Bearer-only door)
rc=$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
  -d '{}' "$API/web-maps/$RANDOM_ID/field-project")
echo "  [1] anon generate -> $rc (expect 401)"
[ "$rc" = "401" ] || { echo "      FAIL: anon generate must be 401, got $rc"; fail=1; }

# [2] non-publisher generate — the role gate fires before anything else
# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh
tv_mint viewer || { echo "inv22: FAIL — no viewer token"; cleanup_files; exit 1; }
VWR="$TV_VIEWER_TOKEN"
rc=$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $VWR" \
  -H 'Content-Type: application/json' -d '{}' "$API/web-maps/$RANDOM_ID/field-project")
echo "  [2] non-publisher generate -> $rc (expect 403)"
case "$rc" in
  403) : ;;
  2*) echo "      FAIL: non-publisher can generate ($rc)"; fail=1 ;;
  *)  echo "      FAIL: expected 403, got $rc"; fail=1 ;;
esac

tv_mint publisher || { echo "inv22: FAIL — no publisher token"; cleanup_files; exit 1; }
PUB="$TV_PUBLISHER_TOKEN"

# [3] absent map — hidden, 404
rc=$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $PUB" \
  -H 'Content-Type: application/json' -d '{}' "$API/web-maps/$RANDOM_ID/field-project")
echo "  [3] publisher generate, absent map -> $rc (expect 404)"
[ "$rc" = "404" ] || { echo "      FAIL: expected 404, got $rc"; fail=1; }

# [4-prep] a hosted layer the publisher OWNS — created fresh (a clean box has no demo layers
# to lean on), with one feature so the packaged datasource has content — and a fixture map
LID=$(curl -sS -X POST -H "Authorization: Bearer $PUB" -H 'Content-Type: application/json' \
  -d '{"display_name":"inv22 fixture layer","geom_type":"point","working_srid":4326,"native_storage":false}' \
  "$API/layers" | jget 'd.get("layer_id","")')
if [ -z "${LID:-}" ]; then
  echo "inv22: FAIL — fixture layer create failed"; cleanup_files; exit 1
fi
# [6b]'s layer: the same publisher owns it, and the fixture map does not hold it
LOUT=$(curl -sS -X POST -H "Authorization: Bearer $PUB" -H 'Content-Type: application/json' \
  -d '{"display_name":"inv22 layer outside the project","geom_type":"point","working_srid":4326,"native_storage":false}' \
  "$API/layers" | jget 'd.get("layer_id","")')
[ -n "${LOUT:-}" ] || { echo "inv22: FAIL — the second fixture layer create failed"; fail=1; }
curl -sS -o /dev/null -X POST -H "Authorization: Bearer $PUB" -H 'Content-Type: application/json' \
  -d '{"lon":-82.5,"lat":27.3}' "$API/layers/$LID/features"
MID=$(curl -sS -X POST -H "Authorization: Bearer $PUB" -H 'Content-Type: application/json' \
  -d '{"title":"inv22 fixture map","definition":{"version":1,"basemap":"light","operationalLayers":[{"layerId":"'"$LID"'"}],"initialView":{"center":[-82.5,27.3],"zoom":15}}}' \
  "$API/web-maps" | jget 'd["id"]')
[ -n "${MID:-}" ] || { echo "inv22: FAIL — fixture map create failed"; cleanup_files; exit 1; }

# live-token baseline for [11]'s nothing-minted check
tok_count(){ curl -sS -H "Authorization: Bearer $PUB" "$API/me/tokens" \
  | jget 'len([t for t in d["tokens"] if not t.get("revoked_at")])'; }

# [4] the positive mint control
GEN_JSON=$(curl -sS -X POST -H "Authorization: Bearer $PUB" -H 'Content-Type: application/json' \
  -d '{}' "$API/web-maps/$MID/field-project")
FPID=$(printf '%s' "$GEN_JSON" | jget 'd["id"]')
PATSCOPE=$(printf '%s' "$GEN_JSON" | jget 'd["pat"]["scope"]')
if [ -n "${FPID:-}" ]; then
  echo "  [4] publisher generate -> id $FPID (scope $PATSCOPE) (ok)"
else
  case "$GEN_JSON" in
    *advertised*)
      # a genuine environmental prerequisite, not a fixture: generation refuses by design until
      # an admin sets the advertised base URL (Admin ▸ Connections). Loud SKIP, never BLOCKED —
      # but never a MASK: a red from the gate legs [1]-[3] (which need no advertised URL) must
      # survive this exit (security-review finding, 2026-08-20).
      curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $PUB" "$API/web-maps/$MID"
      curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $PUB" "$API/layers/$LID"
      [ -n "${LOUT:-}" ] && curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $PUB" "$API/layers/$LOUT"
      cleanup_files
      if [ "$fail" -eq 0 ]; then
        echo "inv22: SKIP — advertised base URL not configured (Admin ▸ Connections); generate legs cannot run on this box"
        exit 3
      fi
      echo "inv22: FAIL — gate legs [1]-[3] failed before the environmental SKIP (see above)"
      exit 1 ;;
  esac
  echo "  [4] publisher generate FAILED: $GEN_JSON"
  fail=1
fi

if [ -n "${FPID:-}" ]; then
  # [5] the minted PAT is findable + labeled + live in the owner's list
  TOK_JSON=$(curl -sS -H "Authorization: Bearer $PUB" "$API/me/tokens")
  TID=$(printf '%s' "$TOK_JSON" | jget 'next((t["id"] for t in d["tokens"] if t["label"].startswith("field-project: ") and not t.get("revoked_at")), "")')
  TSCOPE=$(printf '%s' "$TOK_JSON" | jget 'next((t["scope"] for t in d["tokens"] if t["id"]=="'"${TID:-none}"'"), "")')
  TEXP=$(printf '%s' "$TOK_JSON" | jget 'next((str(t.get("expires_at")) for t in d["tokens"] if t["id"]=="'"${TID:-none}"'"), "")')
  if [ -n "${TID:-}" ] && [ "$TSCOPE" = "${PATSCOPE:-}" ] && [ "$TEXP" = "None" ]; then
    echo "  [5] minted PAT in token list: label ok, scope $TSCOPE, no expiry (ok)"
  else
    echo "  [5] FAIL: minted PAT missing/mislabeled (id='${TID:-}' scope='$TSCOPE' vs '$PATSCOPE' expires='$TEXP')"; fail=1
  fi

  # [6] extract the EMBEDDED token from the artifact; it must be a real credential that
  # reads the ESRI door (the live control the retirement leg [10] needs)
  curl -sS -o "$ARTIFACT" -H "Authorization: Bearer $PUB" "$FP/$FPID/archive"
  EPAT=$(python3 - "$ARTIFACT" <<'PYEOF'
import re, sys, zipfile
try:
    zf = zipfile.ZipFile(sys.argv[1])
    doc = zf.read([n for n in zf.namelist() if n.endswith(".qgs")][0]).decode()
    m = re.search(r"talus:(tpat_[A-Za-z0-9_\-]+)@", doc)
    print(m.group(1) if m else "")
except Exception:
    print("")
PYEOF
)
  if [ -n "${EPAT:-}" ]; then
    rc=$(curl -sS -o /dev/null -w '%{http_code}' -u "talus:$EPAT" \
      "$API/esri-auth/rest/services/$LID/FeatureServer/0?f=json")
    echo "  [6] embedded PAT reads the ESRI door -> $rc (expect 200)"
    [ "$rc" = "200" ] || { echo "      FAIL: embedded PAT cannot read its own datasource ($rc)"; fail=1; }
  else
    echo "  [6] FAIL: no embedded tpat_ found in the artifact"; fail=1
  fi

  # [6b]/[6c] the embedded PAT writes only to the project's layers (once-over 2026-11-15 K6)
  if [ -n "${EPAT:-}" ] && [ -n "${LOUT:-}" ]; then
    K6_UPD='updates=[{"attributes":{"OBJECTID":999999999}}]'
    cat > "$ARTIFACT.verdict.py" <<'PYEOF'
import json, sys
mode, sentence = sys.argv[1], sys.argv[2]
raw = sys.stdin.read()
body, _, code = raw.rpartition("\n")
if code.strip() != "200":
    print("no: http " + code.strip() + ": " + body[:200]); sys.exit(0)
try:
    first = json.loads(body)["updateResults"][0]
except Exception as exc:
    print("no: unreadable result (" + exc.__class__.__name__ + "): " + body[:200]); sys.exit(0)
err = first.get("error") or {}
desc = str(err.get("description") or "")
if mode == "refused":
    ok = first.get("success") is False and err.get("code") == 403 and desc.startswith(sentence)
else:
    ok = sentence not in body and err.get("code") != 403
print("ok" if ok else "no: " + body[:200])
PYEOF
    K6_SENTENCE="This field project's credential can edit only the layers in that project."
    r="$(curl -sS -w '\n%{http_code}' -u "talus:$EPAT" -X POST \
      "$API/esri-auth/rest/services/$LOUT/FeatureServer/0/applyEdits" --data-urlencode 'f=json' --data-urlencode "$K6_UPD")"
    v="$(printf '%s' "$r" | python3 "$ARTIFACT.verdict.py" refused "$K6_SENTENCE")"
    echo "  [6b] embedded PAT edits a layer outside the project -> ${v:-unreadable} (expect ok: refused, the project sentence)"
    [ "$v" = "ok" ] || { echo "      FAIL (LEAKED ALLOW if it got past the gate): $v"; fail=1; }
    r="$(curl -sS -w '\n%{http_code}' -u "talus:$EPAT" -X POST \
      "$API/esri-auth/rest/services/$LID/FeatureServer/0/applyEdits" --data-urlencode 'f=json' --data-urlencode "$K6_UPD")"
    v="$(printf '%s' "$r" | python3 "$ARTIFACT.verdict.py" past-gate "$K6_SENTENCE")"
    echo "  [6c] control: the same edit on the project's own layer -> ${v:-unreadable} (expect ok: past the gate)"
    [ "$v" = "ok" ] || { echo "      FAIL: the project's own layer is refused too — [6b] proves nothing: $v"; fail=1; }
    rm -f "$ARTIFACT.verdict.py"
  else
    echo "  [6b] FAIL: no embedded PAT or no second fixture layer"; fail=1
  fi

  # [7] regenerate is manage-shaped
  rc=$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $VWR" "$FP/$FPID/regenerate")
  echo "  [7] viewer regenerate -> $rc (expect 403)"
  case "$rc" in
    403) : ;;
    2*) echo "      FAIL: wrong user can regenerate ($rc)"; fail=1 ;;
    *)  echo "      FAIL: expected 403, got $rc"; fail=1 ;;
  esac

  # [8] owner regenerate rolls the content identity (ETag/fresh-bytes)
  SHA1=$(curl -sS -H "Authorization: Bearer $PUB" "$FP/$FPID" | jget 'd["contentSha256"]')
  rc=$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $PUB" "$FP/$FPID/regenerate")
  SHA2=$(curl -sS -H "Authorization: Bearer $PUB" "$FP/$FPID" | jget 'd["contentSha256"]')
  echo "  [8] owner regenerate -> $rc (expect 200), sha roll ${SHA1:-?} -> ${SHA2:-?}"
  [ "$rc" = "200" ] || { echo "      FAIL: owner regenerate ($rc)"; fail=1; }
  { [ -n "${SHA1:-}" ] && [ -n "${SHA2:-}" ] && [ "$SHA1" != "$SHA2" ]; } \
    || { echo "      FAIL: contentSha256 did not roll on regenerate"; fail=1; }

  # [9] delete auto-revokes the embedded credential
  rc=$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE -H "Authorization: Bearer $PUB" "$FP/$FPID")
  echo "  [9] delete item -> $rc (expect 204)"
  [ "$rc" = "204" ] || { echo "      FAIL: delete ($rc)"; fail=1; }
  if [ -n "${TID:-}" ]; then
    TREV=$(curl -sS -H "Authorization: Bearer $PUB" "$API/me/tokens" \
      | jget 'next((str(t.get("revoked_at")) for t in d["tokens"] if t["id"]=="'"$TID"'"), "MISSING")')
    if [ "$TREV" != "None" ] && [ "$TREV" != "MISSING" ] && [ -n "$TREV" ]; then
      echo "  [9] embedded PAT auto-revoked (revoked_at $TREV) (ok)"
    else
      echo "      FAIL: embedded PAT not revoked after item delete (revoked_at='$TREV')"; fail=1
    fi
  fi

  # [10] the retirement story, live: the extracted token dies at the door
  if [ -n "${EPAT:-}" ]; then
    rc=$(curl -sS -o /dev/null -w '%{http_code}' -u "talus:$EPAT" \
      "$API/esri-auth/rest/services/$LID/FeatureServer/0?f=json")
    echo "  [10] revoked embedded PAT at the ESRI door -> $rc (expect 401)"
    [ "$rc" = "401" ] || { echo "      FAIL: revoked embedded PAT still works ($rc)"; fail=1; }
  fi

  # [11] a refused generate mints NOTHING (map holds only an invisible layer -> 400)
  MID2=$(curl -sS -X POST -H "Authorization: Bearer $PUB" -H 'Content-Type: application/json' \
    -d '{"title":"inv22 invisible fixture","definition":{"version":1,"basemap":"light","operationalLayers":[{"layerId":"'"$LID"'","visible":false}],"initialView":{"center":[-82.5,27.3],"zoom":15}}}' \
    "$API/web-maps" | jget 'd["id"]')
  before=$(tok_count)
  rc=$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $PUB" \
    -H 'Content-Type: application/json' -d '{}' "$API/web-maps/${MID2:-$RANDOM_ID}/field-project")
  after=$(tok_count)
  echo "  [11] zero-eligible-layers generate -> $rc (expect 400), live tokens $before -> $after"
  [ "$rc" = "400" ] || { echo "      FAIL: expected 400, got $rc"; fail=1; }
  [ "${before:-ERR}" = "${after:-ERR2}" ] || { echo "      FAIL: a refused generate changed the live-token count"; fail=1; }

  # cleanup: fixture maps out; the auto-revoked PAT purged (allowed only because it is dead)
  if [ -n "${MID2:-}" ]; then
    curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $PUB" "$API/web-maps/$MID2" || true
  fi
  if [ -n "${TID:-}" ]; then
    rc=$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE -H "Authorization: Bearer $PUB" "$API/me/tokens/$TID?purge=true")
    echo "  [cleanup] purge auto-revoked PAT -> $rc (expect 204)"
    [ "$rc" = "204" ] || { echo "      FAIL: purge of the dead PAT refused ($rc)"; fail=1; }
  fi
else
  echo "  [5]-[11] SKIPPED-FAIL: no generated fixture"; fail=1
fi

rc=$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE -H "Authorization: Bearer $PUB" "$API/web-maps/$MID")
echo "  [cleanup] delete fixture map -> $rc (expect 204)"
case "$rc" in 2*) : ;; *) echo "      FAIL: fixture map cleanup ($rc)"; fail=1 ;; esac
rc=$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE -H "Authorization: Bearer $PUB" "$API/layers/$LID")
echo "  [cleanup] delete fixture layer -> $rc (expect 200)"
case "$rc" in 2*) : ;; *) echo "      FAIL: fixture layer cleanup ($rc)"; fail=1 ;; esac
if [ -n "${LOUT:-}" ]; then
  rc=$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE -H "Authorization: Bearer $PUB" "$API/layers/$LOUT")
  echo "  [cleanup] delete the second fixture layer -> $rc (expect 200)"
  case "$rc" in 2*) : ;; *) echo "      FAIL: second fixture layer cleanup ($rc)"; fail=1 ;; esac
fi
cleanup_files

if [ "$fail" = "0" ]; then echo "inv22: PASS"; else echo "inv22: FAIL"; exit 1; fi
