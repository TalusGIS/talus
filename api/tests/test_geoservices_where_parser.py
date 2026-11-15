"""The v1 WHERE-clause subset and the spatial filter — the two parsers that decide what a desktop
client is allowed to ask for.

★ Regression 1: grouping parentheses (thread 10, shipped PR #65). ArcGIS Pro wraps its WHERE as
soon as there is more than one predicate — its post-edit refresh emits
`(OBJECTID IN(…) AND OBJECTID NOT IN(<dirty>))` — and refusing that blanked the attribute table
after every edit, recoverable only by re-adding the layer, which DISCARDS PENDING EDITS. The defect
voided three rig runs and sat latent two weeks.

★ Regression 2: the parser's own gate used to be `text.upper().replace("IN (", "").replace("IN(", "")`
— a blind substring strip that made the outcome depend on the CALLER'S DATA. A literal containing
`IN(` had its paren silently removed and slipped through; a literal containing a bare `(` was
refused. `_scan_grammar` exists to make parens inside a literal data rather than grammar, and that
distinction is what the tests below pin.

★ Regression 3: ArcGIS Pro sends its map extent as a rectangular POLYGON while editing, not an
envelope. That returned 400 before the fix and was the 2026-07-18 index-error root cause. QGIS sends
envelopes. Both clients, both paths, explicitly — neither is hypothetical.

Pure-logic only: no DB, no HTTP.

One caveat worth naming: `LOOKUP` below is a hand-built stand-in for what `_esri_fields` returns
from the database. It mirrors that function's real output shape (see the `lookup[...]` assignments
in `_esri_fields`), but nothing here proves the two stay in step — that is exactly the seam item 8a
§4.4 covers and this file does not.
"""
import json

import pytest

from app.routers.geoservices import (
    _CHUTE_FIELD,
    _EsriBadRequest,
    _compile_where,
    _has_ungrouped_paren,
    _parse_spatial_filter,
    _scan_grammar,
    _split_in_list,
    _strip_outer_parens,
)

# Shaped exactly like `_esri_fields`' second return value: name -> {source, kind, esri_type, …}.
# `kind` is the load-bearing key — "fid" routes to the GlobalID branch, "chute" to the refusal.
LOOKUP = {
    "OBJECTID": {"source": "objectid", "kind": "real", "esri_type": "esriFieldTypeOID"},
    "GlobalID": {"source": "id", "kind": "fid", "esri_type": "esriFieldTypeGlobalID"},
    "status": {"source": "status", "kind": "real", "esri_type": "esriFieldTypeString",
               "pg_type": "text"},
    "height_ft": {"source": "height_ft", "kind": "real", "esri_type": "esriFieldTypeDouble",
                  "pg_type": "double precision"},
    _CHUTE_FIELD: {"source": None, "kind": "chute", "esri_type": "esriFieldTypeString"},
}

# The literal string Pro emits on the refresh after an edit. Verbatim, because its exact shape —
# outer wrapper, two IN lists, no space before the first paren — is the whole regression.
PRO_POST_EDIT_REFRESH = "(OBJECTID IN(1,2,3) AND OBJECTID NOT IN(4,5))"


def compile_where(where):
    return _compile_where(where, LOOKUP)


# ---- ★ regression 1: the grouping parenthesis --------------------------------------------------

def test_pro_post_edit_refresh_compiles():
    """THE regression. If this refuses, Pro's attribute table blanks after every edit and the only
    recovery a user finds is re-adding the layer, which discards their pending edits."""
    assert compile_where(PRO_POST_EDIT_REFRESH) == ["objectid:in:1,2,3", "objectid:notin:4,5"]


def test_whole_expression_wrappers_strip_before_the_1_equals_1_shortcircuit():
    """Stripping happens FIRST, so every check downstream — the `1=1` short-circuit included — sees
    the same interior it would have seen unwrapped. Repeatedly, so `((X))` also unwraps."""
    assert compile_where("1=1") == []
    assert compile_where("(1=1)") == []
    assert compile_where("((1=1))") == []


def test_internal_grouping_is_still_refused():
    """Only a wrapper around the ENTIRE expression is stripped. `(A) AND (B)` is real grouping,
    which v1 does not do — and refusing it is what keeps the strip from becoming a way to smuggle
    an OR past the gate."""
    with pytest.raises(_EsriBadRequest):
        compile_where("(status = 'x') AND (height_ft > 5)")
    with pytest.raises(_EsriBadRequest):
        compile_where("(1=1) OR 1=1")


def test_unbalanced_parens_are_left_intact_and_refused():
    """`_strip_outer_parens` must not "fix" malformed input into something acceptable."""
    assert _strip_outer_parens("(status = 'x'") == "(status = 'x'"
    with pytest.raises(_EsriBadRequest):
        compile_where("(status = 'x'")


