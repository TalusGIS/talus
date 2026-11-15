"""The small Shapefile import counts what it unpacks (once-over 2026-11-15 K5).

`open_shapefile_zip` read each member with `zf.read()`, which allocates the whole decompressed member.
The 50 MB cap applies to the upload, and a member can expand far beyond it. Members are now read in
pieces against a running total. Tested with an ordinary small Shapefile and the ceiling set low.
"""
import io
import zipfile

import pytest
import shapefile
from fastapi import HTTPException

from app import file_ingest


def _zipped_shapefile() -> bytes:
    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    w = shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=shapefile.POINT)
    w.field("name", "C", 20)
    for i in range(50):
        w.point(-82.5 + i * 0.001, 27.3)
        w.record(f"n{i}")
    w.close()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("pts.shp", shp.getvalue())
        zf.writestr("pts.shx", shx.getvalue())
        zf.writestr("pts.dbf", dbf.getvalue())
        zf.writestr("pts.prj", 'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",'
                               'SPHEROID["WGS_1984",6378137.0,298.257223563]]]')
    return out.getvalue()


def test_an_ordinary_shapefile_still_reads():
    reader, detected = file_ingest.open_shapefile_zip(_zipped_shapefile())
    assert len(reader) == 50 and detected == 4326


def test_past_the_ceiling_it_stops_with_413(monkeypatch):
    monkeypatch.setattr(file_ingest, "SHAPEFILE_UNPACKED_CEILING", 200)
    with pytest.raises(HTTPException) as exc:
        file_ingest.open_shapefile_zip(_zipped_shapefile())
    assert exc.value.status_code == 413 and "Import data" in exc.value.detail


def test_no_member_is_read_whole_without_counting(monkeypatch):
    """`ZipFile.read` is the unbounded call; the reader must not use it."""
    def _refuse(self, *a, **kw):
        raise AssertionError("ZipFile.read was called: a member was read without counting")

    monkeypatch.setattr(zipfile.ZipFile, "read", _refuse)
    reader, _ = file_ingest.open_shapefile_zip(_zipped_shapefile())
    assert len(reader) == 50


def test_the_ceiling_counts_across_members(monkeypatch):
    """The budget is for the whole Shapefile, not per member: a ceiling just under the sum stops it."""
    data = _zipped_shapefile()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        total = sum(i.file_size for i in zf.infolist())
        biggest = max(i.file_size for i in zf.infolist())
    monkeypatch.setattr(file_ingest, "SHAPEFILE_UNPACKED_CEILING", total - 1)
    assert total - 1 >= biggest
    with pytest.raises(HTTPException) as exc:
        file_ingest.open_shapefile_zip(data)
    assert exc.value.status_code == 413
