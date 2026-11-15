"""9d bulk-import door — pure-function guards (decision-9d-bulk-import.md §8).

No live DB, no stack: the archive fences and the sniffers' DRAFT CONTRACT are pure and
testable, and both are the parts whose silent failure is expensive.

★ The headline test is `test_draft_shape_is_the_field_import_contract`. On 2026-08-21 the
sniffers emitted camelCase drafts (`fieldKey`/`dataType`) while the shared field-import core
reads snake_case (`field_key`/`data_type`), so EVERY draft was rejected with "unknown data
type ''" — and the router ignored the returned summary. A 330,645-row county import
"succeeded" with 0 failed rows and every attribute silently dropped: geometry-only layer,
empty props. Row counts looked perfect; the data was gutted. The guard below fails the
moment a draft stops speaking the core's dialect.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import struct
import zipfile

import pytest
import shapefile  # pyshp
from fastapi import HTTPException

from app import import_jobs_service as svc
from app.routers.layer_fields import _normalize_type


# --- fixtures: real files, built in-process ------------------------------------------------------

def _write_shapefile(dir_path: str, *, name: str = "pts", with_prj: bool = True) -> None:
    w = shapefile.Writer(os.path.join(dir_path, name))
    w.field("ADDNUMBER", "C", size=12)
    w.field("COUNT", "N", size=9)
    w.field("HEIGHT", "F", size=13, decimal=3)
    w.field("OBJECTID", "N", size=9)      # reserved on the ESRI door -> must be RENAMED, not lost
    w.point(-82.5, 27.3)
    w.record("4141", 7, 1.5, 1)
    w.close()
    if with_prj:
        with open(os.path.join(dir_path, f"{name}.prj"), "w", encoding="utf-8") as fh:
            fh.write('GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],'
                     'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]')


def _gpkg(path: str) -> None:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE gpkg_geometry_columns (table_name TEXT, column_name TEXT, "
                "geometry_type_name TEXT, srs_id INTEGER)")
    con.execute("INSERT INTO gpkg_geometry_columns VALUES ('feats', 'geom', 'POINT', 4326)")
    con.execute("CREATE TABLE feats (fid INTEGER PRIMARY KEY, geom BLOB, name TEXT, qty INTEGER, val REAL)")
    con.commit()
    con.close()


# --- ★ the draft contract ------------------------------------------------------------------------

_HOUSE_KEYS = {"field_key", "_renamed_from", "label", "data_type", "required",
               "default_value", "default_kind", "constraints", "numeric_subtype"}


def _assert_house_drafts(drafts: list[dict]) -> None:
    assert drafts, "the sniffer produced no drafts at all"
    for d in drafts:
        # snake_case dialect, exactly what _run_field_import reads
        assert set(d) == _HOUSE_KEYS, f"draft keys drifted from the core's contract: {sorted(d)}"
        assert d["field_key"], "empty field_key — the core would record it as an anonymous error row"
        # the exact check that silently rejected every draft in the 2026-08-21 bug
        assert _normalize_type(d["data_type"]) is not None, f"unknown data type {d['data_type']!r}"


def test_draft_shape_is_the_field_import_contract_shp(tmp_path):
    _write_shapefile(str(tmp_path))
    drafts, family, srid, shp = svc.sniff_shp(str(tmp_path))
    assert family == "point" and shp == "pts.shp" and srid == 4326
    _assert_house_drafts(drafts)
    keys = {d["field_key"] for d in drafts}
    # §3.1 reserved-name rename must fire (and be legible), never silently drop the column
    assert "objectid_src" in keys and "objectid" not in keys
    assert next(d for d in drafts if d["field_key"] == "objectid_src")["_renamed_from"] == "OBJECTID"
    # dbf types map to the house vocabulary
    by_key = {d["field_key"]: d for d in drafts}
    assert by_key["addnumber"]["data_type"] == "text"
    assert by_key["count"]["data_type"] == "number" and by_key["count"]["numeric_subtype"] == "long_integer"
    assert by_key["height"]["numeric_subtype"] == "double"


def test_draft_shape_is_the_field_import_contract_gpkg(tmp_path):
    p = str(tmp_path / "x.gpkg")
    _gpkg(p)
    drafts, family, srid, table = svc.sniff_gpkg(p)
    assert family == "point" and srid == 4326 and table == "feats"
    _assert_house_drafts(drafts)
    by_key = {d["field_key"]: d["data_type"] for d in drafts}
    assert by_key == {"name": "text", "qty": "number", "val": "number"}  # fid + geom excluded


def test_draft_shape_is_the_field_import_contract_geojson(tmp_path):
    p = str(tmp_path / "x.geojson")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-82.5, 27.3]},
             "properties": {"name": "a", "n": 3, "f": 1.25, "b": True}}]}, fh)
    drafts, family, srid = svc.sniff_geojson(p)
    assert family == "point" and srid == 4326
    _assert_house_drafts(drafts)
    assert {d["field_key"]: d["data_type"] for d in drafts} == {
        "name": "text", "n": "number", "f": "number", "b": "boolean"}


# --- GeoJSON field typing: the sniff must agree with what the validator will accept --------------
#
# `_iter_geojson` hands values to the insert untouched, and the field validator refuses a value
# its field's type cannot hold. So a column typed from too little evidence refuses real rows. The
# first cut typed every column from the FIRST feature alone, and a feature layer copied from an
# ArcGIS service lost every row: the service writes a date as epoch milliseconds, a whole number
# far past 32 bits, and the column was typed long_integer.

_INT32 = (-2147483648, 2147483647)


def _geojson_file(tmp_path, rows: list[dict]) -> str:
    p = str(tmp_path / "typed.geojson")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-82.5, 27.3]},
             "properties": r} for r in rows]}, fh)
    return p


def _refusals(drafts: list[dict], rows: list[dict]) -> list[str]:
    """Every (row, column) the field validator would refuse, by the validator's own rules for
    text / number / boolean / date and the two integer subtypes. Empty = the whole file goes in."""
    by_label = {d["label"]: d for d in drafts}
    out = []
    for i, r in enumerate(rows):
        for k, v in r.items():
            if v is None:
                continue
            d = by_label.get(k)
            if d is None:
                out.append(f"row {i}: {k} has no field")
                continue
            dt, sub = d["data_type"], d["numeric_subtype"]
            is_num = isinstance(v, (int, float)) and not isinstance(v, bool)
            if dt == "text" and not isinstance(v, str):
                out.append(f"row {i}: {k} must be text")
            elif dt == "boolean" and not isinstance(v, bool):
                out.append(f"row {i}: {k} must be true or false")
            elif dt == "date" and not (isinstance(v, str) and len(v) == 10):
                out.append(f"row {i}: {k} must be a date")
            elif dt == "number" and not is_num:
                out.append(f"row {i}: {k} must be a number")
            elif dt == "number" and sub == "long_integer":
                if v != int(v):
                    out.append(f"row {i}: {k} must be a whole number")
                elif not _INT32[0] <= v <= _INT32[1]:
                    out.append(f"row {i}: {k} is out of range for a long integer")
    return out


def test_geojson_epoch_millisecond_column_is_not_a_long_integer(tmp_path):
    """The reported failure: every row carries a 13-digit whole number in one column."""
    rows = [{"stopid": i, "lastupdate": 1776959682000 + i} for i in range(1, 6)]
    drafts, _family, _srid = svc.sniff_geojson(_geojson_file(tmp_path, rows))
    _assert_house_drafts(drafts)
    by_key = {d["field_key"]: d for d in drafts}
    assert by_key["lastupdate"]["data_type"] == "number"
    assert by_key["lastupdate"]["numeric_subtype"] == "double"
    assert by_key["stopid"]["numeric_subtype"] == "long_integer"   # an ordinary id stays an integer
    assert _refusals(drafts, rows) == []


def test_geojson_typing_reads_past_the_first_feature(tmp_path):
    rows = [
        {"count": None, "height": 3, "seen": None, "note": None},
        {"count": 7, "height": 2.5, "seen": "2026-04-23", "note": "a", "late": 1},
        {"count": 9, "height": 4, "seen": "2026-04-24", "note": None, "late": 2},
    ]
    drafts, _family, _srid = svc.sniff_geojson(_geojson_file(tmp_path, rows))
    _assert_house_drafts(drafts)
    typed = {d["field_key"]: (d["data_type"], d["numeric_subtype"]) for d in drafts}
    assert typed == {
        "count": ("number", "long_integer"),   # null in the first feature — was typed text
        "height": ("number", "double"),         # a decimal after a whole number
        "seen": ("date", None),                 # YYYY-MM-DD strings
        "note": ("text", None),
        "late": ("number", "long_integer"),     # a column the first feature does not carry
    }
    assert _refusals(drafts, rows) == []


def test_geojson_number_shape_is_tracked_past_the_type_sample(tmp_path):
    """One oversized value and one decimal, both beyond the per-column sample, still widen."""
    from app.routers.layer_fields import _SNIFF_SAMPLE

    rows = [{"big": i, "frac": i} for i in range(_SNIFF_SAMPLE + 5)]
    rows.append({"big": 5000000000, "frac": 0.5})
    drafts, _family, _srid = svc.sniff_geojson(_geojson_file(tmp_path, rows))
    typed = {d["field_key"]: d["numeric_subtype"] for d in drafts}
    assert typed == {"big": "double", "frac": "double"}
    assert _refusals(drafts, rows) == []


def test_refusal_model_catches_the_first_feature_typing(tmp_path):
    """The fixture must be able to fail: typed the old way (first feature only), the same rows
    are refused. Without this the three tests above could pass on a model that refuses nothing."""
    rows = [{"lastupdate": 1776959682000}, {"lastupdate": 1776959694000}]
    old_way = [dict(svc._draft("lastupdate", "number", set(), "long_integer"))]
    assert len(_refusals(old_way, rows)) == 2


def test_geojson_new_key_per_feature_cannot_grow_the_draft_list(tmp_path):
    from app.routers.layer_fields import _MAX_IMPORT_FIELDS

    rows = [{f"k{i}": i} for i in range(_MAX_IMPORT_FIELDS * 3)]
    drafts, _family, _srid = svc.sniff_geojson(_geojson_file(tmp_path, rows))
    # one past the cap: enough for the shared core to refuse the file with "too many fields"
    assert len(drafts) == _MAX_IMPORT_FIELDS + 1


def test_data_table_import_shares_the_32_bit_rule():
    """The data-table Import types GeoJSON and CSV columns with the same sampler."""
    from app.routers.layer_fields import _geojson_to_drafts, _new_sniff, _sniff_csv_cell, _sniffed_type

    doc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [0, 0]},
         "properties": {"big": 1776959682000, "small": 12}}]}
    drafts, _meta = _geojson_to_drafts(json.dumps(doc).encode())
    typed = {d["field_key"]: d["numeric_subtype"] for d in drafts}
    assert typed == {"big": "double", "small": "long_integer"}

    acc = _new_sniff()
    for cell in ("12", "1776959682000"):
        _sniff_csv_cell(acc, cell)
    assert _sniffed_type(acc) == ("number", "double")


def test_sniffer_field_keys_match_the_workers_props_matcher(tmp_path):
    """The worker matches a source column to a field with _sanitize_key; if the sniffer used a
    private slug the fields would exist but every value would land in unmatched (silent loss)."""
    from app.routers.layer_fields import _sanitize_key

    _write_shapefile(str(tmp_path))
    drafts, _f, _s, _shp = svc.sniff_shp(str(tmp_path))
    for d in drafts:
        source_name = d["_renamed_from"] or d["label"]
        assert _sanitize_key(source_name) == d["field_key"]


# --- archive fences ------------------------------------------------------------------------------

def _zip_with(entries: list[tuple[str, bytes]], path: str) -> str:
    with zipfile.ZipFile(path, "w") as z:
        for name, data in entries:
            z.writestr(name, data)
    return path


def test_zip_slip_entry_is_refused_by_name(tmp_path):
    p = _zip_with([("../../evil.txt", b"x"), ("ok.shp", b"y")], str(tmp_path / "s.zip"))
    dest = tmp_path / "out"; dest.mkdir()
    with pytest.raises(HTTPException) as e:
        svc.validate_and_extract_zip(p, str(dest))
    assert e.value.status_code == 422 and "evil.txt" in str(e.value.detail)
    assert not (tmp_path.parent / "evil.txt").exists()
    assert not any(dest.iterdir()), "a refused archive must leave nothing behind"


def test_absolute_path_entry_is_refused(tmp_path):
    p = _zip_with([("/etc/passwd", b"x")], str(tmp_path / "a.zip"))
    dest = tmp_path / "out"; dest.mkdir()
    with pytest.raises(HTTPException) as e:
        svc.validate_and_extract_zip(p, str(dest))
    assert e.value.status_code == 422


def test_symlink_entry_is_refused(tmp_path):
    p = str(tmp_path / "l.zip")
    with zipfile.ZipFile(p, "w") as z:
        info = zipfile.ZipInfo("link")
        info.external_attr = (0o120777 << 16)  # S_IFLNK
        z.writestr(info, "/etc/passwd")
    dest = tmp_path / "out"; dest.mkdir()
    with pytest.raises(HTTPException) as e:
        svc.validate_and_extract_zip(p, str(dest))
    assert e.value.status_code == 422 and "symlink" in str(e.value.detail)


def test_decompression_budget_stops_a_bomb(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "import_max_unpacked_mb", 1)
    p = _zip_with([("big.bin", b"\0" * (4 * 1024 * 1024))], str(tmp_path / "b.zip"))
    dest = tmp_path / "out"; dest.mkdir()
    with pytest.raises(HTTPException) as e:
        svc.validate_and_extract_zip(p, str(dest))
    assert e.value.status_code == 413


def test_duplicate_member_names_are_refused(tmp_path):
    p = _zip_with([("a/x.shp", b"1"), ("b/x.shp", b"2")], str(tmp_path / "d.zip"))
    dest = tmp_path / "out"; dest.mkdir()
    with pytest.raises(HTTPException) as e:
        svc.validate_and_extract_zip(p, str(dest))
    assert e.value.status_code == 422


# --- malformed input is 4xx, never a 500 (the P6 precedent) --------------------------------------

def test_corrupt_zip_is_422(tmp_path):
    p = tmp_path / "junk.zip"; p.write_bytes(b"not a zip at all")
    dest = tmp_path / "out"; dest.mkdir()
    with pytest.raises(HTTPException) as e:
        svc.validate_and_extract_zip(str(p), str(dest))
    assert e.value.status_code == 422


def test_zip_without_shp_member_is_422(tmp_path):
    (tmp_path / "readme.txt").write_text("no shapefile here")
    with pytest.raises(HTTPException) as e:
        svc.sniff_shp(str(tmp_path))
    assert e.value.status_code == 422 and ".shp" in str(e.value.detail)


def test_corrupt_gpkg_is_422(tmp_path):
    p = tmp_path / "x.gpkg"; p.write_bytes(b"definitely not sqlite")
    with pytest.raises(HTTPException) as e:
        svc.sniff_gpkg(str(p))
    assert e.value.status_code == 422


def test_gpkg_without_feature_layer_is_422(tmp_path):
    p = str(tmp_path / "empty.gpkg")
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE gpkg_geometry_columns (table_name TEXT, column_name TEXT, "
                "geometry_type_name TEXT, srs_id INTEGER)")
    con.commit(); con.close()
    with pytest.raises(HTTPException) as e:
        svc.sniff_gpkg(p)
    assert e.value.status_code == 422


def test_invalid_geojson_is_422(tmp_path):
    p = tmp_path / "x.geojson"; p.write_text("{not json")
    with pytest.raises(HTTPException) as e:
        svc.sniff_geojson(str(p))
    assert e.value.status_code == 422


def test_geojson_without_features_is_422(tmp_path):
    p = tmp_path / "x.geojson"; p.write_text('{"type":"FeatureCollection","features":[]}')
    with pytest.raises(HTTPException) as e:
        svc.sniff_geojson(str(p))
    assert e.value.status_code == 422


def test_unsupported_geometry_type_is_422(tmp_path):
    p = tmp_path / "x.geojson"
    p.write_text('{"type":"FeatureCollection","features":[{"type":"Feature",'
                 '"geometry":{"type":"GeometryCollection","geometries":[]},"properties":{}}]}')
    with pytest.raises(HTTPException) as e:
        svc.sniff_geojson(str(p))
    assert e.value.status_code == 422


def test_shapefile_without_prj_reports_no_srid(tmp_path):
    """No .prj => srid None => the door demands an explicit source_srid (the county HARN case)."""
    _write_shapefile(str(tmp_path), with_prj=False)
    _drafts, _family, srid, _shp = svc.sniff_shp(str(tmp_path))
    assert srid is None


# --- staging jail --------------------------------------------------------------------------------

def test_job_dir_is_derived_from_the_job_id_only():
    """A client filename must never reach a path component."""
    root = svc.staging_root()
    d = svc.job_dir("3351af56-1b22-4977-ae5a-1d9209dbd7c7")
    assert d == os.path.join(root, "3351af56-1b22-4977-ae5a-1d9209dbd7c7")
    assert os.path.commonpath([root, d]) == root


def test_reap_staging_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(svc.settings, "import_staging_dir", str(tmp_path))
    jid = "0" * 8 + "-0000-0000-0000-" + "0" * 12
    os.makedirs(svc.job_dir(jid), exist_ok=True)
    svc.reap_staging(jid)
    svc.reap_staging(jid)  # second call must not raise
    assert not os.path.exists(svc.job_dir(jid))


# --- CSV parsing ---------------------------------------------------------------------------------
# ★ The format shipped 2026-09-03 with NO unit tests while every other format carried sniff and
# negative coverage here. These are the baseline: the draft contract, the CRS-family split, the
# sniff↔iterator agreement, and the three 422s.

def _csv(tmp_path, body: str, name: str = "x.csv") -> str:
    p = str(tmp_path / name)
    with open(p, "w", encoding="utf-8", newline="") as fh:
        fh.write(body)
    return p


def test_draft_shape_is_the_field_import_contract_csv(tmp_path):
    p = _csv(tmp_path, "lon,lat,name,qty,val\n-82.5,27.3,a,3,1.25\n")
    drafts, family, srid = svc.sniff_csv(p)
    assert family == "point" and srid == 4326
    _assert_house_drafts(drafts)
    assert {d["field_key"]: d["data_type"] for d in drafts} == {
        "name": "text", "qty": "number", "val": "number"}  # lon/lat become geometry, not fields


def test_projected_xy_reports_no_srid_so_the_caller_must_declare_it(tmp_path):
    """lat/lon-family ⇒ 4326; a projected x/y pair has no defensible default, so the sniff
    reports None and the router's existing "supply source_srid" 422 fires."""
    p = _csv(tmp_path, "easting,northing,name\n500000,4649776,a\n")
    _drafts, family, srid = svc.sniff_csv(p)
    assert family == "point" and srid is None


