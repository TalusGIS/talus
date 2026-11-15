#!/usr/bin/env bash
# Invariant (story-maps attachment picker) — the layer-wide attachment LISTING reveals nothing past
# the view gate, is authenticated-only, is bounded by the server, and never crosses layers.
#
# The model (decision-story-maps-attachment-picker.md §4.1/§5): GET /layers/{id}/attachments rides
# the SAME _gate_read as every sibling attachment door, but behind get_current_user (F2: an
# authoring affordance — anon is 401 before the registry lookup, even on a PUBLIC layer; never the
# siblings' anon-when-public). Rows carry id/feature_id/filename/content_type/byte_size/created_at
# ONLY — never object_key or created_by. `limit` ≤ 200 and `feature_id` ≤ 50 are pydantic-enforced.
#
# Setup: throwaway publisher creates two PRIVATE layers with attachments on —
#   A: feature f1 (PNG + PDF), feature f2 (PNG)     B: feature g1 (PNG)
# Legs:
#   1. anon                          -> 401 (authenticated-only; never 2xx, never 404-probe)
#   2. forged `alg:none` token       -> 401
#   3. signed-in wrong-user (viewer) -> 403 (view gate)
#   4. owner on A                    -> 200; 3 rows; every feature_id ∈ {f1,f2} (B's g1 NEVER appears)
#   5. owner limit=201               -> 422; limit=1&kind=image -> numberReturned=1, numberMatched=2
#   6. owner kind=video              -> 422; kind=image -> 2 rows, all image/png (the PDF is gone)
#   7. row shape                     -> no object_key / created_by key on any row
#   -- make A PUBLIC (direct public_shared tuple, inv08 pattern; anon master switch ON) --
#   8. anon on PUBLIC A              -> STILL 401 (F2 holds; catches a copy-pasted get_optional_user)
#   9. the SIBLING per-feature door, anon on PUBLIC A -> 200 with rows, and NO row names its uploader
#      (decision-public-access.md F2, rider 2026-11-06: the anon surface never carries editor identity).
#      Control: the signed-in owner's answer to the same URL DOES carry created_by_name, so the leg
#      cannot pass on a door that simply stopped returning the key to everyone.
#
# Deny rule: any 2xx from anon/forged/wrong-user = leaked allow = FAIL. A row from B under A = FAIL.
# Needs the running stack + .env (throwaways via _principals.sh). bash 3.2-safe; -sS curl.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv34: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
FORGED='eyJhbGciOiJub25lIn0.eyJzdWIiOiJoYWNrZXIifQ.'
fail=0
TMP="$(mktemp -d)"

# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh
tv_mint publisher || { echo "inv34: FAIL — no owner token"; exit 1; }
OWNER="$TV_PUBLISHER_TOKEN"
tv_mint viewer || { echo "inv34: FAIL — no viewer token"; exit 1; }
WRONG="$TV_VIEWER_TOKEN"
JSON='Content-Type: application/json'

jget() { python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

mk_layer() { # <name> -> layer_id on stdout
  curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" \
    -d "{\"display_name\":\"$1\",\"geom_type\":\"point\",\"working_srid\":4326,\"native_storage\":false}" \
    "$API/layers" | jget 'd.get("layer_id","")'
}
mk_feature() { # <layer_id> <lon> <lat> -> feature id on stdout
  curl -sS -X POST -H "Authorization: Bearer $OWNER" -H "$JSON" -d "{\"lon\":$2,\"lat\":$3}" \
    "$API/layers/$1/features" | jget 'd.get("id","")'
}
upload() { # <layer_id> <feature_id> <file> <content-type> -> http code on stdout
  curl -sS -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $OWNER" \
    -F "file=@$3;type=$4" "$API/layers/$1/features/$2/attachments"
}

# Fixtures: a valid 1x1 PNG (~68 B) and a minimal PDF (magic `%PDF`) — python3 keeps the binary writes portable.
python3 -c "import base64,sys; sys.stdout.buffer.write(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=='))" > "$TMP/ok.png"
printf '%%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%%%EOF\n' > "$TMP/doc.pdf"

LA="$(mk_layer 'inv34 listing gate A')"; LB="$(mk_layer 'inv34 listing gate B')"
case "$LA:$LB" in tal_*:tal_*) : ;; *) echo "inv34: FAIL — layer create failed (A='$LA' B='$LB')"; exit 1 ;; esac
for L in "$LA" "$LB"; do
  curl -sS -o /dev/null -X PUT -H "Authorization: Bearer $OWNER" -H "$JSON" \
    -d '{"attachmentsEnabled":true}' "$API/layers/$L/attachments-enabled"
