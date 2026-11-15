#!/usr/bin/env bash
# Invariant (once-over A1, decision-once-over-security-fixes.md) — the raster upload door never lets
# the UPLOADED BYTES choose what the server opens. Until the fix the door handed the spooled file to
# GDAL with every driver registered, so a text file under a .tif name was opened as a VRT: a
# "raster" whose pixels live in ANOTHER file or at a network address, read by the api container on
# the uploader's behalf, with GDAL's own error text returned to the caller. Now the file's leading
# bytes pick exactly ONE driver (GeoTIFF, JPEG 2000 or ERDAS IMG), anything else is refused before
# GDAL sees it, and the caller is told a fixed sentence.
#
#   [1] publisher POSTs a VRT document (its source is a LOCAL path that does not exist — never a
#       network address) -> 400, `detail` is EXACTLY the fixed "unsupported" sentence, and the body
#       carries none of: "VRT", "/storage", "No such file"
#   [2] publisher POSTs a polyglot — a TIFF signature followed by the same VRT text -> 400 with one
#       of the two fixed sentences, and again no GDAL text
#   [3] neither title appears in the publisher's raster list, and no platform.rasters row carries it
#       (in any status) — a refused upload leaves nothing behind
#   [4] positive control: a small real GeoTIFF (made by the api image's own rasterio) -> 201, IS
#       listed, then deleted. Without it [1]-[3] would pass on a door that refuses everything.
#   [5] (once-over 2026-11-15 F1) a real GeoTIFF that names its own overview file in a LOWER-CASE
#       `overviews` metadata domain, the reference written as bytes that are not valid text -> 400
#       with the fixed "unsupported" sentence, and no platform.rasters row. GDAL finds the domain
#       whatever its case; the check once compared the name case-sensitively and let this through.
#       The reference names 192.0.2.1 (TEST-NET-1, RFC 5737): nothing answers there, so even the
#       old code could reach nothing — the leg reads the door's answer, not a connection.
#
# ★ The two sentences are copied from api/app/raster_cog.py (_MSG_UNSUPPORTED / _MSG_UNREADABLE).
#   They are public wording; if they are reworded there, they must be reworded here.
#
# PRE-FIX BASELINE: not run (this file was written on the fix branch; the dev stack served the old
# code). Expected on the old code, from reading it: [1] is NOT a 400 with the fixed sentence — the
# VRT opens, the convert fails, and the 422 echoes GDAL's message.
#
# Deny rule: a 2xx on [1] or [2] = the server stored a file it must not open = FAIL.
# Needs the running stack + .env (throwaway via _principals.sh). bash 3.2-safe; jq-free; -sS curl.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv52: FAIL — cd"; exit 2; }
API="${API_BASE:-http://localhost:8000/api/v1}"
MSG_UNSUPPORTED='could not read this file as a raster. Supported formats are GeoTIFF/COG, JPEG 2000, and ERDAS IMG.'
MSG_UNREADABLE='could not read this raster: the file is damaged or incomplete.'
fail=0
TMP="$(mktemp -d "${TMPDIR:-/tmp}/inv52.XXXXXX")" || { echo "inv52: FAIL — mktemp"; exit 1; }
SFX="$(openssl rand -hex 4)"
T_VRT="inv52-vrt-$SFX"; T_POLY="inv52-polyglot-$SFX"; T_OK="inv52-control-$SFX"; T_OVR="inv52-ovr-case-$SFX"

# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh
RID=""      # the control raster, while it exists
LEAKED=""   # ids of anything [1]/[2] wrongly stored — removed so a red run leaves no residue
leg_cleanup() {   # runs from the helper's EXIT trap, before principal teardown
  for _r in $RID $LEAKED; do
    curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $PUB" "$API/rasters/$_r"
  done
  rm -rf "$TMP"
}
tv_mint publisher || { echo "inv52: FAIL — no publisher token"; exit 1; }
PUB="$TV_PUBLISHER_TOKEN"

