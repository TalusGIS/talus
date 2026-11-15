"""An uploaded GeoPackage is read as data, within a budget (once-over 2026-11-15 K1).

A GeoPackage is a SQLite file its uploader wrote, and the names in its `gpkg_*` tables point at
whatever the file defines under them: a view, a virtual table, or a table with generated columns is
SQL the uploader chose. It ran with no limit on the api's only event loop. Every reader now refuses
those, opens the file read-only with its schema's functions untrusted, and stops past a budget of
SQLite work. These tests use small ordinary files; the budget is tested by setting it low.
"""
import base64
import sqlite3

import pytest
from fastapi import HTTPException

from app import gpkg_guard
from app import import_jobs_service as svc
from app.routers.layer_features import _geopackage_to_features
from app.routers.layer_fields import _geopackage_to_drafts


def _write(path: str, *, layer: str = "table", metadata_view: bool = False, rows: int = 3) -> None:
    """A minimal GeoPackage. `layer`: "table" (the control), "view", "generated" or "virtual"."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE gpkg_contents (table_name TEXT, data_type TEXT, identifier TEXT)")
    con.execute("INSERT INTO gpkg_contents VALUES ('feats', 'features', 'feats')")
    gc_name = "gpkg_geometry_columns_data" if metadata_view else "gpkg_geometry_columns"
    con.execute(f"CREATE TABLE {gc_name} (table_name TEXT, column_name TEXT, "
                "geometry_type_name TEXT, srs_id INTEGER)")
    con.execute(f"INSERT INTO {gc_name} VALUES ('feats', 'geom', 'POINT', 4326)")
    if metadata_view:
        con.execute("CREATE VIEW gpkg_geometry_columns AS SELECT * FROM gpkg_geometry_columns_data")
    if layer == "table":
        con.execute("CREATE TABLE feats (fid INTEGER PRIMARY KEY, geom BLOB, name TEXT)")
    elif layer == "view":
        con.execute("CREATE TABLE feats_data (fid INTEGER PRIMARY KEY, geom BLOB, name TEXT)")
        con.execute("CREATE VIEW feats AS SELECT * FROM feats_data")
    elif layer == "generated":
        con.execute("CREATE TABLE feats (fid INTEGER PRIMARY KEY, geom BLOB, name TEXT, "
                    "loud TEXT GENERATED ALWAYS AS (upper(name)) VIRTUAL)")
    elif layer == "virtual":
        con.execute("CREATE VIRTUAL TABLE feats USING fts5(geom, name)")
    target = "feats_data" if layer == "view" else "feats"
    con.executemany(f"INSERT INTO {target} (geom, name) VALUES (?, ?)",
                    ((None, f"n{i}") for i in range(rows)))
    con.commit()
    con.close()


def _bytes(tmp_path, **kw) -> bytes:
    p = tmp_path / "upload.gpkg"
    _write(str(p), **kw)
    return p.read_bytes()


REFUSALS = [
    ("view", {"layer": "view"}, gpkg_guard.REFUSED_VIEW),
    ("generated column", {"layer": "generated"}, gpkg_guard.REFUSED_GENERATED),
    ("virtual table", {"layer": "virtual"}, gpkg_guard.REFUSED_VIEW),
    ("metadata table as a view", {"metadata_view": True}, gpkg_guard.REFUSED_VIEW),
]


# --- each reader refuses, with the fixed sentence ---------------------------------------------------


@pytest.mark.parametrize("label,kw,sentence", REFUSALS[:3])
def test_the_schema_preview_refuses(tmp_path, label, kw, sentence):
    with pytest.raises(HTTPException) as exc:
        _geopackage_to_drafts(_bytes(tmp_path, **kw))
    assert exc.value.status_code == 422 and exc.value.detail == sentence, label


def test_the_schema_preview_never_queries_a_metadata_view(tmp_path, monkeypatch):
    """The preview reads gpkg_geometry_columns only when it is a real table (`_sqlite_has_table`), so
    a view under that name is skipped unread: no refusal is needed, and the geometry stays unknown."""
    drafts, meta = _geopackage_to_drafts(_bytes(tmp_path, metadata_view=True))
    assert meta["geometry"] is None and meta["detectedSrid"] is None


@pytest.mark.parametrize("label,kw,sentence", REFUSALS)
def test_the_small_import_refuses(tmp_path, label, kw, sentence):
    data = base64.b64encode(_bytes(tmp_path, **kw)).decode()
    with pytest.raises(HTTPException) as exc:
        _geopackage_to_features(data)
    assert exc.value.status_code == 422 and exc.value.detail == sentence, label


@pytest.mark.parametrize("label,kw,sentence", REFUSALS)
def test_the_bulk_sniff_refuses(tmp_path, label, kw, sentence):
    p = tmp_path / "upload.gpkg"
    _write(str(p), **kw)
    with pytest.raises(HTTPException) as exc:
        svc.sniff_gpkg(str(p))
    assert exc.value.status_code == 422 and exc.value.detail == sentence, label


@pytest.mark.parametrize("label,kw,sentence", REFUSALS)
def test_the_bulk_worker_refuses_before_reading_a_row(tmp_path, label, kw, sentence):
    p = tmp_path / "upload.gpkg"
    _write(str(p), **kw)
    with pytest.raises(gpkg_guard.UnsafeGeoPackage) as exc:
        next(svc._iter_gpkg(str(p)))
    assert str(exc.value) == sentence, label


# --- the controls: an ordinary file still reads ----------------------------------------------------


def test_an_ordinary_geopackage_still_previews(tmp_path):
    drafts, meta = _geopackage_to_drafts(_bytes(tmp_path))
    assert [d["field_key"] for d in drafts] == ["name"]
    assert meta["featureCount"] == 3


def test_an_ordinary_geopackage_still_sniffs_and_streams(tmp_path):
    p = tmp_path / "upload.gpkg"
    _write(str(p))
    drafts, family, srid, table = svc.sniff_gpkg(str(p))
    assert table == "feats" and srid == 4326
    rows = list(svc._iter_gpkg(str(p)))
    assert len(rows) == 3 and all(r["props"]["name"].startswith("n") for r in rows)


# --- the budget, the read-only switch, the quoting -------------------------------------------------


def test_a_statement_past_the_budget_is_stopped(tmp_path):
    p = tmp_path / "upload.gpkg"
    _write(str(p), rows=5000)
    con = sqlite3.connect(str(p))
    try:
        gpkg_guard.guard(con, budget_ops=1000)
        with pytest.raises(sqlite3.OperationalError) as exc:
            list(con.execute("SELECT * FROM feats"))
        assert gpkg_guard.is_budget_stop(exc.value)
    finally:
        con.close()


def test_the_budget_stop_reaches_the_caller_as_the_fixed_sentence(tmp_path, monkeypatch):
    monkeypatch.setattr(gpkg_guard, "WORKER_BUDGET_OPS", 1000)
    p = tmp_path / "upload.gpkg"
    _write(str(p), rows=5000)
    with pytest.raises(gpkg_guard.UnsafeGeoPackage) as exc:
        list(svc._iter_gpkg(str(p)))
    assert str(exc.value) == gpkg_guard.TOO_COSTLY


def test_the_budget_is_wide_enough_for_an_ordinary_full_read(tmp_path):
    """The control for the budget: an ordinary 20,000-row read finishes far inside it."""
    p = tmp_path / "upload.gpkg"
    _write(str(p), rows=20000)
    assert len(list(svc._iter_gpkg(str(p)))) == 20000


def test_a_guarded_connection_cannot_write(tmp_path):
    p = tmp_path / "upload.gpkg"
    _write(str(p))
    con = sqlite3.connect(str(p))
    try:
        gpkg_guard.guard(con, gpkg_guard.PREVIEW_BUDGET_OPS)
        with pytest.raises(sqlite3.OperationalError):
            con.execute("DELETE FROM feats")
    finally:
        con.close()


def test_a_name_from_the_file_is_quoted():
    assert gpkg_guard.quote_ident('a"b') == '"a""b"'
