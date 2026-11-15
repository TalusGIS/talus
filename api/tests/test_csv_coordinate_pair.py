"""CSV coordinate-column detection — the pair must AGREE.

Pure functions, no DB. Before 2026-09-11 the two axes were resolved independently, so a header with
`Longitude` but no `Latitude` that also carried `x`/`y` resolved to `Longitude` + `y`: degrees on one
axis, linear units on the other. `detected` fell to None, the user declared a projected SRID, and the
points landed in the ocean with no error raised. The same two-line test was duplicated inline in the
ingest's manual-override branch, so fixing the helper alone would have left that path broken.

Two things these tests pin, in the kickoff's own words: the fix must NOT narrow the good cases (both-
geographic and both-projected headers keep working exactly as before), and a mixed pair must route
into the EXISTING 422s — no new error surface.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app import import_jobs_service as svc
from app.file_ingest import csv_pair_srid, find_csv_coordinates
from app.routers.layer_features import _csv_to_features


# --- the shared helper ------------------------------------------------------------------------------


def test_both_geographic_is_4326():
    assert find_csv_coordinates(["id", "Longitude", "Latitude", "name"]) == ("Longitude", "Latitude", 4326)
    assert csv_pair_srid("lon", "lat") == 4326


def test_both_projected_is_declare():
    assert find_csv_coordinates(["id", "x", "y", "name"]) == ("x", "y", None)
    assert find_csv_coordinates(["Easting", "Northing"]) == ("Easting", "Northing", None)
    assert csv_pair_srid("easting", "northing") is None


def test_geographic_pair_still_beats_a_projected_pair_in_the_same_header():
    """The precedence half the backlog entry worried about (Streetlight_0.csv carried both pairs) — a
    complete geographic pair wins, unchanged."""
    assert find_csv_coordinates(["Latitude", "Longitude", "x", "y"]) == ("Longitude", "Latitude", 4326)


def test_lone_geographic_name_beside_a_projected_pair_resolves_the_projected_pair():
    """The defect header. `Longitude` has no partner, so the geographic family is incomplete and the
    complete projected pair wins — declare — instead of the old `Longitude` + `y` mixed pair."""
    assert find_csv_coordinates(["Longitude", "x", "y", "name"]) == ("x", "y", None)


def test_truly_mixed_header_never_proposes_a_pair_from_two_families():
    """No complete pair in either family: the preview must hand back a partial from ONE family (the
    wizard pre-fills what it can and asks for the rest), never `Longitude` + `y`."""
    xcol, ycol, srid = find_csv_coordinates(["Longitude", "y", "name"])
    assert (xcol, ycol, srid) == ("Longitude", None, None)
    xcol, ycol, srid = find_csv_coordinates(["x", "Latitude", "name"])
    assert (xcol, ycol, srid) == (None, "Latitude", None)


def test_mixed_pair_is_refused_with_the_reason():
    for x, y in (("Longitude", "y"), ("x", "Latitude"), ("lng", "northing"), ("easting", "lat")):
        with pytest.raises(HTTPException) as e:
            csv_pair_srid(x, y)
        assert e.value.status_code == 422
        assert "coordinate columns disagree" in e.value.detail
        assert x in e.value.detail and y in e.value.detail


def test_geographic_name_beside_an_unlisted_column_is_declare_not_refused():
    """The least-narrowing rule: `Longitude` + `Lat_DD` picked by hand is what the manual pick is FOR.
    It is not mixed (Lat_DD is in neither list) — it falls to declare, and the ingest's range check then
    judges the declared CRS against the numbers."""
    assert csv_pair_srid("Longitude", "Lat_DD") is None
    assert csv_pair_srid("foo", "bar") is None


# --- the ingest's two branches (auto-detect and manual override) -----------------------------------


def test_ingest_auto_detect_routes_a_mixed_header_into_the_existing_no_coordinates_422():
    with pytest.raises(HTTPException) as e:
        _csv_to_features("Longitude,y,name\n-82.5,1120500,a\n")
    assert e.value.status_code == 422 and "no coordinate columns found" in e.value.detail


def test_ingest_auto_detect_good_cases_unchanged():
    srid, kind, rows = _csv_to_features("lon,lat,name\n-82.5,27.3,a\n")
    assert (srid, kind) == (4326, "geojson") and rows[0]["geom"]["coordinates"] == [-82.5, 27.3]
    srid, _k, rows = _csv_to_features("x,y,name\n500000,1120500,a\n")
    assert srid is None and rows[0]["geom"]["coordinates"] == [500000.0, 1120500.0]


def test_ingest_manual_override_refuses_a_mixed_pick():
    """The site the backlog entry missed: the override branch validated only that the named columns
    exist, then ran its own copy of the both-geographic test — so a deliberate `Longitude` + `y` pick
    reached the identical hole. Now it calls the shared rule and refuses with the reason."""
    text = "Longitude,x,y,name\n-82.5,500000,1120500,a\n"
    with pytest.raises(HTTPException) as e:
        _csv_to_features(text, "Longitude", "y")
    assert e.value.status_code == 422 and "coordinate columns disagree" in e.value.detail
    # ...and the same header with an agreeing pick still works both ways.
    assert _csv_to_features(text, "x", "y")[0] is None
    assert _csv_to_features("Longitude,Latitude\n-82.5,27.3\n", "Longitude", "Latitude")[0] == 4326


# --- the bulk-import door already resolved by family; pin it so the third site cannot regress -------


def test_bulk_import_sniff_also_resolves_by_family(tmp_path):
    p = tmp_path / "f.csv"
    p.write_text("Longitude,x,y,name\n-82.5,500000,1120500,a\n")
    _drafts, family, srid = svc.sniff_csv(str(p))
    assert family == "point" and srid is None  # the projected pair, not Longitude + y
    p.write_text("Longitude,y,name\n-82.5,1120500,a\n")
    with pytest.raises(HTTPException) as e:
        svc.sniff_csv(str(p))
    assert e.value.status_code == 422 and "no coordinate columns" in e.value.detail