jget() { python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }
psql_q() { docker compose exec -T postgres psql -U "${TV_PGUSER:-talus}" -d "${TV_PGDB:-talusgis}" -tA -c "$1" < /dev/null 2>/dev/null | tr -d '[:space:]'; }
# upload <title> <file> <body-out> -> http code on stdout (raw request body, the door's own shape)
upload() {
  curl -sS -o "$3" -w '%{http_code}' -X POST -H "Authorization: Bearer $PUB" \
    --data-binary "@$2" "$API/rasters?title=$1"
}

# ---- fixtures ---------------------------------------------------------------------------------
# A VRT is XML that says "my pixels are in that other file". The source here is a local path that
# does not exist, so even a server that DID open it could read nothing and reach no network.
cat > "$TMP/disguised.vrt" <<'XML'
<VRTDataset rasterXSize="16" rasterYSize="16">
  <SRS>EPSG:4326</SRS>
  <GeoTransform>-82.6, 0.0125, 0.0, 27.4, 0.0, -0.0125</GeoTransform>
  <VRTRasterBand dataType="Byte" band="1">
    <SimpleSource>
      <SourceFilename relativeToVRT="0">/storage/inv52-does-not-exist.tif</SourceFilename>
      <SourceBand>1</SourceBand>
      <SrcRect xOff="0" yOff="0" xSize="16" ySize="16"/>
      <DstRect xOff="0" yOff="0" xSize="16" ySize="16"/>
    </SimpleSource>
  </VRTRasterBand>
</VRTDataset>
XML
# The polyglot: a little-endian TIFF signature (II*\0), then the VRT text. python3 writes the NUL.
python3 -c "import sys
sig = b'II*' + bytes(1)
open(sys.argv[2], 'wb').write(sig + open(sys.argv[1], 'rb').read())" "$TMP/disguised.vrt" "$TMP/polyglot.tif"

# One checker for a refusal body, kept in a file so no python source sits inside a $( ) (the bash
# 3.2 nested-quote trap). Prints "ok", or "bad <what was wrong>".
#   argv: <body-file> <allowed sentence> [<second allowed sentence>]
cat > "$TMP/check_refusal.py" <<'PY'
import json, sys
raw = open(sys.argv[1], "rb").read().decode("utf-8", "replace")
allowed = sys.argv[2:]
leaks = [w for w in ("VRT", "/storage", "No such file") if w in raw]
try:
    detail = json.loads(raw).get("detail")
except Exception as exc:
    print("bad body is not JSON (%s)" % exc.__class__.__name__)
    sys.exit(0)
if leaks:
    print("bad the body carries %s" % ", ".join(leaks))
elif detail not in allowed:
    print("bad detail is not the fixed sentence")
else:
    print("ok")
PY
# <list-json> <owned|all> <title>... -> "listed" if any title is in the caller's raster list, else "absent"
cat > "$TMP/titles.py" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
rows = d.get("owned", []) + (d.get("shared", []) if sys.argv[2] == "all" else [])
have = [r.get("title") for r in rows]
print("listed" if any(t in have for t in sys.argv[3:]) else "absent")
PY
# <container-output> <tif-out> -> writes the decoded GeoTIFF (or an empty file if there was none)
cat > "$TMP/decode_tif.py" <<'PY'
import base64, sys
lines = [l for l in open(sys.argv[1], "r", errors="replace").read().splitlines() if l.startswith("TIF:")]
raw = base64.b64decode(lines[-1][4:]) if lines else bytes()
open(sys.argv[2], "wb").write(raw if raw[:2] in (b"II", b"MM") else bytes())
PY

# [1] the VRT
rc="$(upload "$T_VRT" "$TMP/disguised.vrt" "$TMP/r1.json")"
v="$(python3 "$TMP/check_refusal.py" "$TMP/r1.json" "$MSG_UNSUPPORTED")"
echo "  [1] publisher uploads a VRT document -> $rc, body: ${v:-unreadable} (expect 400, ok)"
case "$rc" in
  400) [ "$v" = "ok" ] || { echo "      FAIL: $v"; echo "      body: $(cut -c1-400 "$TMP/r1.json")"; fail=1; } ;;
  2*)  echo "      FAIL: the server ACCEPTED a VRT (leaked allow)"; echo "      body: $(cut -c1-400 "$TMP/r1.json")"; fail=1
       LEAKED="$LEAKED $(jget 'd.get("id","")' < "$TMP/r1.json")" ;;   # so leg_cleanup removes what it stored
  *)   echo "      FAIL: expected 400, got $rc"; echo "      body: $(cut -c1-400 "$TMP/r1.json")"; fail=1 ;;