done
F1="$(mk_feature "$LA" -82.40 27.30)"; F2="$(mk_feature "$LA" -82.41 27.31)"; G1="$(mk_feature "$LB" -82.50 27.40)"
[ -n "$F1" ] && [ -n "$F2" ] && [ -n "$G1" ] || { echo "inv34: FAIL — feature create failed"; exit 1; }
u1="$(upload "$LA" "$F1" "$TMP/ok.png" image/png)"
u2="$(upload "$LA" "$F1" "$TMP/doc.pdf" application/pdf)"
u3="$(upload "$LA" "$F2" "$TMP/ok.png" image/png)"
u4="$(upload "$LB" "$G1" "$TMP/ok.png" image/png)"
[ "$u1$u2$u3$u4" = "201201201201" ] || { echo "inv34: FAIL — fixture uploads ($u1 $u2 $u3 $u4)"; exit 1; }
echo "inv34: private layers A=$LA (f1: png+pdf, f2: png) B=$LB (g1: png), owned by $TV_PUBLISHER_USER"
S="$API/layers/${LA}/attachments"

want() { # <label> <expect> <code>
  echo "  [$1] -> $3 (expect $2)"
  [ "$3" = "$2" ] || { echo "      FAIL"; fail=1; }
}
assert() { # <label> <python-expr over d (the owner GET body)> <url>
  body="$(curl -sS -H "Authorization: Bearer $OWNER" "$3")"
  if printf '%s' "$body" | python3 -c "import sys,json;d=json.load(sys.stdin);sys.exit(0 if ($2) else 1)" 2>/dev/null; then
    echo "  [$1] -> ok"
  else
    echo "  [$1] -> FAIL"; echo "      body: $(printf '%s' "$body" | cut -c1-300)"; fail=1
  fi
}

want "1 anon on private           " 401 \
  "$(curl -sS -o /dev/null -w '%{http_code}' "$S")"
want "2 forged alg:none           " 401 \
  "$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $FORGED" "$S")"
want "3 wrong-user (viewer)       " 403 \
  "$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $WRONG" "$S")"
want "4 owner on A (control)      " 200 \
  "$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $OWNER" "$S")"
assert "4 A lists 3 rows, all f1/f2  " \
  "d['numberMatched']==3 and len(d['attachments'])==3 and {r['feature_id'] for r in d['attachments']}=={'$F1','$F2'}" "$S"
assert "4 B's row never under A      " \
  "all(r['feature_id']!='$G1' for r in d['attachments'])" "$S"
want "5 owner limit=201           " 422 \
  "$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $OWNER" "$S?limit=201")"
assert "5 limit=1 kind=image bound   " \
  "d['numberReturned']==1 and len(d['attachments'])==1 and d['numberMatched']==2" "$S?limit=1&kind=image"
want "6 owner kind=video          " 422 \
  "$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $OWNER" "$S?kind=video")"
assert "6 kind=image drops the PDF   " \
  "len(d['attachments'])==2 and all(r['content_type']=='image/png' for r in d['attachments'])" "$S?kind=image"
assert "6 feature_id filter (f2 only)" \
  "len(d['attachments'])==1 and d['attachments'][0]['feature_id']=='$F2'" "$S?feature_id=$F2"
assert "7 rows carry no storage/actor" \
  "all('object_key' not in r and 'created_by' not in r and 'created_by_name' not in r for r in d['attachments'])" "$S"

