"""Reading an uploaded GeoPackage as data, within a budget (once-over 2026-11-15 K1).

A GeoPackage is a SQLite database the uploader wrote. The names in its `gpkg_*` tables point at
whatever the file defines under those names, and a "table" can be a view, a virtual table or a table
with generated columns: SQL the uploader chose, which ran here with no limit, synchronously on the
api's only event loop. One upload could stop the api for everyone.

Every reader of an uploaded GeoPackage now:
- opens it read-only, with the schema's own functions untrusted (`query_only`, `trusted_schema=OFF`);
- reads only plain tables: `require_plain_table` refuses a view, a virtual table, or a table with a
  generated column, for the feature table and for every `gpkg_*` table it queries;
- runs under a budget of SQLite work (`set_progress_handler` counts the VM's own operations, so time
  the import worker spends inserting between rows does not count). Measured 2026-11-15: an ordinary
  full read costs about 9 operations a row, at about 16 million a second;
- quotes a name taken from the file with `quote_ident`.

The interactive readers also leave the event loop (their callers use the thread pool).
"""
from __future__ import annotations

import sqlite3

# Budgets in SQLite VM operations. The handler is called every _STEP operations.
_STEP = 1000
# Preview, sniff and the small (50,000-feature) import: about six seconds of SQLite work at most.
PREVIEW_BUDGET_OPS = 100_000_000
# The bulk worker: 2.5x what the largest honest import needs (2 million rows x 200 columns at about
# one operation a column). With views, virtual tables and generated columns refused it is a backstop.
WORKER_BUDGET_OPS = 1_000_000_000

REFUSED_VIEW = ("this GeoPackage's layer is a view or a virtual table, not a table. "
                "Save the layer as an ordinary table in a new GeoPackage and upload that.")
REFUSED_GENERATED = ("this GeoPackage's layer has computed (generated) columns. "
                     "Save the layer as an ordinary table in a new GeoPackage and upload that.")
TOO_COSTLY = ("reading this GeoPackage took too much work and was stopped. "
              "Save the layer as an ordinary table in a new GeoPackage and upload that.")


class UnsafeGeoPackage(ValueError):
    """The file asks for something this importer does not do. `str(exc)` is a fixed sentence."""


def guard(con: sqlite3.Connection, budget_ops: int) -> None:
    """Make `con` read-only, untrusting and budgeted. Call before the first query on the file."""
    con.execute("PRAGMA query_only = ON")
    con.execute("PRAGMA trusted_schema = OFF")
    spent = {"steps": 0}
    limit = max(1, budget_ops // _STEP)

    def _over_budget() -> int:
        spent["steps"] += 1
        return 1 if spent["steps"] > limit else 0

    con.set_progress_handler(_over_budget, _STEP)


def is_budget_stop(exc: BaseException) -> bool:
    """A statement stopped by the budget raises sqlite3.OperationalError('interrupted')."""
    return isinstance(exc, sqlite3.OperationalError) and "interrupt" in str(exc).lower()


def quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def require_plain_table(con: sqlite3.Connection, name: str) -> None:
    """Refuse unless `name` is an ordinary table: not a view, not a virtual table, no generated
    column. Raises UnsafeGeoPackage with a fixed sentence."""
    row = con.execute(
        "SELECT type, sql FROM sqlite_master WHERE lower(name) = lower(?) AND type IN ('table', 'view')",
        (name,),
    ).fetchone()
    if row is None:
        return  # absent: the caller's own query reports it as it does today
    kind, ddl = row[0], (row[1] or "")
    if kind != "table" or ddl.lstrip().upper().startswith("CREATE VIRTUAL TABLE"):
        raise UnsafeGeoPackage(REFUSED_VIEW)
    # table_xinfo's `hidden` column: 2 = generated VIRTUAL, 3 = generated STORED.
    if any(col[6] in (2, 3) for col in con.execute(f"PRAGMA table_xinfo({quote_ident(name)})")):
        raise UnsafeGeoPackage(REFUSED_GENERATED)


def require_plain_tables(con: sqlite3.Connection, *names: str) -> None:
    for n in names:
        require_plain_table(con, n)