esac

# [2] the polyglot — an accepted signature the GeoTIFF driver cannot read, or (if a future sniff
# tightens) a refused one. Either fixed sentence is right; GDAL's words are not.
rc="$(upload "$T_POLY" "$TMP/polyglot.tif" "$TMP/r2.json")"
v="$(python3 "$TMP/check_refusal.py" "$TMP/r2.json" "$MSG_UNREADABLE" "$MSG_UNSUPPORTED")"
echo "  [2] publisher uploads a TIFF-signature + VRT polyglot -> $rc, body: ${v:-unreadable} (expect 400, ok)"
case "$rc" in
  400) [ "$v" = "ok" ] || { echo "      FAIL: $v"; echo "      body: $(cut -c1-400 "$TMP/r2.json")"; fail=1; } ;;
  2*)  echo "      FAIL: the server ACCEPTED the polyglot (leaked allow)"; echo "      body: $(cut -c1-400 "$TMP/r2.json")"; fail=1
       LEAKED="$LEAKED $(jget 'd.get("id","")' < "$TMP/r2.json")" ;;
  *)   echo "      FAIL: expected 400, got $rc"; echo "      body: $(cut -c1-400 "$TMP/r2.json")"; fail=1 ;;
esac

# [3] nothing was left behind: not in the publisher's list, and no row in any status
curl -sS -o "$TMP/mine.json" -H "Authorization: Bearer $PUB" "$API/me/rasters"
listed="$(python3 "$TMP/titles.py" "$TMP/mine.json" all "$T_VRT" "$T_POLY" 2>/dev/null)"
rows="$(psql_q "SELECT count(*) FROM platform.rasters WHERE title IN ('$T_VRT','$T_POLY');")"
echo "  [3] refused uploads in /me/rasters: ${listed:-unreadable}; platform.rasters rows: ${rows:-unreadable} (expect absent, 0)"
[ "$listed" = "absent" ] || { echo "      FAIL: a refused upload is listed (or the list could not be read: $(cut -c1-200 "$TMP/mine.json"))"; fail=1; }
[ "$rows" = "0" ] || { echo "      FAIL: a refused upload left a platform.rasters row (or the count could not be read)"; fail=1; }

# [4] positive control — a real GeoTIFF, made by the api image's own rasterio (the same way the
# raster verify-*.sh scripts make theirs; no binary fixture is kept in the repo). It comes back as
# one base64 line behind a marker, so anything else the container prints is ignored.
docker compose exec -T api python - > "$TMP/ok.out" 2>/dev/null <<'PY'
import base64, sys
import numpy as np
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds
w = h = 64
data = (np.arange(w * h) % 251).astype("uint8").reshape(1, h, w)
with MemoryFile() as mem:
    with mem.open(driver="GTiff", width=w, height=h, count=1, dtype="uint8", crs="EPSG:4326",
                  transform=from_bounds(-82.60, 27.20, -82.40, 27.40, w, h)) as dst:
        dst.write(data)
    sys.stdout.write("TIF:" + base64.b64encode(mem.read()).decode() + "\n")