def test_function_calls_are_refused():
    """An opener only counts as an IN list when the preceding token is the bare word `IN`, so
    `upper(` — and a field literally named `MAIN(` — stay refused."""
    with pytest.raises(_EsriBadRequest):
        compile_where("upper(status) = 'X'")
    assert _has_ungrouped_paren("MAIN(status) = 'X'") is True
    assert _has_ungrouped_paren("status IN(1,2)") is False
    assert _has_ungrouped_paren("status NOT IN (1,2)") is False


# ---- ★ regression 2: grammar vs data ------------------------------------------------------------

def test_scan_grammar_yields_only_characters_outside_literals():
    """The primitive everything else keys off."""
    assert list(_scan_grammar("a'b'c")) == [(0, "a"), (4, "c")]
    # `''` inside a literal is an escaped quote — the scan must stay inside, matching `_literal`.
    assert [ch for _, ch in _scan_grammar("'O''Hara'")] == []


def test_a_paren_inside_a_literal_is_data_not_grammar():
    """The blind-substring bug: the gate's outcome must not depend on the caller's DATA.

    Before `_scan_grammar`, `'IN(x'` had its paren stripped and slipped through while `'a(b'` was
    refused — two literals, same structure, opposite outcomes, for no reason a user could see.
    """
    assert compile_where("status = 'a(b'") == ["status:eq:a(b"]
    assert compile_where("status = 'IN(x'") == ["status:eq:IN(x"]


def test_escaped_quotes_unescape():
    assert compile_where("status = 'O''Hara'") == ["status:eq:O'Hara"]


# ---- the IN-list splitter ----------------------------------------------------------------------

def test_split_in_list_ignores_commas_inside_quotes():
    assert _split_in_list("1,2,3") == ["1", "2", "3"]
    assert _split_in_list("'a,b','c'") == ["'a,b'", "'c'"]
    assert _split_in_list("'a'',b','c'") == ["'a'',b'", "'c'"]
    assert _split_in_list(" 'a' , 'b' ") == ["'a'", "'b'"]


def test_a_comma_inside_an_in_value_is_refused_not_silently_split():
    """The splitter gets it right, then `_compile_where` refuses anyway — the house filter string is
    comma-joined, so a comma inside a value cannot survive the round trip. A DELIBERATE documented
    limit; the point of pinning it is that it stays an honest refusal rather than becoming a silent
    mis-split."""
    with pytest.raises(_EsriBadRequest, match="commas"):
        compile_where("status IN ('a,b','c')")


# ---- the rest of the accepted subset ------------------------------------------------------------

def test_and_joined_comparisons_and_like():
    assert compile_where("status = 'x' AND OBJECTID > 5") == ["status:eq:x", "objectid:gt:5"]
    assert compile_where("status LIKE 'A%'") == ["status:like:A%"]
    assert compile_where(None) == []


def test_unsupported_shapes_refuse_honestly():
    for where in ("status = 'x' OR status = 'y'", "status IS NULL", "status IS NOT NULL"):
        with pytest.raises(_EsriBadRequest):
            compile_where(where)
    with pytest.raises(_EsriBadRequest, match="unknown field"):
        compile_where("nope = 'x'")


def test_the_chute_field_is_not_filterable():
    """Synthetic and computed — there is no storage column to filter on."""
    with pytest.raises(_EsriBadRequest, match="not filterable"):
        compile_where(f"{_CHUTE_FIELD} = 'x'")


def test_globalid_accepts_identity_ops_only():
    """A range comparison on a GUID would emit a lexicographic surprise, so it is refused rather
    than answered. Values normalize to the bare lowercase uuid, braced or not."""
    braced = "{6F9EB0A1-1111-2222-3333-444455556666}"
    bare = "6f9eb0a1-1111-2222-3333-444455556666"
    assert compile_where(f"GlobalID = '{braced}'") == [f"id:eq:{bare}"]
    assert compile_where(f"GlobalID = '{bare}'") == [f"id:eq:{bare}"]
    with pytest.raises(_EsriBadRequest):
        compile_where(f"GlobalID > '{bare}'")
    with pytest.raises(_EsriBadRequest, match="GUIDs"):
        compile_where("GlobalID = 'zzzz'")


# ---- ★ regression 2, part two: the keyword checks, fixed alongside these tests -------------------
# `_scan_grammar` taught the PAREN check that a literal's contents are data, but the OR/IS NULL
# search, the AND split and the NOT search kept scanning raw text. Found 2026-07-27 while writing
# `test_globalid_accepts_identity_ops_only` — `'not-a-guid'` was refused as a NOT operator.
# Fixed by `_grammar_mask`; these are the tests that were red first.

def test_the_word_not_inside_a_literal_is_data():
    """`'not started'` is an unremarkable municipal status value."""
    assert compile_where("status = 'not started'") == ["status:eq:not started"]
    assert compile_where("status LIKE '%not%'") == ["status:like:%not%"]


def test_the_word_or_inside_a_literal_is_data():
    assert compile_where("status = 'red or blue'") == ["status:eq:red or blue"]
    assert compile_where("status = 'is null'") == ["status:eq:is null"]


