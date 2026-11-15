"""The shared WHERE builder transforms a 4326 bbox / filter geometry INTO the layer's storage SRID
(decision-crs-native-slice1.md; found in a field test: a State Plane layer's rows were all
"outside" the pack's extent, so Refresh removed them). ``_where`` is a pure builder, so its output is rendered
with ``as_string(None)`` and asserted on shape — no database."""
from __future__ import annotations

from app.routers.layer_query import _where

VALID = {"id", "geom", "props"}
BBOX = "-82.6,27.3,-82.46,27.38"


def render(composed) -> str:
    return composed.as_string(None)


def test_default_layer_keeps_the_plain_4326_envelope():
    where, params = _where([], BBOX, None, VALID, {})
    text = render(where)
    assert "t.geom && ST_MakeEnvelope(%(bbminx)s, %(bbminy)s, %(bbmaxx)s, %(bbmaxy)s, 4326)" in text
    assert "ST_Transform" not in text
    assert params == {"bbminx": -82.6, "bbminy": 27.3, "bbmaxx": -82.46, "bbmaxy": 27.38}


def test_native_layer_transforms_the_envelope_into_storage_srid():
    where, params = _where([], BBOX, None, VALID, {}, storage_srid=6443)
    text = render(where)
    assert ("t.geom && (SELECT assets.shape_in_srid(ST_MakeEnvelope(%(bbminx)s, %(bbminy)s, %(bbmaxx)s, "
            "%(bbmaxy)s, 4326), 6443))") in text
    assert params["bbminx"] == -82.6


def test_native_layer_never_converts_the_box_with_a_bare_transform():
    """A bare ST_Transform of the caller's box raises for a box far outside the storage system's area
    (the door answered 500) and returns nonsense for a world-scale one. The V118 function decides by the
    system's area of use; a box it cannot hold becomes NULL and matches no row."""
    where, _ = _where([], "0,0,1,1", None, VALID, {}, storage_srid=6443)
    text = render(where)
    assert "assets.shape_in_srid(" in text and "ST_Transform(" not in text


def test_exact_intersect_goes_through_the_same_function():
    gj = '{"type":"Point","coordinates":[100,10]}'
    where, _ = _where([], None, None, VALID, {}, geom_intersect=(gj, 4326), storage_srid=6443)
    text = render(where)
    assert "ST_MakeValid(assets.shape_in_srid(ST_SetSRID(ST_GeomFromGeoJSON(%(_gjson)s), %(_gsrid)s), 6443))" in text
    assert "ST_Transform(" not in text


def test_exact_intersect_targets_the_storage_srid():
    gj = '{"type":"Point","coordinates":[-82.53,27.34]}'
    where, params = _where([], None, None, VALID, {}, geom_intersect=(gj, 4326), storage_srid=2882)
    text = render(where)
    assert "ST_SetSRID(ST_GeomFromGeoJSON(%(_gjson)s), %(_gsrid)s), 2882)" in text
    assert params["_gsrid"] == 4326


def test_exact_intersect_default_layer_targets_4326():
    gj = '{"type":"Point","coordinates":[-82.53,27.34]}'
    where, _ = _where([], None, None, VALID, {}, geom_intersect=(gj, 3857))
    assert "%(_gsrid)s), 4326)" in render(where)


def test_storage_srid_none_or_zero_means_4326():
    where, _ = _where([], BBOX, None, VALID, {}, storage_srid=0)  # type: ignore[arg-type]
    assert "ST_Transform" not in render(where)
