#!/usr/bin/env bash
# derive-proj-db.sh — make, or check, db/proj/proj.db: the database's coordinate reference database
# with the three shifting NAD83 -> WGS 84 operations withdrawn (two for NAD83(HARN), one for NAD83(CSRS)).
#
# Why this file exists. The database image is its publisher's; Talus does not build it. Its
# coordinate library reads a reference database (proj.db) and, for NAD83(HARN) and Canada's original
# NAD83(CSRS), takes a shift that it takes for no other realization of NAD83 used in the United
# States, so a layer on either was drawn about 0.6 m or 1.2 m from every other layer of the same place
# (api/datum_grids/withdraw_nad83_shifts.py has the detail). db/proj/proj.db is the image's own file
# with those operations marked deprecated, mounted over the image's copy by docker-compose.yml. The
# api image makes the same change to its own two copies when it is built.
#
#   scripts/derive-proj-db.sh            write db/proj/proj.db and db/proj/SHA256SUMS from the
#                                        database image pinned in docker-compose.yml
#   scripts/derive-proj-db.sh --check    prove the shipped file is that image's own file with those
#                                        rows changed and nothing else; writes nothing
#
# ★ Run it again, and commit the result, whenever the database image's pin in docker-compose.yml
#   moves: the file belongs to one version of the library. --check is what tells you it is stale.
#
# Needs docker (the pinned image is pulled if it is not here) and python3. Starts no service and
# touches no running stack: the image's file is read with a one-off `docker run … cat`.
# bash 3.2-safe.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT" || { echo "derive-proj-db: cannot cd to the repository root" >&2; exit 2; }

MODE="write"
case "${1:-}" in
  "") : ;;
  --check) MODE="check" ;;
  *) echo "usage: scripts/derive-proj-db.sh [--check]" >&2; exit 2 ;;
esac

OUT="db/proj/proj.db"
SUMS="db/proj/SHA256SUMS"
WITHDRAW="api/datum_grids/withdraw_nad83_shifts.py"
[ -f "$WITHDRAW" ] || { echo "derive-proj-db: $WITHDRAW is missing" >&2; exit 2; }

# The database image: the one the `postgres` service itself is pinned to in docker-compose.yml (other
# services use the same image today; this reads the service the file is mounted into).
IMAGE="$(awk '
  /^  postgres:[[:space:]]*$/ { inside = 1; next }
  inside && /^  [A-Za-z0-9_-]+:[[:space:]]*$/ { inside = 0 }
  inside && $1 == "image:" && $2 ~ /^postgis\/postgis@sha256:[0-9a-f]+$/ { print $2; exit }
' docker-compose.yml)"
[ -n "$IMAGE" ] || { echo "derive-proj-db: the postgres service in docker-compose.yml is not pinned to a postgis image by digest" >&2; exit 2; }

TMP="$(mktemp -d "${TMPDIR:-/tmp}/talus-projdb.XXXXXX")" || { echo "derive-proj-db: cannot make a temporary folder" >&2; exit 2; }
cleanup() { [ -n "${TMP:-}" ] && [ -d "$TMP" ] && rm -f "$TMP/own.db" "$TMP/changed.db" && rmdir "$TMP" 2>/dev/null; }
trap cleanup EXIT

if ! docker run --rm --network none --entrypoint cat "$IMAGE" /usr/share/proj/proj.db > "$TMP/own.db" 2> "$TMP/err.txt"; then
  echo "derive-proj-db: could not read /usr/share/proj/proj.db out of $IMAGE" >&2
  head -3 "$TMP/err.txt" >&2; rm -f "$TMP/err.txt"; exit 2
fi
rm -f "$TMP/err.txt"
[ -s "$TMP/own.db" ] || { echo "derive-proj-db: the image's proj.db came back empty" >&2; exit 2; }

sha() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'; else shasum -a 256 "$1" | awk '{print $1}'; fi; }

# What the file SHOULD be: the image's own, with the operations withdrawn by the same script the
# api image's build uses. Made in the temporary folder in both modes; --check compares against it.
cp "$TMP/own.db" "$TMP/changed.db" || exit 2
python3 "$WITHDRAW" "$TMP/changed.db" > "$TMP/err.txt" 2>&1 || { cat "$TMP/err.txt" >&2; rm -f "$TMP/err.txt"; echo "derive-proj-db: the operations could not be withdrawn from the image's file" >&2; exit 1; }
rm -f "$TMP/err.txt"

if [ "$MODE" = "write" ]; then
  mkdir -p db/proj && cp "$TMP/changed.db" "$OUT" || exit 2
  {
    echo "# db/proj/proj.db — derived by scripts/derive-proj-db.sh; do not edit by hand."
    echo "# image:           $IMAGE"
    echo "# image's own:     $(sha "$TMP/own.db")"
    echo "$(sha "$OUT")  proj.db"
  } > "$SUMS"
  echo "wrote $OUT ($(wc -c < "$OUT" | tr -d ' ') bytes) and $SUMS"
fi

