#!/usr/bin/env bash
# Invariant C4 (media path) / threat T3 — attachment bytes are never retrievable without authorization.
#
# Bytes are always proxied through the API and every download runs _gate_read first (view, or
# anon-when-public). On a PRIVATE layer, an anon or non-viewer request for the bytes must be denied.
# Setup: owner creates a private layer, enables attachments, adds a feature + an attachment, then:
#   anon -> 404 (existence-hiding), non-viewer -> 403, owner -> 200 (control).
#
# Needs the running stack + .env (throwaways self-provisioned via _principals.sh). bash 3.2-safe; -sS curl.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv04b: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
fail=0
TMP="$(mktemp "${TMPDIR:-/tmp}/inv04b.XXXXXX")"; PNG="$TMP.png"
# 1x1 transparent PNG (unambiguous magic bytes for the upload allowlist / magic fence)
printf '%s' "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==" | base64 -d > "$PNG" 2>/dev/null || \
printf '%s' "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==" | base64 -D > "$PNG"
# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh
leg_cleanup() { rm -f "$PNG" "$TMP"; }   # runs from the helper's EXIT trap, before principal teardown

tv_mint publisher || { echo "inv04b: FAIL — no owner token"; rm -f "$PNG"; exit 1; }
OWNER="$TV_PUBLISHER_TOKEN"; OH="Authorization: Bearer $OWNER"

LID=$(curl -sS -X POST -H "$OH" -H 'Content-Type: application/json' \
  -d '{"display_name":"inv04b media test","geom_type":"point","working_srid":4326,"native_storage":false}' \
  "$API/layers" | python3 -c "import sys,json;print(json.load(sys.stdin).get('layer_id',''))" 2>/dev/null)
[ -n "$LID" ] || { echo "inv04b: FAIL — layer create failed"; exit 1; }
echo "  private layer $LID"

# enable attachments (owner/admin)
curl -sS -o /dev/null -X PUT -H "$OH" -H 'Content-Type: application/json' -d '{"attachmentsEnabled":true}' "$API/layers/${LID}/attachments-enabled"

# a feature to attach to
FID=$(curl -sS -X POST -H "$OH" -H 'Content-Type: application/json' -d '{"lon":-82.5,"lat":27.3}' \
  "$API/layers/${LID}/features" | python3 -c "import sys,json;print(json.load(sys.stdin).get('id',''))" 2>/dev/null)
[ -n "$FID" ] || { echo "inv04b: FAIL — feature create failed"; curl -sS -o /dev/null -X DELETE -H "$OH" "$API/layers/${LID}"; exit 1; }

# upload the attachment (multipart)
AID=$(curl -sS -X POST -H "$OH" -F "file=@${PNG};type=image/png" \
  "$API/layers/${LID}/features/${FID}/attachments" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('id') or d.get('attachmentId') or d.get('attachment_id',''))" 2>/dev/null)
[ -n "$AID" ] || { echo "inv04b: FAIL — attachment upload failed"; curl -sS -o /dev/null -X DELETE -H "$OH" "$API/layers/${LID}"; exit 1; }
echo "  attachment $AID uploaded"
DL="$API/layers/${LID}/attachments/${AID}"

# [1] anon — no token → denied (private layer, existence-hiding 404)
ac=$(curl -sS -o /dev/null -w '%{http_code}' "$DL")
echo "  [1] anon download        -> $ac (expect 404)"
case "$ac" in 401|403|404) : ;; 2*) echo "      FAIL: anon downloaded a private attachment"; fail=1 ;; *) echo "      FAIL: expected deny, got $ac"; fail=1 ;; esac

# [2] authenticated NON-viewer (throwaway viewer: has a users row + talus-viewer role, but NO
#     tuple on this fresh layer) → denied. tv_mint fails loudly, so no silent-owner false pass.
if ! tv_mint viewer; then
  echo "  [2] non-viewer token FAILED to mint — cannot test"; fail=1
else
  nc=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TV_VIEWER_TOKEN" "$DL")
  echo "  [2] non-viewer download  -> $nc (expect 403/404)"
  case "$nc" in 403|404) : ;; 2*) echo "      FAIL: a non-viewer downloaded the attachment"; fail=1 ;; *) echo "      FAIL: expected deny, got $nc"; fail=1 ;; esac
fi

# [3] owner → 200 (control: the byte path works when authorized)
oc=$(curl -sS -o /dev/null -w '%{http_code}' -H "$OH" "$DL")
echo "  [3] owner download       -> $oc (expect 200)"
[ "$oc" = "200" ] || { echo "      FAIL: owner could not download its own attachment ($oc)"; fail=1; }

curl -sS -o /dev/null -X DELETE -H "$OH" "$API/layers/${LID}"
if [ "$fail" -eq 0 ]; then echo "inv04b: PASS"; else echo "inv04b: FAIL"; fi
exit "$fail"
