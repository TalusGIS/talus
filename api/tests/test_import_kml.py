"""KML / KMZ on the 9d import door — pure guards.

No live DB, no stack. Two things whose silent failure is expensive: the sniffer's DRAFT CONTRACT
(the 2026-08-21 attribute-loss lesson — `_assert_house_drafts` twin of test_import_jobs.py), and
the untrusted-bytes fences a brand-new parser adds (DTD/entity refusal, KMZ through the archive
validator, NetworkLinks never fetched). The rest pins the geometry mapping a Google Earth file
actually exercises: folders → a column, altitude dropped, inner rings kept, MultiGeometry of one
family → Multi*, mixed families → a reasoned error row, overlays → reasoned error rows.
"""
from __future__ import annotations

import os
import zipfile

import pytest
from fastapi import HTTPException

from app import import_jobs_service as svc
from app.routers.layer_fields import _normalize_type

KML_NS = 'xmlns="http://www.opengis.net/kml/2.2"'


def _kml(body: str, ns: str = KML_NS) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><kml {ns}><Document><name>doc</name>{body}</Document></kml>'


def _write(tmp_path, text: str, name: str = "upload.kml") -> tuple[str, str]:
    p = str(tmp_path / name)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(text)
    return p, str(tmp_path)


def _assert_house_drafts(drafts):
    assert drafts, "sniffer produced no drafts"
    for d in drafts:
        assert set(d) >= {"field_key", "label", "data_type", "required", "default_value",
                          "default_kind", "constraints", "numeric_subtype", "_renamed_from"}
        assert _normalize_type(d["data_type"]) is not None


PM_POINT = ('<Placemark><name>Hydrant 1</name><description>&lt;b&gt;html&lt;/b&gt;</description>'
            '<ExtendedData><Data name="status"><value>ok</value></Data></ExtendedData>'
            '<Point><coordinates>-82.5,27.3,12.0</coordinates></Point></Placemark>')


# --- draft contract + the basics ---------------------------------------------------------------

def test_sniff_drafts_speak_the_house_dialect(tmp_path):
    p, jail = _write(tmp_path, _kml(f"<Folder><name>Water</name><Folder><name>Hydrants</name>{PM_POINT}</Folder></Folder>"))
    drafts, family, srid = svc.sniff_kml(p, jail)
    assert family == "point" and srid == 4326
    _assert_house_drafts(drafts)
    assert {d["field_key"]: d["data_type"] for d in drafts} == {
        "name": "text", "description": "text", "kml_folder": "text", "status": "text"}


def test_rows_carry_folder_path_and_drop_altitude(tmp_path):
    p, jail = _write(tmp_path, _kml(f"<Folder><name>Water</name><Folder><name>Hydrants</name>{PM_POINT}</Folder></Folder>"))
    rows = list(svc._iter_kml(p, jail))
    assert len(rows) == 1 and rows[0]["error"] is None
    assert rows[0]["geom"] == {"type": "Point", "coordinates": [-82.5, 27.3]}
    assert rows[0]["props"]["kml_folder"] == "Water/Hydrants"
    assert rows[0]["props"]["name"] == "Hydrant 1"
    assert rows[0]["props"]["description"] == "<b>html</b>"   # text, never rendered — that's the popup's job to escape
    assert rows[0]["props"]["status"] == "ok"


def test_unnamespaced_kml_from_an_old_exporter_still_parses(tmp_path):
    p, jail = _write(tmp_path, _kml(PM_POINT, ns=""))
    _d, family, _s = svc.sniff_kml(p, jail)
    assert family == "point"


def test_schema_types_are_honoured_and_coerced(tmp_path):
    body = ('<Schema name="S" id="S"><SimpleField type="int" name="count"/><SimpleField type="double" name="len"/>'
            '<SimpleField type="bool" name="lit"/></Schema>'
            '<Placemark><name>a</name><ExtendedData><SchemaData schemaUrl="#S">'
            '<SimpleData name="count">7</SimpleData><SimpleData name="len">1.5</SimpleData><SimpleData name="lit">true</SimpleData>'
            '</SchemaData></ExtendedData><Point><coordinates>-82.5,27.3</coordinates></Point></Placemark>')
    p, jail = _write(tmp_path, _kml(body))
    drafts, _f, _s = svc.sniff_kml(p, jail)
    by = {d["field_key"]: (d["data_type"], d["numeric_subtype"]) for d in drafts}
    assert by["count"] == ("number", "long_integer") and by["len"] == ("number", "double") and by["lit"] == ("boolean", None)
    row = next(svc._iter_kml(p, jail))
    assert row["props"]["count"] == 7 and row["props"]["len"] == 1.5 and row["props"]["lit"] is True