def test_the_word_and_inside_a_literal_is_data():
    """Worst of the three: the AND split cut the literal in half, so the refusal named a fragment
    (`unsupported literal in where clause: 'black`) the user never typed."""
    assert compile_where("status = 'black and white'") == ["status:eq:black and white"]
    assert compile_where("status = 'R and D' AND status = 'x'") == [
        "status:eq:R and D", "status:eq:x"]


def test_real_keyword_operators_are_still_refused():
    """★ The half that must NOT move. Unmasking literals may not unmask GRAMMAR: a real OR, IS
    NULL, or NOT outside NOT IN stays refused, including when a literal sits beside it."""
    for where in ("status = 'x' OR status = 'y'",
                  "status = 'not started' OR status = 'done'",
                  "status IS NULL",
                  "status = 'and' AND status IS NOT NULL",
                  "status NOT LIKE 'x'"):
        with pytest.raises(_EsriBadRequest):
            compile_where(where)


def test_an_in_list_already_treats_the_word_not_as_data():
    """Not xfail — this PASSES today, and that is what makes the defect above incoherent rather
    than merely strict: the IN branch matches before the NOT check, so the identical value is
    accepted through `IN` while `test_the_word_not_inside_a_literal_is_data` shows `=` refusing it.

    Deliberately asserts only the side that is correct today, so it stays green through the fix —
    the strict xfails above are what force the marks to be cleaned up.
    """
    assert compile_where("status IN ('not started','done')") == ["status:in:not started,done"]


# ---- ★ regression 3: the spatial filter, both clients -------------------------------------------

PRO_EXTENT_POLYGON = json.dumps({
    "rings": [[[-82.6, 27.3], [-82.4, 27.3], [-82.4, 27.5], [-82.6, 27.5], [-82.6, 27.3]]],
    "spatialReference": {"wkid": 4326},
})


def test_pro_sends_its_map_extent_as_a_polygon():
    """★ The 2026-07-18 index-error root cause. Pro sends a rectangular POLYGON while editing, not
    an envelope; this returned 400 before the fix. Routes to the EXACT ST_Intersects path."""
    bbox, geom = _parse_spatial_filter({
        "geometry": PRO_EXTENT_POLYGON, "geometryType": "esriGeometryPolygon",
    })
    assert bbox is None, "a polygon must not take the envelope fast path"
    assert geom is not None
    geojson, srid = geom
    assert json.loads(geojson)["type"] == "Polygon"
    assert srid == 4326, "the wkid rides in on the geometry's own spatialReference"


def test_qgis_sends_envelopes_in_both_forms():
    """QGIS sends an envelope — bare comma-separated or as JSON. Both take the fast index `&&`
    path, which is why the polygon case above had to be added rather than substituted."""
    bare = _parse_spatial_filter({"geometry": "-82.6,27.3,-82.4,27.5"})
    as_json = _parse_spatial_filter({"geometry": json.dumps(
        {"xmin": -82.6, "ymin": 27.3, "xmax": -82.4, "ymax": 27.5})})
    assert bare == ("-82.6,27.3,-82.4,27.5", None)
    assert as_json == bare


def test_no_geometry_filters_nothing():
    assert _parse_spatial_filter({}) == (None, None)


def test_the_other_esri_geometry_types_route_to_exact_intersect():
    for gtype, doc, expected in (
        ("esriGeometryPolyline", {"paths": [[[-82.6, 27.3], [-82.4, 27.5]]]}, "MultiLineString"),
        ("esriGeometryMultipoint", {"points": [[-82.6, 27.3]]}, "MultiPoint"),
        ("esriGeometryPoint", {"x": -82.6, "y": 27.3}, "Point"),
    ):
        bbox, geom = _parse_spatial_filter(
            {"geometry": json.dumps(doc), "geometryType": gtype})
        assert bbox is None
        assert json.loads(geom[0])["type"] == expected


def test_a_web_mercator_envelope_converts_to_4326():
    """The fast path stays in 4326, so a 3857 envelope is converted here rather than in SQL."""
    bbox, geom = _parse_spatial_filter({
        "geometry": json.dumps({"xmin": -9195000.0, "ymin": 3160000.0,
                                "xmax": -9173000.0, "ymax": 3185000.0}),
        "inSR": "3857",
    })
    assert geom is None
    xmin, ymin, xmax, ymax = (float(v) for v in bbox.split(","))
    assert -83.0 < xmin < -82.0 and -83.0 < xmax < -82.0
    assert 27.0 < ymin < 28.0 and 27.0 < ymax < 28.0


def test_unsupported_spatial_rel_and_malformed_geometry_refuse():
    with pytest.raises(_EsriBadRequest, match="spatialRel"):
        _parse_spatial_filter({"geometry": "-82.6,27.3,-82.4,27.5",
                               "spatialRel": "esriSpatialRelContains"})
    with pytest.raises(_EsriBadRequest):
        _parse_spatial_filter({"geometry": "not,an,envelope"})
    with pytest.raises(_EsriBadRequest):
        _parse_spatial_filter({"geometry": json.dumps({"rings": []}),
                               "geometryType": "esriGeometryPolygon"})