PY
python3 "$TMP/decode_tif.py" "$TMP/ok.out" "$TMP/ok.tif" 2>/dev/null
if [ -s "$TMP/ok.tif" ]; then
  rc="$(upload "$T_OK" "$TMP/ok.tif" "$TMP/r4.json")"
  RID="$(jget 'd.get("id","")' < "$TMP/r4.json")"
  echo "  [4] control: publisher uploads a real GeoTIFF -> $rc (expect 201)"
  if [ "$rc" = "201" ] && [ -n "$RID" ]; then
    curl -sS -o "$TMP/mine2.json" -H "Authorization: Bearer $PUB" "$API/me/rasters"
    seen="$(python3 "$TMP/titles.py" "$TMP/mine2.json" owned "$T_OK" 2>/dev/null)"
    echo "      the accepted raster in /me/rasters: ${seen:-unreadable} (expect listed — so [3]'s 'absent' means something)"
    [ "$seen" = "listed" ] || { echo "      FAIL: an accepted raster is not in the owner's list — [3] proves nothing"; fail=1; }
    dc="$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE -H "Authorization: Bearer $PUB" "$API/rasters/$RID")"
    echo "      [cleanup] delete the control raster -> $dc (expect 200)"
    if [ "$dc" = "200" ]; then RID=""; else echo "      FAIL: control raster cleanup ($dc)"; fail=1; fi
  else
    echo "      FAIL: the door refused a real GeoTIFF — [1] and [2] prove nothing on a door that refuses everything"
    echo "      body: $(cut -c1-400 "$TMP/r4.json")"; fail=1
  fi
else
  echo "  [4] FAIL: could not make the control GeoTIFF in the api container (is the stack up?) — without it"
  echo "      [1]-[3] cannot be told from a door that refuses everything"; fail=1
fi

# [5] the overview reference in a lower-case domain (once-over 2026-11-15 F1). The fixture is made
# by the api image's own rasterio, then the reference's placeholder is replaced by bytes that are
# not valid UTF-8, as the unit tests do.
docker compose exec -T api python - > "$TMP/ovr.out" 2>/dev/null <<'PY'
import base64, os, sys, tempfile
import numpy as np
import rasterio
from rasterio.transform import from_bounds
work = tempfile.mkdtemp()
path = os.path.join(work, "upload.tif")
target = b"http://192.0.2.1/" + bytes([0xFF, 0xFE]) + b".tif"
marker = "Z" * len(target)
with rasterio.open(path, "w", driver="GTiff", width=64, height=64, count=1, dtype="uint8",
                   crs="EPSG:4326", transform=from_bounds(-82.60, 27.20, -82.40, 27.40, 64, 64)) as dst:
    dst.write(np.zeros((1, 64, 64), dtype="uint8"))
with rasterio.open(path, "r+", driver="GTiff") as ds:
    ds.update_tags(ns="overviews", OVERVIEW_FILE=marker)
data = open(path, "rb").read()
if data.count(marker.encode()) == 1:
    sys.stdout.write("TIF:" + base64.b64encode(data.replace(marker.encode(), target)).decode() + "\n")
PY
python3 "$TMP/decode_tif.py" "$TMP/ovr.out" "$TMP/ovr.tif" 2>/dev/null
if [ -s "$TMP/ovr.tif" ]; then
  rc="$(upload "$T_OVR" "$TMP/ovr.tif" "$TMP/r5.json")"
  v="$(python3 "$TMP/check_refusal.py" "$TMP/r5.json" "$MSG_UNSUPPORTED")"
  echo "  [5] publisher uploads a GeoTIFF naming its overview file in a lower-case domain -> $rc, body: ${v:-unreadable} (expect 400, ok)"
  case "$rc" in
    400) [ "$v" = "ok" ] || { echo "      FAIL: $v"; echo "      body: $(cut -c1-400 "$TMP/r5.json")"; fail=1; } ;;
    2*)  echo "      FAIL: the server ACCEPTED a file that names its own overview source (leaked allow)"; fail=1
         LEAKED="$LEAKED $(jget 'd.get("id","")' < "$TMP/r5.json")" ;;
    *)   echo "      FAIL: expected 400, got $rc"; echo "      body: $(cut -c1-400 "$TMP/r5.json")"; fail=1 ;;
  esac
  rows="$(psql_q "SELECT count(*) FROM platform.rasters WHERE title = '$T_OVR';")"
  echo "      platform.rasters rows for it: ${rows:-unreadable} (expect 0)"
  [ "$rows" = "0" ] || { echo "      FAIL: the refused upload left a platform.rasters row (or the count could not be read)"; fail=1; }
else
  echo "  [5] FAIL: could not make the overview-reference fixture in the api container"; fail=1
fi

if [ "$fail" = "0" ]; then echo "inv52: PASS"; else echo "inv52: FAIL"; exit 1; fi