def test_schema_fields_absent_from_the_first_placemark_are_still_drafted(tmp_path):
    """Real writers declare the field list once in <Schema> and omit empty values per placemark
    (a real county wetland export: 6 of 10 fields on any row). The layer must get all ten."""
    body = ('<Schema name="S" id="S"><SimpleField type="int" name="objectid"/><SimpleField type="string" name="creator"/>'
            '<SimpleField type="float" name="shape_Area"/></Schema>'
            '<Placemark><ExtendedData><SchemaData schemaUrl="#S"><SimpleData name="objectid">1</SimpleData>'
            '<SimpleData name="shape_Area">1.24e-06</SimpleData></SchemaData>'
            '<Data name="extra"><value>x</value></Data></ExtendedData>'
            '<MultiGeometry><Polygon><outerBoundaryIs><LinearRing><coordinates>0,0 1,0 1,1 0,1 0,0</coordinates></LinearRing></outerBoundaryIs></Polygon></MultiGeometry></Placemark>')
    p, jail = _write(tmp_path, _kml(body))
    drafts, family, _s = svc.sniff_kml(p, jail)
    by = {d["field_key"]: (d["data_type"], d["_renamed_from"]) for d in drafts}
    assert family == "polygon"
    assert by["creator"] == ("text", None)                 # declared, absent on the first row, still drafted
    assert by["objectid_src"] == ("number", "objectid")    # the Esri-door reserved-name rename
    assert by["shape_area"][0] == "number" and by["extra"] == ("text", None)
    row = next(svc._iter_kml(p, jail))
    assert row["geom"]["type"] == "MultiPolygon" and row["props"]["shape_Area"] == 1.24e-06


# --- geometry mapping ------------------------------------------------------------------------------

def test_polygon_keeps_inner_ring_and_closes_an_open_one(tmp_path):
    body = ('<Placemark><Polygon><outerBoundaryIs><LinearRing><coordinates>0,0 1,0 1,1 0,1</coordinates></LinearRing></outerBoundaryIs>'
            '<innerBoundaryIs><LinearRing><coordinates>0.2,0.2 0.4,0.2 0.4,0.4 0.2,0.4 0.2,0.2</coordinates></LinearRing></innerBoundaryIs>'
            '</Polygon></Placemark>')
    p, jail = _write(tmp_path, _kml(body))
    _d, family, _s = svc.sniff_kml(p, jail)
    assert family == "polygon"
    row = next(svc._iter_kml(p, jail))
    rings = row["geom"]["coordinates"]
    assert len(rings) == 2 and rings[0][0] == rings[0][-1] and len(rings[0]) == 5


def test_multigeometry_of_one_family_becomes_multi(tmp_path):
    body = ('<Placemark><MultiGeometry><LineString><coordinates>0,0 1,1</coordinates></LineString>'
            '<LineString><coordinates>2,2 3,3</coordinates></LineString></MultiGeometry></Placemark>')
    p, jail = _write(tmp_path, _kml(body))
    row = next(svc._iter_kml(p, jail))
    assert row["family"] == "line" and row["geom"]["type"] == "MultiLineString" and len(row["geom"]["coordinates"]) == 2


def test_placemark_mixing_families_is_a_reasoned_error_row(tmp_path):
    body = ('<Placemark><MultiGeometry><Point><coordinates>0,0</coordinates></Point>'
            '<LineString><coordinates>0,0 1,1</coordinates></LineString></MultiGeometry></Placemark>' + PM_POINT)
    p, jail = _write(tmp_path, _kml(body))
    rows = list(svc._iter_kml(p, jail))
    assert rows[0]["error"] is not None and "mixes geometry families" in rows[0]["error"]
    assert rows[1]["error"] is None
    # the sniff skips the unusable first placemark and types the layer from the first GOOD one
    _d, family, _s = svc.sniff_kml(p, jail)
    assert family == "point"