def test_sniff_and_iterator_share_one_per_column_decision(tmp_path):
    """The 2,977-refusal contract (2b33573): a column the SAMPLE typed text must not receive
    numbers from later numeric-looking cells, or the E4 validator refuses every such row."""
    body = "lon,lat,trunk_height\n-82.5,27.3,10-15\n" + "".join(
        f"-82.5,27.3,{i}\n" for i in range(50))
    p = _csv(tmp_path, body)
    drafts, _f, _s = svc.sniff_csv(p)
    assert {d["field_key"]: d["data_type"] for d in drafts} == {"trunk_height": "text"}
    for r in svc._iter_csv(p):
        assert isinstance(r["props"]["trunk_height"], str)


def test_csv_without_coordinate_columns_is_422(tmp_path):
    p = _csv(tmp_path, "name,qty\na,1\n")
    with pytest.raises(HTTPException) as e:
        svc.sniff_csv(p)
    assert e.value.status_code == 422 and "no coordinate columns" in e.value.detail


def test_ambiguous_coordinate_columns_are_422(tmp_path):
    p = _csv(tmp_path, "lon,lng,lat,name\n-82.5,-82.5,27.3,a\n")
    with pytest.raises(HTTPException) as e:
        svc.sniff_csv(p)
    assert e.value.status_code == 422 and "multiple coordinate-candidate" in e.value.detail