# Both modes end with the proof: the shipped file holds, row for row and object for object, exactly
# what the image's own file holds after the operations are withdrawn, and every index in it holds exactly
# its table's rows (a dump shows rows and schema text, never what is inside an index). -B: the check
# writes nothing, not even a bytecode cache beside the withdrawal script it loads.
[ -f "$OUT" ] || { echo "derive-proj-db: FAIL — $OUT is missing (run scripts/derive-proj-db.sh)"; exit 1; }
python3 -B - "$TMP/own.db" "$TMP/changed.db" "$OUT" "$SUMS" "$IMAGE" "$WITHDRAW" <<'PY'
import hashlib, importlib.util, sqlite3, sys
from pathlib import Path

own_path, expected_path, shipped_path, sums_path, image, withdraw_path = sys.argv[1:7]
bad = []
# The rows the withdrawal is meant to change: the script's own list, so the check and the change cannot drift.
_spec = importlib.util.spec_from_file_location("withdraw_nad83_shifts", withdraw_path)
_mod = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_mod)
WITHDRAWN = tuple(_mod.WITHDRAWN)


def ro(path):
    """Opened read-only: a check must never be able to write the file it is checking."""
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def meta(path):
    c = ro(path)
    try:
        return dict(c.execute("SELECT key, value FROM metadata WHERE key LIKE 'DATABASE.LAYOUT.VERSION.%' "
                              "OR key IN ('PROJ.VERSION', 'EPSG.VERSION')"))
    finally:
        c.close()


def dump(path):
    """Every table, row, index, trigger and view as SQL text, in order: two files can then be
    compared whatever their page layout."""
    c = ro(path)
    try:
        return list(c.iterdump())
    finally:
        c.close()


def structure(path):
    """What a dump cannot show: whether every index holds exactly its table's rows (an index built to
    leave a row out hides it from the library's lookups while the dump stays the same), and the
    file's own bookkeeping."""
    c = ro(path)
    try:
        out = {"integrity": c.execute("PRAGMA integrity_check").fetchall()}
        for name in ("page_count", "freelist_count", "user_version", "application_id"):
            out[name] = c.execute(f"PRAGMA {name}").fetchone()[0]
        return out
    finally:
        c.close()


m_own, m_shipped = meta(own_path), meta(shipped_path)
print(f"  the pinned image's own file: PROJ {m_own.get('PROJ.VERSION')}, EPSG {m_own.get('EPSG.VERSION')}, "
      f"layout {m_own.get('DATABASE.LAYOUT.VERSION.MAJOR')}.{m_own.get('DATABASE.LAYOUT.VERSION.MINOR')}")
if m_own != m_shipped:
    bad.append(f"db/proj/proj.db is for another version of the library ({m_shipped}); the image has {m_own}")
else:
    d_own, d_expected, d_shipped = dump(own_path), dump(expected_path), dump(shipped_path)
    differ = [(a, b) for a, b in zip(d_own, d_shipped) if a != b] if len(d_own) == len(d_shipped) else None
    print("  lines of the shipped file that differ from the image's own: "
          + (str(len(differ)) if differ is not None else "the two have different numbers of lines"))
    if d_shipped != d_expected:
        bad.append("db/proj/proj.db is not the image's own file with the listed NAD83 operations withdrawn: "
                   "something else differs")
    elif differ is None or len(differ) != len(WITHDRAWN):
        bad.append(f"withdrawing the operations did not change exactly {len(WITHDRAWN)} rows of the image's file")
    s_expected, s_shipped = structure(expected_path), structure(shipped_path)
    if s_shipped["integrity"] != [("ok",)]:
        bad.append("db/proj/proj.db fails SQLite's integrity check, so an index does not hold its table's rows: "
                   + "; ".join(r[0] for r in s_shipped["integrity"][:3]))
    elif s_shipped != s_expected:
        bad.append(f"db/proj/proj.db's page bookkeeping differs from the expected file's: {s_shipped} against {s_expected}")

want = None
try:
    for line in open(sums_path):
        if line.strip().endswith("proj.db") and not line.startswith("#"):
            want = line.split()[0]
        if line.startswith("# image:") and line.split()[-1] != image:
            bad.append(f"db/proj/SHA256SUMS was written for {line.split()[-1]}, not the image pinned now")
        if line.startswith("# image's own:") and line.split()[-1] != hashlib.sha256(open(own_path, "rb").read()).hexdigest():
            bad.append("the pinned image's own proj.db does not match the one recorded in db/proj/SHA256SUMS")
except OSError:
    bad.append("db/proj/SHA256SUMS is missing")
got = hashlib.sha256(open(shipped_path, "rb").read()).hexdigest()
if want != got:
    bad.append("db/proj/proj.db does not match the checksum recorded in db/proj/SHA256SUMS")

for b in bad:
    print("  FAIL:", b)
if bad:
    print("derive-proj-db: FAIL — run scripts/derive-proj-db.sh and commit db/proj/")
    sys.exit(1)
print("derive-proj-db: ok — db/proj/proj.db is the pinned image's own file with "
      + ", ".join("EPSG:" + c for c in WITHDRAWN) + " withdrawn, and nothing else changed")
PY