def test_mixed_families_across_placemarks_follow_the_geojson_rule(tmp_path):
    """One family per layer, decided by the first usable placemark; the others are rows the
    worker fails against the layer's geometry type (the GeoJSON path's exact behaviour)."""
    body = PM_POINT + '<Placemark><LineString><coordinates>0,0 1,1</coordinates></LineString></Placemark>'
    p, jail = _write(tmp_path, _kml(body))
    _d, family, _s = svc.sniff_kml(p, jail)
    assert family == "point"
    fams = [r["family"] for r in svc._iter_kml(p, jail)]
    assert fams == ["point", "line"]


def test_overlays_and_network_links_are_skipped_rows_and_never_fetched(tmp_path):
    body = ('<NetworkLink><name>live</name><Link><href>http://127.0.0.1:9/never</href></Link></NetworkLink>'
            '<GroundOverlay><name>img</name><Icon><href>x.png</href></Icon></GroundOverlay>' + PM_POINT)
    p, jail = _write(tmp_path, _kml(body))
    rows = list(svc._iter_kml(p, jail))
    assert [r["error"] is None for r in rows] == [False, False, True]
    assert "NetworkLink not imported" in rows[0]["error"] and "GroundOverlay not imported" in rows[1]["error"]


def test_google_my_maps_address_only_export_is_refused_with_the_csv_path_named(tmp_path):
    """Shape of a real Google My Maps export (2026-09-17, values withheld): <Data name>/<value>
    columns named `unnamed (N)`, CDATA descriptions, a StyleMap, and placemarks that carry an
    <address> but no coordinates. Nothing to import; the refusal must say why and where to go."""
    pm = ('<Placemark><name>x</name><description><![CDATA[a<br>b]]></description>'
          '<styleUrl>#icon-1</styleUrl><address>1 Main St, Somewhere, FL</address>'
          '<ExtendedData><Data name="unnamed (1)"><value>v</value></Data><Data name="All Home Addresses"><value>w</value></Data></ExtendedData></Placemark>')
    body = '<StyleMap id="icon-1"><Pair><key>normal</key><styleUrl>#s</styleUrl></Pair></StyleMap>' + pm * 3 + '<Placemark><name>y</name></Placemark>'
    p, jail = _write(tmp_path, _kml(body))
    with pytest.raises(HTTPException) as e:
        svc.sniff_kml(p, jail)
    assert e.value.status_code == 422
    assert "4 Placemarks" in e.value.detail and "3 of them carry a Google Maps <address>" in e.value.detail
    assert "Batch geocode addresses" in e.value.detail
    rows = list(svc._iter_kml(p, jail))
    assert len(rows) == 4 and all(r["error"] == "Placemark has no geometry" for r in rows)
    assert rows[0]["props"]["unnamed (1)"] == "v"   # attributes are read even when nothing can be imported


# --- G3b: address-only KML → batch-geocoding rows (decision-kml-address-geocode.md) ---------------

MYMAPS_PM = ('<Placemark><name>n</name><description><![CDATA[a<br>b]]></description><styleUrl>#icon-1</styleUrl>'
             '<address>1 Main St, Somewhere, FL</address>'
             '<ExtendedData><Data name="unnamed (1)"><value>v</value></Data><Data name="All Home Addresses"><value>w</value></Data></ExtendedData></Placemark>')


def test_kml_rows_puts_the_address_first_and_fills_missing_columns_as_text(tmp_path):
    body = MYMAPS_PM + '<Placemark><name>no-address</name><ExtendedData><Data name="extra"><value>7</value></Data></ExtendedData></Placemark>' + \
           '<GroundOverlay><name>img</name></GroundOverlay>'
    p, jail = _write(tmp_path, _kml(body))
    headers, rows, stats = svc.kml_rows(p, jail)
    assert headers[0] == "address" and headers[1:4] == ["name", "description", "unnamed (1)"]
    assert stats == {"placemarks": 2, "withAddress": 1, "withGeometry": 0, "skipped": 1}
    assert len(rows) == 2 and all(set(r) == set(headers) for r in rows)
    assert rows[0]["address"] == "1 Main St, Somewhere, FL" and rows[0]["description"] == "a<br>b"
    assert rows[1]["address"] == "" and rows[1]["extra"] == "7" and rows[1]["unnamed (1)"] == ""
    assert all(isinstance(v, str) for r in rows for v in r.values())