def test_empty_csv_is_422(tmp_path):
    with pytest.raises(HTTPException) as e:
        svc.sniff_csv(_csv(tmp_path, ""))
    assert e.value.status_code == 422


@pytest.mark.parametrize("cell", ["01234", "007", "-01", "00"])
def test_leading_zero_identifiers_survive_as_text(tmp_path, cell):
    """A ZIP, FIPS code, parcel PIN or well ID is an identifier, not a number. int() renders
    '01234' as 1234 and the staged file is reaped on completion, so the original is gone."""
    assert svc._csv_num(cell) == cell
    p = _csv(tmp_path, f"lon,lat,zip\n-82.5,27.3,{cell}\n")
    drafts, _f, _s = svc.sniff_csv(p)
    assert {d["field_key"]: d["data_type"] for d in drafts} == {"zip": "text"}
    rows = list(svc._iter_csv(p))
    assert rows[0]["props"]["zip"] == cell  # verbatim, not coerced


def test_plain_zero_and_decimals_are_still_numbers():
    """The leading-zero rule must not swallow legitimate numerics."""
    assert svc._csv_num("0") == 0
    assert svc._csv_num("0.5") == 0.5
    assert svc._csv_num("-0.25") == -0.25
    assert svc._csv_num("42") == 42


@pytest.mark.parametrize("cell", ["nan", "NaN", "inf", "-inf", "Infinity", "1e999", "9" * 5000])
def test_non_finite_spellings_never_become_numbers(cell):
    """float() accepts every one of these. As a COORDINATE they pass an isinstance() guard and
    PostGIS stores them without complaint — ST_IsValid(POINT(NaN NaN)) is even true."""
    assert svc._csv_num(cell) == cell


