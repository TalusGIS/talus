"""Vector exports have a byte ceiling, counted while the rows arrive (once-over 2026-11-15 K2).

`export_features` fetched every row with a client-side cursor (the whole result lands at `execute`)
and built the file in memory, capped at 250,000 rows but not by bytes, on a door that serves public
layers to anyone. The rows now come from a server-side cursor in batches, and past
`settings.export_max_bytes` the export answers 413. Pure logic: the cursor is a stub.
"""
import asyncio
import inspect

import pytest
from fastapi import HTTPException

from app.config import settings
from app.routers import layer_query
from app.routers.layer_query import _fetch_export_rows, _row_bytes


class _Cursor:
    def __init__(self, rows, batch_log):
        self._rows = list(rows)
        self._log = batch_log

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, q, params=None):
        self._log.append(("execute", params))

    async def fetchmany(self, n):
        out, self._rows = self._rows[:n], self._rows[n:]
        self._log.append(("fetchmany", len(out)))
        return out

    async def fetchall(self):
        raise AssertionError("fetchall: the whole result would be held before it could be counted")


class _Conn:
    def __init__(self, rows):
        self.rows = rows
        self.log: list = []
        self.cursor_kwargs: dict = {}

    def cursor(self, **kw):
        self.cursor_kwargs = kw
        return _Cursor(self.rows, self.log)


ROW = {"id": "x", "geometry": {"type": "Point", "coordinates": [1, 2]}, "name": "a" * 100}


def test_rows_under_the_ceiling_all_come_back_from_a_server_side_cursor():
    conn = _Conn([dict(ROW) for _ in range(4500)])
    rows = asyncio.run(_fetch_export_rows(conn, "q", {"_cap": 10}, layer_id="tal_x", fmt="geojson"))
    assert len(rows) == 4500
    assert conn.cursor_kwargs.get("name"), "not a named (server-side) cursor: the whole result lands at execute"
    assert [e for e in conn.log if e[0] == "fetchmany"][:3] == [("fetchmany", 2000), ("fetchmany", 2000), ("fetchmany", 500)]


def test_past_the_ceiling_the_export_answers_413(monkeypatch):
    monkeypatch.setattr(settings, "export_max_bytes", 50_000)
    conn = _Conn([dict(ROW) for _ in range(4500)])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_fetch_export_rows(conn, "q", {}, layer_id="tal_x", fmt="csv"))
    assert exc.value.status_code == 413 and "too large to build at once" in exc.value.detail
    fetched = sum(e[1] for e in conn.log if e[0] == "fetchmany")
    assert fetched == 2000, "it kept fetching past the ceiling"


def test_the_estimate_counts_text_json_and_bytes():
    assert _row_bytes({"a": "xyz", "b": b"12", "c": {"k": 1}, "d": 7}) == 3 + 2 + len('{"k": 1}') + 16


def test_no_export_branch_fetches_its_rows_whole():
    for name in ("_export_geojson", "_export_csv", "_export_shapefile", "_export_geopackage"):
        src = inspect.getsource(getattr(layer_query, name))
        assert "_fetch_export_rows(" in src, f"{name} does not use the capped fetch"
        assert "fetchall()" not in src, f"{name} still fetches a result whole"