def test_kml_rows_omits_the_address_column_when_no_placemark_has_one_and_counts_geometry(tmp_path):
    p, jail = _write(tmp_path, _kml(PM_POINT + PM_POINT))
    headers, rows, stats = svc.kml_rows(p, jail)
    assert "address" not in headers and headers[:2] == ["name", "description"]
    assert stats["withGeometry"] == 2 and stats["withAddress"] == 0 and len(rows) == 2
    assert rows[0]["status"] == "ok"


def test_kml_rows_from_a_kmz_and_the_folder_column(tmp_path):
    jail = str(tmp_path); p = os.path.join(jail, "upload.kml")
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("doc.kml", _kml(f"<Folder><name>Clients</name>{MYMAPS_PM}</Folder>"))
    headers, rows, stats = svc.kml_rows(p, jail)
    assert rows[0]["kml_folder"] == "Clients" and stats["withAddress"] == 1


def test_kml_rows_shares_the_import_doors_fences(tmp_path):
    p, jail = _write(tmp_path, '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e "x">]>' + _kml(MYMAPS_PM))
    with pytest.raises(HTTPException) as e:
        svc.kml_rows(p, jail)
    assert e.value.status_code == 422


def test_only_overlays_is_a_422_that_says_so(tmp_path):
    p, jail = _write(tmp_path, _kml('<NetworkLink><Link><href>http://example.invalid/x.kml</href></Link></NetworkLink>'))
    with pytest.raises(HTTPException) as e:
        svc.sniff_kml(p, jail)
    assert e.value.status_code == 422 and "network links" in e.value.detail


def test_placemark_without_geometry_is_a_reasoned_row_and_model_is_unsupported(tmp_path):
    body = ('<Placemark><name>nogeom</name></Placemark>'
            '<Placemark><name>model</name><Model><Location><longitude>0</longitude><latitude>0</latitude></Location></Model></Placemark>' + PM_POINT)
    p, jail = _write(tmp_path, _kml(body))
    rows = list(svc._iter_kml(p, jail))
    assert rows[0]["error"] == "Placemark has no geometry"
    assert "unsupported KML geometry (Model)" in rows[1]["error"]
    assert rows[2]["error"] is None


# --- untrusted-bytes fences ----------------------------------------------------------------------

def test_doctype_is_refused_before_any_parser_sees_it(tmp_path):
    lol = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;">]>'
           f'<kml {KML_NS}><Document><Placemark><name>&lol2;</name><Point><coordinates>0,0</coordinates></Point></Placemark></Document></kml>')
    p, jail = _write(tmp_path, lol)
    with pytest.raises(HTTPException) as e:
        svc.sniff_kml(p, jail)
    assert e.value.status_code == 422 and "DTD" in e.value.detail


def test_external_entity_is_refused(tmp_path):
    xxe = ('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
           f'<kml {KML_NS}><Document><Placemark><name>&xxe;</name></Placemark></Document></kml>')
    p, jail = _write(tmp_path, xxe)
    with pytest.raises(HTTPException) as e:
        svc.sniff_kml(p, jail)
    assert e.value.status_code == 422


def test_utf16_xxe_that_slips_the_prescan_is_still_refused_by_the_parser(tmp_path):
    """The ASCII pre-scan cannot see a UTF-16 DOCTYPE (security review 2026-09-17). The fence is
    ElementTree: no external entity is resolved, so the reference is an undefined entity → 422."""
    xxe = ('<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE x [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
           f'<kml {KML_NS}><Document><Placemark><name>&xxe;</name><Point><coordinates>0,0</coordinates></Point></Placemark></Document></kml>')
    p = str(tmp_path / "upload.kml")
    with open(p, "wb") as fh:
        fh.write(xxe.encode("utf-16"))
    with pytest.raises(HTTPException) as e:
        svc.sniff_kml(p, str(tmp_path))
    assert e.value.status_code == 422 and "passwd" not in (e.value.detail or "")


def test_doctype_past_the_prescan_window_is_still_refused_by_the_parser(tmp_path):
    """A >64 KiB leading comment pushes the DOCTYPE past the pre-scan; the parser still refuses
    the external entity, and the DTD's SYSTEM URL is never fetched (it points at a closed port)."""
    pad = "<!-- " + ("x" * (svc._KML_PROLOG_SCAN + 1024)) + " -->"
    xxe = ('<?xml version="1.0"?>' + pad +
           '<!DOCTYPE kml SYSTEM "http://127.0.0.1:9/never.dtd" [<!ENTITY xxe SYSTEM "file:///etc/hostname">]>'
           f'<kml {KML_NS}><Document><Placemark><name>&xxe;</name><Point><coordinates>0,0</coordinates></Point></Placemark></Document></kml>')
    p, jail = _write(tmp_path, xxe)
    with pytest.raises(HTTPException) as e:
        svc.sniff_kml(p, jail)
    assert e.value.status_code == 422