@pytest.mark.parametrize("cell", ["1_000", "١٢٣", "１２３"])
def test_python_literal_spellings_a_csv_never_means_stay_text(cell):
    """int()/float() accept underscores and non-ASCII digit systems; a spreadsheet does not."""
    assert svc._csv_num(cell) == cell


@pytest.mark.parametrize("bad", ["nan", "inf", "1e999"])
def test_non_finite_coordinates_are_a_counted_row_error_not_a_stored_point(tmp_path, bad):
    """The wall: one NaN point turns the layer's whole ST_Extent into BOX(NaN NaN,NaN NaN),
    breaking zoom-to-layer and the extent picker for every clean feature in it. These must land
    as counted row errors — which is what _iter_csv's docstring already promised."""
    p = _csv(tmp_path, f"lon,lat,name\n{bad},{bad},a\n-82.5,27.3,b\n")
    rows = list(svc._iter_csv(p))
    assert rows[0]["geom"] is None and rows[0]["error"] == "missing or non-numeric coordinates"
    assert rows[1]["geom"] == {"type": "Point", "coordinates": [-82.5, 27.3]}


def test_csv_finite_rejects_nan_and_infinity_directly():
    assert svc._csv_finite(1) and svc._csv_finite(-0.5) and svc._csv_finite(0)
    assert not svc._csv_finite(float("nan"))
    assert not svc._csv_finite(float("inf"))
    assert not svc._csv_finite(float("-inf"))
    assert not svc._csv_finite("3") and not svc._csv_finite(None)
