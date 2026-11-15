"""Withdraw the three shifting NAD83 -> WGS 84 operations from a PROJ reference database (proj.db).

Why. The coordinate library takes, for any conversion, the registered operation with the best stated
accuracy. For the realizations of NAD83 used in the United States, the register's last step to WGS 84
changes nothing. For NAD83(HARN) it also holds two seven-parameter shifts, EPSG:1900 and EPSG:1901, rated
better than the do-nothing one (EPSG:1580), so the library took a shift: a layer on a HARN system was
drawn about 0.6 m from layers on NAD27, original NAD83, NAD83(2011) or GPS coordinates of the same
place. Canada's original NAD83(CSRS) has the same arrangement, a shift (EPSG:1946) rated better than its
do-nothing operation (EPSG:1842), and a layer on it was drawn about 1.2 m from the same numbers on a
later CSRS version, on NAD83(2011) or on GPS. Marking the three shifts deprecated makes the library
ignore them, and HARN and CSRS then meet the others. Nothing else in the register is changed.

Used in three places, so the rows are named once, here:
  * the api image build (Dockerfile), on the copy inside pyproj and the copy inside rasterio;
  * scripts/derive-proj-db.sh, which makes db/proj/proj.db for the database from the pinned image;
  * the same script's --check, which proves the shipped file differs from the image's own in these
    rows and nothing else.

    python3 withdraw_nad83_shifts.py PROJ_DB [PROJ_DB ...]

Exits non-zero unless, in every file named, each listed row exists and ends deprecated, and each
do-nothing operation is still in force. Running it twice changes nothing the second time.
"""
import sqlite3
import sys

# The shifts withdrawn, each with the do-nothing operation the library then takes for the same pair.
WITHDRAWN = {
    "1900": "1580",   # NAD83(HARN) to WGS 84 (2)    -> (1)
    "1901": "1580",   # NAD83(HARN) to WGS 84 (3)    -> (1)
    "1946": "1842",   # NAD83(CSRS) to WGS 84 (2)    -> (1)
}
KEPT = tuple(sorted(set(WITHDRAWN.values())))


def _table(conn: sqlite3.Connection) -> str:
    """The table that holds the rows: newer layouts make `helmert_transformation` a view over it."""
    kind = conn.execute("SELECT type FROM sqlite_master WHERE name = 'helmert_transformation'").fetchone()
    if kind is None:
        raise SystemExit("not a PROJ reference database: no helmert_transformation")
    return "helmert_transformation_table" if kind[0] == "view" else "helmert_transformation"


def state(path: str) -> dict[str, int]:
    """{code: deprecated} for the withdrawn and the kept operations in `path`."""
    codes = (*WITHDRAWN, *KEPT)
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT code, deprecated FROM helmert_transformation WHERE auth_name = 'EPSG' "
            f"AND code IN ({', '.join('?' * len(codes))})",
            codes,
        ).fetchall()
    finally:
        conn.close()
    return {str(code): int(dep) for code, dep in rows}


def withdraw(path: str) -> int:
    """Mark the shifts deprecated in `path`. Returns how many rows changed (0 on a second run)."""
    conn = sqlite3.connect(path)
    try:
        changed = conn.execute(
            f"UPDATE {_table(conn)} SET deprecated = 1 WHERE auth_name = 'EPSG' "
            f"AND code IN ({', '.join('?' * len(WITHDRAWN))}) AND deprecated = 0",
            tuple(WITHDRAWN),
        ).rowcount
        conn.commit()
    finally:
        conn.close()
    return changed


def main(paths: list[str]) -> int:
    if not paths:
        print(__doc__)
        return 2
    bad = 0
    for path in paths:
        changed = withdraw(path)
        now = state(path)
        ok = all(now.get(code) == 1 for code in WITHDRAWN) and all(now.get(code) == 0 for code in KEPT)
        withdrawn = ", ".join(f"EPSG:{c} {now.get(c)}" for c in WITHDRAWN)
        kept = ", ".join(f"EPSG:{c} {now.get(c)}" for c in KEPT)
        print(f"  {'ok  ' if ok else 'FAIL'} {path}: {changed} row(s) changed; deprecated: {withdrawn}; kept in force: {kept}")
        bad += 0 if ok else 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