# ---- anon master switch (decision-public-sharing-reachable.md) — inv31 pattern ----------------
# Leg 8 must be an HONEST public-layer probe: switch ON + a direct public_shared tuple is exactly the
# state in which the SIBLING doors answer anon 200. This door must still answer 401.
psql_q(){ docker compose exec -T postgres psql -U "${TV_PGUSER:-talus}" -d "${TV_PGDB:-talusgis}" -tA -c "$1" < /dev/null 2>/dev/null | tr -d '[:space:]'; }
INV34_ORIG_COL="$(psql_q "SELECT public_sharing_enabled FROM platform.org_settings WHERE id = true;")"
STORE=$(tv_env OPENFGA_STORE_ID); MODEL=$(tv_env OPENFGA_MODEL_ID)
fga_write() { # <writes|deletes>
  docker compose run --rm -T openfga-cli -c "curl -sS -X POST http://openfga:8080/stores/${STORE}/write -H 'content-type: application/json' -d '{\"authorization_model_id\":\"${MODEL}\",\"$1\":{\"tuple_keys\":[{\"user\":\"user:*\",\"relation\":\"public_shared\",\"object\":\"layer:${LA}\"}]}}'" >/dev/null 2>&1
}
leg_cleanup(){
  case "$INV34_ORIG_COL" in
    t) psql_q "UPDATE platform.org_settings SET public_sharing_enabled = true WHERE id = true;" >/dev/null ;;
    f) psql_q "UPDATE platform.org_settings SET public_sharing_enabled = false WHERE id = true;" >/dev/null ;;
    *) psql_q "UPDATE platform.org_settings SET public_sharing_enabled = NULL WHERE id = true;" >/dev/null ;;
  esac
  fga_write deletes
  curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/layers/$LA"
  curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OWNER" "$API/layers/$LB"
  rm -rf "$TMP"
}
psql_q "UPDATE platform.org_settings SET public_sharing_enabled = true WHERE id = true;" >/dev/null
fga_write writes
echo "  anon master switch: column was '${INV34_ORIG_COL:-NULL}' -> ON; A is public_shared (both restored by leg_cleanup)"

want "8 sibling: anon per-feature on PUBLIC A (positive control)" 200 \
  "$(curl -sS -o /dev/null -w '%{http_code}' "$API/layers/$LA/features/$F1/attachments")"
want "8 anon LISTING on PUBLIC A  " 401 \
  "$(curl -sS -o /dev/null -w '%{http_code}' "$S")"

# Leg 9 — the per-feature door answers anon on a public layer by design; what it must not do is say
# WHO uploaded each file. The owner control comes first: a fixture whose rows never had a name would
# make the anon assertion pass on anything.
PF="$API/layers/$LA/features/$F1/attachments"
assert "9 control: owner's rows name the uploader" \
  "len(d['attachments'])==2 and all(r.get('created_by_name') for r in d['attachments'])" "$PF"
curl -sS -o "$TMP/anon_pf.json" "$PF"
# One python read of the saved body prints a verdict line: "ok", or "bad <what was wrong>". No nested
# quoting inside a command substitution (the bash 3.2 trap).
anon_verdict="$(python3 - "$TMP/anon_pf.json" <<'PY'
import json, sys
try:
    rows = json.load(open(sys.argv[1]))["attachments"]
except Exception as exc:  # not JSON, or not the list shape: the leg cannot vouch for anything
    print("bad unreadable body (%s)" % exc.__class__.__name__); sys.exit(0)
leaked = sorted({k for r in rows for k in ("created_by_name", "created_by", "object_key") if k in r})
if len(rows) != 2:
    print("bad expected 2 rows, got %d" % len(rows))
elif leaked:
    print("bad rows carry %s" % ", ".join(leaked))
else:
    print("ok")
PY
)"
if [ "$anon_verdict" = "ok" ]; then
  echo "  [9 anon per-feature on PUBLIC A names no uploader] -> ok"
else
  echo "  [9 anon per-feature on PUBLIC A names no uploader] -> FAIL"; echo "      $anon_verdict"; fail=1
fi

if [ "$fail" = 0 ]; then echo "inv34: PASS"; else echo "inv34: FAIL"; exit 1; fi