def test_folder_nesting_past_the_cap_is_a_422_not_a_500(tmp_path):
    deep = "<Folder><name>f</name>" * (svc._KML_MAX_DEPTH + 2) + PM_POINT + "</Folder>" * (svc._KML_MAX_DEPTH + 2)
    p, jail = _write(tmp_path, _kml(deep))
    with pytest.raises(HTTPException) as e:
        svc.sniff_kml(p, jail)
    assert e.value.status_code == 422 and "deeper than" in e.value.detail


def test_geometry_nesting_past_the_cap_is_a_reasoned_row(tmp_path):
    deep = "<MultiGeometry>" * (svc._KML_MAX_DEPTH + 2) + "<Point><coordinates>0,0</coordinates></Point>" + "</MultiGeometry>" * (svc._KML_MAX_DEPTH + 2)
    p, jail = _write(tmp_path, _kml(f"<Placemark>{deep}</Placemark>" + PM_POINT))
    rows = list(svc._iter_kml(p, jail))
    assert "deeper than" in rows[0]["error"] and rows[1]["error"] is None


def test_malformed_xml_is_422(tmp_path):
    p, jail = _write(tmp_path, "<kml><Document><Placemark></Document>")
    with pytest.raises(HTTPException) as e:
        svc.sniff_kml(p, jail)
    assert e.value.status_code == 422 and "well-formed" in e.value.detail


def test_kmz_is_detected_by_magic_and_uses_doc_kml(tmp_path):
    jail = str(tmp_path)
    p = os.path.join(jail, "upload.kml")   # the router stages every KML upload as .kml — magic decides
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("files/icon.png", b"\x89PNG not really")
        z.writestr("other.kml", _kml('<Placemark><LineString><coordinates>0,0 1,1</coordinates></LineString></Placemark>'))
        z.writestr("doc.kml", _kml(PM_POINT))
    _d, family, _s = svc.sniff_kml(p, jail)
    assert family == "point"   # doc.kml won over other.kml


def test_kmz_is_unpacked_once_and_reused_by_the_worker(tmp_path):
    """The live sequence: the router sniffs (unpacks), then the worker iterates in the same jail.
    The first live KMZ job died here on the validator's duplicate-member guard (2026-09-17)."""
    jail = str(tmp_path)
    p = os.path.join(jail, "upload.kml")
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("doc.kml", _kml(PM_POINT))
        z.writestr("upload.kml", _kml(PM_POINT))   # a member named like the staged file must not collide
    _d, family, _s = svc.sniff_kml(p, jail)
    rows = list(svc._iter_kml(p, jail))
    assert family == "point" and len(rows) == 1 and rows[0]["error"] is None
    assert os.path.isdir(os.path.join(jail, "kmz")) and os.path.getsize(p) > 0


def test_kmz_traversal_entry_is_refused_by_the_shared_validator(tmp_path):
    jail = str(tmp_path)
    p = os.path.join(jail, "upload.kml")
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("../../etc/doc.kml", _kml(PM_POINT))
    with pytest.raises(HTTPException) as e:
        svc.sniff_kml(p, jail)
    assert e.value.status_code == 422 and "traversal" in e.value.detail


def test_kmz_without_a_kml_member_is_422(tmp_path):
    jail = str(tmp_path)
    p = os.path.join(jail, "upload.kml")
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("readme.txt", b"nothing here")
    with pytest.raises(HTTPException) as e:
        svc.sniff_kml(p, jail)
    assert e.value.status_code == 422 and "no .kml" in e.value.detail


def test_kml_is_in_the_format_list():
    assert "kml" in svc.FORMATS


def test_every_format_has_exactly_one_staged_extension():
    """The router writes `upload<ext>` and the worker reads it back by the SAME table. On
    2026-09-17 the worker carried its own inline copy without 'kml' and the first live KMZ job died
    with KeyError('kml') after provisioning the layer. Pin the sets equal so a sixth format cannot
    repeat it."""
    assert set(svc.FORMATS) == set(svc.STAGED_EXT)
    from app.routers import import_jobs as router
    assert router._EXT is svc.STAGED_EXT
